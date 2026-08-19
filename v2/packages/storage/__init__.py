"""Storage boundary and accepted SQLite runtime preflight."""

from packages.storage.sqlite_profile import (
    REQUIRED_SQLITE_PROFILE,
    SQLiteBuildProfile,
    assert_sqlite_runtime,
    inspect_sqlite_runtime,
    sqlite_runtime_mismatches,
)

COMPONENT_NAME = "storage"
COMPONENT_STATUS = "stage-2-skeleton"

__all__ = [
    "COMPONENT_NAME",
    "COMPONENT_STATUS",
    "REQUIRED_SQLITE_PROFILE",
    "SQLiteBuildProfile",
    "assert_sqlite_runtime",
    "inspect_sqlite_runtime",
    "sqlite_runtime_mismatches",
]
