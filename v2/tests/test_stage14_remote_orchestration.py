"""Stage 14 source-side Project Manager publisher orchestration gates."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

import pytest
from packages.domain.candidate_package import build_candidate_package
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.publisher.remote_orchestration import (
    PublishInputs,
    RemoteOrchestrationError,
    Transport,
    publish_candidate,
)
from packages.storage.content_pointer import read_content_pointer
from packages.storage.safe_files import atomic_write_new

from test_stage5_candidate_builder import (
    _daily_candidate,
    _seed_database,
    _snapshot_workspace,
)


def _source_root(root: Path, database: Path, release_id: str, state_hash: str) -> Path:
    source = root / "source"
    releases = source / "releases"
    releases.mkdir(parents=True, mode=0o700)
    source.chmod(0o700)
    releases.chmod(0o700)
    token = hashlib.sha256(release_id.encode()).hexdigest()[:32]
    target = releases / f"{token}.sqlite"
    atomic_write_new(target, database.read_bytes(), mode=0o600)
    atomic_write_new(
        source / "active.json",
        canonical_json_line(
            {
                "database": f"releases/{token}.sqlite",
                "releaseId": release_id,
                "stateHash": state_hash,
            }
        ),
        mode=0o600,
    )
    return source


def _fixture(tmp_path: Path) -> tuple[PublishInputs, str]:
    base = tmp_path / "base.sqlite"
    state_hash = _seed_database(base)
    workspace, attestation = _snapshot_workspace(tmp_path)
    candidate = _daily_candidate(state_hash, attestation.identity)
    staging_parent = tmp_path / "staging"
    package_store = tmp_path / "packages"
    staging_parent.mkdir(mode=0o700)
    package_store.mkdir(mode=0o700)
    staging = staging_parent / "daily.sqlite"
    built = build_candidate_package(
        source_database=base,
        staging_database=staging,
        package_store=package_store,
        candidate=candidate,
        v2_workspace=workspace,
    )
    expected = cast(dict[str, object], candidate["expectedBase"])
    source = _source_root(
        tmp_path,
        base,
        cast(str, expected["releaseId"]),
        state_hash,
    )
    work = tmp_path / "publisher"
    work.mkdir(mode=0o700)
    return (
        PublishInputs(
            package=built.package.path,
            candidate_staging=staging,
            source_root=source,
            work_root=work,
            application_release_id="app_release_stage14_test",
            created_at="2026-08-20T10:30:00Z",
            finished_at="2026-08-20T10:30:01Z",
            duration_ms=1000,
        ),
        cast(str, candidate["candidateId"]),
    )


def _successful_transport(calls: list[JsonObject]) -> Transport:
    def transport(content: bytes) -> tuple[int, bytes, bytes]:
        request = cast(JsonObject, json.loads(content))
        calls.append(request)
        delta = cast(dict[str, object], request["delta"])
        result = {
            "database_sha256": "a" * 64,
            "loopback_verified": True,
            "pointer_sha256": "b" * 64,
            "previous_release_id": delta["baseReleaseId"],
            "previous_state_hash": delta["beforeStateHash"],
            "release_id": delta["releaseId"],
            "request_id": request["requestId"],
            "state_hash": delta["afterStateHash"],
            "status": "published",
        }
        return 0, canonical_json_line(result), b""

    return transport


def test_remote_orchestrator_commits_source_and_replays_without_transport(tmp_path: Path) -> None:
    inputs, candidate_id = _fixture(tmp_path)
    before = read_content_pointer(inputs.source_root)
    calls: list[JsonObject] = []
    result = publish_candidate(inputs, _successful_transport(calls))
    after = read_content_pointer(inputs.source_root)

    assert result["status"] == "published"
    assert result["candidateId"] == candidate_id
    assert after.release_id == result["releaseId"]
    assert after.state_hash == result["sourceStateHash"]
    assert after.release_id != before.release_id
    assert len(calls) == 1

    replay = publish_candidate(inputs, _successful_transport(calls))
    assert replay["status"] == "already_succeeded"
    assert replay["idempotencyDisposition"] == "replayed"
    assert len(calls) == 1


def test_remote_transport_failure_does_not_commit_source_pointer(tmp_path: Path) -> None:
    inputs, _candidate_id = _fixture(tmp_path)
    pointer = (inputs.source_root / "active.json").read_bytes()

    def failed(_content: bytes) -> tuple[int, bytes, bytes]:
        return 40, b'{"status":"failed"}\n', b"forced command rejected request"

    with pytest.raises(RemoteOrchestrationError, match="restricted remote transport failed"):
        publish_candidate(inputs, failed)
    assert (inputs.source_root / "active.json").read_bytes() == pointer
    assert not tuple((inputs.work_root / "results").iterdir())


def test_remote_result_identity_mismatch_does_not_commit_source_pointer(tmp_path: Path) -> None:
    inputs, _candidate_id = _fixture(tmp_path)
    pointer = (inputs.source_root / "active.json").read_bytes()
    calls: list[JsonObject] = []
    normal = _successful_transport(calls)

    def mismatched(content: bytes) -> tuple[int, bytes, bytes]:
        code, stdout, stderr = normal(content)
        result = json.loads(stdout)
        result["state_hash"] = "f" * 64
        return code, canonical_json_line(result), stderr

    with pytest.raises(RemoteOrchestrationError, match="differs from exact request target"):
        publish_candidate(inputs, mismatched)
    assert (inputs.source_root / "active.json").read_bytes() == pointer


def test_source_pointer_security_metadata_is_preserved(tmp_path: Path) -> None:
    inputs, _candidate_id = _fixture(tmp_path)
    before = os.stat(inputs.source_root / "active.json", follow_symlinks=False)
    publish_candidate(inputs, _successful_transport([]))
    after = os.stat(inputs.source_root / "active.json", follow_symlinks=False)
    assert (after.st_uid, after.st_gid, after.st_mode & 0o777, after.st_nlink) == (
        before.st_uid,
        before.st_gid,
        0o600,
        1,
    )
