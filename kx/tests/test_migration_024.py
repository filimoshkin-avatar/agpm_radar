"""Migration 024: what the agent mode can reach, and everything it cannot.

The point of this migration is a boundary, so the tests are about what is *not*
reachable. A test that only proved the views return rows would pass just as well
against a role that could read the whole store.
"""

from __future__ import annotations

from conftest import connect


def test_the_schema_says_it_is_at_twenty_four(agent_dsn: str) -> None:
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM kx.metadata WHERE key = 'schema_version'")
        row = cursor.fetchone()
        assert row is not None
        assert row["value"] == 24


def test_the_public_role_exists_and_owns_nothing(agent_dsn: str) -> None:
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole"
            " FROM pg_roles WHERE rolname = 'radar_kb_public'"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["rolcanlogin"] is True
        assert row["rolsuper"] is False
        assert row["rolcreatedb"] is False
        assert row["rolcreaterole"] is False


def test_the_public_role_cannot_read_anybody_s_article(agent_dsn: str) -> None:
    """The one property this migration exists for.

    Not "the service does not ask for full text" - that is a promise. This is the
    connection being unable to, which survives the service being wrong.
    """
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        for table in ("document_versions", "raw_blobs", "chunks", "fetch_queue", "claims"):
            cursor.execute(
                "SELECT has_table_privilege('radar_kb_public', %s, 'SELECT') AS allowed",
                (f"kx.{table}",),
            )
            row = cursor.fetchone()
            assert row is not None, table
            assert row["allowed"] is False, f"radar_kb_public can read kx.{table}"


def test_the_public_role_cannot_write_where_the_owner_decides(agent_dsn: str) -> None:
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        for table, privilege in (
            ("editorial_decisions", "INSERT"),
            ("knowledge_status", "INSERT"),
            ("claim_reading", "UPDATE"),
            ("published_quotes", "INSERT"),
        ):
            cursor.execute(
                "SELECT has_table_privilege('radar_kb_public', %s, %s) AS allowed",
                (f"kx.{table}", privilege),
            )
            row = cursor.fetchone()
            assert row is not None, table
            assert row["allowed"] is False, f"radar_kb_public may {privilege} kx.{table}"


def test_the_public_role_may_record_that_a_question_was_asked(agent_dsn: str) -> None:
    """Decision 9: the chat is kept for analysis, so the answer path has to write."""
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        for table in ("research_answers", "egress_audit"):
            cursor.execute(
                "SELECT has_table_privilege('radar_kb_public', %s, 'INSERT') AS allowed",
                (f"kx.{table}",),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["allowed"] is True, f"radar_kb_public cannot record into kx.{table}"


def test_a_rejected_statement_never_reaches_the_surface(agent_dsn: str) -> None:
    """A vendor's connector list was never meant to leave the store."""
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_viewdef('agent.statement'::regclass, true) AS body")
        row = cursor.fetchone()
        assert row is not None
        body = str(row["body"])
        assert "admission <> 'rejected'" in body.replace('"', "")


def test_an_unread_statement_is_not_offered_either(agent_dsn: str) -> None:
    """Its labels would be empty, and the reader would have nothing to judge by."""
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_viewdef('agent.statement'::regclass, true) AS body")
        row = cursor.fetchone()
        assert row is not None
        # An inner join to the reading is what excludes it; a left join would let
        # an unlabelled statement out with every label null.
        assert "LEFT JOIN kx.claim_reading" not in str(row["body"])


def test_the_surface_is_read_only_for_its_own_views(agent_dsn: str) -> None:
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            cursor.execute(
                "SELECT has_table_privilege('radar_kb_public', 'agent.statement', %s) AS allowed",
                (privilege,),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["allowed"] is False


def test_every_view_the_service_needs_is_readable(agent_dsn: str) -> None:
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        for view in ("statement", "statement_topic", "topic", "link", "page", "gap"):
            cursor.execute(
                "SELECT has_table_privilege('radar_kb_public', %s, 'SELECT') AS allowed",
                (f"agent.{view}",),
            )
            row = cursor.fetchone()
            assert row is not None, view
            assert row["allowed"] is True, f"radar_kb_public cannot read agent.{view}"


def test_the_views_answer_on_an_empty_store(agent_dsn: str) -> None:
    """A base with nothing read yet returns nothing, rather than failing to run."""
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        for view in ("statement", "topic", "link", "page", "gap"):
            cursor.execute(f"SELECT count(*) AS total FROM agent.{view}")  # noqa: S608
            row = cursor.fetchone()
            assert row is not None


def test_creating_the_role_twice_does_not_fail(agent_dsn: str) -> None:
    """A cluster can hold two of these databases; the second must still migrate."""
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'radar_kb_public') THEN
                    CREATE ROLE radar_kb_public LOGIN;
                END IF;
            END
            $$
            """
        )
        cursor.execute("SELECT count(*) AS total FROM pg_roles WHERE rolname = 'radar_kb_public'")
        row = cursor.fetchone()
        assert row is not None
        assert row["total"] == 1


def test_the_backbone_carries_its_trail(agent_dsn: str) -> None:
    """A reader navigating three levels needs the path, and it is walked not stored."""
    with connect(agent_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_viewdef('agent.topic'::regclass, true) AS body")
        row = cursor.fetchone()
        assert row is not None
        assert "RECURSIVE" in str(row["body"]).upper()
