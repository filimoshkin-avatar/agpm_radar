"""Durable source-side orchestration for the restricted Radar V2 remote activator."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from packages.delta.engine import (
    build_delta,
    finalize_release_database,
    inspect_release_database,
)
from packages.domain.candidate_package import verify_candidate_package
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.publisher.project_manager import build_project_manager_report
from packages.publisher.remote_activation import replace_pointer
from packages.storage.content_pointer import read_content_pointer
from packages.storage.mutation_lock import acquire_mutation_lock, release_mutation_lock
from packages.storage.safe_files import (
    atomic_write_new,
    ensure_private_directory,
    read_regular_file,
)

Transport = Callable[[bytes], tuple[int, bytes, bytes]]
RESULT_FIELDS: Final = frozenset(
    {
        "database_sha256",
        "loopback_verified",
        "pointer_sha256",
        "previous_release_id",
        "previous_state_hash",
        "release_id",
        "request_id",
        "state_hash",
        "status",
    }
)


class RemoteOrchestrationError(RuntimeError):
    """The source-side publication could not be completed or proven safely."""


@dataclass(frozen=True, slots=True)
class PublishInputs:
    package: Path
    candidate_staging: Path
    source_root: Path
    work_root: Path
    application_release_id: str
    created_at: str
    finished_at: str
    duration_ms: int


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _token(value: str) -> str:
    if (
        not value
        or len(value) > 128
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@-"
            for character in value
        )
    ):
        raise RemoteOrchestrationError("identifier contains forbidden characters")
    return value


def _release_id(candidate_id: str) -> str:
    return f"rel_{hashlib.sha256(candidate_id.encode()).hexdigest()[:24]}"


def _pointer_bytes(release_id: str, state_hash: str) -> bytes:
    database = hashlib.sha256(release_id.encode()).hexdigest()[:32]
    return canonical_json_line(
        {
            "database": f"releases/{database}.sqlite",
            "releaseId": release_id,
            "stateHash": state_hash,
        }
    )


def _load_remote_result(content: bytes) -> JsonObject:
    try:
        value: object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemoteOrchestrationError(f"remote result is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise RemoteOrchestrationError("remote result is not an object")
    result = cast(dict[str, object], value)
    if set(result) != RESULT_FIELDS:
        raise RemoteOrchestrationError("remote result has unknown or missing fields")
    for key in ("request_id", "release_id", "state_hash", "database_sha256", "pointer_sha256"):
        if not isinstance(result[key], str):
            raise RemoteOrchestrationError(f"remote result {key} is invalid")
    if result["status"] != "published" or result["loopback_verified"] is not True:
        raise RemoteOrchestrationError("remote publication was not proven successful")
    return cast(JsonObject, result)


def ssh_transport(*, host: str, identity: Path) -> Transport:
    """Build a no-shell SSH transport for one forced-command identity."""
    _token(host)
    identity_content = read_regular_file(identity, expected_mode=0o600)
    if not identity_content:
        raise RemoteOrchestrationError("SSH identity is empty")

    def invoke(request: bytes) -> tuple[int, bytes, bytes]:
        process = subprocess.run(  # noqa: S603
            [
                "/usr/bin/ssh",
                "-i",
                str(identity),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                host,
            ],
            input=request,
            capture_output=True,
            check=False,
            timeout=60,
        )
        return process.returncode, process.stdout, process.stderr

    return invoke


def _save_exact(path: Path, content: bytes) -> None:
    if path.exists():
        if read_regular_file(path, expected_mode=0o600) != content:
            raise RemoteOrchestrationError(f"retained bytes differ: {path.name}")
        return
    atomic_write_new(path, content, mode=0o600)


def publish_candidate(inputs: PublishInputs, transport: Transport) -> JsonObject:
    """Verify, remotely publish, source-commit and return one PM-compatible result."""
    package = verify_candidate_package(inputs.package)
    candidate = package.candidate
    candidate_id = _token(cast(str, candidate["candidateId"]))
    operation = cast(str, candidate["operation"])
    expected = cast(dict[str, object], candidate["expectedBase"])
    release_id = _release_id(candidate_id)
    ensure_private_directory(inputs.work_root)
    for name in ("deltas", "releases", "requests", "results"):
        ensure_private_directory(inputs.work_root / name)
    lock = acquire_mutation_lock(inputs.work_root)
    try:
        result_path = inputs.work_root / "results" / f"{candidate_id}.json"
        if result_path.exists():
            result = cast(
                JsonObject,
                json.loads(read_regular_file(result_path, expected_mode=0o600)),
            )
            build_project_manager_report(result)
            replay = dict(result)
            replay["status"] = "already_succeeded"
            replay["idempotencyDisposition"] = "replayed"
            replay["startedAt"] = inputs.created_at
            replay["finishedAt"] = inputs.finished_at
            replay["durationMs"] = inputs.duration_ms
            build_project_manager_report(replay)
            return replay

        source_pointer_path = inputs.source_root / "active.json"
        source_pointer_content = read_regular_file(source_pointer_path, expected_mode=0o600)
        source = read_content_pointer(inputs.source_root)
        source_report = inspect_release_database(source.database_path)
        if (
            source.release_id != expected["releaseId"]
            or source.state_hash != expected["logicalStateHash"]
            or source_report.release.sequence != expected["sequence"]
        ):
            raise RemoteOrchestrationError("candidate expected base differs from source pointer")

        metadata = json.loads(
            read_regular_file(inputs.package / "payload/package-metadata.json", expected_mode=0o400)
        )

        release_path = inputs.work_root / "releases" / f"{candidate_id}.sqlite"
        if not release_path.exists():
            finalize_release_database(
                inputs.candidate_staging,
                release_path,
                release_id=release_id,
                candidate_id=candidate_id,
                operation=operation,
                created_at=inputs.created_at,
                activated_at=inputs.finished_at,
                expected_base_release_id=cast(str, expected["releaseId"]),
                expected_base_sequence=cast(int, expected["sequence"]),
                expected_before_state_hash=cast(str, expected["logicalStateHash"]),
            )
        release = inspect_release_database(release_path)
        if release.digest.state_hash != metadata["stagingAfterStateHash"]:
            raise RemoteOrchestrationError("finalized release differs from immutable package")
        asset_descriptors = (
            cast(list[dict[str, object]], candidate["inputAssets"])
            if operation == "gazette"
            else []
        )
        delta = build_delta(
            source.database_path,
            release_path,
            candidate_id=candidate_id,
            operation=operation,
            release_id=release_id,
            application_release_id=inputs.application_release_id,
            created_at=inputs.created_at,
            assets=tuple(asset_descriptors),
        )
        delta_bytes = canonical_json_line(delta)
        _save_exact(inputs.work_root / "deltas" / f"{candidate_id}.json", delta_bytes)
        request_id = f"req_{hashlib.sha256(delta_bytes).hexdigest()[:24]}"
        request = canonical_json_line(
            {
                "action": "publish",
                "assetPayloads": {
                    cast(str, descriptor["relativePath"]): read_regular_file(
                        inputs.package / "assets" / cast(str, descriptor["relativePath"]),
                        expected_mode=0o400,
                    ).hex()
                    for descriptor in asset_descriptors
                },
                "delta": delta,
                "expectedCurrentPointerSha256": _sha256(source_pointer_content),
                "requestId": request_id,
                "rollbackPointer": None,
            }
        )
        _save_exact(inputs.work_root / "requests" / f"{candidate_id}.json", request)
        exit_code, stdout, stderr = transport(request)
        if exit_code != 0:
            message = stderr.decode("utf-8", errors="replace")[-2000:]
            raise RemoteOrchestrationError(
                f"restricted remote transport failed ({exit_code}): {message}"
            )
        remote = _load_remote_result(stdout)
        if (
            remote["request_id"] != request_id
            or remote["release_id"] != release_id
            or remote["state_hash"] != release.digest.state_hash
        ):
            raise RemoteOrchestrationError("remote result differs from exact request target")

        target_pointer = _pointer_bytes(release_id, release.digest.state_hash)
        source_stat = os.stat(source_pointer_path, follow_symlinks=False)
        target_name = cast(str, json.loads(target_pointer)["database"])
        target_path = inputs.source_root / target_name
        target_content = read_regular_file(release_path, expected_mode=0o600)
        if target_path.exists():
            if read_regular_file(target_path, expected_mode=0o600) != target_content:
                raise RemoteOrchestrationError("source release target contains different bytes")
        else:
            atomic_write_new(target_path, target_content, mode=0o600)
        replace_pointer(
            inputs.source_root,
            target_pointer,
            uid=source_stat.st_uid,
            gid=source_stat.st_gid,
            expected=source_pointer_content,
        )
        committed = read_content_pointer(inputs.source_root)
        if (
            committed.release_id != release_id
            or committed.state_hash != release.digest.state_hash
            or inspect_release_database(committed.database_path).file_sha256
            != _sha256(target_content)
        ):
            raise RemoteOrchestrationError("source pointer commit verification failed")

        llm = cast(dict[str, object], candidate["llmOutcome"])
        warnings: list[dict[str, object]] = []
        if llm["status"] in {"fallback", "unavailable"}:
            warnings.append(
                {
                    "code": (
                        "LLM_FALLBACK_USED" if llm["status"] == "fallback" else "LLM_UNAVAILABLE"
                    ),
                    "message": "LLM fallback state is explicit in the accepted candidate.",
                    "ownerVisible": True,
                }
            )
        raw: dict[str, object] = {
            "activeReleaseId": release_id,
            "candidateId": candidate_id,
            "checks": [
                {
                    "id": "candidate.package",
                    "message": "Immutable candidate package verified.",
                    "status": "passed",
                },
                {
                    "id": "database.source",
                    "message": "Source release and delta verified.",
                    "status": "passed",
                },
                {
                    "id": "database.production",
                    "message": "Restricted remote activation verified.",
                    "status": "passed",
                },
                {
                    "id": "public.smoke",
                    "message": "Remote loopback and public verification passed.",
                    "status": "passed",
                },
            ],
            "contractVersion": "1.0.0",
            "durationMs": inputs.duration_ms,
            "error": None,
            "exitCode": 0,
            "finishedAt": inputs.finished_at,
            "idempotencyDisposition": "executed",
            "llmOutcome": llm,
            "operation": operation,
            "productionStateHash": release.digest.state_hash,
            "publicationSucceeded": True,
            "publishingBlocked": False,
            "releaseId": release_id,
            "rollback": {
                "attempted": False,
                "required": False,
                "restoredReleaseId": None,
                "restoredStateHash": None,
                "succeeded": None,
            },
            "sourceStateHash": release.digest.state_hash,
            "startedAt": inputs.created_at,
            "status": "published",
            "warnings": warnings,
        }
        if operation in {"daily", "correction"}:
            raw["issueDate"] = cast(dict[str, object], candidate["desiredIssue"])["issueDate"]
        result = cast(JsonObject, raw)
        build_project_manager_report(result)
        _save_exact(result_path, canonical_json_line(result))
        return result
    finally:
        release_mutation_lock(lock)


__all__ = [
    "PublishInputs",
    "RemoteOrchestrationError",
    "Transport",
    "publish_candidate",
    "ssh_transport",
]
