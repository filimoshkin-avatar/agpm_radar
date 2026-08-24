"""Migration 026: the grants are made to say what 024's comment claimed.

024 promised the serving role had "no privilege anywhere in `kx`". A review of
the live grants found it reaching eleven tables. The test 024 shipped with could
not catch that: it checked a *denylist* of five tables the role must not read, so
every grant nobody thought to add to the list passed.

The test here is the other shape. It asserts the whole reachable set, so a grant
added later fails until somebody writes it down and says why.
"""

from __future__ import annotations

from conftest import connect

#: Everything `radar_kb_public` may touch, and the only reason each one is here.
#:
#: A new entry in this set is a deliberate widening of a public surface. If a
#: change needs one, the reason belongs beside it.
ALLOWED = {
    # The six views the reader sees, plus the vectors of those same statements.
    ("agent", "statement", "SELECT"),
    ("agent", "statement_topic", "SELECT"),
    ("agent", "statement_vector", "SELECT"),
    ("agent", "topic", "SELECT"),
    ("agent", "link", "SELECT"),
    ("agent", "page", "SELECT"),
    ("agent", "gap", "SELECT"),
    # The schema-version gate every command runs before it does anything.
    ("kx", "metadata", "SELECT"),
    # The answer cache reads its own rows; decision 9 keeps the chat for analysis.
    ("kx", "research_answers", "SELECT"),
    ("kx", "research_answers", "INSERT"),
    # A model call must leave an audit row. The SELECT that `RETURNING egress_id`
    # needs is granted on that one column, so it is a column privilege and does
    # not appear here - `test_the_audit_row_can_actually_be_written` covers it.
    ("kx", "egress_audit", "INSERT"),
}


def reachable(dsn: str) -> set[tuple[str, str, str]]:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_schema, table_name, privilege_type
            FROM information_schema.table_privileges
            WHERE grantee = 'radar_kb_public'
            """
        )
        return {
            (str(row["table_schema"]), str(row["table_name"]), str(row["privilege_type"]))
            for row in cursor.fetchall()
        }


def test_the_role_reaches_exactly_what_is_written_down(least_privilege_dsn: str) -> None:
    granted = reachable(least_privilege_dsn)
    unexpected = granted - ALLOWED
    assert not unexpected, (
        "radar_kb_public reaches something nobody wrote down: "
        f"{sorted(unexpected)}. Add it to ALLOWED with the reason, or revoke it."
    )


def test_everything_written_down_is_actually_granted(least_privilege_dsn: str) -> None:
    """The other direction: a promise the grants do not keep is the same defect."""
    missing = ALLOWED - reachable(least_privilege_dsn)
    assert not missing, f"the service is promised {sorted(missing)} and does not have it"


def test_the_corpus_vectors_are_out_of_reach(least_privilege_dsn: str) -> None:
    """A chunk vector is a derivative of full text the reader may not read.

    19 851 of them were reachable before this migration, through a blanket SELECT
    on `text_embeddings` that the semantic arm needed one slice of.
    """
    with connect(least_privilege_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege('radar_kb_public', 'kx.text_embeddings', 'SELECT')"
            " AS allowed"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["allowed"] is False

        cursor.execute("SELECT pg_get_viewdef('agent.statement_vector'::regclass, true) AS body")
        row = cursor.fetchone()
        assert row is not None
        # The narrowing is in the view, not in whoever queries it: a caller that
        # forgets the filter gets nothing rather than the corpus.
        assert "'claim_evidence'" in str(row["body"])


def test_the_audit_row_can_actually_be_written(least_privilege_dsn: str) -> None:
    """Grants that look right and break the code path are the dangerous kind.

    Revoking the table-wide SELECT on `egress_audit` passed every grant assertion
    and took the live answer endpoint down: `record_egress` ends `RETURNING
    egress_id`, and a RETURNING clause needs SELECT on the columns it names. So
    this test exercises the statement rather than the privilege.
    """
    with connect(least_privilege_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE radar_kb_public")
        cursor.execute(
            """
            INSERT INTO kx.egress_audit
                (provider, model, purpose, payload_chars, payload_sha256, outcome,
                 worker_release)
            VALUES ('zai', 'glm-5.2', 'test', 1, %s, 'succeeded', 'test')
            RETURNING egress_id
            """,
            ("0" * 64,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["egress_id"]
        # And still no reading of anybody else's call.
        cursor.execute("RESET ROLE")
        cursor.execute(
            "SELECT has_column_privilege('radar_kb_public', 'kx.egress_audit',"
            " 'error_detail', 'SELECT') AS allowed"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["allowed"] is False


def test_the_public_search_reads_the_narrowed_view(least_privilege_dsn: str) -> None:
    """The query has to use what the role can see, or the surface simply breaks."""
    from radar_kx.search import AGENT_SEARCH_SQL

    assert "agent.statement_vector" in AGENT_SEARCH_SQL
    assert "kx.text_embeddings" not in AGENT_SEARCH_SQL
