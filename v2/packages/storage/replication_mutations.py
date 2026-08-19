"""Typed Stage 5 replicated-row mutations and disposable staging replay."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast
from urllib.parse import quote, urlsplit

from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.hashing import (
    REPLICATED_TABLES,
    logical_state_hash,
    table_hash,
    verify_database,
)
from packages.storage.migrations import configure_staging_connection
from packages.storage.safe_files import (
    SafeFilesystemError,
    open_directory_nofollow,
    open_regular_file_nofollow,
    relative_parts,
)
from packages.storage.sqlite_profile import assert_sqlite_runtime

type JsonScalar = None | bool | int | float | str
type MutationAction = str

MUTATION_FORMAT: Final = "radar-replication-mutations/v1"
_SHA256: Final = re.compile(r"^[a-f0-9]{64}$")
_SQL_PAYLOAD: Final = re.compile(
    r"(?is)\b(?:drop|alter|create)\s+(?:table|index|view|trigger)\b"
    r"|\binsert\s+into\b|\bdelete\s+from\b|\bupdate\s+[A-Za-z_][A-Za-z0-9_]*\s+set\b"
    r"|\bselect\s+.+?\s+from\b|\bpragma\s+[A-Za-z_]|\battach\s+database\b"
)
_SECRET: Final = (
    re.compile(r"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
)
_HOST_PATH: Final = re.compile(
    r"(?i)(?:^|[\s'\"])(?:/(?:root|mnt|etc|srv|opt|var)(?:/|\b)|[A-Z]:\\|file://)"
)
_DATE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TIMESTAMP: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class MutationValidationError(ValueError):
    """A mutation document is incomplete, executable, unsafe or out of contract."""


class MutationConflictError(RuntimeError):
    """A staging row does not match the mutation's optimistic precondition."""


class StagingReplayError(RuntimeError):
    """A mutation document could not be replayed into a new staging database."""


@dataclass(frozen=True, slots=True)
class TableMutationSpec:
    """Exact content-writer surface copied from sqlite-contract v1."""

    primary_key: tuple[str, ...]
    columns: tuple[str, ...]
    actions: frozenset[str]
    json_columns: frozenset[str] = frozenset()
    relative_path_columns: frozenset[str] = frozenset()


def _spec(
    primary_key: tuple[str, ...],
    columns: tuple[str, ...],
    actions: tuple[str, ...],
    *,
    json_columns: tuple[str, ...] = (),
    relative_path_columns: tuple[str, ...] = (),
) -> TableMutationSpec:
    return TableMutationSpec(
        primary_key,
        columns,
        frozenset(actions),
        frozenset(json_columns),
        frozenset(relative_path_columns),
    )


TABLE_SPECS: Final[dict[str, TableMutationSpec]] = {
    "content_releases": _spec(
        ("release_id",),
        (
            "release_id",
            "sequence",
            "base_release_id",
            "candidate_id",
            "operation",
            "schema_version",
            "before_state_hash",
            "after_state_hash",
            "created_at",
            "activated_at",
        ),
        ("insert",),
    ),
    "source_snapshots": _spec(
        ("snapshot_id",),
        ("snapshot_id", "manifest_sha256", "payload_sha256", "collected_at", "item_count"),
        ("insert",),
    ),
    "sources": _spec(
        ("source_id",),
        ("source_id", "name", "url", "source_type", "enabled", "updated_at"),
        ("upsert",),
    ),
    "materials": _spec(
        ("material_id",),
        (
            "material_id",
            "title",
            "url",
            "canonical_url",
            "source_name",
            "published_at",
            "publication_date_status",
            "summary",
            "agpm_takeaway",
            "brief",
            "content_hash",
            "created_at",
            "updated_at",
        ),
        ("upsert",),
    ),
    "material_sources": _spec(
        ("material_id", "source_id"),
        ("material_id", "source_id", "source_url", "provider", "first_seen_at", "last_seen_at"),
        ("upsert", "delete"),
    ),
    "material_evidence": _spec(
        ("evidence_id",),
        (
            "evidence_id",
            "material_id",
            "kind",
            "content_sha256",
            "media_type",
            "public_url",
            "metadata_json",
            "created_at",
        ),
        ("upsert", "delete"),
        json_columns=("metadata_json",),
    ),
    "editorial_queue": _spec(
        ("queue_id",),
        (
            "queue_id",
            "material_id",
            "state",
            "target_issue_date",
            "priority",
            "reason",
            "created_at",
            "updated_at",
        ),
        ("upsert", "delete"),
    ),
    "issues": _spec(
        ("issue_id",),
        (
            "issue_id",
            "issue_date",
            "issue_number",
            "title",
            "brief",
            "lifecycle_status",
            "published_at",
            "publication_origin",
            "empty_reason",
            "content_hash",
            "created_at",
            "updated_at",
        ),
        ("upsert",),
    ),
    "issue_materials": _spec(
        ("issue_id", "material_id"),
        (
            "issue_id",
            "material_id",
            "sort_order",
            "perimeter",
            "verdict",
            "summary",
            "agpm_takeaway",
            "brief",
            "theses_json",
            "trend_notes",
            "flags_json",
            "key_material",
            "signal_score",
            "signal_strength",
            "created_at",
            "updated_at",
        ),
        ("upsert", "delete"),
        json_columns=("theses_json", "flags_json"),
    ),
    "issue_analysis": _spec(
        ("issue_id",),
        (
            "issue_id",
            "headline",
            "analysis_json",
            "theses_json",
            "brief",
            "llm_status",
            "requested_model",
            "effective_model",
            "provider",
            "prompt_version",
            "updated_at",
        ),
        ("upsert", "delete"),
        json_columns=("analysis_json", "theses_json"),
    ),
    "material_analysis": _spec(
        ("issue_id", "material_id"),
        (
            "issue_id",
            "material_id",
            "short_text",
            "agpm_angle",
            "llm_status",
            "requested_model",
            "effective_model",
            "provider",
            "prompt_version",
            "updated_at",
        ),
        ("upsert", "delete"),
    ),
    "llm_attempts": _spec(
        ("attempt_id",),
        (
            "attempt_id",
            "scope",
            "issue_id",
            "material_id",
            "requested_model",
            "attempted_model",
            "provider",
            "attempt_order",
            "status",
            "error_code",
            "started_at",
            "finished_at",
        ),
        ("insert",),
    ),
    "source_rules": _spec(
        ("host",),
        ("host", "date_strategy", "notes", "updated_at"),
        ("upsert", "delete"),
    ),
    "material_quality": _spec(
        ("issue_id", "material_id"),
        (
            "issue_id",
            "material_id",
            "publication_date_status",
            "issue_date_delta_days",
            "severity",
            "review_status",
            "reason",
            "updated_at",
        ),
        ("upsert", "delete"),
    ),
    "material_rubrics": _spec(
        ("issue_id", "material_id", "rubric_id"),
        ("issue_id", "material_id", "rubric_id", "confidence", "source"),
        ("upsert", "delete"),
    ),
    "daily_stats": _spec(
        ("issue_id",),
        (
            "issue_id",
            "viewed",
            "included",
            "cut",
            "near",
            "mid",
            "far",
            "core",
            "adjacent",
            "updated_at",
        ),
        ("upsert",),
    ),
    "gazettes": _spec(
        ("gazette_id",),
        (
            "gazette_id",
            "period",
            "title",
            "lifecycle_status",
            "published_at",
            "asset_manifest_sha256",
            "content_hash",
            "created_at",
            "updated_at",
        ),
        ("upsert",),
    ),
    "gazette_assets": _spec(
        ("gazette_id", "relative_path"),
        ("gazette_id", "relative_path", "sha256", "bytes", "media_type"),
        ("upsert", "delete"),
        relative_path_columns=("relative_path",),
    ),
}

_NULLABLE_COLUMNS: Final = frozenset(
    {
        ("content_releases", "base_release_id"),
        ("sources", "url"),
        ("materials", "canonical_url"),
        ("materials", "source_name"),
        ("materials", "published_at"),
        ("materials", "summary"),
        ("materials", "agpm_takeaway"),
        ("materials", "brief"),
        ("material_sources", "source_url"),
        ("material_sources", "provider"),
        ("material_sources", "first_seen_at"),
        ("material_sources", "last_seen_at"),
        ("material_evidence", "public_url"),
        ("editorial_queue", "target_issue_date"),
        ("editorial_queue", "reason"),
        ("issues", "issue_number"),
        ("issues", "brief"),
        ("issues", "published_at"),
        ("issues", "publication_origin"),
        ("issues", "empty_reason"),
        ("issue_materials", "summary"),
        ("issue_materials", "agpm_takeaway"),
        ("issue_materials", "brief"),
        ("issue_materials", "trend_notes"),
        ("issue_materials", "signal_score"),
        ("issue_analysis", "headline"),
        ("issue_analysis", "brief"),
        ("issue_analysis", "requested_model"),
        ("issue_analysis", "effective_model"),
        ("issue_analysis", "provider"),
        ("material_analysis", "short_text"),
        ("material_analysis", "agpm_angle"),
        ("material_analysis", "requested_model"),
        ("material_analysis", "effective_model"),
        ("material_analysis", "provider"),
        ("llm_attempts", "material_id"),
        ("llm_attempts", "requested_model"),
        ("llm_attempts", "attempted_model"),
        ("llm_attempts", "provider"),
        ("llm_attempts", "error_code"),
        ("source_rules", "notes"),
        ("material_quality", "issue_date_delta_days"),
        ("material_quality", "reason"),
        ("material_rubrics", "confidence"),
        ("gazettes", "published_at"),
    }
)
_INTEGER_COLUMNS: Final = frozenset(
    {
        ("content_releases", "sequence"),
        ("content_releases", "schema_version"),
        ("source_snapshots", "item_count"),
        ("sources", "enabled"),
        ("editorial_queue", "priority"),
        ("issues", "issue_number"),
        ("issue_materials", "sort_order"),
        ("issue_materials", "key_material"),
        ("issue_materials", "signal_score"),
        ("llm_attempts", "attempt_order"),
        ("material_quality", "issue_date_delta_days"),
        ("daily_stats", "viewed"),
        ("daily_stats", "included"),
        ("daily_stats", "cut"),
        ("daily_stats", "near"),
        ("daily_stats", "mid"),
        ("daily_stats", "far"),
        ("daily_stats", "core"),
        ("daily_stats", "adjacent"),
        ("gazette_assets", "bytes"),
    }
)
_REAL_COLUMNS: Final = frozenset({("material_rubrics", "confidence")})
_MINIMUM_COLUMNS: Final[dict[tuple[str, str], int]] = {
    ("content_releases", "sequence"): 0,
    ("source_snapshots", "item_count"): 0,
    ("llm_attempts", "attempt_order"): 1,
    ("daily_stats", "viewed"): 0,
    ("daily_stats", "included"): 0,
    ("daily_stats", "cut"): 0,
    ("daily_stats", "near"): 0,
    ("daily_stats", "mid"): 0,
    ("daily_stats", "far"): 0,
    ("daily_stats", "core"): 0,
    ("daily_stats", "adjacent"): 0,
    ("gazette_assets", "bytes"): 0,
}
_SHA256_COLUMNS: Final = frozenset(
    {
        ("content_releases", "before_state_hash"),
        ("content_releases", "after_state_hash"),
        ("source_snapshots", "manifest_sha256"),
        ("source_snapshots", "payload_sha256"),
        ("materials", "content_hash"),
        ("material_evidence", "content_sha256"),
        ("issues", "content_hash"),
        ("gazettes", "asset_manifest_sha256"),
        ("gazettes", "content_hash"),
        ("gazette_assets", "sha256"),
    }
)
_DATE_COLUMNS: Final = frozenset(
    {
        ("editorial_queue", "target_issue_date"),
        ("issues", "issue_date"),
    }
)
_TIMESTAMP_COLUMNS: Final = frozenset(
    {
        ("content_releases", "created_at"),
        ("content_releases", "activated_at"),
        ("source_snapshots", "collected_at"),
        ("sources", "updated_at"),
        ("materials", "published_at"),
        ("materials", "created_at"),
        ("materials", "updated_at"),
        ("material_sources", "first_seen_at"),
        ("material_sources", "last_seen_at"),
        ("material_evidence", "created_at"),
        ("editorial_queue", "created_at"),
        ("editorial_queue", "updated_at"),
        ("issues", "published_at"),
        ("issues", "created_at"),
        ("issues", "updated_at"),
        ("issue_materials", "created_at"),
        ("issue_materials", "updated_at"),
        ("issue_analysis", "updated_at"),
        ("material_analysis", "updated_at"),
        ("llm_attempts", "started_at"),
        ("llm_attempts", "finished_at"),
        ("source_rules", "updated_at"),
        ("material_quality", "updated_at"),
        ("daily_stats", "updated_at"),
        ("gazettes", "published_at"),
        ("gazettes", "created_at"),
        ("gazettes", "updated_at"),
    }
)
_URI_COLUMNS: Final = frozenset(
    {
        ("sources", "url"),
        ("materials", "url"),
        ("materials", "canonical_url"),
        ("material_sources", "source_url"),
        ("material_evidence", "public_url"),
    }
)
_ENUM_COLUMNS: Final[dict[tuple[str, str], frozenset[object]]] = {
    ("content_releases", "operation"): frozenset({"daily", "correction", "gazette"}),
    ("sources", "enabled"): frozenset({0, 1}),
    ("materials", "publication_date_status"): frozenset(
        {"resolved", "low_confidence", "unresolved"}
    ),
    ("editorial_queue", "state"): frozenset({"manual", "deferred", "review"}),
    ("issues", "lifecycle_status"): frozenset({"draft", "published"}),
    ("issues", "publication_origin"): frozenset({"v2", "legacy_inferred"}),
    ("issue_materials", "perimeter"): frozenset({"near", "mid", "far"}),
    ("issue_materials", "verdict"): frozenset({"core", "adjacent"}),
    ("issue_materials", "key_material"): frozenset({0, 1}),
    ("issue_materials", "signal_strength"): frozenset({"strong", "context", "watch"}),
    ("issue_analysis", "llm_status"): frozenset({"success", "fallback", "unavailable"}),
    ("material_analysis", "llm_status"): frozenset({"success", "fallback", "unavailable"}),
    ("llm_attempts", "scope"): frozenset({"issue", "material"}),
    ("llm_attempts", "status"): frozenset({"success", "error", "invalid", "skipped"}),
    ("material_quality", "publication_date_status"): frozenset(
        {"resolved", "low_confidence", "unresolved"}
    ),
    ("material_quality", "severity"): frozenset({"ok", "low", "medium", "high"}),
    ("material_quality", "review_status"): frozenset({"ok", "monitor", "queued"}),
    ("gazettes", "lifecycle_status"): frozenset({"draft", "published"}),
}


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Evidence from one transaction on a disposable staging copy."""

    staging_path: Path
    applied: int
    idempotent_skips: int
    before_counts: dict[str, int]
    after_counts: dict[str, int]
    after_hashes: dict[str, str]
    after_state_hash: str


def _object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise MutationValidationError(f"{label} must be an object with string keys")
    return cast(JsonObject, value)


def _scalar(value: object, label: str) -> JsonScalar:
    if value is None or isinstance(value, str | int | float | bool):
        if isinstance(value, float) and not (-float("inf") < value < float("inf")):
            raise MutationValidationError(f"{label} is non-finite")
        return value
    raise MutationValidationError(f"{label} must be a JSON scalar")


def _scan_text(value: str, label: str) -> None:
    if _SQL_PAYLOAD.search(value):
        raise MutationValidationError(f"SQL/DDL payload is forbidden at {label}")
    forbidden_state_name = ".open" + "claw"
    if _HOST_PATH.search(value) or forbidden_state_name in value.lower():
        raise MutationValidationError(f"host-local path is forbidden at {label}")
    if any(pattern.search(value) for pattern in _SECRET):
        raise MutationValidationError(f"secret-shaped content is forbidden at {label}")


def _safe_relative_path(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise MutationValidationError(f"{label} must be a relative path")
    try:
        relative_parts(value)
    except SafeFilesystemError as error:
        raise MutationValidationError(f"unsafe relative path at {label}: {value!r}") from error


def _validate_scalar(value: object, label: str) -> JsonScalar:
    scalar = _scalar(value, label)
    if isinstance(scalar, str):
        _scan_text(scalar, label)
    return scalar


def _validate_date(value: str, label: str) -> None:
    if _DATE.fullmatch(value) is None:
        raise MutationValidationError(f"{label} must be an ISO date")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise MutationValidationError(f"{label} is not a real date") from error


def _validate_timestamp(value: str, label: str) -> None:
    if _TIMESTAMP.fullmatch(value) is None:
        raise MutationValidationError(f"{label} must be second-precision UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise MutationValidationError(f"{label} is not a real UTC timestamp") from error


def _reject_json_constant(constant: str) -> None:
    raise MutationValidationError(f"non-finite JSON constant is forbidden: {constant}")


def _validate_column_scalar(table: str, column: str, value: object, label: str) -> JsonScalar:
    if value is None:
        if (table, column) not in _NULLABLE_COLUMNS:
            raise MutationValidationError(f"{label} cannot be null")
        return None
    if (table, column) in _INTEGER_COLUMNS:
        if not isinstance(value, int) or isinstance(value, bool):
            raise MutationValidationError(f"{label} must be an integer")
        scalar: JsonScalar = value
    elif (table, column) in _REAL_COLUMNS:
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise MutationValidationError(f"{label} must be a real number")
        scalar = value
    else:
        if not isinstance(value, str):
            raise MutationValidationError(f"{label} must be text")
        scalar = value
        _scan_text(value, label)
    enum = _ENUM_COLUMNS.get((table, column))
    if enum is not None and scalar not in enum:
        raise MutationValidationError(f"{label} is outside the contract enum")
    minimum = _MINIMUM_COLUMNS.get((table, column))
    if minimum is not None and isinstance(scalar, int) and scalar < minimum:
        raise MutationValidationError(f"{label} must be at least {minimum}")
    if isinstance(scalar, str):
        if (table, column) in _SHA256_COLUMNS and _SHA256.fullmatch(scalar) is None:
            raise MutationValidationError(f"{label} must be lowercase SHA-256")
        if (table, column) in _DATE_COLUMNS:
            _validate_date(scalar, label)
        if (table, column) in _TIMESTAMP_COLUMNS:
            _validate_timestamp(scalar, label)
        if (table, column) in _URI_COLUMNS:
            parsed = urlsplit(scalar)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
                raise MutationValidationError(
                    f"{label} must be an absolute HTTP(S) URI without userinfo"
                )
    return scalar


def _row_hash(values: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_line(dict(values))).hexdigest()


def row_after_sha256(values: Mapping[str, object]) -> str:
    """Return the required canonical full-row hash for a mutation."""
    return _row_hash(values)


def _validate_expected_before(value: object, action: str, label: str) -> None:
    expected = _object(value, label)
    state = expected.get("state")
    if state == "absent":
        if set(expected) != {"state"} or action == "delete":
            raise MutationValidationError(f"invalid absent precondition at {label}")
        return
    if state == "present":
        if set(expected) != {"state", "rowSha256"}:
            raise MutationValidationError(f"invalid present precondition at {label}")
        digest = expected.get("rowSha256")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise MutationValidationError(f"invalid rowSha256 at {label}")
        return
    raise MutationValidationError(f"precondition state must be absent or present at {label}")


def validate_mutation_document(
    document: Mapping[str, object], candidate: Mapping[str, object]
) -> None:
    """Validate exact mutation shape, coverage and candidate binding without executing SQL."""
    root = _object(document, "replication mutations")
    required = {
        "mutationFormat",
        "contractVersion",
        "candidateId",
        "operation",
        "schemaVersion",
        "completeness",
        "mutations",
    }
    if set(root) != required:
        raise MutationValidationError("replication mutation root has unknown or missing fields")
    if root.get("mutationFormat") != MUTATION_FORMAT or root.get("contractVersion") != "1.0.0":
        raise MutationValidationError("unsupported replication mutation contract")
    for field in ("candidateId", "operation", "schemaVersion"):
        if root.get(field) != candidate.get(field):
            raise MutationValidationError(f"mutation document {field} differs from candidate")

    raw_mutations = root.get("mutations")
    if not isinstance(raw_mutations, list) or not raw_mutations:
        raise MutationValidationError("mutations must be a non-empty array")
    counts: Counter[str] = Counter()
    identities: set[tuple[str, tuple[tuple[str, JsonScalar], ...]]] = set()
    for index, raw_mutation in enumerate(raw_mutations, start=1):
        label = f"mutations[{index - 1}]"
        mutation = _object(raw_mutation, label)
        if mutation.get("sequence") != index:
            raise MutationValidationError("mutation sequences must be contiguous and ordered")
        table = mutation.get("table")
        action = mutation.get("action")
        if not isinstance(table, str) or table not in TABLE_SPECS:
            raise MutationValidationError(f"unknown or non-content table at {label}")
        if table == "content_releases":
            raise MutationValidationError(
                "Project Manager candidate cannot author content_releases"
            )
        if not isinstance(action, str) or action not in TABLE_SPECS[table].actions:
            raise MutationValidationError(f"action is not allowed for {table}")
        spec = TABLE_SPECS[table]
        expected_fields = {"sequence", "action", "table", "key", "expectedBefore"}
        if action != "delete":
            expected_fields |= {"values", "rowAfterSha256"}
        if set(mutation) != expected_fields:
            raise MutationValidationError(f"unknown or missing mutation fields at {label}")
        key = _object(mutation.get("key"), f"{label}.key")
        if set(key) != set(spec.primary_key):
            raise MutationValidationError(f"primary key shape differs for {table}")
        normalized_key = tuple(
            (
                column,
                _validate_column_scalar(
                    table,
                    column,
                    key[column],
                    f"{label}.key.{column}",
                ),
            )
            for column in spec.primary_key
        )
        identity = (table, normalized_key)
        if identity in identities:
            raise MutationValidationError(f"package mutates one row more than once: {table}")
        identities.add(identity)
        _validate_expected_before(mutation.get("expectedBefore"), action, f"{label}.expectedBefore")
        if action != "delete":
            values = _object(mutation.get("values"), f"{label}.values")
            if set(values) != set(spec.columns):
                raise MutationValidationError(f"full row column set differs for {table}")
            for column in spec.columns:
                scalar = _validate_column_scalar(
                    table,
                    column,
                    values[column],
                    f"{label}.values.{column}",
                )
                if column in spec.json_columns:
                    if not isinstance(scalar, str):
                        raise MutationValidationError(
                            f"{table}.{column} must be canonical JSON text"
                        )

                    try:
                        parsed = json.loads(scalar, parse_constant=_reject_json_constant)
                    except json.JSONDecodeError as error:
                        raise MutationValidationError(
                            f"invalid JSON text in {table}.{column}"
                        ) from error
                    canonical = json.dumps(
                        parsed,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    if canonical != scalar:
                        raise MutationValidationError(
                            f"non-canonical JSON text in {table}.{column}"
                        )
                if column in spec.relative_path_columns:
                    _safe_relative_path(scalar, f"{label}.values.{column}")
            if any(values[column] != key[column] for column in spec.primary_key):
                raise MutationValidationError(f"row primary key differs from key for {table}")
            digest = mutation.get("rowAfterSha256")
            if not isinstance(digest, str) or digest != _row_hash(values):
                raise MutationValidationError(f"rowAfterSha256 differs for {table}")
        counts[table] += 1

    completeness = _object(root.get("completeness"), "completeness")
    if set(completeness) != {"affectedTableCount", "totalMutationCount", "tables"}:
        raise MutationValidationError("completeness has unknown or missing fields")
    tables = completeness.get("tables")
    if not isinstance(tables, list):
        raise MutationValidationError("completeness.tables must be an array")
    declared: dict[str, int] = {}
    prior = ""
    for index, raw_entry in enumerate(tables):
        entry = _object(raw_entry, f"completeness.tables[{index}]")
        if set(entry) != {"table", "mutationCount"}:
            raise MutationValidationError("completeness table entry shape differs")
        table = entry.get("table")
        count = entry.get("mutationCount")
        if not isinstance(table, str) or table not in REPLICATED_TABLES or table <= prior:
            raise MutationValidationError(
                "completeness tables must be unique and lexically ordered"
            )
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise MutationValidationError("completeness mutationCount must be positive")
        declared[table] = count
        prior = table
    if declared != dict(sorted(counts.items())):
        raise MutationValidationError(
            "completeness declaration differs from actual affected tables"
        )
    if completeness.get("affectedTableCount") != len(counts):
        raise MutationValidationError("affectedTableCount differs from actual table count")
    if completeness.get("totalMutationCount") != len(raw_mutations):
        raise MutationValidationError("totalMutationCount differs from actual mutations")


def build_mutation_document(
    candidate: Mapping[str, object], mutations: Sequence[Mapping[str, object]]
) -> JsonObject:
    """Add a deterministic complete declaration to caller-authored typed row mutations."""
    counts = Counter(str(mutation.get("table")) for mutation in mutations)
    raw_document: dict[str, object] = {
        "candidateId": candidate.get("candidateId"),
        "completeness": {
            "affectedTableCount": len(counts),
            "tables": [
                {"mutationCount": counts[table], "table": table} for table in sorted(counts)
            ],
            "totalMutationCount": len(mutations),
        },
        "contractVersion": "1.0.0",
        "mutationFormat": MUTATION_FORMAT,
        "mutations": [dict(mutation) for mutation in mutations],
        "operation": candidate.get("operation"),
        "schemaVersion": candidate.get("schemaVersion"),
    }
    document = cast(JsonObject, raw_document)
    validate_mutation_document(document, candidate)
    return document


def _read_row(
    connection: sqlite3.Connection, table: str, spec: TableMutationSpec, key: Mapping[str, object]
) -> dict[str, object] | None:
    where = " AND ".join(f'"{column}" = ?' for column in spec.primary_key)
    columns = ", ".join(f'"{column}"' for column in spec.columns)
    query = f'SELECT {columns} FROM "{table}" WHERE {where}'
    row = connection.execute(query, tuple(key[column] for column in spec.primary_key)).fetchone()
    if row is None:
        return None
    return dict(zip(spec.columns, row, strict=True))


def _apply_one(connection: sqlite3.Connection, mutation: JsonObject) -> bool:
    table = cast(str, mutation["table"])
    action = cast(str, mutation["action"])
    spec = TABLE_SPECS[table]
    key = _object(mutation["key"], "mutation key")
    current = _read_row(connection, table, spec, key)
    if action != "delete":
        values = _object(mutation["values"], "mutation values")
        if current is not None and _row_hash(current) == mutation["rowAfterSha256"]:
            return False
    elif current is None:
        return False
    expected = _object(mutation["expectedBefore"], "expectedBefore")
    if expected["state"] == "absent":
        if current is not None:
            raise MutationConflictError(f"expected absent row in {table}")
    elif current is None or _row_hash(current) != expected["rowSha256"]:
        raise MutationConflictError(f"expected row hash differs in {table}")

    where = " AND ".join(f'"{column}" = ?' for column in spec.primary_key)
    if action == "delete":
        connection.execute(
            f'DELETE FROM "{table}" WHERE {where}',
            tuple(key[column] for column in spec.primary_key),
        )
        return True
    values = _object(mutation["values"], "mutation values")
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
        key_columns = ", ".join(f'"{column}"' for column in spec.primary_key)
        query = (
            f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders}) '
            f"ON CONFLICT ({key_columns}) DO UPDATE SET {updates}"
        )
    connection.execute(query, tuple(values[column] for column in spec.columns))
    return True


def _open_staging_parent(path: Path) -> int:
    try:
        return open_directory_nofollow(path)
    except OSError as error:
        raise StagingReplayError(f"unsafe SQLite path boundary: {path}: {error}") from error


def replay_to_staging(
    source_path: Path,
    staging_path: Path,
    document: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    expected_source_state_hash: str | None = None,
) -> ReplayReport:
    """Copy a source DB and replay allowlisted mutations only into a new staging file."""
    validate_mutation_document(document, candidate)
    assert_sqlite_runtime()
    affected = sorted(
        {
            cast(str, _object(raw, "mutation")["table"])
            for raw in cast(list[object], document["mutations"])
        }
    )
    source_descriptor: int | None = None
    staging_parent_descriptor: int | None = None
    staging_identity: tuple[int, int] | None = None
    staging_name = staging_path.name
    staging_absolute = Path(os.path.abspath(staging_path))
    try:
        source_descriptor = open_regular_file_nofollow(source_path)
        staging_parent_descriptor = _open_staging_parent(staging_path.parent)
        parent_metadata = os.fstat(staging_parent_descriptor)
        if stat.S_IMODE(parent_metadata.st_mode) & 0o077:
            raise StagingReplayError("staging parent permissions are broader than private")
        if relative_parts(staging_name) != (staging_name,):
            raise StagingReplayError("staging database name is not a simple relative name")
        flags = (
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        reservation = os.open(
            staging_name,
            flags,
            0o600,
            dir_fd=staging_parent_descriptor,
        )
        reserved = os.fstat(reservation)
        try:
            if (
                not stat.S_ISREG(reserved.st_mode)
                or stat.S_IMODE(reserved.st_mode) != 0o600
                or reserved.st_nlink != 1
            ):
                raise StagingReplayError("staging reservation is not a private single-link file")
            staging_identity = (reserved.st_dev, reserved.st_ino)
        finally:
            os.close(reservation)
        source_uri = f"file:/proc/self/fd/{source_descriptor}?mode=ro&immutable=1"
        staging_proc_path = f"/proc/self/fd/{staging_parent_descriptor}/{staging_name}"
        staging_uri = f"file:{quote(staging_proc_path, safe='/')}?mode=rw"
        with (
            sqlite3.connect(source_uri, uri=True) as source,
            sqlite3.connect(staging_uri, uri=True) as staging,
        ):
            source.execute("PRAGMA query_only = ON")
            source.execute("BEGIN")
            if (
                expected_source_state_hash is not None
                and logical_state_hash(source) != expected_source_state_hash
            ):
                raise StagingReplayError("source logical state changed before staging replay")
            source.backup(staging)
            configure_staging_connection(staging)
            before_counts = {
                table: int(staging.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in affected
            }
            applied = 0
            skipped = 0
            staging.execute("BEGIN IMMEDIATE")
            try:
                for raw in cast(list[object], document["mutations"]):
                    if _apply_one(staging, _object(raw, "mutation")):
                        applied += 1
                    else:
                        skipped += 1
                violations = tuple(staging.execute("PRAGMA foreign_key_check"))
                if violations:
                    raise StagingReplayError(f"foreign-key check failed: {violations!r}")
                staging.commit()
            except BaseException:
                staging.rollback()
                raise
            verify_database(staging)
            staging.commit()
            after_counts = {
                table: int(staging.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in affected
            }
            after_hashes = {table: table_hash(staging, table) for table in affected}
            after_state_hash = logical_state_hash(staging)
            source.rollback()
        current = os.stat(
            staging_name,
            dir_fd=staging_parent_descriptor,
            follow_symlinks=False,
        )
        if (
            staging_identity != (current.st_dev, current.st_ino)
            or not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_nlink != 1
        ):
            raise StagingReplayError("staging database identity changed during replay")
        current_parent_descriptor = _open_staging_parent(staging_path.parent)
        try:
            current_parent = os.fstat(current_parent_descriptor)
            if (current_parent.st_dev, current_parent.st_ino) != (
                parent_metadata.st_dev,
                parent_metadata.st_ino,
            ):
                raise StagingReplayError("staging parent path changed during replay")
        finally:
            os.close(current_parent_descriptor)
        return ReplayReport(
            staging_path=staging_absolute,
            applied=applied,
            idempotent_skips=skipped,
            before_counts=before_counts,
            after_counts=after_counts,
            after_hashes=after_hashes,
            after_state_hash=after_state_hash,
        )
    except BaseException as error:
        if staging_parent_descriptor is not None and staging_identity is not None:
            with suppress(FileNotFoundError):
                current = os.stat(
                    staging_name,
                    dir_fd=staging_parent_descriptor,
                    follow_symlinks=False,
                )
                if staging_identity == (current.st_dev, current.st_ino):
                    os.unlink(staging_name, dir_fd=staging_parent_descriptor)
        if isinstance(error, SafeFilesystemError):
            raise StagingReplayError(f"unsafe SQLite path boundary: {error}") from error
        raise
    finally:
        if staging_parent_descriptor is not None:
            os.close(staging_parent_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)


__all__ = [
    "MUTATION_FORMAT",
    "TABLE_SPECS",
    "MutationConflictError",
    "MutationValidationError",
    "ReplayReport",
    "StagingReplayError",
    "build_mutation_document",
    "replay_to_staging",
    "row_after_sha256",
    "validate_mutation_document",
]
