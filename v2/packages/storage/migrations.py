"""Deterministic, checksum-pinned SQLite schema migrations."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE, assert_sqlite_runtime

MIGRATIONS_DIRECTORY: Final = Path(__file__).with_name("migrations")
EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()


class MigrationError(RuntimeError):
    """The database cannot safely advance to the requested schema."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable SQL migration loaded from the application artifact."""

    version: str
    sql: str
    checksum: str


def discover_migrations(directory: Path = MIGRATIONS_DIRECTORY) -> tuple[Migration, ...]:
    """Load migrations in lexical version order and reject ambiguous names."""
    paths = tuple(sorted(directory.glob("*.sql")))
    if not paths:
        raise MigrationError("no schema migrations found")
    migrations: list[Migration] = []
    seen: set[str] = set()
    for path in paths:
        version, separator, _name = path.stem.partition("_")
        if not separator or not version.isdigit() or version in seen:
            raise MigrationError(f"invalid or duplicate migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        lowered = sql.lower()
        if "begin transaction" in lowered or "begin immediate" in lowered or "commit;" in lowered:
            raise MigrationError(f"migration controls its own transaction: {path.name}")
        seen.add(version)
        migrations.append(
            Migration(
                version=version,
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(migrations)


def configure_staging_connection(connection: sqlite3.Connection) -> None:
    """Apply the exact writer pragmas required by the Stage 1 contract."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA secure_delete = ON")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("PRAGMA busy_timeout = 5000")


def _installed_migrations(connection: sqlite3.Connection) -> dict[str, str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return {}
    return {
        str(row[0]): str(row[1])
        for row in connection.execute("SELECT version, checksum FROM schema_migrations")
    }


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    applied_at: str,
    migrations: tuple[Migration, ...] | None = None,
) -> tuple[str, ...]:
    """Apply pending migrations atomically with explicit deterministic metadata."""
    assert_sqlite_runtime()
    configure_staging_connection(connection)
    selected = migrations or discover_migrations()
    installed = _installed_migrations(connection)
    known = {migration.version for migration in selected}
    unknown = sorted(set(installed) - known)
    if unknown:
        raise MigrationError(f"database has unknown migration versions: {', '.join(unknown)}")
    for migration in selected:
        prior = installed.get(migration.version)
        if prior is not None and prior != migration.checksum:
            raise MigrationError(f"migration checksum mismatch: {migration.version}")

    applied: list[str] = []
    connection.execute("BEGIN IMMEDIATE")
    try:
        for migration in selected:
            if migration.version in installed:
                continue
            for statement in _complete_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.checksum, applied_at),
            )
            applied.append(migration.version)
        connection.execute(f"PRAGMA application_id = {REQUIRED_SQLITE_PROFILE.application_id}")
        connection.execute(f"PRAGMA user_version = {REQUIRED_SQLITE_PROFILE.user_version}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return tuple(applied)


def _complete_statements(script: str) -> tuple[str, ...]:
    """Split trusted SQL without allowing sqlite3.executescript to auto-commit."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise MigrationError("migration ends with an incomplete SQL statement")
    return tuple(statements)


def create_database(path: Path, *, applied_at: str) -> None:
    """Create a new contract database; never overwrite an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        reservation = os.open(path, flags, 0o600)
    except FileExistsError:
        raise MigrationError(f"database already exists: {path}") from None
    except OSError as error:
        raise MigrationError(f"cannot safely reserve database path: {path}: {error}") from error
    reserved = os.fstat(reservation)
    try:
        with sqlite3.connect(path) as connection:
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (reserved.st_dev, reserved.st_ino):
                raise MigrationError(f"database path changed after reservation: {path}")
            if stat.S_IMODE(current.st_mode) != 0o600:
                raise MigrationError(f"database permissions are not 0600: {path}")
            apply_migrations(connection, applied_at=applied_at)
    except BaseException:
        try:
            current = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino):
                path.unlink()
        raise
    finally:
        os.close(reservation)


__all__ = [
    "EMPTY_SHA256",
    "MIGRATIONS_DIRECTORY",
    "Migration",
    "MigrationError",
    "apply_migrations",
    "configure_staging_connection",
    "create_database",
    "discover_migrations",
]
