"""Deterministic SQLite storage and accepted runtime preflight."""

from packages.storage.migrations import (
    EMPTY_SHA256,
    MigrationError,
    apply_migrations,
    create_database,
    discover_migrations,
)
from packages.storage.sqlite_profile import (
    REQUIRED_SQLITE_PROFILE,
    SQLiteBuildProfile,
    assert_sqlite_runtime,
    inspect_sqlite_runtime,
    sqlite_runtime_mismatches,
)

COMPONENT_NAME = "storage"
COMPONENT_STATUS = "stage-5-implemented"

__all__ = [
    "COMPONENT_NAME",
    "COMPONENT_STATUS",
    "EMPTY_SHA256",
    "REQUIRED_SQLITE_PROFILE",
    "MigrationError",
    "SQLiteBuildProfile",
    "apply_migrations",
    "assert_sqlite_runtime",
    "create_database",
    "discover_migrations",
    "inspect_sqlite_runtime",
    "sqlite_runtime_mismatches",
]
