"""Canonical row/table/state hashing and sealed-artifact checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

REPLICATED_TABLES: Final = (
    "application_compatibility",
    "content_releases",
    "daily_stats",
    "editorial_queue",
    "gazette_assets",
    "gazettes",
    "issue_analysis",
    "issue_materials",
    "issues",
    "legacy_issue_provenance",
    "legacy_publication_evidence",
    "llm_attempts",
    "material_analysis",
    "material_evidence",
    "material_quality",
    "material_rubrics",
    "material_sources",
    "materials",
    "rubrics",
    "schema_migrations",
    "source_rules",
    "source_snapshots",
    "sources",
)
METADATA_TABLES: Final = (
    "application_compatibility",
    "content_releases",
    "schema_migrations",
)
STATE_HASHED_TABLES: Final = tuple(
    table for table in REPLICATED_TABLES if table not in METADATA_TABLES
)
JSON_TEXT_COLUMNS: Final = frozenset(
    {
        ("issue_analysis", "analysis_json"),
        ("issue_analysis", "theses_json"),
        ("issue_materials", "flags_json"),
        ("issue_materials", "theses_json"),
        ("legacy_publication_evidence", "details_json"),
        ("material_evidence", "metadata_json"),
    }
)
HASH_EXCLUDED_COLUMNS: Final = {"content_releases": frozenset({"after_state_hash"})}


@dataclass(frozen=True, slots=True)
class DatabaseDigest:
    """Canonical digest evidence for one replicated database."""

    state_hash: str
    table_hashes: dict[str, str]
    table_counts: dict[str, int]


def _normalize_json(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", key): _normalize_json(item) for key, item in value.items()
        }
    if isinstance(value, float) and value == 0:
        return 0.0
    return value


def _canonical_json(value: JsonValue) -> bytes:
    return json.dumps(
        _normalize_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _table_shape(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if table not in REPLICATED_TABLES:
        raise ValueError(f"not a replicated contract table: {table}")
    rows = tuple(connection.execute(f'PRAGMA table_info("{table}")'))
    if not rows:
        raise RuntimeError(f"missing replicated contract table: {table}")
    excluded = HASH_EXCLUDED_COLUMNS.get(table, frozenset())
    columns = tuple(str(row[1]) for row in rows if str(row[1]) not in excluded)
    primary_key = tuple(
        str(row[1])
        for row in sorted(rows, key=lambda item: int(item[5]))
        if int(row[5]) > 0 and str(row[1]) not in excluded
    )
    if not primary_key:
        raise RuntimeError(f"contract table has no hash ordering key: {table}")
    return columns, primary_key


def canonical_table_lines(connection: sqlite3.Connection, table: str) -> tuple[bytes, ...]:
    """Render every row as normalized JSON in primary-key order."""
    columns, primary_key = _table_shape(connection, table)
    select_columns = ", ".join(f'"{column}"' for column in columns)
    order_columns = ", ".join(f'"{column}"' for column in primary_key)
    query = f'SELECT {select_columns} FROM "{table}" ORDER BY {order_columns}'  # noqa: S608
    lines: list[bytes] = []
    for row in connection.execute(query):
        record: dict[str, JsonValue] = {}
        for column, raw_value in zip(columns, row, strict=True):
            value: JsonValue
            if (table, column) in JSON_TEXT_COLUMNS:
                parsed = json.loads(str(raw_value))
                value = cast(JsonValue, parsed)
            elif raw_value is None or isinstance(raw_value, bool | int | float | str):
                value = raw_value
            else:
                raise TypeError(f"unsupported SQLite value in {table}.{column}")
            record[column] = value
        lines.append(_canonical_json(record) + b"\n")
    return tuple(lines)


def table_hash(connection: sqlite3.Connection, table: str) -> str:
    """Hash one table independently of SQLite page layout."""
    digest = hashlib.sha256()
    for line in canonical_table_lines(connection, table):
        digest.update(line)
    return digest.hexdigest()


def logical_state_hash(connection: sqlite3.Connection) -> str:
    """Hash state tables in lexical table/primary-key order."""
    digest = hashlib.sha256()
    for table in STATE_HASHED_TABLES:
        for line in canonical_table_lines(connection, table):
            parsed_row = cast(JsonValue, json.loads(line))
            digest.update(_canonical_json({"row": parsed_row, "table": table}))
            digest.update(b"\n")
    return digest.hexdigest()


def database_digest(connection: sqlite3.Connection) -> DatabaseDigest:
    """Collect per-table counts/hashes and the aggregate domain hash."""
    return DatabaseDigest(
        state_hash=logical_state_hash(connection),
        table_hashes={table: table_hash(connection, table) for table in REPLICATED_TABLES},
        table_counts={
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'  # noqa: S608
                ).fetchone()[0]
            )
            for table in REPLICATED_TABLES
        },
    )


def rebuild_and_check_fts(connection: sqlite3.Connection) -> int:
    """Rebuild the published-only FTS projection and run contract parity checks."""
    connection.execute("DELETE FROM published_materials_fts")
    connection.execute(
        """
        INSERT INTO published_materials_fts(
          document_id, issue_id, issue_date, material_id, title, summary,
          agpm_takeaway, source_name, url
        )
        SELECT document_id, issue_id, issue_date, material_id, title, summary,
               agpm_takeaway, source_name, url
        FROM pub_search_documents_v1
        ORDER BY issue_date, issue_id, material_id
        """
    )
    source_count = int(
        connection.execute("SELECT COUNT(*) FROM pub_search_documents_v1").fetchone()[0]
    )
    fts_count = int(
        connection.execute("SELECT COUNT(*) FROM published_materials_fts").fetchone()[0]
    )
    if source_count != fts_count:
        raise RuntimeError(f"FTS projection mismatch: source={source_count}, fts={fts_count}")
    connection.execute(
        "INSERT INTO published_materials_fts(published_materials_fts) VALUES ('integrity-check')"
    )
    return fts_count


def verify_database(connection: sqlite3.Connection) -> None:
    """Fail closed unless schema identity, integrity, FKs and FTS are all valid."""
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != REQUIRED_SQLITE_PROFILE.application_id:
        raise RuntimeError(f"unexpected application_id: {application_id}")
    if user_version != REQUIRED_SQLITE_PROFILE.user_version:
        raise RuntimeError(f"unexpected user_version: {user_version}")
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {integrity}")
    foreign_key_rows = tuple(connection.execute("PRAGMA foreign_key_check"))
    if foreign_key_rows:
        raise RuntimeError(f"SQLite foreign key check failed: {foreign_key_rows!r}")
    rebuild_and_check_fts(connection)


def file_sha256(path: Path) -> str:
    """Hash an artifact file without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "METADATA_TABLES",
    "REPLICATED_TABLES",
    "STATE_HASHED_TABLES",
    "DatabaseDigest",
    "canonical_table_lines",
    "database_digest",
    "file_sha256",
    "logical_state_hash",
    "rebuild_and_check_fts",
    "table_hash",
    "verify_database",
]
