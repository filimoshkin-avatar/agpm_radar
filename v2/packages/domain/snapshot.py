"""Canonical immutable collected-input snapshots for the Legacy/V2 fork boundary."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from packages.storage.safe_files import (
    SafeFilesystemError,
    ensure_private_directory,
    open_directory_nofollow,
    publish_flat_directory,
    read_regular_file_at,
)

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

SNAPSHOT_FORMAT: Final = "radar-collected-input-snapshot/v1"
MANIFEST_NAME: Final = "manifest.json"
CANDIDATES_NAME: Final = "candidates.jsonl"
EVIDENCE_NAME: Final = "safe-evidence-index.json"
CHECKSUMS_NAME: Final = "checksums.sha256"
PAYLOAD_NAMES: Final = (CANDIDATES_NAME, EVIDENCE_NAME)
SNAPSHOT_NAMES: Final = frozenset({MANIFEST_NAME, CANDIDATES_NAME, EVIDENCE_NAME, CHECKSUMS_NAME})
_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{7,127}$")
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_UTC_TIMESTAMP_PATTERN: Final = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
)


class SnapshotError(RuntimeError):
    """The collected-input snapshot cannot be created or consumed safely."""


class SnapshotIntegrityError(SnapshotError):
    """Snapshot bytes, canonical form, membership or hashes do not match."""


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    """The complete identity required at both branch consumption boundaries."""

    snapshot_id: str
    manifest_sha256: str
    checksums_sha256: str
    payload_sha256: str
    item_count: int


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """One pinned regular file read from an immutable snapshot directory."""

    name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class VerifiedSnapshot:
    """A verified in-memory byte set safe to copy into one branch workspace."""

    identity: SnapshotIdentity
    collected_at: str
    files: tuple[SnapshotFile, ...]

    def file_map(self) -> dict[str, bytes]:
        """Return a fresh name-to-bytes mapping for atomic branch-copy publication."""
        return {file.name: file.content for file in self.files}


def sha256_bytes(content: bytes) -> str:
    """Return a lowercase SHA-256 digest."""
    return hashlib.sha256(content).hexdigest()


def _normalize_json(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | str):
        return unicodedata.normalize("NFC", value) if isinstance(value, str) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SnapshotError("snapshot JSON cannot contain non-finite numbers")
        return 0.0 if value == 0 else value
    if isinstance(value, list | tuple):
        return [_normalize_json(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise SnapshotError("snapshot JSON object keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise SnapshotError(f"snapshot JSON key collides after NFC normalization: {key}")
            normalized[key] = _normalize_json(raw_value)
        return normalized
    raise SnapshotError(f"unsupported snapshot JSON value: {type(value).__name__}")


def _canonical_json(value: object) -> bytes:
    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_json_line(value: object) -> bytes:
    return _canonical_json(value) + b"\n"


def canonical_json_line(value: object) -> bytes:
    """Render one NFC-normalized, key-sorted, whitespace-free UTF-8 JSON line."""
    return _canonical_json_line(value)


def _reject_constant(value: str) -> None:
    raise SnapshotIntegrityError(f"non-finite JSON constant is forbidden: {value}")


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    normalized_keys: set[str] = set()
    for key, value in pairs:
        normalized_key = unicodedata.normalize("NFC", key)
        if key in parsed or normalized_key in normalized_keys:
            raise SnapshotIntegrityError(f"duplicate/colliding JSON key: {key}")
        parsed[key] = value
        normalized_keys.add(normalized_key)
    return parsed


def _parse_json(content: bytes, label: str) -> JsonValue:
    try:
        decoded = content.decode("utf-8")
        parsed: object = json.loads(
            decoded,
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_constant,
        )
        return _normalize_json(parsed)
    except (UnicodeDecodeError, json.JSONDecodeError, SnapshotError) as error:
        if isinstance(error, SnapshotIntegrityError):
            raise
        raise SnapshotIntegrityError(f"invalid canonical JSON in {label}: {error}") from error


def _require_object(value: JsonValue, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise SnapshotIntegrityError(f"{label} must be a JSON object")
    return value


def parse_canonical_json_object(content: bytes, label: str) -> JsonObject:
    """Parse one object and reject any byte representation other than the canonical form."""
    parsed = _require_object(_parse_json(content, label), label)
    if _canonical_json_line(parsed) != content:
        raise SnapshotIntegrityError(f"{label} is not byte-canonical JSON")
    return parsed


def _validate_snapshot_id(snapshot_id: str) -> None:
    if not _ID_PATTERN.fullmatch(snapshot_id):
        raise SnapshotError(f"invalid snapshot id: {snapshot_id!r}")


def _validate_timestamp(timestamp: str) -> None:
    if not _UTC_TIMESTAMP_PATTERN.fullmatch(timestamp):
        raise SnapshotError(f"timestamp must be second-precision UTC: {timestamp!r}")
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise SnapshotError(
            f"timestamp is not a real UTC calendar instant: {timestamp!r}"
        ) from error


def aggregate_payload_sha256(payloads: Mapping[str, bytes]) -> str:
    """Hash payload names, lengths and exact bytes with unambiguous domain separation."""
    if set(payloads) != set(PAYLOAD_NAMES):
        raise SnapshotError("payload set does not match the Stage 4 snapshot contract")
    digest = hashlib.sha256(b"radar-collected-input-payload/v1\0")
    for name in sorted(payloads):
        name_bytes = name.encode("utf-8")
        content = payloads[name]
        digest.update(len(name_bytes).to_bytes(8, "big"))
        digest.update(name_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _render_checksums(files: Mapping[str, bytes]) -> bytes:
    expected_names = {MANIFEST_NAME, *PAYLOAD_NAMES}
    if set(files) != expected_names:
        raise SnapshotError("checksum inputs do not match manifest plus payload files")
    return "".join(f"{sha256_bytes(files[name])}  {name}\n" for name in sorted(files)).encode(
        "ascii"
    )


def _render_snapshot(
    snapshot_id: str,
    collected_at: str,
    candidates: Sequence[Mapping[str, object]],
    safe_evidence_index: Mapping[str, object],
) -> tuple[dict[str, bytes], SnapshotIdentity]:
    _validate_snapshot_id(snapshot_id)
    _validate_timestamp(collected_at)
    candidate_lines: list[bytes] = []
    for index, candidate in enumerate(candidates):
        normalized = _normalize_json(candidate)
        if not isinstance(normalized, dict):
            raise SnapshotError(f"candidate {index} is not an object")
        candidate_lines.append(_canonical_json_line(normalized))
    evidence = _normalize_json(safe_evidence_index)
    if not isinstance(evidence, dict):
        raise SnapshotError("safe evidence index must be an object")
    payloads = {
        CANDIDATES_NAME: b"".join(candidate_lines),
        EVIDENCE_NAME: _canonical_json_line(evidence),
    }
    payload_sha256 = aggregate_payload_sha256(payloads)
    item_count = len(candidate_lines)
    payload_records: list[dict[str, object]] = []
    for name in sorted(payloads):
        content = payloads[name]
        payload_records.append(
            {
                "bytes": len(content),
                "path": name,
                "sha256": sha256_bytes(content),
            }
        )
    manifest = {
        "collectedAt": collected_at,
        "itemCount": item_count,
        "payloadFiles": payload_records,
        "payloadSha256": payload_sha256,
        "snapshotFormat": SNAPSHOT_FORMAT,
        "snapshotId": snapshot_id,
    }
    manifest_bytes = _canonical_json_line(manifest)
    checksum_inputs = {MANIFEST_NAME: manifest_bytes, **payloads}
    checksums_bytes = _render_checksums(checksum_inputs)
    files = {**checksum_inputs, CHECKSUMS_NAME: checksums_bytes}
    identity = SnapshotIdentity(
        snapshot_id=snapshot_id,
        manifest_sha256=sha256_bytes(manifest_bytes),
        checksums_sha256=sha256_bytes(checksums_bytes),
        payload_sha256=payload_sha256,
        item_count=item_count,
    )
    return files, identity


def create_snapshot(
    store_root: Path,
    *,
    snapshot_id: str,
    collected_at: str,
    candidates: Sequence[Mapping[str, object]],
    safe_evidence_index: Mapping[str, object],
) -> VerifiedSnapshot:
    """Create one canonical snapshot atomically, then re-open and verify it from disk."""
    files, expected_identity = _render_snapshot(
        snapshot_id,
        collected_at,
        candidates,
        safe_evidence_index,
    )
    ensure_private_directory(store_root)
    try:
        snapshot_path = publish_flat_directory(store_root, snapshot_id, files)
        verified = verify_snapshot(snapshot_path)
    except SafeFilesystemError as error:
        raise SnapshotError(f"cannot publish immutable snapshot safely: {error}") from error
    if verified.identity != expected_identity:
        raise SnapshotIntegrityError("post-create snapshot identity differs from rendered bytes")
    return verified


def _manifest_payload_records(manifest: JsonObject) -> dict[str, JsonObject]:
    raw_records = manifest.get("payloadFiles")
    if not isinstance(raw_records, list) or len(raw_records) != len(PAYLOAD_NAMES):
        raise SnapshotIntegrityError("manifest payloadFiles has an invalid shape")
    records: dict[str, JsonObject] = {}
    for raw_record in raw_records:
        if not isinstance(raw_record, dict) or set(raw_record) != {"bytes", "path", "sha256"}:
            raise SnapshotIntegrityError("manifest payload record has an invalid shape")
        path = raw_record.get("path")
        if not isinstance(path, str) or path in records:
            raise SnapshotIntegrityError("manifest payload path is invalid or duplicated")
        records[path] = raw_record
    if tuple(sorted(records)) != tuple(sorted(PAYLOAD_NAMES)):
        raise SnapshotIntegrityError("manifest payload membership differs from the contract")
    return records


def _verify_candidates(content: bytes) -> int:
    if content and not content.endswith(b"\n"):
        raise SnapshotIntegrityError("candidates.jsonl has a truncated final record")
    count = 0
    for index, line in enumerate(content.splitlines()):
        if not line:
            raise SnapshotIntegrityError(f"candidates.jsonl contains blank line {index + 1}")
        parsed = _require_object(
            _parse_json(line, f"candidates.jsonl line {index + 1}"), "candidate"
        )
        if _canonical_json(parsed) != line:
            raise SnapshotIntegrityError(f"candidates.jsonl line {index + 1} is not canonical JSON")
        count += 1
    return count


def verify_snapshot(snapshot_path: Path) -> VerifiedSnapshot:
    """Verify exact bytes, canonical form and all hashes at the moment of consumption."""
    directory_descriptor: int | None = None
    try:
        directory_descriptor = open_directory_nofollow(snapshot_path)
        before = os.fstat(directory_descriptor)
        if stat.S_IMODE(before.st_mode) != 0o500:
            raise SnapshotIntegrityError("snapshot directory mode must be exactly 0500")
        initial_names = frozenset(os.listdir(directory_descriptor))
        if initial_names != SNAPSHOT_NAMES:
            raise SnapshotIntegrityError(f"snapshot membership mismatch: {sorted(initial_names)}")
        files = {
            name: read_regular_file_at(
                directory_descriptor,
                name,
                label=str(snapshot_path / name),
                expected_mode=0o400,
            )
            for name in sorted(SNAPSHOT_NAMES)
        }
        final_names = frozenset(os.listdir(directory_descriptor))
        after = os.fstat(directory_descriptor)
        directory_signature_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        directory_signature_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if initial_names != final_names or directory_signature_before != directory_signature_after:
            raise SnapshotIntegrityError("snapshot directory changed while being consumed")

        manifest_value = _parse_json(files[MANIFEST_NAME], MANIFEST_NAME)
        manifest = _require_object(manifest_value, MANIFEST_NAME)
        required_manifest_keys = {
            "collectedAt",
            "itemCount",
            "payloadFiles",
            "payloadSha256",
            "snapshotFormat",
            "snapshotId",
        }
        if set(manifest) != required_manifest_keys:
            raise SnapshotIntegrityError("manifest has missing or unknown properties")
        if _canonical_json_line(manifest) != files[MANIFEST_NAME]:
            raise SnapshotIntegrityError("manifest.json is not byte-canonical JSON")
        snapshot_id = manifest.get("snapshotId")
        collected_at = manifest.get("collectedAt")
        item_count = manifest.get("itemCount")
        payload_sha256 = manifest.get("payloadSha256")
        if not isinstance(snapshot_id, str) or not _ID_PATTERN.fullmatch(snapshot_id):
            raise SnapshotIntegrityError("manifest snapshotId is invalid")
        if snapshot_path.name != snapshot_id:
            raise SnapshotIntegrityError("snapshot directory name differs from snapshotId")
        if not isinstance(collected_at, str):
            raise SnapshotIntegrityError("manifest collectedAt is invalid")
        try:
            _validate_timestamp(collected_at)
        except SnapshotError as error:
            raise SnapshotIntegrityError("manifest collectedAt is invalid") from error
        if not isinstance(item_count, int) or isinstance(item_count, bool) or item_count < 0:
            raise SnapshotIntegrityError("manifest itemCount is invalid")
        if not isinstance(payload_sha256, str) or not _SHA256_PATTERN.fullmatch(payload_sha256):
            raise SnapshotIntegrityError("manifest payloadSha256 is invalid")
        if manifest.get("snapshotFormat") != SNAPSHOT_FORMAT:
            raise SnapshotIntegrityError("unsupported snapshot format")

        payload_records = _manifest_payload_records(manifest)
        payloads = {name: files[name] for name in PAYLOAD_NAMES}
        for name, content in payloads.items():
            record = payload_records[name]
            if record.get("bytes") != len(content) or record.get("sha256") != sha256_bytes(content):
                raise SnapshotIntegrityError(f"manifest payload descriptor mismatch: {name}")
        calculated_payload_sha256 = aggregate_payload_sha256(payloads)
        if calculated_payload_sha256 != payload_sha256:
            raise SnapshotIntegrityError("aggregate payload SHA-256 mismatch")
        candidate_count = _verify_candidates(files[CANDIDATES_NAME])
        if candidate_count != item_count:
            raise SnapshotIntegrityError("manifest itemCount differs from candidates.jsonl")
        evidence = _require_object(_parse_json(files[EVIDENCE_NAME], EVIDENCE_NAME), EVIDENCE_NAME)
        if _canonical_json_line(evidence) != files[EVIDENCE_NAME]:
            raise SnapshotIntegrityError("safe-evidence-index.json is not byte-canonical JSON")
        expected_checksums = _render_checksums({MANIFEST_NAME: files[MANIFEST_NAME], **payloads})
        if files[CHECKSUMS_NAME] != expected_checksums:
            raise SnapshotIntegrityError("checksums.sha256 is non-canonical or mismatched")

        identity = SnapshotIdentity(
            snapshot_id=snapshot_id,
            manifest_sha256=sha256_bytes(files[MANIFEST_NAME]),
            checksums_sha256=sha256_bytes(files[CHECKSUMS_NAME]),
            payload_sha256=calculated_payload_sha256,
            item_count=item_count,
        )
        return VerifiedSnapshot(
            identity=identity,
            collected_at=collected_at,
            files=tuple(SnapshotFile(name=name, content=files[name]) for name in sorted(files)),
        )
    except SnapshotError:
        raise
    except (OSError, SafeFilesystemError, ValueError, TypeError) as error:
        raise SnapshotIntegrityError(f"snapshot verification failed closed: {error}") from error
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def copy_verified_snapshot(snapshot: VerifiedSnapshot, destination_root: Path) -> Path:
    """Publish an independent immutable copy and verify that copy before returning it."""
    ensure_private_directory(destination_root)
    try:
        destination = publish_flat_directory(
            destination_root,
            snapshot.identity.snapshot_id,
            snapshot.file_map(),
        )
        copied = verify_snapshot(destination)
    except SafeFilesystemError as error:
        raise SnapshotError(f"cannot publish branch snapshot copy safely: {error}") from error
    if copied.identity != snapshot.identity or copied.files != snapshot.files:
        raise SnapshotIntegrityError("branch copy differs from its verified source byte set")
    return destination


def load_snapshot_json(path: Path) -> JsonObject:
    """Parse a canonical JSON fixture through the same normalization rules."""
    return _require_object(_parse_json(path.read_bytes(), str(path)), str(path))


__all__ = [
    "CANDIDATES_NAME",
    "CHECKSUMS_NAME",
    "EVIDENCE_NAME",
    "MANIFEST_NAME",
    "PAYLOAD_NAMES",
    "SNAPSHOT_FORMAT",
    "SNAPSHOT_NAMES",
    "JsonObject",
    "JsonValue",
    "SnapshotError",
    "SnapshotFile",
    "SnapshotIdentity",
    "SnapshotIntegrityError",
    "VerifiedSnapshot",
    "aggregate_payload_sha256",
    "canonical_json_line",
    "copy_verified_snapshot",
    "create_snapshot",
    "load_snapshot_json",
    "parse_canonical_json_object",
    "sha256_bytes",
    "verify_snapshot",
]
