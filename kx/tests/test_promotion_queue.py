"""Decision 6's queue: what counts as an independent confirmation.

The judge is shown an ordered pair and answers "the second confirms the first",
so its verdict reads as directional. But which statement is shown first is
decided by `source.claim_id < other.claim_id` - a comparison of two uuids. A
query that reads that order is counting a coin flip.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from conftest import connect, seed_statement
from radar_kx.config import Settings
from radar_kx.database import Database


def _settings(dsn: str) -> Settings:
    base = Settings.from_environment()
    return Settings(
        **{
            **{field: getattr(base, field) for field in Settings.__dataclass_fields__},
            "dsn": dsn,
            "min_free_bytes": 1024,
            "capacity_path": str(Path(__file__).resolve().parent),
        }
    )


def _make_it_a_statement(dsn: str, claim_id: str, *, status: str = "observed_signal") -> None:
    """The evidence row and the birth status `agent.statement` and the queue need."""
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_id FROM kx.claims WHERE claim_id = %s", (claim_id,))
        found = cursor.fetchone()
        assert found is not None
        # `validate_exact_claim_evidence` re-cuts the span out of `canonical_text`
        # and refuses anything that does not reproduce.
        quote = "text"
        cursor.execute(
            """
            INSERT INTO kx.claim_evidence (
                claim_id, version_id, char_start, char_end, quote_text, quote_sha256,
                match_status
            ) VALUES (%s, %s, 0, 4, %s, %s, 'exact')
            """,
            (claim_id, found["version_id"], quote, hashlib.sha256(quote.encode()).hexdigest()),
        )
        cursor.execute(
            "INSERT INTO kx.knowledge_status (unit_kind, unit_id, status, method, set_by)"
            " VALUES ('claim', %s, %s, 'rule', 'test')",
            (claim_id, status),
        )


def test_a_confirmation_counts_from_whichever_end_the_coin_landed(migrated_dsn: str) -> None:
    """Two confirmations, one on each side of the uuid ordering, and the floor is two.

    Counting one side only, the statement has one confirmation and never reaches
    the owner. On production this is not a corner case: the `to` side alone
    cleared the floor for 682 statements and the `from` side for 674, and only
    113 were in both.
    """
    subject = seed_statement(
        migrated_dsn,
        url="https://example.com/subject",
        published_on="2026-01-01",
        first_seen_on="2026-01-02",
    )
    first = seed_statement(
        migrated_dsn,
        url="https://one.example.com/a",
        published_on="2026-01-01",
        first_seen_on="2026-01-02",
    )
    second = seed_statement(
        migrated_dsn,
        url="https://two.example.com/b",
        published_on="2026-01-01",
        first_seen_on="2026-01-02",
    )
    for claim_id in (subject, first, second):
        _make_it_a_statement(migrated_dsn, claim_id)

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        # Exactly what the pipeline writes: the pair in uuid order, whichever way
        # round that puts the statement under review.
        for other in (first, second):
            low, high = sorted((subject, other))
            cursor.execute(
                """
                INSERT INTO kx.knowledge_links
                    (from_kind, from_id, to_kind, to_id, link_type, created_by, method)
                VALUES ('claim', %s, 'claim', %s, 'supports', 'test', 'model')
                """,
                (low, high),
            )

    database = Database(_settings(migrated_dsn))
    total, rows = database.promotion_candidates(limit=25)
    offered = {str(row["claim_id"]): row for row in rows}

    assert subject in offered, (
        "two independent sources confirm this statement; which side of the uuid "
        "ordering each landed on is not evidence about it"
    )
    assert offered[subject]["independent"] == 2
    assert total >= 1
