"""Migration 028: a subject counts the statements it shows, and no others."""

from __future__ import annotations

import hashlib

from conftest import ADOPTED_MIGRATIONS, MIGRATION_028, _apply, connect, seed_statement


def test_028_leaves_the_schema_where_it_says_it_does(baseline_dsn: str) -> None:
    _apply(baseline_dsn, ADOPTED_MIGRATIONS[: ADOPTED_MIGRATIONS.index(MIGRATION_028) + 1])
    with connect(baseline_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM kx.metadata WHERE key = 'schema_version'")
        row = cursor.fetchone()
        assert row is not None
        assert row["value"] == 28


def test_the_count_on_a_subject_is_the_list_under_it(migrated_dsn: str) -> None:
    """The card lists knowledge; before 028 the number also counted the chronicle.

    5 098 of 18 260 placements on production are observatory statements, so a
    subject could announce a number and then show a visibly shorter list with
    nothing on the page to explain the difference.
    """
    knowledge = seed_statement(
        migrated_dsn,
        url="https://example.com/k",
        published_on="2026-01-01",
        first_seen_on="2026-01-02",
        admission="knowledge",
    )
    chronicle = seed_statement(
        migrated_dsn,
        url="https://example.com/o",
        published_on="2026-01-01",
        first_seen_on="2026-01-02",
        admission="observatory",
    )
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO kx.topics (topic_key, title, source, level, state, created_by)
            VALUES ('t-one', 'Одна тема', 'authored', 1, 'accepted', 'test')
            RETURNING topic_id
            """
        )
        found = cursor.fetchone()
        assert found is not None
        topic_id = found["topic_id"]
        for claim_id in (knowledge, chronicle):
            cursor.execute("SELECT version_id FROM kx.claims WHERE claim_id = %s", (claim_id,))
            version = cursor.fetchone()
            assert version is not None
            # `validate_exact_claim_evidence` re-cuts the span out of
            # `canonical_text` and refuses anything that does not reproduce, so
            # the quotation has to be the seeded text itself.
            quote = "text"
            cursor.execute(
                """
                INSERT INTO kx.claim_evidence (
                    claim_id, version_id, char_start, char_end, quote_text,
                    quote_sha256, match_status
                ) VALUES (%s, %s, 0, 4, %s, %s, 'exact')
                """,
                (
                    claim_id,
                    version["version_id"],
                    quote,
                    hashlib.sha256(quote.encode()).hexdigest(),
                ),
            )
            cursor.execute(
                "INSERT INTO kx.claim_topics (claim_id, topic_id, assigned_by, method)"
                " VALUES (%s, %s, 'test', 'model')",
                (claim_id, topic_id),
            )
        cursor.execute("SELECT statements FROM agent.topic WHERE topic_key = 't-one'")
        counted = cursor.fetchone()
        assert counted is not None
        cursor.execute(
            "SELECT count(*) AS listed FROM agent.statement AS statement"
            " JOIN agent.statement_topic AS placed USING (claim_id)"
            " WHERE placed.topic_key = 't-one' AND statement.admission = 'knowledge'"
        )
        listed = cursor.fetchone()
        assert listed is not None

    assert counted["statements"] == listed["listed"] == 1, (
        "the subject counted two and lists one - the chronicle is a separate tab"
    )
