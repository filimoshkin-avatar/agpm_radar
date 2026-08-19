"""Host-local publisher audit journal regressions."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from packages.publisher.audit_journal import (
    AuditEvent,
    AuditJournalError,
    append_event,
    read_and_verify,
)


def _event(identifier: str) -> AuditEvent:
    return AuditEvent(
        event_id=identifier,
        occurred_at="2026-01-01T00:00:00Z",
        actor_id="synthetic-actor",
        action="bootstrap",
        release_id="synthetic-release",
        candidate_id="synthetic-candidate",
        before_state_hash="0" * 64,
        after_state_hash="1" * 64,
        result="success",
    )


def test_journal_is_external_append_only_and_hash_chained(tmp_path: Path) -> None:
    journal = tmp_path / "publisher-audit.jsonl"
    first_hash = append_event(journal, _event("one"))
    second_hash = append_event(journal, _event("two"))
    records = read_and_verify(journal)
    assert records[0]["record_hash"] == first_hash
    assert records[1]["previous_hash"] == first_hash
    assert records[1]["record_hash"] == second_hash
    assert journal.stat().st_mode & 0o777 == 0o600


def test_journal_rejects_duplicate_and_tampering(tmp_path: Path) -> None:
    journal = tmp_path / "publisher-audit.jsonl"
    append_event(journal, _event("one"))
    with pytest.raises(AuditJournalError, match="duplicate"):
        append_event(journal, _event("one"))
    record = json.loads(journal.read_text())
    record["result"] = "tampered"
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(AuditJournalError, match="hash mismatch"):
        read_and_verify(journal)


def test_journal_serializes_concurrent_appends(tmp_path: Path) -> None:
    journal = tmp_path / "publisher-audit.jsonl"
    event_count = 32
    barrier = threading.Barrier(event_count)

    def append_concurrently(index: int) -> str:
        barrier.wait()
        return append_event(journal, _event(f"event-{index}"))

    with ThreadPoolExecutor(max_workers=event_count) as executor:
        hashes = tuple(executor.map(append_concurrently, range(event_count)))

    records = read_and_verify(journal)
    assert len(records) == event_count
    assert len(set(hashes)) == event_count
    assert [record["sequence"] for record in records] == list(range(event_count))


def test_journal_rejects_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "unrelated.txt"
    target.write_text("must remain unchanged\n", encoding="utf-8")
    journal = tmp_path / "publisher-audit.jsonl"
    journal.symlink_to(target)

    with pytest.raises(AuditJournalError, match="safely open"):
        append_event(journal, _event("one"))

    assert target.read_text(encoding="utf-8") == "must remain unchanged\n"
