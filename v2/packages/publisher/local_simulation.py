"""Disposable source/production activation and rollback simulation for Radar V2 Stage 7."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from packages.delta.engine import (
    DeltaApplyReport,
    DeltaConflictError,
    apply_delta_to_staging,
    inspect_release_database,
    validate_delta,
)
from packages.domain.candidates import CandidateValidationError, validate_llm_outcome
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.publisher.project_manager import build_project_manager_report
from packages.publisher.state_machine import PublisherStateError, PublisherStateMachine
from packages.storage.safe_files import (
    SafeFilesystemError,
    atomic_write_new,
    ensure_private_directory,
    open_directory_nofollow,
    read_regular_file,
    relative_parts,
)

ACTIVE_POINTER_NAME: Final = "active.json"
_NORMAL_STATES: Final = (
    "RECEIVED",
    "VALIDATED",
    "SOURCE_STAGED",
    "ARTIFACTS_BUILT",
    "DELTA_BUILT",
    "REMOTE_STAGED",
    "REMOTE_VERIFIED",
    "REMOTE_ACTIVE",
    "API_REOPENED",
    "LOOPBACK_VERIFIED",
    "PUBLIC_VERIFIED",
    "SOURCE_COMMITTED",
    "SUCCEEDED",
)


class LocalPublisherError(RuntimeError):
    """Disposable local publisher could not prove a required transition."""


class PublisherLockBusyError(LocalPublisherError):
    """Another publisher owns the exclusive local mutation lock."""


class SimulatedPublisherCrashError(LocalPublisherError):
    """Test-only crash injected after one durable state transition."""


@dataclass(frozen=True, slots=True)
class ActivePointer:
    """Bound active release marker resolved below one local simulation root."""

    release_id: str
    state_hash: str
    database: str
    database_path: Path


def _candidate_token(candidate_id: str) -> str:
    return hashlib.sha256(candidate_id.encode()).hexdigest()[:24]


def _release_relative(release_id: str) -> str:
    token = hashlib.sha256(release_id.encode()).hexdigest()[:32]
    return f"releases/{token}.sqlite"


def _pointer_bytes(release_id: str, state_hash: str) -> bytes:
    return canonical_json_line(
        {
            "database": _release_relative(release_id),
            "releaseId": release_id,
            "stateHash": state_hash,
        }
    )


def _parse_pointer(root: Path, content: bytes) -> ActivePointer:
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalPublisherError(f"active pointer JSON is invalid: {error}") from error
    if not isinstance(parsed, dict) or set(parsed) != {"database", "releaseId", "stateHash"}:
        raise LocalPublisherError("active pointer has unknown or missing fields")
    database = parsed["database"]
    release_id = parsed["releaseId"]
    state_hash = parsed["stateHash"]
    if not isinstance(database, str) or relative_parts(database)[:1] != ("releases",):
        raise LocalPublisherError("active pointer database path is outside releases")
    if not isinstance(release_id, str) or not release_id:
        raise LocalPublisherError("active pointer releaseId is invalid")
    if (
        not isinstance(state_hash, str)
        or len(state_hash) != 64
        or any(character not in "0123456789abcdef" for character in state_hash)
    ):
        raise LocalPublisherError("active pointer stateHash is invalid")
    path = root.joinpath(*relative_parts(database))
    report = inspect_release_database(path)
    if report.release.release_id != release_id or report.digest.state_hash != state_hash:
        raise LocalPublisherError("active pointer release/state differs from database")
    return ActivePointer(release_id, state_hash, database, path)


def read_active_pointer(root: Path) -> ActivePointer:
    """Resolve and prove one active pointer without following symlinks."""
    return _parse_pointer(root, read_regular_file(root / ACTIVE_POINTER_NAME, expected_mode=0o600))


def _write_descriptor(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise LocalPublisherError("short pointer write")
        remaining = remaining[written:]


def _replace_pointer(root: Path, content: bytes) -> None:
    """Atomically replace only the tiny active marker; release artifacts remain immutable."""
    directory = open_directory_nofollow(root)
    token = hashlib.sha256(content).hexdigest()[:24]
    temporary = f".{ACTIVE_POINTER_NAME}.{token}.next"
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
        except FileExistsError:
            existing = read_regular_file(root / temporary, expected_mode=0o600)
            if existing != content:
                raise LocalPublisherError(
                    "stale pointer staging file has different bytes"
                ) from None
        else:
            try:
                _write_descriptor(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        current = os.stat(ACTIVE_POINTER_NAME, dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_nlink != 1
        ):
            raise LocalPublisherError("active pointer is not a private single-link file")
        os.replace(
            temporary,
            ACTIVE_POINTER_NAME,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    except OSError as error:
        raise LocalPublisherError(f"atomic pointer replacement failed: {error}") from error
    finally:
        os.close(directory)


def install_initial_release(root: Path, database_path: Path) -> ActivePointer:
    """Install a verified initial release under a new local simulation root."""
    ensure_private_directory(root)
    ensure_private_directory(root / "releases")
    report = inspect_release_database(database_path)
    destination = root.joinpath(*relative_parts(_release_relative(report.release.release_id)))
    content = read_regular_file(database_path)
    if destination.exists():
        if read_regular_file(destination, expected_mode=0o600) != content:
            raise LocalPublisherError("initial release path already has different bytes")
    else:
        atomic_write_new(destination, content, mode=0o600)
    pointer = root / ACTIVE_POINTER_NAME
    content_pointer = _pointer_bytes(report.release.release_id, report.digest.state_hash)
    if pointer.exists():
        if read_regular_file(pointer, expected_mode=0o600) != content_pointer:
            raise LocalPublisherError("initial active pointer already differs")
    else:
        atomic_write_new(pointer, content_pointer, mode=0o600)
    return read_active_pointer(root)


def _target_path(root: Path, release_id: str) -> Path:
    return root.joinpath(*relative_parts(_release_relative(release_id)))


def _verify_target(path: Path, delta: Mapping[str, object]) -> None:
    report = inspect_release_database(path)
    if (
        report.release.release_id != delta["releaseId"]
        or report.release.sequence != delta["targetSequence"]
        or report.digest.state_hash != delta["afterStateHash"]
    ):
        raise LocalPublisherError("preserved staging release differs from delta target")
    expected = cast(list[dict[str, object]], delta["expectedTables"])
    for item in expected:
        table = cast(str, item["table"])
        if (
            report.digest.table_counts[table] != item["afterRowCount"]
            or report.digest.table_hashes[table] != item["afterLogicalSha256"]
        ):
            raise LocalPublisherError(f"preserved staging table differs: {table}")


def _apply_or_reuse(
    base: Path, target: Path, delta: Mapping[str, object]
) -> DeltaApplyReport | None:
    if target.exists():
        _verify_target(target, delta)
        return None
    return apply_delta_to_staging(base, target, delta)


def _save_exact(path: Path, content: bytes) -> None:
    if path.exists():
        if read_regular_file(path, expected_mode=0o600) != content:
            raise LocalPublisherError(f"preserved evidence differs: {path.name}")
        return
    atomic_write_new(path, content, mode=0o600)


def _warnings(llm: Mapping[str, object]) -> list[JsonObject]:
    status = llm.get("status")
    if status == "fallback":
        return [
            {
                "code": "LLM_FALLBACK_USED",
                "message": "Primary LLM was unavailable or rejected; accepted fallback was used.",
                "ownerVisible": True,
            }
        ]
    if status == "unavailable":
        return [
            {
                "code": "LLM_UNAVAILABLE",
                "message": "All LLM providers were unavailable; deterministic fallback was used.",
                "ownerVisible": True,
            }
        ]
    return []


def _rollback_result(
    delta: Mapping[str, object],
    llm: Mapping[str, object],
    previous: ActivePointer,
    *,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    issue_date: str | None,
) -> JsonObject:
    raw: dict[str, object] = {
        "activeReleaseId": previous.release_id,
        "candidateId": delta["candidateId"],
        "checks": [
            {
                "id": "public.smoke",
                "message": "Public smoke failed after local activation.",
                "status": "failed",
            },
            {
                "id": "rollback.proof",
                "message": "Previous pointer, release and state hash were restored.",
                "status": "passed",
            },
        ],
        "contractVersion": "1.0.0",
        "durationMs": duration_ms,
        "error": {
            "category": "smoke",
            "code": "PUBLIC_SMOKE_FAILED_ROLLED_BACK",
            "message": "New release failed public smoke and previous release was restored.",
            "nextAction": "Diagnose the candidate while the previous release remains active.",
            "retryable": False,
        },
        "exitCode": 34,
        "finishedAt": finished_at,
        "idempotencyDisposition": "executed",
        "llmOutcome": dict(llm),
        "operation": delta["operation"],
        "productionStateHash": previous.state_hash,
        "publicationSucceeded": False,
        "publishingBlocked": False,
        "releaseId": delta["releaseId"],
        "rollback": {
            "attempted": True,
            "required": True,
            "restoredReleaseId": previous.release_id,
            "restoredStateHash": previous.state_hash,
            "succeeded": True,
        },
        "sourceStateHash": previous.state_hash,
        "startedAt": started_at,
        "status": "rolled_back",
        "warnings": _warnings(llm),
    }
    if issue_date is not None:
        raw["issueDate"] = issue_date
    return cast(JsonObject, raw)


def _reconciliation_result(
    delta: Mapping[str, object],
    llm: Mapping[str, object],
    *,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    issue_date: str | None,
) -> JsonObject:
    raw: dict[str, object] = {
        "activeReleaseId": None,
        "candidateId": delta["candidateId"],
        "checks": [
            {
                "id": "rollback.proof",
                "message": "Previous pointer/release/hash could not be proven.",
                "status": "failed",
            }
        ],
        "contractVersion": "1.0.0",
        "durationMs": duration_ms,
        "error": {
            "category": "rollback",
            "code": "ROLLBACK_NOT_PROVEN",
            "message": "Rollback could not be proven after local activation.",
            "nextAction": "Stop publishing and reconcile the active release manually.",
            "retryable": False,
        },
        "exitCode": 35,
        "finishedAt": finished_at,
        "idempotencyDisposition": "executed",
        "llmOutcome": dict(llm),
        "operation": delta["operation"],
        "publicationSucceeded": False,
        "publishingBlocked": True,
        "releaseId": delta["releaseId"],
        "rollback": {
            "attempted": True,
            "required": True,
            "restoredReleaseId": None,
            "restoredStateHash": None,
            "succeeded": False,
        },
        "startedAt": started_at,
        "status": "needs_reconciliation",
        "warnings": _warnings(llm),
    }
    if issue_date is not None:
        raw["issueDate"] = issue_date
    return cast(JsonObject, raw)


class LocalPublisherSimulator:
    """Run the accepted state machine against two private disposable release roots."""

    def __init__(
        self,
        *,
        source_root: Path,
        production_root: Path,
        work_root: Path,
    ) -> None:
        self.source_root = source_root
        self.production_root = production_root
        self.work_root = work_root
        ensure_private_directory(work_root)
        ensure_private_directory(work_root / "inputs")
        ensure_private_directory(work_root / "previous")
        ensure_private_directory(work_root / "results")
        self.state_machine = PublisherStateMachine(work_root / "publisher-audit.jsonl")

    def _lock(self) -> int:
        path = self.work_root / "radar-mutation.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise LocalPublisherError("publisher lock is not a private single-link file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError as error:
            raise PublisherLockBusyError("local radar_mutation lock is busy") from error
        except BaseException:
            if "descriptor" in locals():
                os.close(descriptor)
            raise

    def _transition(
        self,
        delta: Mapping[str, object],
        event: str,
        occurred_at: str,
        *,
        reason: str | None = None,
        crash_after_state: str | None,
    ) -> str:
        state = self.state_machine.transition(
            candidate_id=cast(str, delta["candidateId"]),
            release_id=cast(str, delta["releaseId"]),
            event=event,
            occurred_at=occurred_at,
            before_state_hash=cast(str, delta["beforeStateHash"]),
            after_state_hash=cast(str, delta["afterStateHash"]),
            reason=reason,
        ).state
        if state == crash_after_state:
            raise SimulatedPublisherCrashError(f"simulated crash after durable state {state}")
        return cast(str, state)

    def _result_path(self, candidate_id: str) -> Path:
        return self.work_root / "results" / f"{_candidate_token(candidate_id)}.json"

    def _input_path(self, candidate_id: str) -> Path:
        return self.work_root / "inputs" / f"{_candidate_token(candidate_id)}.json"

    def _save_result(self, candidate_id: str, result: JsonObject) -> None:
        build_project_manager_report(result)
        _save_exact(self._result_path(candidate_id), canonical_json_line(result))

    def _read_result(self, candidate_id: str) -> JsonObject:
        try:
            parsed = json.loads(
                read_regular_file(self._result_path(candidate_id), expected_mode=0o600)
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LocalPublisherError(f"saved result is invalid JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise LocalPublisherError("saved result is not an object")
        result = cast(JsonObject, parsed)
        build_project_manager_report(result)
        return result

    def publish(
        self,
        document: Mapping[str, object],
        *,
        llm_outcome: Mapping[str, object],
        started_at: str,
        finished_at: str,
        duration_ms: int,
        issue_date: str | None = None,
        public_smoke_passes: bool = True,
        prove_rollback: bool = True,
        crash_after_state: str | None = None,
    ) -> JsonObject:
        """Execute or resume one deterministic local publication under the exclusive lock."""
        delta = validate_delta(document)
        try:
            llm = validate_llm_outcome(llm_outcome)
        except CandidateValidationError as error:
            raise LocalPublisherError(f"publisher LLM outcome is invalid: {error}") from error
        candidate_id = cast(str, delta["candidateId"])
        release_id = cast(str, delta["releaseId"])
        lock = self._lock()
        try:
            _save_exact(
                self._input_path(candidate_id),
                canonical_json_line(
                    {
                        "delta": delta,
                        "issueDate": issue_date,
                        "llmOutcome": llm,
                    }
                ),
            )
            state = self.state_machine.receive(
                candidate_id=candidate_id,
                release_id=release_id,
                occurred_at=started_at,
                before_state_hash=cast(str, delta["beforeStateHash"]),
                after_state_hash=cast(str, delta["afterStateHash"]),
            ).state
            if state == "SUCCEEDED":
                replay = dict(self._read_result(candidate_id))
                replay["status"] = "already_succeeded"
                replay["idempotencyDisposition"] = "replayed"
                replay["startedAt"] = started_at
                replay["finishedAt"] = finished_at
                replay["durationMs"] = duration_ms
                result = replay
                build_project_manager_report(result)
                return result
            result_path = self._result_path(candidate_id)
            if state == "ROLLED_BACK":
                if result_path.exists():
                    return self._read_result(candidate_id)
                previous = read_active_pointer(self.production_root)
                source = read_active_pointer(self.source_root)
                expected_base = (delta["baseReleaseId"], delta["beforeStateHash"])
                if (previous.release_id, previous.state_hash) != expected_base or (
                    source.release_id,
                    source.state_hash,
                ) != expected_base:
                    raise LocalPublisherError(
                        "cannot recover rolled-back result without the proven base pointers"
                    )
                result = _rollback_result(
                    delta,
                    llm,
                    previous,
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    issue_date=issue_date,
                )
                self._save_result(candidate_id, result)
                return result
            if state == "NEEDS_RECONCILIATION":
                if result_path.exists():
                    return self._read_result(candidate_id)
                result = _reconciliation_result(
                    delta,
                    llm,
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    issue_date=issue_date,
                )
                self._save_result(candidate_id, result)
                return result
            if state in {"FAILED_PRE_ACTIVATION", "REJECTED"}:
                return self._read_result(candidate_id)
            disposition = "executed" if state == "RECEIVED" else "resumed"
            source_before = read_active_pointer(self.source_root)
            production_before = read_active_pointer(self.production_root)
            state_name = cast(str, state)
            source_expected = (
                (delta["releaseId"], delta["afterStateHash"])
                if state_name == "SOURCE_COMMITTED"
                else (delta["baseReleaseId"], delta["beforeStateHash"])
            )
            if (source_before.release_id, source_before.state_hash) != source_expected:
                raise DeltaConflictError("source active release/state differs from checkpoint")
            if state_name == "FAILED_POST_REMOTE_ACTIVATION":
                production_expected = {
                    (delta["baseReleaseId"], delta["beforeStateHash"]),
                    (delta["releaseId"], delta["afterStateHash"]),
                }
            elif state_name in _NORMAL_STATES and _NORMAL_STATES.index(
                state_name
            ) >= _NORMAL_STATES.index("REMOTE_ACTIVE"):
                production_expected = {(delta["releaseId"], delta["afterStateHash"])}
            else:
                production_expected = {(delta["baseReleaseId"], delta["beforeStateHash"])}
            if (
                production_before.release_id,
                production_before.state_hash,
            ) not in production_expected:
                raise DeltaConflictError("production active release/state differs from checkpoint")

            source_target = _target_path(self.source_root, release_id)
            production_target = _target_path(self.production_root, release_id)
            previous_path = (
                self.work_root / "previous" / f"production-{_candidate_token(candidate_id)}.json"
            )

            if state == "RECEIVED":
                state = self._transition(
                    delta,
                    "validation_passed",
                    started_at,
                    crash_after_state=crash_after_state,
                )
            if state == "VALIDATED":
                _apply_or_reuse(source_before.database_path, source_target, delta)
                state = self._transition(
                    delta,
                    "source_staging_passed",
                    started_at,
                    crash_after_state=crash_after_state,
                )
            if state == "SOURCE_STAGED":
                _verify_target(source_target, delta)
                state = self._transition(
                    delta,
                    "artifact_build_passed",
                    started_at,
                    crash_after_state=crash_after_state,
                )
            if state == "ARTIFACTS_BUILT":
                validate_delta(delta)
                state = self._transition(
                    delta,
                    "delta_build_passed",
                    started_at,
                    crash_after_state=crash_after_state,
                )
            if state == "DELTA_BUILT":
                _apply_or_reuse(production_before.database_path, production_target, delta)
                state = self._transition(
                    delta,
                    "transport_and_remote_stage_passed",
                    started_at,
                    crash_after_state=crash_after_state,
                )
            if state == "REMOTE_STAGED":
                _verify_target(production_target, delta)
                state = self._transition(
                    delta,
                    "remote_verification_passed",
                    started_at,
                    crash_after_state=crash_after_state,
                )
            if state == "REMOTE_VERIFIED":
                _save_exact(
                    previous_path,
                    read_regular_file(
                        self.production_root / ACTIVE_POINTER_NAME,
                        expected_mode=0o600,
                    ),
                )
                _replace_pointer(
                    self.production_root,
                    _pointer_bytes(release_id, cast(str, delta["afterStateHash"])),
                )
                state = self._transition(
                    delta,
                    "remote_pointer_activated",
                    started_at,
                    crash_after_state=crash_after_state,
                )
            if state == "REMOTE_ACTIVE":
                read_active_pointer(self.production_root)
                state = self._transition(
                    delta,
                    "api_connections_reopened",
                    started_at,
                    crash_after_state=crash_after_state,
                )
            if state == "API_REOPENED":
                active = read_active_pointer(self.production_root)
                if active.release_id != release_id or active.state_hash != delta["afterStateHash"]:
                    raise LocalPublisherError("loopback release/hash proof failed")
                state = self._transition(
                    delta,
                    "loopback_release_and_hash_verified",
                    started_at,
                    crash_after_state=crash_after_state,
                )
            if state == "LOOPBACK_VERIFIED" and not public_smoke_passes:
                state = self._transition(
                    delta,
                    "public_verification_failed",
                    finished_at,
                    reason="injected local public smoke failure",
                    crash_after_state=crash_after_state,
                )
            if state == "FAILED_POST_REMOTE_ACTIVATION":
                previous_content = read_regular_file(previous_path, expected_mode=0o600)
                if prove_rollback:
                    _replace_pointer(self.production_root, previous_content)
                    previous = _parse_pointer(self.production_root, previous_content)
                    state = self._transition(
                        delta,
                        "previous_pointer_and_hash_verified",
                        finished_at,
                        crash_after_state=crash_after_state,
                    )
                    result = _rollback_result(
                        delta,
                        llm,
                        previous,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                        issue_date=issue_date,
                    )
                else:
                    state = self._transition(
                        delta,
                        "rollback_not_proven",
                        finished_at,
                        crash_after_state=crash_after_state,
                    )
                    result = _reconciliation_result(
                        delta,
                        llm,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                        issue_date=issue_date,
                    )
                self._save_result(candidate_id, result)
                return result
            if state == "LOOPBACK_VERIFIED":
                state = self._transition(
                    delta,
                    "public_release_and_hash_verified",
                    finished_at,
                    crash_after_state=crash_after_state,
                )
            if state == "PUBLIC_VERIFIED":
                _replace_pointer(
                    self.source_root,
                    _pointer_bytes(release_id, cast(str, delta["afterStateHash"])),
                )
                state = self._transition(
                    delta,
                    "source_pointer_committed",
                    finished_at,
                    crash_after_state=crash_after_state,
                )
            if state != "SOURCE_COMMITTED":
                raise PublisherStateError(f"unexpected pre-success state: {state}")
            if result_path.exists():
                result = self._read_result(candidate_id)
                if result.get("status") != "published":
                    raise LocalPublisherError("saved pre-success result is not published")
                self._transition(
                    delta,
                    "result_persisted",
                    finished_at,
                    crash_after_state=crash_after_state,
                )
                return result
            raw_result: dict[str, object] = {
                "activeReleaseId": release_id,
                "candidateId": candidate_id,
                "checks": [
                    {
                        "id": "database.source",
                        "message": "Source staging release and complete table digest passed.",
                        "status": "passed",
                    },
                    {
                        "id": "database.production",
                        "message": ("Production staging release and complete table digest passed."),
                        "status": "passed",
                    },
                    {
                        "id": "activation.reopen",
                        "message": "Active pointer was reopened and release/hash verified.",
                        "status": "passed",
                    },
                    {
                        "id": "public.smoke",
                        "message": "Disposable public smoke passed.",
                        "status": "passed",
                    },
                ],
                "contractVersion": "1.0.0",
                "durationMs": duration_ms,
                "error": None,
                "exitCode": 0,
                "finishedAt": finished_at,
                "idempotencyDisposition": disposition,
                "llmOutcome": llm,
                "operation": delta["operation"],
                "productionStateHash": delta["afterStateHash"],
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
                "sourceStateHash": delta["afterStateHash"],
                "startedAt": started_at,
                "status": "published",
                "warnings": _warnings(llm),
            }
            if issue_date is not None:
                raw_result["issueDate"] = issue_date
            result = cast(JsonObject, raw_result)
            self._save_result(candidate_id, result)
            if crash_after_state == "RESULT_SAVED":
                raise SimulatedPublisherCrashError(
                    "simulated crash after durable result save before SUCCEEDED"
                )
            self._transition(
                delta,
                "result_persisted",
                finished_at,
                crash_after_state=crash_after_state,
            )
            return result
        except SafeFilesystemError as error:
            raise LocalPublisherError(
                f"unsafe local publisher filesystem boundary: {error}"
            ) from error
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)


__all__ = [
    "ACTIVE_POINTER_NAME",
    "ActivePointer",
    "LocalPublisherError",
    "LocalPublisherSimulator",
    "PublisherLockBusyError",
    "SimulatedPublisherCrashError",
    "install_initial_release",
    "read_active_pointer",
]
