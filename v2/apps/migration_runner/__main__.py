"""Apply an application migration bundle to one inactive staging database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from packages.deployment.manifest import parse_application_manifest
from packages.deployment.migration import migrate_staging_connection, migration_report_document
from packages.storage.migrations import discover_migrations
from packages.storage.mutation_lock import acquire_mutation_lock, release_mutation_lock
from packages.storage.safe_files import open_regular_file_nofollow, read_regular_file


def main(argv: list[str] | None = None) -> int:
    """Migrate only the explicitly named staging DB after verifying its base state."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-database", type=Path, required=True)
    parser.add_argument("--compatibility-manifest", type=Path, required=True)
    parser.add_argument("--migrations", type=Path, required=True)
    parser.add_argument("--lock-root", type=Path, required=True)
    parser.add_argument("--activated-at", required=True)
    parser.add_argument("--expected-state-hash", required=True)
    arguments = parser.parse_args(argv)
    if (
        not arguments.staging_database.name.endswith(".sqlite")
        or "staging" not in arguments.staging_database.name
    ):
        raise RuntimeError("migration runner accepts only an explicitly named staging SQLite file")
    manifest = parse_application_manifest(read_regular_file(arguments.compatibility_manifest))
    migrations = discover_migrations(arguments.migrations)
    database_descriptor = open_regular_file_nofollow(
        arguments.staging_database,
        expected_mode=0o600,
    )
    before = os.fstat(database_descriptor)
    lock: int | None = None
    try:
        lock = acquire_mutation_lock(arguments.lock_root)
        with sqlite3.connect(arguments.staging_database) as connection:
            report = migrate_staging_connection(
                connection,
                manifest=manifest,
                activated_at=arguments.activated_at,
                migrations=migrations,
            )
    finally:
        if lock is not None:
            release_mutation_lock(lock)
        os.close(database_descriptor)
    after = os.stat(arguments.staging_database, follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_nlink) != (
        after.st_dev,
        after.st_ino,
        after.st_nlink,
    ):
        raise RuntimeError("staging database path changed during migration")
    for suffix in ("-journal", "-shm", "-wal"):
        if Path(str(arguments.staging_database) + suffix).exists():
            raise RuntimeError(f"migration runner left a forbidden SQLite sidecar: {suffix}")
    if report.state_hash != arguments.expected_state_hash:
        raise RuntimeError("migrated staging state differs from the approved base state")
    print(
        json.dumps(
            migration_report_document(report),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
