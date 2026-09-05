"""Publication recovery exercises the real remote activator, not a success stub."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import pytest
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.publisher import remote_activation, remote_orchestration
from packages.publisher.remote_orchestration import PublishInputs, Transport, publish_candidate
from packages.storage.content_pointer import read_content_pointer
from tools import run_stage15_dual

from test_stage14_remote_orchestration import _fixture, _source_root


def _remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[PublishInputs, Path, Transport, list[JsonObject]]:
    inputs, _candidate_id = _fixture(tmp_path)
    inputs = replace(inputs, application_release_id="app_release_synthetic01")
    base = read_content_pointer(inputs.source_root)
    parent = tmp_path / "remote"
    parent.mkdir(mode=0o700)
    root = _source_root(parent, base.database_path, base.release_id, base.state_hash)
    requests: list[JsonObject] = []

    def health(_url: str, _release_id: str, _state_hash: str) -> None:
        pass

    monkeypatch.setattr(remote_activation, "_health", health)

    def transport(content: bytes) -> tuple[int, bytes, bytes]:
        request = cast(JsonObject, json.loads(content))
        requests.append(request)
        result = remote_activation.activate_request(
            request,
            content_root=root,
            incoming_root=parent / "incoming",
            audit_root=parent / "audit",
            mutation_root=parent / "mutation",
            gazette_root=parent / "gazettes",
            api_uid=os.getuid(),
            api_gid=os.getgid(),
        )
        return 0, canonical_json_line(asdict(result)), b""

    return inputs, root, transport, requests


@pytest.mark.parametrize("renamed_base", [False, True])
def test_retry_after_lost_remote_response_converges_without_reapplying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, renamed_base: bool
) -> None:
    inputs, remote, transport, requests = _remote(tmp_path, monkeypatch)
    if renamed_base:
        # Like application migration 0004: the release id stays the same but its
        # database pathname changes. Even JSON formatting is part of the fence.
        for root in (inputs.source_root, remote):
            pointer = read_content_pointer(root)
            pointer.database_path.rename(root / "releases" / "migrated-base.sqlite")
            (root / "active.json").write_text(
                json.dumps(
                    {
                        "stateHash": pointer.state_hash,
                        "database": "releases/migrated-base.sqlite",
                        "releaseId": pointer.release_id,
                    },
                    indent=2,
                )
            )
    base = read_content_pointer(inputs.source_root)

    def lose_response(content: bytes) -> tuple[int, bytes, bytes]:
        transport(content)
        return 255, b"", b"synthetic lost SSH response"

    with pytest.raises(remote_orchestration.RemoteOrchestrationError, match="transport failed"):
        publish_candidate(inputs, lose_response)
    activated = read_content_pointer(remote)
    original_database = activated.database_path.read_bytes()
    original_pointer = (remote / "active.json").read_bytes()
    assert read_content_pointer(inputs.source_root).release_id == base.release_id
    assert activated.release_id != base.release_id

    result = publish_candidate(inputs, transport)
    assert result["status"] == "published"
    assert read_content_pointer(inputs.source_root).release_id == activated.release_id
    assert (remote / "active.json").read_bytes() == original_pointer
    assert activated.database_path.read_bytes() == original_database
    assert requests[0] == requests[1]
    assert len(list((remote / "releases").iterdir())) == 2


@pytest.mark.parametrize("crash_at", ["remote_audit", "source_result"])
def test_retry_recovers_crash_between_pointer_commit_and_result_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_at: str
) -> None:
    inputs, remote, transport, requests = _remote(tmp_path, monkeypatch)
    if crash_at == "remote_audit":

        def crash_save(_root: Path, _result: remote_activation.RemoteActivationResult) -> None:
            raise OSError("synthetic crash before remote audit")

        with monkeypatch.context() as fault:
            fault.setattr(remote_activation, "_save_result", crash_save)
            with pytest.raises(OSError, match="synthetic crash"):
                publish_candidate(inputs, transport)
        assert not list((remote.parent / "audit").glob("*.result.json"))
    else:
        normal_save = remote_orchestration._save_exact

        def crash_local_save(path: Path, content: bytes) -> None:
            if path.parent.name == "results":
                raise OSError("synthetic crash before local result")
            normal_save(path, content)

        with monkeypatch.context() as fault:
            fault.setattr(remote_orchestration, "_save_exact", crash_local_save)
            with pytest.raises(OSError, match="synthetic crash"):
                publish_candidate(inputs, transport)

    target = read_content_pointer(remote)
    result = publish_candidate(inputs, transport)
    assert result["status"] == "published"
    assert read_content_pointer(inputs.source_root).state_hash == target.state_hash
    assert read_content_pointer(inputs.source_root).release_id == target.release_id
    assert requests[0] == requests[1]
    assert len(list((remote / "releases").iterdir())) == 2


def test_replay_rejects_changed_request_and_new_stale_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, remote, transport, requests = _remote(tmp_path, monkeypatch)
    publish_candidate(inputs, transport)
    pointer = (remote / "active.json").read_bytes()
    changed = dict(requests[0])
    changed["expectedCurrentPointerSha256"] = "0" * 64
    with pytest.raises(remote_activation.RemoteActivationError, match="different bytes"):
        transport(canonical_json_line(changed))
    new_stale = {**requests[0], "requestId": "new-stale-request"}
    # Repeating a rejected new request must not turn it into a recoverable one.
    for _ in range(2):
        with pytest.raises(remote_activation.RemoteActivationError, match="request fence"):
            transport(canonical_json_line(new_stale))
    assert (remote / "active.json").read_bytes() == pointer


@pytest.mark.parametrize("audit_saved", [True, False])
def test_old_success_cannot_reactivate_after_explicit_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_saved: bool
) -> None:
    inputs, remote, transport, requests = _remote(tmp_path, monkeypatch)
    base_pointer = (remote / "active.json").read_bytes()
    if audit_saved:
        publish_candidate(inputs, transport)
    else:

        def crash_save(_root: Path, _result: remote_activation.RemoteActivationResult) -> None:
            raise OSError("synthetic crash before audit")

        with monkeypatch.context() as fault:
            fault.setattr(remote_activation, "_save_result", crash_save)
            with pytest.raises(OSError, match="synthetic crash"):
                publish_candidate(inputs, transport)
    request = requests[0]
    rollback: JsonObject = {
        "action": "rollback",
        "requestId": "explicit-test-rollback",
        "assetPayloads": {},
        "delta": None,
        "expectedCurrentPointerSha256": hashlib.sha256(
            (remote / "active.json").read_bytes()
        ).hexdigest(),
        "rollbackPointer": json.loads(base_pointer),
    }
    transport(canonical_json_line(rollback))
    with pytest.raises(remote_activation.RemoteActivationError, match="explicitly rolled back"):
        transport(canonical_json_line(request))
    assert (remote / "active.json").read_bytes() == base_pointer


def test_recovery_requires_live_health_and_honours_reconciliation_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, remote, transport, requests = _remote(tmp_path, monkeypatch)
    publish_candidate(inputs, transport)
    request = requests[0]

    def unavailable(_url: str, _release_id: str, _state_hash: str) -> None:
        raise remote_activation.RemoteActivationError("synthetic health unavailable")

    monkeypatch.setattr(remote_activation, "_health", unavailable)
    with pytest.raises(remote_activation.RemoteActivationError, match="health unavailable"):
        transport(canonical_json_line(request))
    marker = remote.parent / "audit" / "NEEDS_RECONCILIATION"
    marker.write_bytes(b"synthetic reconciliation block\n")
    marker.chmod(0o600)
    with pytest.raises(remote_activation.RemoteActivationError, match="NEEDS_RECONCILIATION"):
        transport(canonical_json_line(request))


@pytest.mark.parametrize("crash_at", ["remote_response", "source_result"])
def test_daily_next_attempt_reuses_exact_publication_even_after_source_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_at: str
) -> None:
    inputs, remote, transport, requests = _remote(tmp_path, monkeypatch)
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"synthetic":true}\n')
    legacy.chmod(0o644)
    args = argparse.Namespace(
        issue_date="2026-08-20",
        legacy_json=legacy,
        source_root=inputs.source_root,
        publisher_root=inputs.work_root,
        application_release_id=inputs.application_release_id,
        started_at=inputs.created_at,
        finished_at=inputs.finished_at,
        duration_ms=inputs.duration_ms,
        ssh_host="synthetic-host",
        ssh_identity=tmp_path / "unused-test-key",
    )
    builds = 0

    def build(_args: argparse.Namespace, _legacy: Path, _root: Path) -> JsonObject:
        nonlocal builds
        builds += 1
        return {"package": str(inputs.package), "staging": str(inputs.candidate_staging)}

    monkeypatch.setattr(run_stage15_dual, "_build_candidate", build)

    def lose_response(content: bytes) -> tuple[int, bytes, bytes]:
        transport(content)
        return 255, b"", b"synthetic lost response"

    first = run_stage15_dual._next_attempt_root(tmp_path / "daily-run")
    prepared = run_stage15_dual._publication_for_attempt(args, first)
    assert prepared is not None
    built, publish_args = prepared
    normal_save = remote_orchestration._save_exact

    def crash_save(path: Path, content: bytes) -> None:
        if path.parent.name == "results":
            raise OSError("synthetic local result crash")
        normal_save(path, content)

    with monkeypatch.context() as fault:
        chosen_transport = lose_response if crash_at == "remote_response" else transport
        fault.setattr(run_stage15_dual, "ssh_transport", lambda **_kwargs: chosen_transport)
        if crash_at == "source_result":
            fault.setattr(remote_orchestration, "_save_exact", crash_save)
        with pytest.raises((OSError, remote_orchestration.RemoteOrchestrationError)):
            run_stage15_dual._publish(publish_args, built, first)

    assert run_stage15_dual._source_has_issue(inputs.source_root, args.issue_date) == (
        crash_at == "source_result"
    )
    args.started_at = "2026-08-21T10:30:00Z"
    args.finished_at = "2026-08-21T10:31:00Z"
    args.duration_ms = 60_000
    args.application_release_id = "app_release_newer_runtime"
    second = run_stage15_dual._next_attempt_root(first.parent)
    resumed = run_stage15_dual._publication_for_attempt(args, second)
    assert resumed is not None
    rebuilt, resumed_args = resumed
    assert builds == 1
    assert resumed_args.started_at == inputs.created_at
    assert resumed_args.finished_at == inputs.finished_at
    assert resumed_args.application_release_id == inputs.application_release_id
    assert (first / "publication-input.json").read_bytes() == (
        second / "publication-input.json"
    ).read_bytes()
    monkeypatch.setattr(run_stage15_dual, "ssh_transport", lambda **_kwargs: transport)
    result = run_stage15_dual._publish(resumed_args, rebuilt, second)
    assert result["status"] == "published"
    assert requests[0] == requests[1]
    assert (inputs.source_root / "active.json").read_bytes() == (
        remote / "active.json"
    ).read_bytes()

    # Changed Legacy evidence cannot be attached to the old publication.
    legacy.write_text('{"synthetic":true,"changed":true}\n')
    third = run_stage15_dual._next_attempt_root(first.parent)
    with pytest.raises(run_stage15_dual.Stage15DualRunError, match="retained publication input"):
        run_stage15_dual._publication_for_attempt(args, third)
