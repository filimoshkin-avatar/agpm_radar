"""Migration 022: the fifteen decisions of 2026-08-23, as things the store can hold.

Each test names the decision it protects, because a constraint whose reason is
only in a migration comment is a constraint the next person deletes.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import psycopg
import pytest

from conftest import connect


def _one(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    with connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        assert row is not None
        return dict(row)


def test_the_schema_says_it_is_at_twenty_two(migrated_dsn: str) -> None:
    row = _one(migrated_dsn, "SELECT value FROM kx.metadata WHERE key = 'schema_version'")
    assert row["value"] == 22


def test_a_retelling_must_name_whose_claim_it_retells(migrated_dsn: str) -> None:
    # Decision 1: four outlets repeating one Gartner forecast are one source. A
    # retelling that cannot say what it retells records nothing useful.
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        cursor.execute(
            "INSERT INTO kx.claim_reading"
            " (claim_id, material_kind, is_retelling, admission, read_by, method)"
            " VALUES (gen_random_uuid(), 'forecast', true, 'knowledge', 'test', 'model')"
        )


def test_every_kind_of_material_has_a_freshness_rule_that_says_why(
    migrated_dsn: str,
) -> None:
    # Decision 11: a rule per kind, not a date per statement - and a rule nobody
    # can explain later is a rule nobody can revise.
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT material_kind, valid_for, rationale FROM kx.material_kind_freshness")
        rules = [dict(row) for row in cursor.fetchall()]
    assert {row["material_kind"] for row in rules} == {
        "fact",
        "opinion",
        "case",
        "forecast",
        "product_release",
        "incident",
    }
    assert all(row["rationale"] for row in rules)
    # A product release goes stale before a case does.
    by_kind = {str(row["material_kind"]): cast(timedelta, row["valid_for"]) for row in rules}
    assert by_kind["product_release"] < by_kind["case"]


def test_a_status_cannot_be_rewritten(migrated_dsn: str) -> None:
    # Her §2.2: a statement must not pass from signal to canon unnoticed. A status
    # that can be overwritten leaves no evidence a promotion happened.
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.knowledge_status (unit_kind, unit_id, status, method, set_by)"
            " VALUES ('claim', gen_random_uuid(), 'observed_signal', 'rule', 'test')"
        )
    for statement in (
        "UPDATE kx.knowledge_status SET status = 'canon'",
        "DELETE FROM kx.knowledge_status",
    ):
        with (
            connect(migrated_dsn) as connection,
            connection.cursor() as cursor,
            pytest.raises(psycopg.errors.RaiseException),
        ):
            cursor.execute(statement)


def test_a_model_proposal_is_never_the_status_in_force(migrated_dsn: str) -> None:
    # Decision 6: the machine proposes, the owner confirms. Until she does, the
    # proposal is in the table and not in the view anything publishes from.
    unit = "11111111-1111-1111-1111-111111111111"
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.knowledge_status (unit_kind, unit_id, status, method, set_by)"
            " VALUES ('idea', %s, 'observed_signal', 'rule', 'test'),"
            "        ('idea', %s, 'operationalization', 'model', 'glm-5.2')",
            (unit, unit),
        )
    row = _one(
        migrated_dsn,
        "SELECT status, method FROM kx.knowledge_status_current WHERE unit_id = %s",
        (unit,),
    )
    assert row["status"] == "observed_signal"

    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kx.knowledge_status (unit_kind, unit_id, status, method, set_by)"
            " VALUES ('idea', %s, 'operationalization', 'manual', 'owner')",
            (unit,),
        )
    row = _one(
        migrated_dsn,
        "SELECT status, method FROM kx.knowledge_status_current WHERE unit_id = %s",
        (unit,),
    )
    assert (row["status"], row["method"]) == ("operationalization", "manual")


def test_only_the_four_link_types_of_the_launch_are_accepted(migrated_dsn: str) -> None:
    # Decision 12: four of her eighteen live at launch. The other fourteen are a
    # CHECK away, and admitting them silently would be admitting them undecided.
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        cursor.execute(
            "INSERT INTO kx.knowledge_links"
            " (from_kind, from_id, to_kind, to_id, link_type, created_by, method)"
            " VALUES ('claim', gen_random_uuid(), 'idea', gen_random_uuid(),"
            "         'broader_than', 'test', 'model')"
        )


def test_a_unit_cannot_link_to_itself(migrated_dsn: str) -> None:
    unit = "22222222-2222-2222-2222-222222222222"
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        cursor.execute(
            "INSERT INTO kx.knowledge_links"
            " (from_kind, from_id, to_kind, to_id, link_type, created_by, method)"
            " VALUES ('claim', %s, 'claim', %s, 'related_to', 'test', 'model')",
            (unit, unit),
        )


def test_the_gaps_map_says_what_was_missing(migrated_dsn: str) -> None:
    # Decision 8: a statement with nowhere to go is not dropped. The row is what
    # separates "examined, no place" from "not examined yet".
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.NotNullViolation),
    ):
        cursor.execute(
            "INSERT INTO kx.claim_gaps (claim_id, noted_by, method)"
            " VALUES (gen_random_uuid(), 'test', 'model')"
        )


def test_promotion_and_freshness_review_are_recordable_decisions(
    migrated_dsn: str,
) -> None:
    with connect(migrated_dsn) as connection, connection.cursor() as cursor:
        for kind in ("status_promotion", "freshness_review"):
            cursor.execute(
                "INSERT INTO kx.editorial_decisions"
                " (object_kind, object_key, verdict, actor, scope)"
                " VALUES (%s, 'x', 'confirmed', 'owner', 'editor')",
                (kind,),
            )
        cursor.execute(
            "SELECT count(*) AS total FROM kx.editorial_decisions"
            " WHERE object_kind IN ('status_promotion', 'freshness_review')"
        )
        assert cursor.fetchone()["total"] == 2  # type: ignore[index]
