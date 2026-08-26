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
MIGRATION_022 = "022_knowledge_units.sql"
MIGRATION_023 = "023_document_dates.sql"
MIGRATION_024 = "024_agent_surface.sql"
MIGRATION_025 = "025_link_judgements.sql"
MIGRATION_026 = "026_agent_least_privilege.sql"
MIGRATION_027 = "027_reading_repairs.sql"
MIGRATION_028 = "028_topic_counts.sql"
MIGRATION_029 = "029_graph_holds_knowledge.sql"
MIGRATION_030 = "030_agent_sees_entities.sql"
MIGRATION_031 = "031_access_keys.sql"
MIGRATION_032 = "032_statement_trail.sql"
MIGRATION_033 = "033_chain_passes.sql"
MIGRATION_034 = "034_chain_passes_are_writable.sql"
#: Written and verified, awaiting the owner's decision to apply it. Deliberately
#: outside ADOPTED_MIGRATIONS: `SCHEMA_VERSION` is bumped when a migration reaches
#: production, not when it is written, and `require_schema` is a hard equality -
#: a fixture at 35 would refuse every `Database` the rest of the suite builds.
MIGRATION_035 = "035_an_answer_may_follow_a_refusal.sql"
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
    MIGRATION_022,
    MIGRATION_023,
    MIGRATION_024,
    MIGRATION_025,
    MIGRATION_026,
    MIGRATION_027,
    MIGRATION_028,
    MIGRATION_029,
    MIGRATION_030,
    MIGRATION_031,
    MIGRATION_032,
    MIGRATION_033,
    MIGRATION_034,
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
    """A database at schema 34 - the version the deployed release requires."""
    _apply(baseline_dsn, ADOPTED_MIGRATIONS)
    return baseline_dsn


@pytest.fixture
def superseding_dsn(migrated_dsn: str) -> str:
    """A database at schema 35: everything adopted, plus the migration on review."""
    _apply(migrated_dsn, (MIGRATION_035,))
    return migrated_dsn


@pytest.fixture
def dated_dsn(migrated_dsn: str) -> str:
    """Alias kept for the tests written against 023 specifically."""
    return migrated_dsn


@pytest.fixture
def agent_dsn(migrated_dsn: str) -> str:
    """Alias kept for the tests written against 024 specifically."""
    return migrated_dsn


@pytest.fixture
def judged_dsn(migrated_dsn: str) -> str:
    """Alias kept for the tests written against 025 specifically."""
    return migrated_dsn


@pytest.fixture
def least_privilege_dsn(migrated_dsn: str) -> str:
    """Alias kept for the tests written against 026 specifically."""
    return migrated_dsn


@pytest.fixture
def repaired_dsn(migrated_dsn: str) -> str:
    """Alias kept for the tests written against 027 and 028 specifically."""
    return migrated_dsn


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


def seed_statement(
    dsn: str,
    *,
    url: str,
    published_on: str | None,
    first_seen_on: str,
    material_kind: str = "forecast",
    admission: str = "knowledge",
    valid_until: str | None = None,
    read_at: str | None = None,
) -> str:
    """One statement with the whole chain under it, for the repair tests.

    documents -> raw_blobs -> document_versions -> processing_runs -> claims,
    plus the reading and the document's dates. Returns the claim id.

    `shown_on`/`shown_kind` are derived, never passed: migration 023's
    `what_is_shown_is_what_was_found` requires that a published date is the one
    shown, and that a first-seen date carries no precision.
    """
    shown_kind = "published" if published_on else "first_seen"
    shown_on = published_on or first_seen_on
    import hashlib
    import uuid

    document = hashlib.sha256(url.encode()).hexdigest()
    raw = hashlib.sha256(url.encode() + b"body").hexdigest()
    text_hash = hashlib.sha256(url.encode() + b"text").hexdigest()
    version = hashlib.sha256(f"{document}\0{raw}\0{text_hash}".encode()).hexdigest()
    claim_id = str(uuid.uuid4())
    with _connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            (document, url),
        )
        cursor.execute(
            "INSERT INTO kx.raw_blobs (raw_sha256, compression, raw_bytes, stored_bytes, content)"
            " VALUES (%s, 'gzip', 4, 4, %s) ON CONFLICT DO NOTHING",
            (raw, b"body"),
        )
        cursor.execute(
            """
            INSERT INTO kx.document_versions (
                version_id, document_id, raw_sha256, source_kind, canonical_text,
                canonical_text_sha256, title, language, parser_name, parser_version,
                parser_config_sha256, quality, is_complete, fetched_at
            ) VALUES (%s, %s, %s, 'network', 'text', %s, '', 'ru', 'radar-kx',
                      'canonical-v4', %s, 'trafilatura', true, %s)
            ON CONFLICT DO NOTHING
            """,
            (version, document, raw, text_hash, text_hash, f"{shown_on} 00:00:00+00"),
        )
        cursor.execute(
            """
            INSERT INTO kx.processing_runs (
                version_id, processor, processor_version, parameters_sha256, status
            ) VALUES (%s, 'test', '1', %s, 'succeeded')
            ON CONFLICT DO NOTHING RETURNING run_id
            """,
            (version, text_hash),
        )
        found = cursor.fetchone()
        run_id = found["run_id"] if found else None
        if run_id is None:
            cursor.execute(
                "SELECT run_id FROM kx.processing_runs WHERE version_id = %s LIMIT 1",
                (version,),
            )
            run_id = one(cursor)["run_id"]
        cursor.execute(
            """
            INSERT INTO kx.claims (
                claim_id, version_id, processing_run_id, claim_kind, predicate,
                object_text, normalized_text
            ) VALUES (%s, %s, %s, 'asserted', 'says', 'something', 'something')
            """,
            (claim_id, version, run_id),
        )
        cursor.execute(
            """
            INSERT INTO kx.document_dates (
                document_id, published_raw, raw_source, published_on, date_precision,
                shown_on, shown_kind
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                document,
                published_on,
                "source_material" if published_on else "none",
                published_on,
                "day" if published_on else "none",
                shown_on,
                shown_kind,
            ),
        )
        cursor.execute(
            """
            INSERT INTO kx.claim_reading (
                claim_id, material_kind, primary_source, is_retelling, admission,
                valid_until, read_by, method, read_at
            ) VALUES (%s, %s, '', false, %s, %s, 'test', 'model', coalesce(%s, clock_timestamp()))
            """,
            (claim_id, material_kind, admission, valid_until, read_at),
        )
    return claim_id
