"""Stage 0a, second half: a publication date, or the radar's own, said out loud.

The parser's whole job is to be honest about what the source gave it. Every shape
tested here occurs in `source_materials.published_raw`; the counts beside them are
the production measurement of 2026-08-23 over 7 986 non-empty values.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from conftest import connect
from radar_kx.dates import parse_published, resolve, summarize

FOUND_AT = datetime(2026, 8, 21, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected", "precision"),
    [
        ("2026-08-20", date(2026, 8, 20), "day"),
        ("2026-08-20T00:00:00Z", date(2026, 8, 20), "day"),
        ("2026-08-20 11:04:00", date(2026, 8, 20), "day"),
        ("April 13, 2026", date(2026, 4, 13), "day"),
        ("Sept 3, 2025", date(2025, 9, 3), "day"),
        ("2026-06", date(2026, 6, 1), "month"),
        ("2026-06-??", date(2026, 6, 1), "month"),
        ("2026-06-xx", date(2026, 6, 1), "month"),
        ("2026-06-2026", date(2026, 6, 1), "month"),
        ("2026", date(2026, 1, 1), "year"),
        ("2025-??-??", date(2025, 1, 1), "year"),
        ("2 hours ago", None, "none"),
        ("1 month ago", None, "none"),
        ("2026-02-31", None, "none"),
        ("Farvardin 3, 1405", None, "none"),
        ("", None, "none"),
        (None, None, "none"),
    ],
)
def test_what_the_source_said_is_read_at_the_precision_it_said_it(
    raw: str | None, expected: date | None, precision: str
) -> None:
    assert parse_published(raw) == (expected, precision)


def test_a_month_is_stored_as_its_first_day_but_not_called_one() -> None:
    """Sorting a chronicle is the only thing the first-of-the-month form is for."""
    found, precision = parse_published("2026-06")
    assert found == date(2026, 6, 1)
    assert precision != "day"


def test_the_perimeter_wins_over_the_corpus_row() -> None:
    resolved = resolve(
        document_id="d",
        issue_raw="2026-08-11",
        material_raw="2026-01-01",
        found_at=FOUND_AT,
    )
    assert resolved.published_on == date(2026, 8, 11)
    assert resolved.raw_source == "issue_perimeter"
    assert resolved.shown_kind == "published"


def test_the_corpus_row_answers_when_the_perimeter_is_silent() -> None:
    resolved = resolve(
        document_id="d", issue_raw=None, material_raw="2025-12-24", found_at=FOUND_AT
    )
    assert resolved.raw_source == "source_material"
    assert resolved.shown_on == date(2025, 12, 24)


def test_an_unreadable_date_falls_back_to_the_radar_and_says_so() -> None:
    """The owner's rule: lean on publication where there is one, and name which."""
    resolved = resolve(
        document_id="d", issue_raw=None, material_raw="2 hours ago", found_at=FOUND_AT
    )
    assert resolved.published_on is None
    assert resolved.date_precision == "none"
    assert resolved.shown_on == FOUND_AT.date()
    assert resolved.shown_kind == "first_seen"
    # Kept, so a shape the parser cannot read can still be found and added.
    assert resolved.published_raw == "2 hours ago"


def test_a_document_nobody_dated_still_has_a_day() -> None:
    resolved = resolve(document_id="d", issue_raw=None, material_raw=None, found_at=FOUND_AT)
    assert resolved.shown_kind == "first_seen"
    assert resolved.raw_source == "none"


def test_the_summary_names_what_it_could_not_read() -> None:
    report = summarize(
        [
            resolve(document_id="a", issue_raw="2026-08-01", material_raw=None, found_at=FOUND_AT),
            resolve(document_id="b", issue_raw="2026-08", material_raw=None, found_at=FOUND_AT),
            resolve(document_id="c", issue_raw="3 days ago", material_raw=None, found_at=FOUND_AT),
        ]
    )
    assert report["documents"] == 3
    assert report["byShownDate"] == {"published": 2, "first_seen": 1}
    assert report["byPrecision"]["month"] == 1
    assert report["unreadable"] == {"3 days ago": 1}
    assert report["publishedRange"]["earliest"] == "2026-08-01"


# ---------------------------------------------------------------------------
# The migration the table needs (023), on a scratch database
# ---------------------------------------------------------------------------


def test_023_leaves_the_schema_where_it_says_it_does(baseline_dsn: str) -> None:
    """023's own stamp, checked on 023 rather than on the newest schema.

    `migrated_dsn` is whatever production runs, so asserting a number on it goes
    red the moment the next migration lands, blaming this one for a line it does
    not own.
    """
    from conftest import ADOPTED_MIGRATIONS, MIGRATION_023, _apply

    _apply(baseline_dsn, ADOPTED_MIGRATIONS[: ADOPTED_MIGRATIONS.index(MIGRATION_023) + 1])
    with connect(baseline_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM kx.metadata WHERE key = 'schema_version'")
        row = cursor.fetchone()
        assert row is not None
        assert row["value"] == 23


def test_the_table_refuses_a_shown_date_that_contradicts_its_label(dated_dsn: str) -> None:
    with connect(dated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)",
            ("f" * 64, "https://example.org/a"),
        )
        # A publication date on screen has to be one that was actually parsed.
        with pytest.raises(Exception, match="what_is_shown_is_what_was_found"):
            cursor.execute(
                "INSERT INTO kx.document_dates (document_id, raw_source, date_precision,"
                " shown_on, shown_kind) VALUES (%s, 'none', 'none', %s, 'published')",
                ("f" * 64, date(2026, 8, 1)),
            )


def test_a_precision_without_a_date_is_refused(dated_dsn: str) -> None:
    with connect(dated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)",
            ("e" * 64, "https://example.org/b"),
        )
        with pytest.raises(Exception, match="a_parsed_date_names_its_precision"):
            cursor.execute(
                "INSERT INTO kx.document_dates (document_id, raw_source, published_on,"
                " date_precision, shown_on, shown_kind)"
                " VALUES (%s, 'source_material', NULL, 'day', %s, 'first_seen')",
                ("e" * 64, date(2026, 8, 1)),
            )


def test_a_resolved_row_stores_and_replaces(dated_dsn: str) -> None:
    with connect(dated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.documents (document_id, canonical_url) VALUES (%s, %s)",
            ("d" * 64, "https://example.org/c"),
        )
        for published, kind in ((date(2026, 8, 4), "published"), (date(2026, 8, 5), "published")):
            cursor.execute(
                """
                INSERT INTO kx.document_dates (document_id, published_raw, raw_source,
                    published_on, date_precision, shown_on, shown_kind)
                VALUES (%s, %s, 'issue_perimeter', %s, 'day', %s, %s)
                ON CONFLICT (document_id) DO UPDATE SET
                    published_on = EXCLUDED.published_on, shown_on = EXCLUDED.shown_on
                """,
                ("d" * 64, published.isoformat(), published, published, kind),
            )
        cursor.execute("SELECT count(*) AS rows, max(shown_on) AS shown FROM kx.document_dates")
        row = cursor.fetchone()
        assert row is not None
        assert row["rows"] == 1
        assert row["shown"] == date(2026, 8, 5)
