"""Pinned, published-only SQLite access for the Radar V2 public API."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeVar

from packages.publisher.local_simulation import ActivePointer, read_active_pointer
from packages.storage.hashing import logical_state_hash
from packages.storage.safe_files import open_regular_file_nofollow, read_regular_file
from packages.storage.sqlite_profile import REQUIRED_SQLITE_PROFILE
from packages.validation.public_issue import verify_public_database_connection

PUBLIC_READ_OBJECTS: Final = frozenset(
    {
        "pub_gazettes_v1",
        "pub_gazette_assets_v1",
        "pub_health_v1",
        "pub_issue_analysis_v1",
        "pub_issue_materials_v1",
        "pub_issues_v1",
        "pub_material_analysis_v1",
        "pub_material_quality_v1",
        "pub_material_rubrics_v1",
        "pub_search_documents_v1",
        "pub_stats_v1",
        "published_materials_fts",
    }
)
_ACTIVE_POINTER_MODE: Final = 0o600
_DATABASE_MODE: Final = 0o600
_LOAD_ATTEMPTS: Final = 3
_PUBLIC_FUNCTIONS: Final = frozenset(
    {"coalesce", "count", "date", "lower", "max", "min", "substr", "sum"}
)
_T = TypeVar("_T")


class PublicDatabaseError(RuntimeError):
    """The active public database cannot be proven safe and current."""


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    """Safe active markers exposed by health and reload tests."""

    release_id: str
    schema_version: int
    state_hash: str


@dataclass(slots=True)
class _OpenedDatabase:
    descriptor: int
    connection: sqlite3.Connection
    pointer_bytes: bytes
    identity: DatabaseIdentity

    def close(self) -> None:
        self.connection.close()
        os.close(self.descriptor)


def _signature(descriptor: int) -> tuple[int, int, int, int, int, int, int]:
    metadata = os.fstat(descriptor)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _public_authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    _database: str | None,
    source: str | None,
) -> int:
    """Allow SELECTs whose reads originate only from frozen public views."""
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        function_name = (arg2 or arg1 or "").lower()
        return sqlite3.SQLITE_OK if function_name in _PUBLIC_FUNCTIONS else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and (
        arg1 in PUBLIC_READ_OBJECTS or source in PUBLIC_READ_OBJECTS
    ):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _connect_pointer(pointer: ActivePointer, pointer_bytes: bytes) -> _OpenedDatabase:
    descriptor = open_regular_file_nofollow(pointer.database_path, expected_mode=_DATABASE_MODE)
    before = _signature(descriptor)
    uri = f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except BaseException:
        os.close(descriptor)
        raise
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 2000")
        verify_public_database_connection(connection)
        required_objects = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type IN ('view', 'table')"
            )
        }
        missing = sorted(PUBLIC_READ_OBJECTS - required_objects)
        if missing:
            raise PublicDatabaseError(
                "active database is missing published API projections: " + ", ".join(missing)
            )
        release = connection.execute(
            """
            SELECT release_id, after_state_hash
            FROM content_releases
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
        if release is None:
            raise PublicDatabaseError("active database has no content release marker")
        state_hash = logical_state_hash(connection)
        if (
            str(release[0]) != pointer.release_id
            or str(release[1]) != pointer.state_hash
            or state_hash != pointer.state_hash
        ):
            raise PublicDatabaseError("active pointer and pinned database markers differ")
        source_count = int(
            connection.execute("SELECT COUNT(*) FROM pub_search_documents_v1").fetchone()[0]
        )
        fts_count = int(
            connection.execute("SELECT COUNT(*) FROM published_materials_fts").fetchone()[0]
        )
        if source_count != fts_count:
            raise PublicDatabaseError("published search projection parity failed")
        if _signature(descriptor) != before:
            raise PublicDatabaseError("active database changed during verification")
        connection.set_authorizer(_public_authorizer)
        return _OpenedDatabase(
            descriptor=descriptor,
            connection=connection,
            pointer_bytes=pointer_bytes,
            identity=DatabaseIdentity(
                release_id=pointer.release_id,
                schema_version=REQUIRED_SQLITE_PROFILE.user_version,
                state_hash=state_hash,
            ),
        )
    except BaseException:
        connection.close()
        os.close(descriptor)
        raise


class ActiveDatabaseManager:
    """Serialize public queries and reopen after an atomic content-pointer switch."""

    def __init__(self, active_root: Path) -> None:
        self._active_root = active_root
        self._lock = threading.RLock()
        self._opened: _OpenedDatabase | None = None

    def _pointer_bytes(self) -> bytes:
        return read_regular_file(
            self._active_root / "active.json",
            expected_mode=_ACTIVE_POINTER_MODE,
        )

    def _ensure_opened(self) -> _OpenedDatabase:
        observed = self._pointer_bytes()
        if self._opened is not None and self._opened.pointer_bytes == observed:
            return self._opened
        last_error: BaseException | None = None
        for _attempt in range(_LOAD_ATTEMPTS):
            before = self._pointer_bytes()
            try:
                pointer = read_active_pointer(self._active_root)
                candidate = _connect_pointer(pointer, before)
            except BaseException as error:
                last_error = error
                continue
            after = self._pointer_bytes()
            if before != after:
                candidate.close()
                last_error = PublicDatabaseError("active pointer changed while reopening")
                continue
            previous = self._opened
            self._opened = candidate
            if previous is not None:
                previous.close()
            return candidate
        raise PublicDatabaseError("cannot open a stable active public database") from last_error

    def execute(self, operation: Callable[[sqlite3.Connection, DatabaseIdentity], _T]) -> _T:
        """Run one bounded public operation against a stable serialized connection."""
        with self._lock:
            opened = self._ensure_opened()
            return operation(opened.connection, opened.identity)

    def identity(self) -> DatabaseIdentity:
        """Return current safe markers, reopening first when the pointer changed."""
        return self.execute(lambda _connection, identity: identity)

    def close(self) -> None:
        """Close the pinned descriptor and connection."""
        with self._lock:
            if self._opened is not None:
                self._opened.close()
                self._opened = None


__all__ = [
    "PUBLIC_READ_OBJECTS",
    "ActiveDatabaseManager",
    "DatabaseIdentity",
    "PublicDatabaseError",
]
