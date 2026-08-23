"""Scratch-database fixtures for the tests that exercise real SQL.

Most of the suite is pure and runs anywhere. The migration and provenance tests
need a PostgreSQL that can create a database and load ``pgcrypto``, ``pg_trgm``,
``unaccent`` and ``vector``, which is not a given on every machine - so they skip
unless ``RADAR_KX_TEST_ADMIN_DSN`` names one. ``scripts/verify_migrations.sh``
sets it up and runs them; ``scripts/verify.sh`` stays green without a database.

Nothing here can reach production: the DSN is supplied by the developer, the
database is created and dropped by the fixture, and the name is fixed to a
``radar_kx_test_`` prefix.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import dict_row

SQL_DIRECTORY = Path(__file__).resolve().parents[1] / "sql"
BASELINE_MIGRATIONS = (
    "000_extensions.sql",
    "001_initial.sql",
    "002_issue_perimeter.sql",
)
#: The migrations the deployed release requires, in order. Kept in step with
#: ``SCHEMA_VERSION`` so the fixture builds the database the code says it needs.
MIGRATION_003 = "003_provenance_and_publication.sql"
MIGRATION_004 = "004_publication_caveat.sql"
MIGRATION_005 = "005_source_independence.sql"
MIGRATION_006 = "006_duplicate_containment.sql"
MIGRATION_007 = "007_extraction.sql"
MIGRATION_008 = "008_concepts.sql"
MIGRATION_009 = "009_run_retry.sql"
MIGRATION_010 = "010_acquisition.sql"
MIGRATION_011 = "011_transient_exhausted.sql"
MIGRATION_012 = "012_candidate_ideas.sql"
MIGRATION_013 = "013_publication.sql"
MIGRATION_014 = "014_graph.sql"
MIGRATION_015 = "015_knowledge_release.sql"
MIGRATION_016 = "016_editorial_decisions.sql"
MIGRATION_017 = "017_research.sql"
MIGRATION_018 = "018_editorial_object_kinds.sql"
MIGRATION_019 = "019_topic_skeleton.sql"
MIGRATION_020 = "020_text_embeddings.sql"
MIGRATION_021 = "021_binding_method_votes.sql"
ADOPTED_MIGRATIONS = (
    MIGRATION_003,
    MIGRATION_004,
    MIGRATION_005,
    MIGRATION_006,
    MIGRATION_007,
    MIGRATION_008,
    MIGRATION_009,
    MIGRATION_010,
    MIGRATION_011,
    MIGRATION_012,
    MIGRATION_013,
    MIGRATION_014,
    MIGRATION_015,
    MIGRATION_016,
    MIGRATION_017,
    MIGRATION_018,
    MIGRATION_019,
    MIGRATION_020,
    MIGRATION_021,
)

#: The hand-applied production hotfix of 2026-08-22 (defect D1): operator_artifact
#: was added straight to the running database, so the repository schema and
#: production diverged. Migration 003 has to land on both.
PRODUCTION_DRIFT = """
SET search_path = kx, public;
ALTER TABLE fetch_attempts DROP CONSTRAINT fetch_attempts_source_kind_check;
ALTER TABLE fetch_attempts ADD CONSTRAINT fetch_attempts_source_kind_check CHECK (
    source_kind = ANY (ARRAY['network', 'network_robots_override', 'legacy_snapshot',
                             'legacy_truncated', 'operator_artifact'])
);
ALTER TABLE document_versions DROP CONSTRAINT document_versions_source_kind_check;
ALTER TABLE document_versions ADD CONSTRAINT document_versions_source_kind_check CHECK (
    source_kind = ANY (ARRAY['network', 'network_robots_override', 'legacy_snapshot',
                             'legacy_truncated', 'operator_artifact'])
);
"""


def admin_dsn() -> str:
    dsn = os.environ.get("RADAR_KX_TEST_ADMIN_DSN")
    if not dsn:
        pytest.skip("set RADAR_KX_TEST_ADMIN_DSN to run the SQL-backed tests")
    return dsn


def _connect(dsn: str) -> Connection[dict[str, object]]:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


def _apply(dsn: str, names: tuple[str, ...]) -> None:
    with _connect(dsn) as connection, connection.cursor() as cursor:
        for name in names:
            cursor.execute((SQL_DIRECTORY / name).read_text(encoding="utf-8"))


def _database_dsn(admin: str, name: str) -> str:
    parts = [item for item in admin.split() if not item.startswith("dbname=")]
    parts.append(f"dbname={name}")
    return " ".join(parts)


def make_database(admin: str, name: str, *, drifted: bool) -> str:
    if not name.startswith("radar_kx_test_"):
        raise ValueError("scratch database names must start with radar_kx_test_")
    with _connect(admin) as connection, connection.cursor() as cursor:
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = 'radar_kx'")
        if cursor.fetchone() is None:
            cursor.execute("CREATE ROLE radar_kx LOGIN")
        cursor.execute(f'CREATE DATABASE "{name}"')
    dsn = _database_dsn(admin, name)
    _apply(dsn, BASELINE_MIGRATIONS)
    if drifted:
        with _connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(PRODUCTION_DRIFT)
    return dsn


def drop_database(admin: str, name: str) -> None:
    with _connect(admin) as connection, connection.cursor() as cursor:
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def baseline_dsn() -> Iterator[str]:
    """A database at schema 2, exactly as the repository describes it."""
    admin = admin_dsn()
    name = "radar_kx_test_baseline"
    yield make_database(admin, name, drifted=False)
    drop_database(admin, name)


@pytest.fixture
def drifted_dsn() -> Iterator[str]:
    """A database at schema 2 plus the hand-applied production hotfix."""
    admin = admin_dsn()
    name = "radar_kx_test_drifted"
    yield make_database(admin, name, drifted=True)
    drop_database(admin, name)


@pytest.fixture
def schema3_dsn(baseline_dsn: str) -> str:
    """A database at schema 3 - what production looked like before 004."""
    _apply(baseline_dsn, (MIGRATION_003,))
    return baseline_dsn


@pytest.fixture
def caveat_dsn(migrated_dsn: str) -> str:
    """Alias kept for the tests written against 004 specifically."""
    return migrated_dsn


@pytest.fixture
def migrated_dsn(baseline_dsn: str) -> str:
    """A database at schema 21 - the version the deployed release requires."""
    _apply(baseline_dsn, ADOPTED_MIGRATIONS)
    return baseline_dsn


def apply_migration_003(dsn: str) -> None:
    _apply(dsn, (MIGRATION_003,))


def apply_adopted_migrations(dsn: str) -> None:
    _apply(dsn, ADOPTED_MIGRATIONS)


def connect(dsn: str) -> Connection[dict[str, object]]:
    return _connect(dsn)


def one(cursor: psycopg.Cursor[dict[str, object]]) -> dict[str, object]:
    """The single row a query was written to return, or a failure that says so."""
    row = cursor.fetchone()
    assert row is not None, "expected exactly one row"
    return row
