"""Migration 030: the entities exist and the reader can now see them.

The pass filled `entities` with 6 129 names and `claim_entities` with 11 916
mentions. None of it reached the agent mode: the serving role sees the `agent`
schema and nothing else, and there was no view there for either table.
"""

from __future__ import annotations

from conftest import ADOPTED_MIGRATIONS, MIGRATION_030, _apply, connect


def test_030_leaves_the_schema_where_it_says_it_does(baseline_dsn: str) -> None:
    _apply(baseline_dsn, ADOPTED_MIGRATIONS[: ADOPTED_MIGRATIONS.index(MIGRATION_030) + 1])
    with connect(baseline_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM kx.metadata WHERE key = 'schema_version'")
        row = cursor.fetchone()
        assert row is not None
        assert row["value"] == 30


def test_an_entity_named_only_by_rejected_statements_is_not_exposed(migrated_dsn: str) -> None:
    """Listing it would say the base holds something it threw out.

    The narrowing is in the view rather than in whoever queries it, so a caller
    that forgets the filter gets nothing rather than the whole table.
    """
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        for view in ("entity", "statement_entity"):
            cursor.execute("SELECT pg_get_viewdef(%s::regclass, true) AS body", (f"agent.{view}",))
            row = cursor.fetchone()
            assert row is not None
            assert "agent.statement" in str(row["body"]), f"agent.{view} does not narrow"


def test_the_serving_role_reaches_the_two_new_views_and_no_table(
    least_privilege_dsn: str,
) -> None:
    with connect(least_privilege_dsn) as connection, connection.cursor() as cursor:
        for view in ("agent.entity", "agent.statement_entity"):
            cursor.execute(
                "SELECT has_table_privilege('radar_kb_public', %s, 'SELECT') AS allowed", (view,)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["allowed"] is True, f"the reader cannot see {view}"
        # And still not the tables under them: a mention row carries `found_by`
        # and `surface_form`, which are the machine's working notes.
        for table in ("kx.entities", "kx.claim_entities"):
            cursor.execute(
                "SELECT has_table_privilege('radar_kb_public', %s, 'SELECT') AS allowed", (table,)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["allowed"] is False, f"the reader reaches {table} directly"
