"""Migration 027: the rows stage 0b wrote under two rules that were wrong.

A rule corrected in code and left uncorrected in the rows is half a correction.
The owner's queues are built from the rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import ADOPTED_MIGRATIONS, MIGRATION_027, _apply, connect, seed_statement


def _up_to_026(dsn: str) -> None:
    _apply(dsn, ADOPTED_MIGRATIONS[: ADOPTED_MIGRATIONS.index(MIGRATION_027)])


def test_027_leaves_the_schema_where_it_says_it_does(baseline_dsn: str) -> None:
    _apply(baseline_dsn, ADOPTED_MIGRATIONS[: ADOPTED_MIGRATIONS.index(MIGRATION_027) + 1])
    with connect(baseline_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM kx.metadata WHERE key = 'schema_version'")
        row = cursor.fetchone()
        assert row is not None
        assert row["value"] == 27


def test_freshness_is_measured_from_the_document_not_from_the_pass(baseline_dsn: str) -> None:
    """The defect exactly: a statement clocked from the day the job ran.

    6 625 of 13 876 production statements had a `valid_until` one interval after
    `read_at` to the day, because their document carried no published date and
    the arithmetic fell through to `now()`.
    """
    _up_to_026(baseline_dsn)
    undated = seed_statement(
        baseline_dsn,
        url="https://example.com/undated",
        published_on=None,
        first_seen_on="2026-04-16",
        material_kind="forecast",
        # What the pass wrote: one interval from the day it ran, not from the doc.
        valid_until="2027-08-23 00:00:00+00",
        read_at="2026-08-23 00:00:00+00",
    )
    dated = seed_statement(
        baseline_dsn,
        url="https://example.com/dated",
        published_on="2025-11-02",
        first_seen_on="2026-04-16",
        material_kind="forecast",
        valid_until="2026-11-02 00:00:00+00",
        read_at="2026-08-23 00:00:00+00",
    )

    _apply(baseline_dsn, (MIGRATION_027,))

    with connect(baseline_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT valid_for FROM kx.material_kind_freshness WHERE material_kind = 'forecast'"
        )
        interval = cursor.fetchone()
        assert interval is not None
        span: timedelta = interval["valid_for"]  # type: ignore[assignment]

        cursor.execute(
            "SELECT claim_id, valid_until FROM kx.claim_reading WHERE claim_id = ANY(%s)",
            ([undated, dated],),
        )
        moved = {str(row["claim_id"]): row["valid_until"] for row in cursor.fetchall()}

    assert moved[undated] == datetime(2026, 4, 16, tzinfo=UTC) + span, (
        "an undated statement must be clocked from the day the radar first saw it"
    )
    assert moved[dated] == datetime(2025, 11, 2, tzinfo=UTC) + span, (
        "a dated statement was already right and must not move"
    )


def test_a_statement_the_base_threw_out_leaves_no_gap(baseline_dsn: str) -> None:
    """Decision 8's queue is about what the base cannot place, not about refuse."""
    _up_to_026(baseline_dsn)
    rejected = seed_statement(
        baseline_dsn,
        url="https://example.com/rejected",
        published_on="2026-01-01",
        first_seen_on="2026-01-02",
        admission="rejected",
    )
    admitted = seed_statement(
        baseline_dsn,
        url="https://example.com/admitted",
        published_on="2026-01-01",
        first_seen_on="2026-01-02",
        admission="knowledge",
    )
    with connect(baseline_dsn) as connection, connection.cursor() as cursor:
        for claim_id in (rejected, admitted):
            cursor.execute(
                "INSERT INTO kx.claim_gaps (claim_id, missing, noted_by, method)"
                " VALUES (%s, 'нет темы', 'test', 'model')",
                (claim_id,),
            )

    _apply(baseline_dsn, (MIGRATION_027,))

    with connect(baseline_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT claim_id FROM kx.claim_gaps WHERE claim_id = ANY(%s)",
            ([rejected, admitted],),
        )
        left = {str(row["claim_id"]) for row in cursor.fetchall()}
    assert left == {admitted}
