"""Strict Radar V2 full-seed and row-level delta engine for Stage 7."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast

from packages.domain.snapshot import JsonObject, JsonValue, canonical_json_line
from packages.storage.hashing import DatabaseDigest, database_digest
from packages.storage.migrations import configure_staging_connection
from packages.storage.replication_mutations import (
    TABLE_SPECS,
    MutationValidationError,
    TableMutationSpec,
    row_after_sha256,
    validate_replication_key,
    validate_replication_row,
)
from packages.storage.safe_files import (
    SafeFilesystemError,
    atomic_write_new,
    ensure_private_directory,
    open_regular_file_nofollow,
    relative_parts,
)
from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE, assert_sqlite_runtime

DELTA_CONTRACT_VERSION: Final = "1.0.0"
FULL_SEED_FORMAT: Final = "radar-v2-full-seed/v1"
TABLE_CONTRACT_VERSION: Final = "1.0.0"
CONTRACT_TABLE_ORDER: Final = (
    "schema_migrations",
    "application_compatibility",
    "content_releases",
    "source_snapshots",
    "sources",
    "materials",
    "material_sources",
    "material_evidence",
    "editorial_queue",
    "issues",
    "legacy_issue_provenance",
    "legacy_publication_evidence",
    "issue_materials",
    "issue_analysis",
    "material_analysis",
    "llm_attempts",
    "source_rules",
    "material_quality",
    "rubrics",
    "material_rubrics",
    "daily_stats",
    "gazettes",
    "gazette_assets",
)
_UPSERT_ORDER: Final = (
    "source_snapshots",
    "sources",
    "materials",
    "material_sources",
    "material_evidence",
    "editorial_queue",
    "issues",
    "rubrics",
    "issue_materials",
    "issue_analysis",
    "material_analysis",
    "llm_attempts",
    "source_rules",
    "material_quality",
    "material_rubrics",
    "daily_stats",
    "gazettes",
    "gazette_assets",
    "content_releases",
)
_DELETE_ORDER: Final = tuple(reversed(_UPSERT_ORDER))
_ID: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{7,127}$")
_SHA256: Final = re.compile(r"^[a-f0-9]{64}$")
_TIMESTAMP: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_OPERATIONS: Final = frozenset({"daily", "correction", "gazette"})
type _FileSignature = tuple[int, int, int, int, int, int, int]


class DeltaValidationError(ValueError):
    """A full seed or delta is malformed, incomplete or outside contract v1."""


class DeltaConflictError(RuntimeError):
    """The supplied base database does not satisfy a delta fence/precondition."""


class DeltaApplyError(RuntimeError):
    """A delta could not be applied and verified on a create-only staging copy."""


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    """Current immutable content release fence from one database."""

    release_id: str
    sequence: int
    state_hash: str


@dataclass(frozen=True, slots=True)
class FullSeedReport:
    """Evidence for one exported or imported full database seed."""

    database_path: Path
    manifest: JsonObject
    file_sha256: str
    file_bytes: int
    digest: DatabaseDigest


@dataclass(frozen=True, slots=True)
class ReleaseDatabaseReport:
    """Read-only identity and digest evidence for one release database."""

    database_path: Path
    release: ReleaseIdentity
    digest: DatabaseDigest
    file_sha256: str
    file_bytes: int


@dataclass(frozen=True, slots=True)
class DeltaApplyReport:
    """Complete evidence from one transactional staging apply."""

    staging_path: Path
    applied_operations: int
    idempotent_operations: int
    already_applied: bool
    release_id: str
    sequence: int
    state_hash: str
    table_hashes: dict[str, str]
    table_counts: dict[str, int]
    file_sha256: str


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DeltaValidationError(f"{label} must be an object with string keys")
    return cast(dict[str, object], value)


def _identifier(value: object, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise DeltaValidationError(f"{label} is not a contract identifier")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DeltaValidationError(f"{label} must be lowercase SHA-256")
    return value


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise DeltaValidationError(f"{label} must be second-precision UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise DeltaValidationError(f"{label} is not a real UTC timestamp") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise DeltaValidationError(f"{label} is not canonical UTC")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DeltaValidationError(f"{label} must be a non-negative integer")
    return value


def _file_signature(metadata: os.stat_result) -> _FileSignature:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _descriptor_signature(descriptor: int, path: Path) -> _FileSignature:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_nlink != 1
    ):
        raise DeltaValidationError(f"database is not a private single-link file: {path}")
    return _file_signature(metadata)


def _assert_descriptor_unchanged(
    descriptor: int,
    expected: _FileSignature,
    path: Path,
) -> None:
    if _descriptor_signature(descriptor, path) != expected:
        raise DeltaValidationError(f"database changed or widened during consumption: {path}")


def _read_descriptor_bytes(
    descriptor: int,
    path: Path,
    *,
    expected: _FileSignature,
) -> bytes:
    _assert_descriptor_unchanged(descriptor, expected, path)
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    _assert_descriptor_unchanged(descriptor, expected, path)
    return b"".join(chunks)


def _read_pinned_bytes(path: Path) -> bytes:
    descriptor = open_regular_file_nofollow(path)
    try:
        signature = _descriptor_signature(descriptor, path)
        return _read_descriptor_bytes(descriptor, path, expected=signature)
    finally:
        os.close(descriptor)


def _connect_read_only(path: Path) -> tuple[int, sqlite3.Connection, _FileSignature]:
    descriptor = open_regular_file_nofollow(path)
    signature = _descriptor_signature(descriptor, path)
    uri = f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except BaseException:
        os.close(descriptor)
        raise
    connection.execute("PRAGMA query_only = ON")
    return descriptor, connection, signature


def _verify_read_only(connection: sqlite3.Connection) -> DatabaseDigest:
    assert_sqlite_runtime()
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != REQUIRED_SQLITE_PROFILE.application_id:
        raise DeltaValidationError(f"unexpected application_id: {application_id}")
    if user_version != REQUIRED_SQLITE_PROFILE.user_version:
        raise DeltaValidationError(f"unexpected user_version: {user_version}")
    if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
        raise DeltaValidationError("SQLite integrity_check failed")
    violations = tuple(connection.execute("PRAGMA foreign_key_check"))
    if violations:
        raise DeltaValidationError(f"SQLite foreign keys failed: {violations!r}")
    source_count = int(
        connection.execute("SELECT COUNT(*) FROM pub_search_documents_v1").fetchone()[0]
    )
    fts_count = int(
        connection.execute("SELECT COUNT(*) FROM published_materials_fts").fetchone()[0]
    )
    if source_count != fts_count:
        raise DeltaValidationError(
            f"FTS projection count mismatch: source={source_count}, fts={fts_count}"
        )
    return database_digest(connection)


def _latest_release(connection: sqlite3.Connection) -> ReleaseIdentity:
    rows = tuple(
        connection.execute(
            "SELECT release_id, sequence, after_state_hash FROM content_releases "
            "ORDER BY sequence DESC LIMIT 2"
        )
    )
    if not rows:
        raise DeltaValidationError("database has no content release marker")
    if len(rows) > 1 and int(rows[0][1]) == int(rows[1][1]):
        raise DeltaValidationError("content release sequence is ambiguous")
    return ReleaseIdentity(str(rows[0][0]), int(rows[0][1]), str(rows[0][2]))


def inspect_release_database(path: Path) -> ReleaseDatabaseReport:
    """Read-only inspect one sealed release database and return complete digest evidence."""
    descriptor, connection, signature = _connect_read_only(path)
    try:
        content = _read_descriptor_bytes(descriptor, path, expected=signature)
        digest = _verify_read_only(connection)
        release = _latest_release(connection)
        if release.state_hash != digest.state_hash:
            raise DeltaValidationError("release marker state hash differs from database")
        _assert_descriptor_unchanged(descriptor, signature, path)
    finally:
        connection.close()
        os.close(descriptor)
    return ReleaseDatabaseReport(
        database_path=path.absolute(),
        release=release,
        digest=digest,
        file_sha256=hashlib.sha256(content).hexdigest(),
        file_bytes=len(content),
    )


def _table_rows(
    connection: sqlite3.Connection,
    table: str,
    spec: TableMutationSpec,
) -> dict[tuple[object, ...], dict[str, object]]:
    columns = ", ".join(f'"{column}"' for column in spec.columns)
    order = ", ".join(f'"{column}"' for column in spec.primary_key)
    query = f'SELECT {columns} FROM "{table}" ORDER BY {order}'
    result: dict[tuple[object, ...], dict[str, object]] = {}
    for raw in connection.execute(query):
        row = dict(zip(spec.columns, raw, strict=True))
        key = tuple(row[column] for column in spec.primary_key)
        result[key] = row
    return result


def _key_object(spec: TableMutationSpec, identity: tuple[object, ...]) -> JsonObject:
    return cast(JsonObject, dict(zip(spec.primary_key, identity, strict=True)))


def _expected_tables(before: DatabaseDigest, after: DatabaseDigest) -> list[JsonValue]:
    return [
        {
            "afterLogicalSha256": after.table_hashes[table],
            "afterRowCount": after.table_counts[table],
            "beforeLogicalSha256": before.table_hashes[table],
            "beforeRowCount": before.table_counts[table],
            "table": table,
        }
        for table in CONTRACT_TABLE_ORDER
    ]


def _safe_asset(value: object, label: str) -> JsonObject:
    asset = _object(value, label)
    if set(asset) != {"bytes", "mediaType", "relativePath", "sha256"}:
        raise DeltaValidationError(f"{label} has unknown or missing fields")
    relative = asset["relativePath"]
    if not isinstance(relative, str):
        raise DeltaValidationError(f"{label}.relativePath must be text")
    try:
        relative_parts(relative)
    except SafeFilesystemError as error:
        raise DeltaValidationError(f"unsafe asset path: {relative!r}") from error
    _sha256(asset["sha256"], f"{label}.sha256")
    _nonnegative_integer(asset["bytes"], f"{label}.bytes")
    media = asset["mediaType"]
    if not isinstance(media, str) or not 1 <= len(media) <= 200 or "/" not in media:
        raise DeltaValidationError(f"{label}.mediaType is invalid")
    return cast(JsonObject, asset)


def _operation_identity(
    operation: Mapping[str, object],
) -> tuple[str, tuple[tuple[str, object], ...]]:
    table = cast(str, operation["table"])
    key = _object(operation["key"], "delta operation key")
    spec = TABLE_SPECS[table]
    return table, tuple((column, key[column]) for column in spec.primary_key)


def validate_delta(document: Mapping[str, object]) -> JsonObject:
    """Validate the frozen delta-v1 shape and all typed row operations."""
    root = _object(document, "delta")
    required = {
        "afterStateHash",
        "applicationReleaseId",
        "assets",
        "baseReleaseId",
        "baseSequence",
        "beforeStateHash",
        "candidateId",
        "contractVersion",
        "createdAt",
        "expectedTables",
        "operation",
        "operations",
        "releaseId",
        "schemaVersionAfter",
        "schemaVersionBefore",
        "tableContractVersion",
        "targetSequence",
    }
    if set(root) != required:
        raise DeltaValidationError("delta root has unknown or missing fields")
    if root["contractVersion"] != DELTA_CONTRACT_VERSION:
        raise DeltaValidationError("unsupported delta contractVersion")
    release_id = cast(str, _identifier(root["releaseId"], "delta releaseId"))
    candidate_id = cast(str, _identifier(root["candidateId"], "delta candidateId"))
    application_release_id = cast(
        str, _identifier(root["applicationReleaseId"], "delta applicationReleaseId")
    )
    base_release_id = _identifier(root["baseReleaseId"], "delta baseReleaseId", nullable=True)
    operation_name = root["operation"]
    if operation_name not in _OPERATIONS:
        raise DeltaValidationError("delta operation is invalid")
    base_sequence = _nonnegative_integer(root["baseSequence"], "delta baseSequence")
    target_sequence = _nonnegative_integer(root["targetSequence"], "delta targetSequence")
    if target_sequence != base_sequence + 1:
        raise DeltaValidationError("delta targetSequence must equal baseSequence + 1")
    schema_before = _nonnegative_integer(root["schemaVersionBefore"], "delta schemaVersionBefore")
    schema_after = _nonnegative_integer(root["schemaVersionAfter"], "delta schemaVersionAfter")
    if (
        schema_before != REQUIRED_SQLITE_PROFILE.user_version
        or schema_after != schema_before
        or root["tableContractVersion"] != TABLE_CONTRACT_VERSION
    ):
        raise DeltaValidationError("content delta cannot change schema/table contract")
    before_state = _sha256(root["beforeStateHash"], "delta beforeStateHash")
    after_state = _sha256(root["afterStateHash"], "delta afterStateHash")
    created_at = _timestamp(root["createdAt"], "delta createdAt")
    if base_sequence > 0 and base_release_id is None:
        raise DeltaValidationError("nonzero base sequence requires baseReleaseId")

    expected = root["expectedTables"]
    if not isinstance(expected, list) or len(expected) != len(CONTRACT_TABLE_ORDER):
        raise DeltaValidationError("delta expectedTables must cover all replicated tables")
    seen_tables: list[str] = []
    for index, raw in enumerate(expected):
        item = _object(raw, f"expectedTables[{index}]")
        if set(item) != {
            "afterLogicalSha256",
            "afterRowCount",
            "beforeLogicalSha256",
            "beforeRowCount",
            "table",
        }:
            raise DeltaValidationError("expectedTables entry shape differs")
        table = item["table"]
        if not isinstance(table, str):
            raise DeltaValidationError("expectedTables table must be text")
        seen_tables.append(table)
        _nonnegative_integer(item["beforeRowCount"], f"expectedTables[{index}].beforeRowCount")
        _nonnegative_integer(item["afterRowCount"], f"expectedTables[{index}].afterRowCount")
        _sha256(item["beforeLogicalSha256"], f"expectedTables[{index}].beforeLogicalSha256")
        _sha256(item["afterLogicalSha256"], f"expectedTables[{index}].afterLogicalSha256")
    if tuple(seen_tables) != CONTRACT_TABLE_ORDER:
        raise DeltaValidationError("expectedTables order/coverage differs from contract")

    assets = root["assets"]
    if not isinstance(assets, list) or len(assets) > 1_000:
        raise DeltaValidationError("delta assets must be an array of at most 1000 items")
    asset_paths: set[str] = set()
    for index, raw in enumerate(assets):
        asset = _safe_asset(raw, f"assets[{index}]")
        path = cast(str, asset["relativePath"])
        if path in asset_paths:
            raise DeltaValidationError(f"duplicate delta asset path: {path}")
        asset_paths.add(path)

    operations = root["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= 10_000:
        raise DeltaValidationError("delta operations must be a bounded non-empty array")
    identities: set[tuple[str, tuple[tuple[str, object], ...]]] = set()
    release_markers = 0
    for index, raw in enumerate(operations, start=1):
        item = _object(raw, f"operations[{index - 1}]")
        if item.get("sequence") != index:
            raise DeltaValidationError("delta operation sequences must be contiguous")
        table = item.get("table")
        action = item.get("action")
        if not isinstance(table, str) or table not in TABLE_SPECS:
            raise DeltaValidationError("delta operation targets an unknown/non-content table")
        spec = TABLE_SPECS[table]
        if not isinstance(action, str) or action not in spec.actions:
            raise DeltaValidationError(f"delta action is not allowed for {table}")
        required_fields = {
            "action",
            "expectedBefore",
            "key",
            "rowAfterHash",
            "sequence",
            "table",
        }
        if action != "delete":
            required_fields.add("values")
        if set(item) != required_fields:
            raise DeltaValidationError("delta operation has unknown or missing fields")
        try:
            key = validate_replication_key(table, _object(item["key"], "delta operation key"))
        except MutationValidationError as exc:
            raise DeltaValidationError(str(exc)) from exc
        identity = _operation_identity(item)
        if identity in identities:
            raise DeltaValidationError(f"delta mutates one row more than once: {table}")
        identities.add(identity)
        expected_before = item["expectedBefore"]
        if expected_before != "absent":
            _sha256(expected_before, "delta expectedBefore")
        if action == "insert" and expected_before != "absent":
            raise DeltaValidationError("delta insert requires absent precondition")
        if action == "delete":
            if item["rowAfterHash"] is not None:
                raise DeltaValidationError("delta tombstone rowAfterHash must be null")
        else:
            try:
                values = validate_replication_row(
                    table,
                    key,
                    _object(item["values"], "delta operation values"),
                )
            except MutationValidationError as exc:
                raise DeltaValidationError(str(exc)) from exc
            if item["rowAfterHash"] != row_after_sha256(values):
                raise DeltaValidationError(f"delta rowAfterHash differs for {table}")
        if table == "content_releases":
            release_markers += 1
            if action != "insert" or index != len(operations):
                raise DeltaValidationError("content release marker must be the final insert")
            content_values = _object(item["values"], "content release values")
            if (
                content_values.get("release_id") != release_id
                or content_values.get("candidate_id") != candidate_id
                or content_values.get("operation") != operation_name
                or content_values.get("base_release_id") != base_release_id
                or content_values.get("sequence") != target_sequence
                or content_values.get("before_state_hash") != before_state
                or content_values.get("after_state_hash") != after_state
                or content_values.get("created_at") != created_at
            ):
                raise DeltaValidationError("content release marker differs from delta envelope")
    if release_markers != 1:
        raise DeltaValidationError("delta requires exactly one final content release marker")
    if not application_release_id:
        raise DeltaValidationError("delta application release is empty")
    return cast(JsonObject, root)


def build_delta(
    base_path: Path,
    target_path: Path,
    *,
    release_id: str,
    candidate_id: str,
    operation: str,
    application_release_id: str,
    created_at: str,
    assets: Sequence[Mapping[str, object]] = (),
) -> JsonObject:
    """Derive one complete ordered delta from two verified release databases."""
    base_descriptor, base, base_signature = _connect_read_only(base_path)
    target_descriptor, target, target_signature = _connect_read_only(target_path)
    try:
        before = _verify_read_only(base)
        after = _verify_read_only(target)
        base_release = _latest_release(base)
        target_release = _latest_release(target)
        if base_release.state_hash != before.state_hash:
            raise DeltaValidationError("base release marker state hash differs from database")
        if target_release.state_hash != after.state_hash:
            raise DeltaValidationError("target release marker state hash differs from database")
        if target_release.release_id != release_id:
            raise DeltaValidationError("target database releaseId differs from request")
        if target_release.sequence != base_release.sequence + 1:
            raise DeltaValidationError("target release sequence is missing or out of order")

        changes: dict[
            str,
            tuple[
                dict[tuple[object, ...], dict[str, object]],
                dict[tuple[object, ...], dict[str, object]],
            ],
        ] = {}
        for table in CONTRACT_TABLE_ORDER:
            spec = TABLE_SPECS.get(table)
            if spec is None:
                if before.table_hashes[table] != after.table_hashes[table]:
                    raise DeltaValidationError(
                        f"content delta changed application-owned table: {table}"
                    )
                continue
            changes[table] = (_table_rows(base, table, spec), _table_rows(target, table, spec))

        operations: list[JsonValue] = []
        for table in _DELETE_ORDER:
            if table not in changes:
                continue
            before_rows, after_rows = changes[table]
            spec = TABLE_SPECS[table]
            for identity in sorted(set(before_rows) - set(after_rows), key=repr):
                if "delete" not in spec.actions:
                    raise DeltaValidationError(f"delta cannot delete immutable row from {table}")
                row = before_rows[identity]
                operations.append(
                    {
                        "action": "delete",
                        "expectedBefore": row_after_sha256(row),
                        "key": _key_object(spec, identity),
                        "rowAfterHash": None,
                        "sequence": len(operations) + 1,
                        "table": table,
                    }
                )
        for table in _UPSERT_ORDER:
            if table not in changes:
                continue
            before_rows, after_rows = changes[table]
            spec = TABLE_SPECS[table]
            for identity in sorted(after_rows, key=repr):
                before_row = before_rows.get(identity)
                after_row = after_rows[identity]
                if before_row == after_row:
                    continue
                if before_row is None:
                    action = "insert" if "insert" in spec.actions else "upsert"
                    expected_before = "absent"
                else:
                    if "upsert" not in spec.actions:
                        raise DeltaValidationError(f"delta cannot rewrite immutable row in {table}")
                    action = "upsert"
                    expected_before = row_after_sha256(before_row)
                key = _key_object(spec, identity)
                values = validate_replication_row(table, key, after_row)
                operations.append(
                    {
                        "action": action,
                        "expectedBefore": expected_before,
                        "key": key,
                        "rowAfterHash": row_after_sha256(values),
                        "sequence": len(operations) + 1,
                        "table": table,
                        "values": values,
                    }
                )

        raw: dict[str, object] = {
            "afterStateHash": after.state_hash,
            "applicationReleaseId": application_release_id,
            "assets": [dict(asset) for asset in assets],
            "baseReleaseId": base_release.release_id,
            "baseSequence": base_release.sequence,
            "beforeStateHash": before.state_hash,
            "candidateId": candidate_id,
            "contractVersion": DELTA_CONTRACT_VERSION,
            "createdAt": created_at,
            "expectedTables": _expected_tables(before, after),
            "operation": operation,
            "operations": operations,
            "releaseId": release_id,
            "schemaVersionAfter": REQUIRED_SQLITE_PROFILE.user_version,
            "schemaVersionBefore": REQUIRED_SQLITE_PROFILE.user_version,
            "tableContractVersion": TABLE_CONTRACT_VERSION,
            "targetSequence": target_release.sequence,
        }
        result = validate_delta(raw)
        _assert_descriptor_unchanged(base_descriptor, base_signature, base_path)
        _assert_descriptor_unchanged(target_descriptor, target_signature, target_path)
        return result
    except MutationValidationError as error:
        raise DeltaValidationError(str(error)) from error
    finally:
        base.close()
        target.close()
        os.close(base_descriptor)
        os.close(target_descriptor)


def finalize_release_database(
    content_staging_path: Path,
    release_path: Path,
    *,
    release_id: str,
    candidate_id: str,
    operation: str,
    created_at: str,
    activated_at: str,
    expected_base_release_id: str,
    expected_base_sequence: int,
    expected_before_state_hash: str,
) -> ReleaseIdentity:
    """Add the publisher-owned release marker to a new verified source staging copy."""
    release_id = cast(str, _identifier(release_id, "releaseId"))
    candidate_id = cast(str, _identifier(candidate_id, "candidateId"))
    expected_base_release_id = cast(
        str, _identifier(expected_base_release_id, "expected base releaseId")
    )
    if operation not in _OPERATIONS:
        raise DeltaValidationError("release operation is invalid")
    _timestamp(created_at, "release createdAt")
    _timestamp(activated_at, "release activatedAt")
    _nonnegative_integer(expected_base_sequence, "expected base sequence")
    _sha256(expected_before_state_hash, "expected before state hash")

    descriptor, source, signature = _connect_read_only(content_staging_path)
    try:
        content = _read_descriptor_bytes(
            descriptor,
            content_staging_path,
            expected=signature,
        )
        content_digest = _verify_read_only(source)
        base = _latest_release(source)
        if (
            base.release_id != expected_base_release_id
            or base.sequence != expected_base_sequence
            or base.state_hash != expected_before_state_hash
        ):
            raise DeltaConflictError("content staging base marker differs from expected release")
        _assert_descriptor_unchanged(descriptor, signature, content_staging_path)
    finally:
        source.close()
        os.close(descriptor)

    ensure_private_directory(release_path.parent)
    atomic_write_new(release_path, content, mode=0o600)
    values: dict[str, object] = {
        "activated_at": activated_at,
        "after_state_hash": content_digest.state_hash,
        "base_release_id": expected_base_release_id,
        "before_state_hash": expected_before_state_hash,
        "candidate_id": candidate_id,
        "created_at": created_at,
        "operation": operation,
        "release_id": release_id,
        "schema_version": REQUIRED_SQLITE_PROFILE.user_version,
        "sequence": expected_base_sequence + 1,
    }
    key = {"release_id": release_id}
    validate_replication_row("content_releases", key, values)
    try:
        with sqlite3.connect(release_path) as connection:
            configure_staging_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO content_releases(
                  release_id, sequence, base_release_id, candidate_id, operation,
                  schema_version, before_state_hash, after_state_hash, created_at, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(values[column] for column in TABLE_SPECS["content_releases"].columns),
            )
            connection.commit()
            from packages.storage.hashing import verify_database

            verify_database(connection)
            connection.commit()
            final_digest = database_digest(connection)
            if final_digest.state_hash != content_digest.state_hash:
                raise DeltaApplyError("release marker changed the logical domain state")
            release = _latest_release(connection)
            if (
                release.release_id != release_id
                or release.sequence != expected_base_sequence + 1
                or release.state_hash != content_digest.state_hash
            ):
                raise DeltaApplyError("finalized release marker differs")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.commit()
    except sqlite3.Error as error:
        raise DeltaApplyError(f"cannot finalize release database: {error}") from error
    return release


def _read_current_row(
    connection: sqlite3.Connection,
    table: str,
    spec: TableMutationSpec,
    key: Mapping[str, object],
) -> dict[str, object] | None:
    where = " AND ".join(f'"{column}" = ?' for column in spec.primary_key)
    columns = ", ".join(f'"{column}"' for column in spec.columns)
    query = f'SELECT {columns} FROM "{table}" WHERE {where}'
    row = connection.execute(query, tuple(key[column] for column in spec.primary_key)).fetchone()
    return None if row is None else dict(zip(spec.columns, row, strict=True))


def _apply_operation(connection: sqlite3.Connection, operation: Mapping[str, object]) -> bool:
    table = cast(str, operation["table"])
    action = cast(str, operation["action"])
    spec = TABLE_SPECS[table]
    key = _object(operation["key"], "delta key")
    current = _read_current_row(connection, table, spec, key)
    if action == "delete" and current is None:
        return False
    if action != "delete":
        values = _object(operation["values"], "delta values")
        if current is not None and row_after_sha256(current) == operation["rowAfterHash"]:
            return False
    expected = operation["expectedBefore"]
    if expected == "absent":
        if current is not None:
            raise DeltaConflictError(f"expected absent row in {table}")
    elif current is None or row_after_sha256(current) != expected:
        raise DeltaConflictError(f"expected row hash differs in {table}")

    where = " AND ".join(f'"{column}" = ?' for column in spec.primary_key)
    if action == "delete":
        connection.execute(
            f'DELETE FROM "{table}" WHERE {where}',
            tuple(key[column] for column in spec.primary_key),
        )
        return True
    values = _object(operation["values"], "delta values")
    columns = ", ".join(f'"{column}"' for column in spec.columns)
    placeholders = ", ".join("?" for _column in spec.columns)
    if action == "insert":
        query = f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})'
    else:
        updates = ", ".join(
            f'"{column}" = excluded."{column}"'
            for column in spec.columns
            if column not in spec.primary_key
        )
        keys = ", ".join(f'"{column}"' for column in spec.primary_key)
        query = (
            f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders}) '
            f"ON CONFLICT ({keys}) DO UPDATE SET {updates}"
        )
    connection.execute(query, tuple(values[column] for column in spec.columns))
    return True


def _verify_expected_digest(
    digest: DatabaseDigest,
    expected: Sequence[Mapping[str, object]],
    *,
    side: str,
) -> None:
    for item in expected:
        table = cast(str, item["table"])
        count = cast(int, item[f"{side}RowCount"])
        table_digest = cast(str, item[f"{side}LogicalSha256"])
        if digest.table_counts[table] != count or digest.table_hashes[table] != table_digest:
            raise DeltaConflictError(f"{side} table expectation differs for {table}")


def apply_delta_to_staging(
    base_path: Path,
    staging_path: Path,
    document: Mapping[str, object],
) -> DeltaApplyReport:
    """Apply one delta transactionally to a new private staging-copy inode."""
    delta = validate_delta(document)
    ensure_private_directory(staging_path.parent)
    descriptor, source, signature = _connect_read_only(base_path)
    already_applied = False
    try:
        source_bytes = _read_descriptor_bytes(descriptor, base_path, expected=signature)
        source_digest = _verify_read_only(source)
        current_release = _latest_release(source)
        expected_tables = cast(list[dict[str, object]], delta["expectedTables"])
        if (
            current_release.release_id == delta["releaseId"]
            and current_release.sequence == delta["targetSequence"]
            and source_digest.state_hash == delta["afterStateHash"]
        ):
            _verify_expected_digest(source_digest, expected_tables, side="after")
            already_applied = True
        else:
            if (
                current_release.release_id != delta["baseReleaseId"]
                or current_release.sequence != delta["baseSequence"]
                or source_digest.state_hash != delta["beforeStateHash"]
            ):
                raise DeltaConflictError("delta base release/sequence/state fence differs")
            _verify_expected_digest(source_digest, expected_tables, side="before")
        _assert_descriptor_unchanged(descriptor, signature, base_path)
    finally:
        source.close()
        os.close(descriptor)

    atomic_write_new(staging_path, source_bytes, mode=0o600)
    applied = 0
    skipped = 0
    try:
        with sqlite3.connect(staging_path) as staging:
            configure_staging_connection(staging)
            if not already_applied:
                staging.execute("BEGIN IMMEDIATE")
                try:
                    for raw in cast(list[dict[str, object]], delta["operations"]):
                        if _apply_operation(staging, raw):
                            applied += 1
                        else:
                            skipped += 1
                    violations = tuple(staging.execute("PRAGMA foreign_key_check"))
                    if violations:
                        raise DeltaApplyError(f"foreign-key check failed: {violations!r}")
                    staging.commit()
                except BaseException:
                    staging.rollback()
                    raise
            else:
                skipped = len(cast(list[object], delta["operations"]))
            from packages.storage.hashing import verify_database

            verify_database(staging)
            staging.commit()
            final_digest = database_digest(staging)
            _verify_expected_digest(final_digest, expected_tables, side="after")
            if final_digest.state_hash != delta["afterStateHash"]:
                raise DeltaApplyError("staging logical state differs from delta target")
            final_release = _latest_release(staging)
            if (
                final_release.release_id != delta["releaseId"]
                or final_release.sequence != delta["targetSequence"]
                or final_release.state_hash != delta["afterStateHash"]
            ):
                raise DeltaApplyError("staging release marker differs from delta target")
            staging.execute("PRAGMA journal_mode = DELETE")
            staging.commit()
        for suffix in ("-journal", "-shm", "-wal"):
            if Path(f"{staging_path}{suffix}").exists():
                raise DeltaApplyError(f"staging database left a sidecar: {suffix}")
        metadata = staging_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise DeltaApplyError("staging database is not a private single-link file")
        sealed = inspect_release_database(staging_path)
        if (
            sealed.release.release_id != delta["releaseId"]
            or sealed.release.sequence != delta["targetSequence"]
            or sealed.digest.state_hash != delta["afterStateHash"]
        ):
            raise DeltaApplyError("sealed staging path differs from verified delta target")
        return DeltaApplyReport(
            staging_path=sealed.database_path,
            applied_operations=applied,
            idempotent_operations=skipped,
            already_applied=already_applied,
            release_id=cast(str, delta["releaseId"]),
            sequence=cast(int, delta["targetSequence"]),
            state_hash=sealed.digest.state_hash,
            table_hashes=sealed.digest.table_hashes,
            table_counts=sealed.digest.table_counts,
            file_sha256=sealed.file_sha256,
        )
    except (sqlite3.Error, OSError, MutationValidationError) as error:
        raise DeltaApplyError(f"staging delta apply failed: {error}") from error


def _full_seed_manifest(
    *,
    digest: DatabaseDigest,
    release: ReleaseIdentity,
    file_digest: str,
    file_bytes: int,
    created_at: str,
    application_release_id: str,
) -> JsonObject:
    raw: dict[str, object] = {
        "applicationReleaseId": application_release_id,
        "contractVersion": DELTA_CONTRACT_VERSION,
        "createdAt": created_at,
        "databaseBytes": file_bytes,
        "databaseSha256": file_digest,
        "format": FULL_SEED_FORMAT,
        "logicalStateHash": digest.state_hash,
        "releaseId": release.release_id,
        "schemaVersion": REQUIRED_SQLITE_PROFILE.user_version,
        "sequence": release.sequence,
        "tableContractVersion": TABLE_CONTRACT_VERSION,
        "tables": [
            {
                "logicalSha256": digest.table_hashes[table],
                "rowCount": digest.table_counts[table],
                "table": table,
            }
            for table in CONTRACT_TABLE_ORDER
        ],
    }
    return validate_full_seed_manifest(raw)


def validate_full_seed_manifest(value: Mapping[str, object]) -> JsonObject:
    """Validate the deterministic complete manifest around a concrete SQLite seed."""
    manifest = _object(value, "full seed manifest")
    required = {
        "applicationReleaseId",
        "contractVersion",
        "createdAt",
        "databaseBytes",
        "databaseSha256",
        "format",
        "logicalStateHash",
        "releaseId",
        "schemaVersion",
        "sequence",
        "tableContractVersion",
        "tables",
    }
    if set(manifest) != required:
        raise DeltaValidationError("full seed manifest has unknown or missing fields")
    if (
        manifest["format"] != FULL_SEED_FORMAT
        or manifest["contractVersion"] != DELTA_CONTRACT_VERSION
        or manifest["tableContractVersion"] != TABLE_CONTRACT_VERSION
        or manifest["schemaVersion"] != REQUIRED_SQLITE_PROFILE.user_version
    ):
        raise DeltaValidationError("full seed compatibility differs")
    _identifier(manifest["applicationReleaseId"], "full seed applicationReleaseId")
    _identifier(manifest["releaseId"], "full seed releaseId")
    _timestamp(manifest["createdAt"], "full seed createdAt")
    _sha256(manifest["databaseSha256"], "full seed databaseSha256")
    _sha256(manifest["logicalStateHash"], "full seed logicalStateHash")
    if _nonnegative_integer(manifest["databaseBytes"], "full seed databaseBytes") == 0:
        raise DeltaValidationError("full seed databaseBytes must be positive")
    _nonnegative_integer(manifest["sequence"], "full seed sequence")
    tables = manifest["tables"]
    if not isinstance(tables, list) or len(tables) != len(CONTRACT_TABLE_ORDER):
        raise DeltaValidationError("full seed tables must cover the complete contract")
    names: list[str] = []
    for index, raw in enumerate(tables):
        item = _object(raw, f"full seed tables[{index}]")
        if set(item) != {"logicalSha256", "rowCount", "table"}:
            raise DeltaValidationError("full seed table entry shape differs")
        if not isinstance(item["table"], str):
            raise DeltaValidationError("full seed table name must be text")
        names.append(item["table"])
        _sha256(item["logicalSha256"], "full seed table hash")
        _nonnegative_integer(item["rowCount"], "full seed table row count")
    if tuple(names) != CONTRACT_TABLE_ORDER:
        raise DeltaValidationError("full seed table order/coverage differs")
    return cast(JsonObject, manifest)


def export_full_seed(
    source_path: Path,
    seed_path: Path,
    manifest_path: Path,
    *,
    created_at: str,
    application_release_id: str,
) -> FullSeedReport:
    """Create an exact create-only full seed plus canonical complete manifest."""
    _timestamp(created_at, "full seed createdAt")
    _identifier(application_release_id, "full seed applicationReleaseId")
    descriptor, source, signature = _connect_read_only(source_path)
    try:
        content = _read_descriptor_bytes(descriptor, source_path, expected=signature)
        digest = _verify_read_only(source)
        release = _latest_release(source)
        if release.state_hash != digest.state_hash:
            raise DeltaValidationError("full seed release marker state hash differs")
        _assert_descriptor_unchanged(descriptor, signature, source_path)
    finally:
        source.close()
        os.close(descriptor)
    ensure_private_directory(seed_path.parent)
    ensure_private_directory(manifest_path.parent)
    file_digest = hashlib.sha256(content).hexdigest()
    manifest = _full_seed_manifest(
        digest=digest,
        release=release,
        file_digest=file_digest,
        file_bytes=len(content),
        created_at=created_at,
        application_release_id=application_release_id,
    )
    atomic_write_new(seed_path, content, mode=0o600)
    atomic_write_new(manifest_path, canonical_json_line(manifest), mode=0o600)
    return FullSeedReport(seed_path.resolve(), manifest, file_digest, len(content), digest)


def import_full_seed(
    seed_path: Path,
    manifest_path: Path,
    target_path: Path,
) -> FullSeedReport:
    """Verify and copy a full seed to one new target inode; never overwrite active data."""
    try:
        parsed = json.loads(_read_pinned_bytes(manifest_path))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeltaValidationError(f"invalid full seed manifest JSON: {error}") from error
    manifest = validate_full_seed_manifest(_object(parsed, "full seed manifest"))
    descriptor, seed, signature = _connect_read_only(seed_path)
    try:
        content = _read_descriptor_bytes(descriptor, seed_path, expected=signature)
        digest_value = hashlib.sha256(content).hexdigest()
        if len(content) != manifest["databaseBytes"] or digest_value != manifest["databaseSha256"]:
            raise DeltaValidationError("full seed bytes/hash differ from manifest")
        digest = _verify_read_only(seed)
        release = _latest_release(seed)
        tables = cast(list[dict[str, object]], manifest["tables"])
        for item in tables:
            table = cast(str, item["table"])
            if (
                digest.table_counts[table] != item["rowCount"]
                or digest.table_hashes[table] != item["logicalSha256"]
            ):
                raise DeltaValidationError(f"full seed table evidence differs for {table}")
        if (
            digest.state_hash != manifest["logicalStateHash"]
            or release.release_id != manifest["releaseId"]
            or release.sequence != manifest["sequence"]
            or release.state_hash != digest.state_hash
        ):
            raise DeltaValidationError("full seed release/state evidence differs")
        _assert_descriptor_unchanged(descriptor, signature, seed_path)
    finally:
        seed.close()
        os.close(descriptor)
    ensure_private_directory(target_path.parent)
    atomic_write_new(target_path, content, mode=0o600)
    sealed = inspect_release_database(target_path)
    if (
        sealed.file_sha256 != digest_value
        or sealed.digest != digest
        or sealed.release.release_id != manifest["releaseId"]
        or sealed.release.sequence != manifest["sequence"]
    ):
        raise DeltaApplyError("imported full seed differs after copy and reopen")
    return FullSeedReport(
        sealed.database_path,
        manifest,
        sealed.file_sha256,
        sealed.file_bytes,
        sealed.digest,
    )


__all__ = [
    "CONTRACT_TABLE_ORDER",
    "DELTA_CONTRACT_VERSION",
    "FULL_SEED_FORMAT",
    "TABLE_CONTRACT_VERSION",
    "DeltaApplyError",
    "DeltaApplyReport",
    "DeltaConflictError",
    "DeltaValidationError",
    "FullSeedReport",
    "ReleaseDatabaseReport",
    "ReleaseIdentity",
    "apply_delta_to_staging",
    "build_delta",
    "export_full_seed",
    "finalize_release_database",
    "import_full_seed",
    "inspect_release_database",
    "validate_delta",
    "validate_full_seed_manifest",
]
