"""Migration 035: an answer may follow a refusal for the same question.

017 gave `research_answers` one unique index on the ADR-0006 §10 key and a
trigger forbidding both UPDATE and DELETE. Together they meant more than either
did alone: a recorded refusal held the key forever, and the real answer that
arrived later was returned to the reader and dropped by `ON CONFLICT DO NOTHING`.
The question became permanently uncacheable, so every later reader paid a fresh
model call - and each of those calls could refuse again.

Measured on production 2026-08-26: «Расскажи про «Человеко-агентная система»»
answered in one sweep in 17.8s and refused in the next, same prompt, same
quotations. One row for it in the store: a refusal from 25.08 13:37. Forty keys
were held by refusals, four of them questions the welcome screen still offers.

Applied to production 2026-08-26, and `SCHEMA_VERSION` moved to 35 with it -
that is what applying earns. These tests kept their own fixture name so they read
as being about this migration, and every one of them was watched failing against
schema 34 first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import connect
from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.research import EvidenceElement, refuse

QUESTION = "что такое порог автономии"

EVIDENCE = (
    EvidenceElement(
        ordinal=1,
        claim_id="c1",
        quote_text="Порог автономии определяет границу между классами решений.",
        source_url="https://example.org/a",
        char_start=0,
        char_end=57,
        relevance=0.04,
    ),
)


@pytest.fixture
def store(superseding_dsn: str) -> Database:
    """The real writer, pointed at the migrated database."""
    return Database(_settings(superseding_dsn))


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


def test_035_leaves_the_schema_where_it_says_it_does(superseding_dsn: str) -> None:
    with connect(superseding_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value FROM kx.metadata WHERE key = 'schema_version'")
        row = cursor.fetchone()
        assert row is not None
        assert row["value"] == 35


def test_the_one_key_becomes_two_partial_ones(superseding_dsn: str) -> None:
    with connect(superseding_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = 'kx' AND tablename = 'research_answers'"
        )
        names = {str(row["indexname"]) for row in cursor.fetchall()}
    assert "research_answers_cache_key" not in names
    assert {"research_answers_answer_key", "research_answers_refusal_key"} <= names


def test_an_answer_is_recorded_after_a_refusal_for_the_same_question(store: Database) -> None:
    """The whole point. Before 035 the second call wrote nothing."""
    store.record_answer(
        question=QUESTION,
        scope="public",
        mode="strict",
        package=EVIDENCE,
        refusal=refuse("no_evidence", "в базе нет подходящих подтверждений"),
        answered_by="test",
    )
    store.record_answer(
        question=QUESTION,
        scope="public",
        mode="strict",
        package=EVIDENCE,
        answer_text="Порог автономии — решение организации.",
        answered_by="test",
    )
    cached = store.cached_answer(QUESTION, scope="public")
    assert cached is not None
    assert cached["answer_text"] == "Порог автономии — решение организации."


def test_the_answer_wins_the_lookup_whatever_the_planner_does(store: Database) -> None:
    """Both rows exist under one key, so the read must not be a coin toss."""
    store.record_answer(
        question=QUESTION,
        scope="public",
        mode="strict",
        package=EVIDENCE,
        answer_text="Порог автономии — решение организации.",
        answered_by="test",
    )
    store.record_answer(
        question=QUESTION,
        scope="public",
        mode="strict",
        package=EVIDENCE,
        refusal=refuse("no_evidence", "в базе нет подходящих подтверждений"),
        answered_by="test",
    )
    for _ in range(5):
        cached = store.cached_answer(QUESTION, scope="public")
        assert cached is not None
        assert cached["refusal_reason"] is None


def test_neither_half_of_the_key_may_hold_two_rows(store: Database) -> None:
    """Refusals must not pile up one per ask, and answers stay unique as before."""
    for _ in range(3):
        store.record_answer(
            question=QUESTION,
            scope="public",
            mode="strict",
            package=EVIDENCE,
            refusal=refuse("no_evidence", "в базе нет подходящих подтверждений"),
            answered_by="test",
        )
        store.record_answer(
            question=QUESTION,
            scope="public",
            mode="strict",
            package=EVIDENCE,
            answer_text="Порог автономии — решение организации.",
            answered_by="test",
        )
    with store.connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FILTER (WHERE answer_text IS NOT NULL) AS answers,"
            "       count(*) FILTER (WHERE refusal_reason IS NOT NULL) AS refusals"
            " FROM kx.research_answers WHERE scope = 'public'"
        )
        row = cursor.fetchone()
    assert row is not None
    assert (row["answers"], row["refusals"]) == (1, 1)


def test_a_recorded_row_is_still_immutable(superseding_dsn: str) -> None:
    """035 splits the key; it does not soften what the trigger protects."""
    for statement in (
        "UPDATE kx.research_answers SET answer_text = 'edited'",
        "DELETE FROM kx.research_answers",
    ):
        with (
            connect(superseding_dsn) as connection,
            connection.cursor() as cursor,
            pytest.raises(Exception, match="immutable|reject"),
        ):
            cursor.execute("SET search_path = kx, public")
            cursor.execute(
                "INSERT INTO kx.research_answers (normalized_question, scope, question, mode,"
                " answer_text, verification, evidence_package, answered_by)"
                " VALUES ('q','public','q','strict','a','{}'::jsonb,'[]'::jsonb,'test')"
                " ON CONFLICT DO NOTHING"
            )
            cursor.execute(statement)
