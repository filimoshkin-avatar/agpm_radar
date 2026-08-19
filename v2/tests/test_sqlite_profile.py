"""Exact Stage 1 SQLite build-profile tests."""

from __future__ import annotations

from dataclasses import replace

from packages.storage import (
    REQUIRED_SQLITE_PROFILE,
    assert_sqlite_runtime,
    inspect_sqlite_runtime,
    sqlite_runtime_mismatches,
)


def test_python_links_the_accepted_sqlite_build() -> None:
    observed = inspect_sqlite_runtime()
    assert observed.version == REQUIRED_SQLITE_PROFILE.version
    assert observed.source_id == REQUIRED_SQLITE_PROFILE.source_id
    assert REQUIRED_SQLITE_PROFILE.compile_options <= observed.compile_options
    assert_sqlite_runtime()


def test_runtime_profile_fails_closed_on_version_drift() -> None:
    drifted = replace(REQUIRED_SQLITE_PROFILE, version="0.0.0")
    assert sqlite_runtime_mismatches(drifted) == ("version '0.0.0' != '3.45.1'",)


def test_database_identity_constants_are_pinned() -> None:
    assert REQUIRED_SQLITE_PROFILE.application_id == 1_380_009_010
    assert REQUIRED_SQLITE_PROFILE.user_version == 1
