"""Exact SQLite build profile accepted by the Stage 1 contract."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class SQLiteBuildProfile:
    """SQLite runtime properties that must match before any data work begins."""

    version: str
    source_id: str
    compile_options: frozenset[str]
    application_id: int
    user_version: int


REQUIRED_SQLITE_PROFILE: Final = SQLiteBuildProfile(
    version="3.45.1",
    source_id=(
        "2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1"
    ),
    compile_options=frozenset({"ENABLE_FTS5", "THREADSAFE=1"}),
    application_id=1_380_009_010,
    user_version=1,
)


def inspect_sqlite_runtime() -> SQLiteBuildProfile:
    """Inspect only the in-memory SQLite runtime; no database file is opened."""
    with sqlite3.connect(":memory:") as connection:
        source_id = str(connection.execute("SELECT sqlite_source_id()").fetchone()[0])
        compile_options = frozenset(
            str(row[0]) for row in connection.execute("PRAGMA compile_options")
        )
    return SQLiteBuildProfile(
        version=sqlite3.sqlite_version,
        source_id=source_id,
        compile_options=compile_options,
        application_id=REQUIRED_SQLITE_PROFILE.application_id,
        user_version=REQUIRED_SQLITE_PROFILE.user_version,
    )


def sqlite_runtime_mismatches(
    actual: SQLiteBuildProfile | None = None,
    required: SQLiteBuildProfile = REQUIRED_SQLITE_PROFILE,
) -> tuple[str, ...]:
    """Describe deviations from the accepted version/source/options profile."""
    observed = actual or inspect_sqlite_runtime()
    mismatches: list[str] = []
    if observed.version != required.version:
        mismatches.append(f"version {observed.version!r} != {required.version!r}")
    if observed.source_id != required.source_id:
        mismatches.append("source id differs from the accepted Stage 1 build")
    missing_options = sorted(required.compile_options - observed.compile_options)
    if missing_options:
        mismatches.append(f"missing compile options: {', '.join(missing_options)}")
    return tuple(mismatches)


def assert_sqlite_runtime() -> None:
    """Fail closed when Python is not linked to the accepted SQLite build."""
    mismatches = sqlite_runtime_mismatches()
    if mismatches:
        raise RuntimeError("SQLite runtime contract mismatch: " + "; ".join(mismatches))


__all__ = [
    "REQUIRED_SQLITE_PROFILE",
    "SQLiteBuildProfile",
    "assert_sqlite_runtime",
    "inspect_sqlite_runtime",
    "sqlite_runtime_mismatches",
]
