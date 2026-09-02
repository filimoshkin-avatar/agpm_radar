"""Application-owned migration of one inactive Radar V2 staging database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from packages.deployment.manifest import ApplicationManifest, validate_utc_timestamp
from packages.storage.hashing import logical_state_hash, rebuild_and_check_fts, table_hash
from packages.storage.migrations import Migration, apply_migrations
from packages.storage.sqlite_profile import (
    REQUIRED_SQLITE_PROFILE,
    assert_sqlite_runtime,
    inspect_sqlite_runtime,
)
from packages.validation.public_issue import verify_public_database_connection


class ApplicationMigrationError(RuntimeError):
    """An inactive application staging database could not be migrated safely."""


@dataclass(frozen=True, slots=True)
class ApplicationMigrationReport:
    """Stable evidence from one application-owned staging migration."""

    application_release_id: str
    content_release_id: str
    state_hash: str
    schema_sha256: str
    compatibility_sha256: str
    applied_migrations: tuple[str, ...]


def migrations_from_artifact_files(files: dict[str, bytes]) -> tuple[Migration, ...]:
    """Load lexical, checksum-bound SQL migrations from a verified role artifact mapping."""
    prefix = "packages/storage/migrations/"
    selected = sorted(
        (path, content)
        for path, content in files.items()
        if path.startswith(prefix) and path.endswith(".sql")
    )
    if not selected:
        raise ApplicationMigrationError("migration bundle contains no versioned SQL")
    migrations: list[Migration] = []
    seen: set[str] = set()
    for path, content in selected:
        name = PurePosixPath(path).name
        version, separator, _label = name.removesuffix(".sql").partition("_")
        if not separator or not version.isdigit() or version in seen:
            raise ApplicationMigrationError(f"migration filename is invalid or duplicated: {name}")
        try:
            sql = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ApplicationMigrationError(f"migration is not UTF-8: {name}") from error
        lowered = sql.lower()
        if "begin transaction" in lowered or "begin immediate" in lowered or "commit;" in lowered:
            raise ApplicationMigrationError(f"migration controls its own transaction: {name}")
        seen.add(version)
        migrations.append(
            Migration(
                version=version,
                sql=sql,
                checksum=hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(migrations)


def schema_sha256(connection: sqlite3.Connection) -> str:
    """Hash the complete user-visible SQLite schema in a layout-independent order."""
    rows = tuple(
        connection.execute(
            """
            SELECT type, name, tbl_name, coalesce(sql, '')
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name, tbl_name
            """
        )
    )
    content = "".join(
        json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _release_identity(connection: sqlite3.Connection) -> tuple[str, str]:
    row = connection.execute(
        """
        SELECT release_id, after_state_hash
        FROM content_releases
        ORDER BY sequence DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ApplicationMigrationError("staging database has no content release marker")
    return str(row[0]), str(row[1])


def _compatibility_row(manifest: ApplicationManifest, activated_at: str) -> tuple[object, ...]:
    return (
        manifest.application_release_id,
        manifest.schema_version,
        "1.0.0",
        "1.0.0",
        "1.0.0",
        "1.0.0",
        "1.0.0",
        "1.0.0",
        manifest.sqlite_version,
        activated_at,
    )


def _install_compatibility(
    connection: sqlite3.Connection,
    manifest: ApplicationManifest,
    activated_at: str,
) -> None:
    expected = _compatibility_row(manifest, activated_at)
    existing = connection.execute(
        """
        SELECT application_release_id, schema_version, table_contract_version,
               candidate_contract_version, delta_contract_version, result_contract_version,
               gazette_contract_version, public_api_version, sqlite_runtime_version, activated_at
        FROM application_compatibility
        WHERE application_release_id = ?
        """,
        (manifest.application_release_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != expected:
            raise ApplicationMigrationError("application compatibility id already has other data")
        return
    latest = connection.execute(
        "SELECT MAX(activated_at) FROM application_compatibility"
    ).fetchone()[0]
    if latest is not None:
        try:
            current_activated_at = validate_utc_timestamp(latest, "current activatedAt")
        except ValueError as error:
            raise ApplicationMigrationError(
                "current application activation timestamp is invalid"
            ) from error
        if activated_at <= current_activated_at:
            raise ApplicationMigrationError(
                "application activation time does not advance compatibility"
            )
    connection.execute(
        """
        INSERT INTO application_compatibility(
            application_release_id, schema_version, table_contract_version,
            candidate_contract_version, delta_contract_version, result_contract_version,
            gazette_contract_version, public_api_version, sqlite_runtime_version, activated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        expected,
    )


def migrate_staging_connection(
    connection: sqlite3.Connection,
    *,
    manifest: ApplicationManifest,
    activated_at: str,
    migrations: Sequence[Migration],
) -> ApplicationMigrationReport:
    """Apply an approved migration bundle while preserving the logical content release."""
    assert_sqlite_runtime()
    runtime = inspect_sqlite_runtime()
    if (
        runtime.version != manifest.sqlite_version
        or runtime.source_id != manifest.sqlite_source_id
        or tuple(sorted(runtime.compile_options)) != manifest.sqlite_compile_options
    ):
        raise ApplicationMigrationError("migration runtime differs from application manifest")
    try:
        validate_utc_timestamp(activated_at, "activatedAt")
    except ValueError as error:
        raise ApplicationMigrationError("application activation timestamp is invalid") from error
    if activated_at < manifest.created_at:
        raise ApplicationMigrationError("application activation precedes package creation")
    release_id, release_state = _release_identity(connection)
    before_state = logical_state_hash(connection)
    if before_state != release_state:
        raise ApplicationMigrationError("content release marker differs from logical state")
    try:
        applied = apply_migrations(
            connection,
            applied_at=activated_at,
            migrations=tuple(migrations),
        )
        connection.execute("BEGIN IMMEDIATE")
        _install_compatibility(connection, manifest, activated_at)
        # The search index is a projection of pub_search_documents_v1. A migration may
        # redefine that view (0003 did), and the index is outside the logical state, so
        # it is rebuilt here rather than left stale until the next content publication.
        rebuild_and_check_fts(connection)
        connection.commit()
        verify_public_database_connection(connection)
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    after_state = logical_state_hash(connection)
    if after_state != before_state:
        raise ApplicationMigrationError("application migration changed logical content state")
    current_release_id, current_release_state = _release_identity(connection)
    if (current_release_id, current_release_state) != (release_id, release_state):
        raise ApplicationMigrationError("application migration changed content release identity")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != (
        REQUIRED_SQLITE_PROFILE.user_version
    ):
        raise ApplicationMigrationError("migrated database schema version is incompatible")
    if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
        raise ApplicationMigrationError("migrated database integrity_check failed")
    if tuple(connection.execute("PRAGMA foreign_key_check")):
        raise ApplicationMigrationError("migrated database foreign_key_check failed")
    return ApplicationMigrationReport(
        application_release_id=manifest.application_release_id,
        content_release_id=release_id,
        state_hash=after_state,
        schema_sha256=schema_sha256(connection),
        compatibility_sha256=table_hash(connection, "application_compatibility"),
        applied_migrations=applied,
    )


def migration_report_document(report: ApplicationMigrationReport) -> dict[str, object]:
    """Render a stable JSON-compatible operator report."""
    return cast(
        dict[str, object],
        {
            "applicationReleaseId": report.application_release_id,
            "appliedMigrations": list(report.applied_migrations),
            "compatibilitySha256": report.compatibility_sha256,
            "contentReleaseId": report.content_release_id,
            "schemaSha256": report.schema_sha256,
            "stateHash": report.state_hash,
        },
    )


__all__ = [
    "ApplicationMigrationError",
    "ApplicationMigrationReport",
    "migrate_staging_connection",
    "migration_report_document",
    "migrations_from_artifact_files",
    "schema_sha256",
]
