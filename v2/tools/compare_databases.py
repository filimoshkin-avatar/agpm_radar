"""Compare two replicated Radar V2 SQLite databases by canonical logical state."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Final
from urllib.parse import quote

from packages.storage.hashing import database_digest
from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE

FTS_PROJECTION_QUERY: Final = (
    "SELECT document_id, issue_id, issue_date, material_id, title, summary, agpm_takeaway, "
    "source_name, url FROM pub_search_documents_v1 ORDER BY issue_date, issue_id, material_id"
)
FTS_INDEX_QUERY: Final = (
    "SELECT document_id, issue_id, issue_date, material_id, title, summary, agpm_takeaway, "
    "source_name, url FROM published_materials_fts ORDER BY issue_date, issue_id, material_id"
)


def _verify_fts_projection(connection: sqlite3.Connection, path: Path) -> str:
    projected = tuple(connection.execute(FTS_PROJECTION_QUERY))
    indexed = tuple(connection.execute(FTS_INDEX_QUERY))
    if projected != indexed:
        raise RuntimeError(f"FTS projection content mismatch: {path}")
    digest = hashlib.sha256()
    for row in indexed:
        digest.update(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        )
    with sqlite3.connect(":memory:") as writable_copy:
        connection.backup(writable_copy)
        writable_copy.execute(
            "INSERT INTO published_materials_fts(published_materials_fts) "
            "VALUES ('integrity-check')"
        )
    return digest.hexdigest()


def _open_read_only(path: Path) -> tuple[sqlite3.Connection, str]:
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("PRAGMA busy_timeout = 5000")
    if (
        int(connection.execute("PRAGMA application_id").fetchone()[0])
        != REQUIRED_SQLITE_PROFILE.application_id
    ):
        raise RuntimeError(f"wrong application_id: {path}")
    if (
        int(connection.execute("PRAGMA user_version").fetchone()[0])
        != REQUIRED_SQLITE_PROFILE.user_version
    ):
        raise RuntimeError(f"wrong user_version: {path}")
    if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
        raise RuntimeError(f"integrity_check failed: {path}")
    if tuple(connection.execute("PRAGMA foreign_key_check")):
        raise RuntimeError(f"foreign_key_check failed: {path}")
    try:
        fts_projection_hash = _verify_fts_projection(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection, fts_projection_hash


def compare(source_path: Path, replica_path: Path) -> tuple[bool, dict[str, object]]:
    source, source_fts_hash = _open_read_only(source_path)
    try:
        replica, replica_fts_hash = _open_read_only(replica_path)
        try:
            source_digest = database_digest(source)
            replica_digest = database_digest(replica)
        finally:
            replica.close()
    finally:
        source.close()
    state_equal = source_digest.state_hash == replica_digest.state_hash
    count_mismatches = {
        table: [source_digest.table_counts[table], replica_digest.table_counts[table]]
        for table in source_digest.table_counts
        if source_digest.table_counts[table] != replica_digest.table_counts[table]
    }
    hash_mismatches = {
        table: [source_digest.table_hashes[table], replica_digest.table_hashes[table]]
        for table in source_digest.table_hashes
        if source_digest.table_hashes[table] != replica_digest.table_hashes[table]
    }
    fts_equal = source_fts_hash == replica_fts_hash
    equivalent = state_equal and fts_equal and not count_mismatches and not hash_mismatches
    return equivalent, {
        "equivalent": equivalent,
        "source": dataclasses.asdict(source_digest),
        "replica": dataclasses.asdict(replica_digest),
        "count_mismatches": count_mismatches,
        "table_hash_mismatches": hash_mismatches,
        "fts_projection_hashes": {
            "source": source_fts_hash,
            "replica": replica_fts_hash,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("replica", type=Path)
    args = parser.parse_args()
    equivalent, report = compare(args.source, args.replica)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if equivalent else 1


if __name__ == "__main__":
    raise SystemExit(main())
