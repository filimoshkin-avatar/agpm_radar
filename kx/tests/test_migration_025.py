"""Migration 025: a pair looked at and left alone is a row, not a silence."""

from __future__ import annotations

import psycopg
import pytest

from conftest import connect


def test_025_leaves_the_schema_where_it_says_it_does(baseline_dsn: str) -> None:
    """Its own stamp, so the next migration does not make this one red."""
    from conftest import ADOPTED_MIGRATIONS, MIGRATION_025, _apply

    _apply(baseline_dsn, ADOPTED_MIGRATIONS[: ADOPTED_MIGRATIONS.index(MIGRATION_025) + 1])
    with connect(baseline_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM kx.metadata WHERE key = 'schema_version'")
        row = cursor.fetchone()
        assert row is not None
        assert row["value"] == 25


def test_only_none_lands_here(judged_dsn: str) -> None:
    """Everything else is a row in knowledge_links, and two homes would drift."""
    with connect(judged_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) AS body FROM pg_constraint"
            " WHERE conrelid = 'kx.link_judgements'::regclass AND contype = 'c'"
        )
        bodies = " ".join(str(row["body"]) for row in cursor.fetchall())
        assert "'none'" in bodies


def test_a_pair_is_not_judged_against_itself(judged_dsn: str) -> None:
    with (
        connect(judged_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        cursor.execute(
            "INSERT INTO kx.link_judgements (from_id, to_id, judged_by) VALUES (%s, %s, 'test')",
            ("11111111-1111-1111-1111-111111111111",) * 2,
        )


def test_the_same_pair_is_recorded_once(judged_dsn: str) -> None:
    """Re-running has to be free, not additive: that is the whole point."""
    with connect(judged_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) AS total FROM pg_index WHERE indrelid ="
            " 'kx.link_judgements'::regclass AND indisprimary"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["total"] == 1
