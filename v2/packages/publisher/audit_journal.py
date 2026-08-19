"""External append-only publisher audit journal (never replicated into SQLite)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

GENESIS_HASH: Final = "0" * 64


class AuditJournalError(RuntimeError):
    """The external journal is malformed, forked or cannot be appended safely."""


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Explicit audit data supplied by deterministic publisher orchestration."""

    event_id: str
    occurred_at: str
    actor_id: str
    action: str
    release_id: str
    candidate_id: str
    before_state_hash: str
    after_state_hash: str
    result: str
    reason: str | None = None


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _record_hash(record_without_hash: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(record_without_hash)).hexdigest()


def _verify_bytes(raw: bytes) -> tuple[dict[str, object], ...]:
    """Parse one locked journal snapshot and verify its complete hash chain."""
    if raw and not raw.endswith(b"\n"):
        raise AuditJournalError("audit journal has a truncated final record")
    records: list[dict[str, object]] = []
    previous = GENESIS_HASH
    for expected_sequence, line in enumerate(raw.splitlines()):
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise AuditJournalError("audit journal record is not an object")
        record = cast(dict[str, object], parsed)
        claimed_hash = record.pop("record_hash", None)
        if record.get("sequence") != expected_sequence:
            raise AuditJournalError("audit journal sequence is not contiguous")
        if record.get("previous_hash") != previous:
            raise AuditJournalError("audit journal hash chain is broken")
        calculated = _record_hash(record)
        if claimed_hash != calculated:
            raise AuditJournalError("audit journal record hash mismatch")
        record["record_hash"] = calculated
        records.append(record)
        previous = calculated
    return tuple(records)


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_descriptor(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise AuditJournalError("publisher audit journal is not a regular file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AuditJournalError("publisher audit journal permissions are broader than 0600")


def _open_flags() -> int:
    return os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def read_and_verify(path: Path) -> tuple[dict[str, object], ...]:
    """Read one shared-locked journal snapshot and reject tampering or unsafe paths."""
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise AuditJournalError(f"cannot safely open publisher audit journal: {error}") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        _validate_descriptor(descriptor)
        return _verify_bytes(_read_descriptor(descriptor))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def append_event(path: Path, event: AuditEvent) -> str:
    """Serialize one durable append under an interprocess exclusive file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        descriptor = os.open(path, _open_flags() | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(path, _open_flags())
        except OSError as error:
            raise AuditJournalError(
                f"cannot safely open publisher audit journal: {error}"
            ) from error
    except OSError as error:
        raise AuditJournalError(f"cannot create publisher audit journal: {error}") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _validate_descriptor(descriptor)
        records = _verify_bytes(_read_descriptor(descriptor))
        if any(record.get("event_id") == event.event_id for record in records):
            raise AuditJournalError(f"duplicate audit event_id: {event.event_id}")
        previous = str(records[-1]["record_hash"]) if records else GENESIS_HASH
        record: dict[str, object] = {
            "action": event.action,
            "actor_id": event.actor_id,
            "after_state_hash": event.after_state_hash,
            "before_state_hash": event.before_state_hash,
            "candidate_id": event.candidate_id,
            "event_id": event.event_id,
            "occurred_at": event.occurred_at,
            "previous_hash": previous,
            "reason": event.reason,
            "release_id": event.release_id,
            "result": event.result,
            "sequence": len(records),
        }
        record_hash = _record_hash(record)
        record["record_hash"] = record_hash
        payload = _canonical_bytes(record) + b"\n"
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise AuditJournalError("short append to publisher audit journal")
            remaining = remaining[written:]
        os.fsync(descriptor)
        verified = _verify_bytes(_read_descriptor(descriptor))
        if not verified or verified[-1].get("record_hash") != record_hash:
            raise AuditJournalError("publisher audit journal post-append verification failed")
        if created:
            directory = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return record_hash
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


__all__ = [
    "GENESIS_HASH",
    "AuditEvent",
    "AuditJournalError",
    "append_event",
    "read_and_verify",
]
