from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from tools.rotate_content_state import RetentionError, main


def _database(path: Path, release_id: str, state_hash: str, sequence: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE content_releases ("
        "release_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, "
        "after_state_hash TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO content_releases VALUES (?, ?, ?)",
        (release_id, sequence, state_hash),
    )
    connection.commit()
    connection.close()


def _pointer(root: Path, name: str, release_id: str, state_hash: str) -> None:
    (root / "active.json").write_text(
        json.dumps(
            {
                "database": f"releases/{name}",
                "releaseId": release_id,
                "stateHash": state_hash,
            }
        )
    )


def _age(path: Path, days: int) -> None:
    timestamp = path.stat().st_mtime - days * 86_400
    os.utime(path, (timestamp, timestamp))


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    content = tmp_path / "content"
    work = tmp_path / "work"
    (content / "releases").mkdir(parents=True)
    for name in ("releases", "requests", "results"):
        (work / name).mkdir(parents=True, exist_ok=True)
    return content, work


def _request(
    path: Path,
    *,
    request_id: str,
    candidate_id: str,
    release_id: str,
    state_hash: str,
) -> None:
    path.write_text(
        json.dumps(
            {
                "action": "publish",
                "delta": {
                    "candidateId": candidate_id,
                    "releaseId": release_id,
                    "afterStateHash": state_hash,
                },
                "requestId": request_id,
            }
        )
    )


def _remote_result(
    path: Path,
    *,
    request_id: str,
    release_id: str,
    state_hash: str,
    status: str = "published",
) -> None:
    path.write_text(
        json.dumps(
            {
                "database_sha256": "1" * 64,
                "loopback_verified": True,
                "pointer_sha256": "2" * 64,
                "previous_release_id": "rel-previous",
                "previous_state_hash": "3" * 64,
                "release_id": release_id,
                "request_id": request_id,
                "state_hash": state_hash,
                "status": status,
            }
        )
    )


def test_source_rotation_keeps_active_recent_and_unresolved_base(tmp_path: Path) -> None:
    source, publisher = _roots(tmp_path)
    state = "a" * 64
    for index, name in enumerate(("old.sqlite", "pending.sqlite", "active.sqlite"), 1):
        _database(source / "releases" / name, f"rel-{index}", state, index)
        _age(source / "releases" / name, 20 if name != "active.sqlite" else 0)
    _pointer(source, "active.sqlite", "rel-3", state)
    (publisher / "requests" / "candidate.json").write_text("{}")
    (publisher / "requests" / "candidate.base-pointer.json").write_text(
        json.dumps(
            {
                "database": "releases/pending.sqlite",
                "releaseId": "rel-2",
                "stateHash": state,
            }
        )
    )

    assert (
        main(
            [
                "--retention-days",
                "7",
                "--minimum-releases",
                "2",
                "--apply",
                "source",
                "--source-root",
                str(source),
                "--publisher-root",
                str(publisher),
            ]
        )
        == 0
    )
    assert not (source / "releases" / "old.sqlite").exists()
    assert (source / "releases" / "pending.sqlite").exists()
    assert (source / "releases" / "active.sqlite").exists()


def test_source_malformed_result_blocks_pass(tmp_path: Path) -> None:
    source, publisher = _roots(tmp_path)
    state = "f" * 64
    _database(source / "releases" / "active.sqlite", "rel-active", state, 1)
    _pointer(source, "active.sqlite", "rel-active", state)
    _request(
        publisher / "requests" / "candidate.json",
        request_id="request",
        candidate_id="candidate",
        release_id="rel-candidate",
        state_hash=state,
    )
    (publisher / "results" / "candidate.json").write_text("{}")

    with pytest.raises(RetentionError, match="not a proven success"):
        main(
            [
                "--retention-days",
                "7",
                "--minimum-releases",
                "2",
                "source",
                "--source-root",
                str(source),
                "--publisher-root",
                str(publisher),
            ]
        )


def test_source_request_candidate_mismatch_blocks_pass(tmp_path: Path) -> None:
    source, publisher = _roots(tmp_path)
    state = "f" * 64
    release_id = "rel-candidate"
    _database(source / "releases" / "active.sqlite", "rel-active", state, 1)
    _database(publisher / "releases" / "candidate.sqlite", release_id, state, 2)
    _pointer(source, "active.sqlite", "rel-active", state)
    _request(
        publisher / "requests" / "candidate.json",
        request_id="request",
        candidate_id="different-candidate",
        release_id=release_id,
        state_hash=state,
    )
    (publisher / "results" / "candidate.json").write_text(
        json.dumps(
            {
                "candidateId": "candidate",
                "status": "published",
                "publicationSucceeded": True,
                "publishingBlocked": False,
                "exitCode": 0,
                "error": None,
                "releaseId": release_id,
                "activeReleaseId": release_id,
                "sourceStateHash": state,
                "productionStateHash": state,
            }
        )
    )

    with pytest.raises(RetentionError, match="not a proven success"):
        main(
            [
                "--retention-days",
                "7",
                "--minimum-releases",
                "2",
                "source",
                "--source-root",
                str(source),
                "--publisher-root",
                str(publisher),
            ]
        )


def test_remote_rotation_deletes_only_completed_old_staging(tmp_path: Path) -> None:
    content = tmp_path / "content"
    incoming = tmp_path / "incoming"
    audit = tmp_path / "audit"
    mutation = tmp_path / "mutation"
    for root in (content / "releases", incoming, audit, mutation):
        root.mkdir(parents=True)
    state = "b" * 64
    _database(content / "releases" / "old.sqlite", "rel-old", state, 1)
    _database(content / "releases" / "recent.sqlite", "rel-recent", state, 2)
    _database(content / "releases" / "active.sqlite", "rel-active", state, 3)
    _age(content / "releases" / "old.sqlite", 20)
    _pointer(content, "active.sqlite", "rel-active", state)
    for request_id, completed in (("done", True), ("failed", False)):
        staging = incoming / f"{request_id}.staging.sqlite"
        _database(staging, f"rel-{request_id}", state, 3)
        _age(staging, 20)
        _request(
            incoming / f"{request_id}.json",
            request_id=request_id,
            candidate_id=f"cand-{request_id}",
            release_id=f"rel-{request_id}",
            state_hash=state,
        )
        if completed:
            _remote_result(
                audit / f"{request_id}.result.json",
                request_id=request_id,
                release_id=f"rel-{request_id}",
                state_hash=state,
            )

    assert (
        main(
            [
                "--retention-days",
                "7",
                "--minimum-releases",
                "2",
                "--apply",
                "remote",
                "--content-root",
                str(content),
                "--incoming-root",
                str(incoming),
                "--audit-root",
                str(audit),
                "--mutation-root",
                str(mutation),
            ]
        )
        == 0
    )
    assert not (content / "releases" / "old.sqlite").exists()
    assert (content / "releases" / "active.sqlite").exists()
    assert not (incoming / "done.staging.sqlite").exists()
    assert (incoming / "failed.staging.sqlite").exists()


def test_remote_rotation_blocks_on_reconciliation_marker(tmp_path: Path) -> None:
    content = tmp_path / "content"
    incoming = tmp_path / "incoming"
    audit = tmp_path / "audit"
    mutation = tmp_path / "mutation"
    for root in (content / "releases", incoming, audit, mutation):
        root.mkdir(parents=True)
    state = "c" * 64
    _database(content / "releases" / "active.sqlite", "rel-active", state, 1)
    _pointer(content, "active.sqlite", "rel-active", state)
    (audit / "NEEDS_RECONCILIATION").write_text("blocked")
    with pytest.raises(RetentionError, match="reconciliation marker"):
        main(
            [
                "--retention-days",
                "7",
                "--minimum-releases",
                "2",
                "remote",
                "--content-root",
                str(content),
                "--incoming-root",
                str(incoming),
                "--audit-root",
                str(audit),
                "--mutation-root",
                str(mutation),
            ]
        )


def test_unresolved_publish_protects_remote_base_and_target(tmp_path: Path) -> None:
    content = tmp_path / "content"
    incoming = tmp_path / "incoming"
    audit = tmp_path / "audit"
    mutation = tmp_path / "mutation"
    for root in (content / "releases", incoming, audit, mutation):
        root.mkdir(parents=True)
    state = "d" * 64
    release_id = "rel-target"
    target = f"{hashlib.sha256(release_id.encode()).hexdigest()[:32]}.sqlite"
    for index, name in enumerate(("base.sqlite", target, "active.sqlite"), 1):
        _database(content / "releases" / name, f"rel-{index}", state, index)
        _age(content / "releases" / name, 20 if name != "active.sqlite" else 0)
    _pointer(content, "active.sqlite", "rel-3", state)
    (incoming / "request.json").write_text(
        json.dumps(
            {
                "action": "publish",
                "delta": {"releaseId": release_id, "afterStateHash": state},
            }
        )
    )
    (incoming / "request.base-pointer.json").write_text(
        json.dumps(
            {
                "database": "releases/base.sqlite",
                "releaseId": "rel-1",
                "stateHash": state,
            }
        )
    )

    assert (
        main(
            [
                "--retention-days",
                "7",
                "--minimum-releases",
                "2",
                "--apply",
                "remote",
                "--content-root",
                str(content),
                "--incoming-root",
                str(incoming),
                "--audit-root",
                str(audit),
                "--mutation-root",
                str(mutation),
            ]
        )
        == 0
    )
    assert (content / "releases" / "base.sqlite").exists()
    assert (content / "releases" / target).exists()


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({}, "not a proven success"),
        (
            {
                "database_sha256": "1" * 64,
                "loopback_verified": True,
                "pointer_sha256": "2" * 64,
                "previous_release_id": "rel-previous",
                "previous_state_hash": "3" * 64,
                "release_id": "rel-done",
                "request_id": "done",
                "state_hash": "e" * 64,
                "status": "failed",
            },
            "not a proven success",
        ),
        (
            {
                "database_sha256": "1" * 64,
                "loopback_verified": True,
                "pointer_sha256": "2" * 64,
                "previous_release_id": "rel-previous",
                "previous_state_hash": "3" * 64,
                "release_id": "rel-other",
                "request_id": "done",
                "state_hash": "e" * 64,
                "status": "published",
            },
            "not a proven success",
        ),
    ],
)
def test_remote_malformed_failed_or_mismatched_result_blocks_pass(
    tmp_path: Path,
    result: dict[str, object],
    message: str,
) -> None:
    content = tmp_path / "content"
    incoming = tmp_path / "incoming"
    audit = tmp_path / "audit"
    mutation = tmp_path / "mutation"
    for root in (content / "releases", incoming, audit, mutation):
        root.mkdir(parents=True)
    state = "e" * 64
    _database(content / "releases" / "active.sqlite", "rel-active", state, 1)
    _pointer(content, "active.sqlite", "rel-active", state)
    _request(
        incoming / "done.json",
        request_id="done",
        candidate_id="cand-done",
        release_id="rel-done",
        state_hash=state,
    )
    (audit / "done.result.json").write_text(json.dumps(result))

    with pytest.raises(RetentionError, match=message):
        main(
            [
                "--retention-days",
                "7",
                "--minimum-releases",
                "2",
                "remote",
                "--content-root",
                str(content),
                "--incoming-root",
                str(incoming),
                "--audit-root",
                str(audit),
                "--mutation-root",
                str(mutation),
            ]
        )


def test_remote_request_id_mismatch_blocks_pass(tmp_path: Path) -> None:
    content = tmp_path / "content"
    incoming = tmp_path / "incoming"
    audit = tmp_path / "audit"
    mutation = tmp_path / "mutation"
    for root in (content / "releases", incoming, audit, mutation):
        root.mkdir(parents=True)
    state = "e" * 64
    _database(content / "releases" / "active.sqlite", "rel-active", state, 1)
    _pointer(content, "active.sqlite", "rel-active", state)
    _request(
        incoming / "done.json",
        request_id="different-request",
        candidate_id="cand-done",
        release_id="rel-done",
        state_hash=state,
    )
    _remote_result(
        audit / "done.result.json",
        request_id="done",
        release_id="rel-done",
        state_hash=state,
    )

    with pytest.raises(RetentionError, match="not a proven success"):
        main(
            [
                "--retention-days",
                "7",
                "--minimum-releases",
                "2",
                "remote",
                "--content-root",
                str(content),
                "--incoming-root",
                str(incoming),
                "--audit-root",
                str(audit),
                "--mutation-root",
                str(mutation),
            ]
        )
