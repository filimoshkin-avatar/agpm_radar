"""Migration 029: the graph holds the knowledge, not only where it came from.

11 466 nodes and 21 235 edges, and every edge was provenance. The 229 subjects,
18 325 placements and 15 414 links that stages 0b-2 built were never projected
into it, so "the graph" showed the trace of a citation and five of UC-05's six
modes had nothing to draw.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import ADOPTED_MIGRATIONS, MIGRATION_029, _apply, connect


def test_029_leaves_the_schema_where_it_says_it_does(baseline_dsn: str) -> None:
    _apply(baseline_dsn, ADOPTED_MIGRATIONS[: ADOPTED_MIGRATIONS.index(MIGRATION_029) + 1])
    with connect(baseline_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM kx.metadata WHERE key = 'schema_version'")
        row = cursor.fetchone()
        assert row is not None
        assert row["value"] == 29


def test_the_graph_accepts_a_subject_and_an_entity(migrated_dsn: str) -> None:
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) AS body FROM pg_constraint"
            " WHERE conrelid = 'kx.graph_nodes'::regclass AND conname LIKE '%node_kind%'"
        )
        row = cursor.fetchone()
        assert row is not None
        for kind in ("topic", "entity"):
            assert f"'{kind}'" in str(row["body"])


def test_all_four_link_types_can_reach_the_graph(migrated_dsn: str) -> None:
    """Decision 12 chose four. Two were already in the authored vocabulary."""
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) AS body FROM pg_constraint"
            " WHERE conrelid = 'kx.graph_edges'::regclass AND conname LIKE '%relation%'"
        )
        row = cursor.fetchone()
        assert row is not None
        body = str(row["body"])
        for relation in ("supports", "contradicts", "qualifies", "related_to", "about", "mentions"):
            assert f"'{relation}'" in body


def test_the_builder_and_the_check_agree(migrated_dsn: str) -> None:
    """A vocabulary the code emits and the column refuses is a runtime failure.

    The two lists are written in different files and nothing else compares them.
    """
    from radar_kx.graph import NODE_KINDS, RELATIONS

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        for table, names in (("graph_nodes", NODE_KINDS), ("graph_edges", RELATIONS)):
            column = "node_kind" if table == "graph_nodes" else "relation"
            cursor.execute(
                "SELECT pg_get_constraintdef(oid) AS body FROM pg_constraint"
                " WHERE conrelid = %s::regclass AND conname LIKE %s",
                (f"kx.{table}", f"%{column}%"),
            )
            row = cursor.fetchone()
            assert row is not None
            for name in names:
                assert f"'{name}'" in str(row["body"]), f"{table} refuses {name}"


def test_one_sentence_can_name_several_entities(migrated_dsn: str) -> None:
    """`claims.subject_entity_id` holds one, and one is not the normal case.

    "Gartner says the EU AI Act will change what a PMO signs off" names three.
    """
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) AS total FROM information_schema.columns"
            " WHERE table_schema = 'kx' AND table_name = 'claim_entities'"
        )
        row = cursor.fetchone()
        assert row is not None
        assert int(str(row["total"])) > 0

        claim = "11111111-1111-1111-1111-111111111111"
        cursor.execute(
            "INSERT INTO kx.entities (entity_type, canonical_name)"
            " VALUES ('organisation', 'Gartner'), ('regulation', 'EU AI Act')"
            " RETURNING entity_id"
        )
        found = [str(row["entity_id"]) for row in cursor.fetchall()]
        assert len(found) == 2
        # The claim itself does not exist, so the foreign key must refuse - which
        # is the point: a mention cannot exist without the statement it is in.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cursor.execute(
                "INSERT INTO kx.claim_entities"
                " (claim_id, entity_id, role, surface_form, found_by, method)"
                " VALUES (%s, %s, 'mentioned', 'Gartner', 'test', 'model')",
                (claim, found[0]),
            )


def test_the_worker_can_reach_the_tables_it_writes(migrated_dsn: str) -> None:
    """A grant left out of a migration is a runtime failure, not a lint.

    The first version of 029 created both tables and granted neither, and
    `build-graph` stopped with "permission denied for table claim_entities" the
    moment the projection reached for them.
    """
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        for table in ("claim_entities", "entity_reads", "entities", "entity_aliases"):
            cursor.execute(
                "SELECT has_table_privilege('radar_kx', %s, 'SELECT') AS readable,"
                "       has_table_privilege('radar_kx', %s, 'INSERT') AS writable",
                (f"kx.{table}", f"kx.{table}"),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["readable"] and row["writable"], f"radar_kx cannot use kx.{table}"
