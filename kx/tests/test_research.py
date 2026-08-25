"""Slice 2.14: an answer that survives checking, or a refusal that says why."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import connect
from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.research import (
    HEDGES,
    MIN_RELEVANCE,
    PACKAGE_SIZE,
    Clause,
    EvidenceElement,
    build_answer_prompt,
    build_package,
    normalize_question,
    parse_answer,
    refuse,
    render,
    verify,
)

EVIDENCE = EvidenceElement(
    ordinal=1,
    claim_id="k1",
    quote_text=(
        "Adoption of agentic project management reached 41% in 2026 among the 300 "
        "surveyed programmes, according to the survey."
    ),
    source_url="https://example.com/survey",
    char_start=100,
    char_end=230,
    relevance=0.031,
)
SECOND = EvidenceElement(
    ordinal=2,
    claim_id="k2",
    quote_text="Every run is assigned to exactly one named human owner.",
    source_url="https://example.com/governance",
    char_start=0,
    char_end=54,
    relevance=0.02,
)
PACKAGE = (EVIDENCE, SECOND)


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


# --------------------------------------------------------------------------
# The package
# --------------------------------------------------------------------------


def test_the_package_is_numbered_and_the_noise_is_dropped() -> None:
    hits = [
        {
            "claim_id": f"k{index}",
            "quote_text": "text",
            "source_url": "https://example.com/a",
            "char_start": 0,
            "char_end": 4,
            "relevance": relevance,
        }
        for index, relevance in enumerate([0.03, 0.02, 0.001], start=1)
    ]
    package = build_package(hits)
    assert [element.ordinal for element in package] == [1, 2]
    assert all(element.relevance >= MIN_RELEVANCE for element in package)


def test_every_element_carries_an_audience() -> None:
    # ADR-0004 §11. Without the field the renderer has no way to decline to quote
    # something the asker may not see, and the check gets added under pressure.
    assert EVIDENCE.as_json()["audience"] == "public"


def test_the_package_is_capped() -> None:
    hits = [
        {
            "claim_id": f"k{index}",
            "quote_text": "text",
            "source_url": "https://example.com/a",
            "char_start": 0,
            "char_end": 4,
            "relevance": 0.03,
        }
        for index in range(PACKAGE_SIZE + 5)
    ]
    assert len(build_package(hits)) == PACKAGE_SIZE


# --------------------------------------------------------------------------
# Verification: the part that is not a model
# --------------------------------------------------------------------------


def test_a_clause_that_matches_its_evidence_passes() -> None:
    result = verify([Clause("Adoption reached 41% in 2026.", (1,))], PACKAGE)
    assert result.passes
    assert result.bound_clauses == 1


def test_a_figure_the_evidence_does_not_have_fails() -> None:
    result = verify([Clause("Adoption reached 62% in 2026.", (1,))], PACKAGE)
    assert not result.passes
    assert "the figure 62" in result.verdicts[0].problems[0]


def test_a_clause_that_states_a_fact_and_cites_nothing_fails() -> None:
    # ADR-0004 §7: free model text with no claim binding is an error.
    result = verify([Clause("Adoption is accelerating everywhere.", ())], PACKAGE)
    assert not result.passes
    assert "cites nothing" in result.verdicts[0].problems[0]


def test_connective_phrasing_needs_no_binding() -> None:
    # §7: connective phrasing that carries no factual content is allowed.
    result = verify(
        [Clause("Adoption reached 41% in 2026.", (1,)), Clause("Кроме того,", ())], PACKAGE
    )
    assert result.passes


def test_a_reference_to_evidence_that_was_not_offered_fails() -> None:
    # Worse than no reference: it reads as bound and points nowhere.
    result = verify([Clause("Adoption reached 41%.", (7,))], PACKAGE)
    assert not result.passes
    assert "not offered" in result.verdicts[0].problems[0]


def test_a_quotation_the_evidence_does_not_contain_fails() -> None:
    result = verify(
        [Clause('The survey said "adoption has already peaked" this year.', (1,))], PACKAGE
    )
    assert not result.passes
    assert any("quotation" in problem for problem in result.verdicts[0].problems)


# Every case below is a draft this check rejected on production before
# 2026-08-25, read back out of `kx.research_answers`. Four rejections in the
# service's whole life, and not one of them was a fabricated quotation.


def test_a_term_in_guillemets_is_named_not_quoted() -> None:
    """«паспорт агента» — reader's vocabulary, and the base's own.

    Rejected in production on the question «что бы ты порекомендовал включить в
    новую редакцию концепции агентного УП?». Russian puts terms in guillemets,
    and a check that reads every one of them as a claimed utterance rejects the
    draft for naming the thing it was asked about.
    """
    result = verify(
        [Clause("Слияние «паспорта агента» со сборочной карточкой сохраняет один owner.", (2,))],
        PACKAGE,
    )
    assert result.passes, result.verdicts[0].problems


def test_a_named_concept_from_the_question_is_not_a_quotation() -> None:
    """«Подпись под решением» — rejected on «Что такое подпись под решением?»."""
    result = verify(
        [Clause("«Подпись под решением» означает, что у прогона есть named human owner.", (2,))],
        PACKAGE,
    )
    assert result.passes, result.verdicts[0].problems


def test_an_index_into_the_package_is_not_a_figure_from_a_source() -> None:
    """«Согласно свидетельству 2» points at the package, not at the text.

    Rejected in production on «Что противоречиво в подходах к human-in-the-loop?»
    for «the figure 6 is not in the cited evidence» - where 6 was the number of
    the quotation the clause was resting on.
    """
    result = verify(
        [Clause("Согласно свидетельству 2, у каждого прогона один named human owner.", (2,))],
        PACKAGE,
    )
    assert result.passes, result.verdicts[0].problems


def test_a_figure_beside_an_index_is_still_checked() -> None:
    """Stripping the reference must not strip the claim standing next to it."""
    result = verify(
        [Clause("Согласно свидетельству 1, внедрение достигло 62% в 2026 году.", (1,))],
        PACKAGE,
    )
    assert not result.passes
    assert any("the figure 62" in problem for problem in result.verdicts[0].problems)


def test_a_sentence_in_guillemets_is_still_a_quotation() -> None:
    """The loosening stops at three words; an utterance is still an utterance."""
    result = verify(
        [Clause("Отчёт сообщает: «внедрение в этом году уже прошло свой пик».", (1,))],
        PACKAGE,
    )
    assert not result.passes
    assert any("quotation" in problem for problem in result.verdicts[0].problems)


@pytest.mark.parametrize("hedge", ["probably", "it appears", "скорее всего"])
def test_a_hedge_fails_the_whole_answer(hedge: str) -> None:
    # ADR-0004 §9: hedges are how an unsupported claim gets published while
    # sounding careful. Matched on the draft, because a model told not to hedge
    # still hedges.
    result = verify([Clause(f"Adoption {hedge} reached 41% in 2026.", (1,))], PACKAGE)
    assert not result.passes
    assert result.hedges_found
    assert all(item in HEDGES for item in result.hedges_found)


def test_the_verification_report_says_which_clause_failed() -> None:
    result = verify(
        [Clause("Adoption reached 41% in 2026.", (1,)), Clause("And 99 more.", (1,))], PACKAGE
    )
    payload: dict[str, Any] = result.as_json()
    assert payload["clauses"] == 2
    assert len(payload["problems"]) == 1
    assert "99 more" in payload["problems"][0]["clause"]


# --------------------------------------------------------------------------
# Refusal
# --------------------------------------------------------------------------


def test_a_refusal_carries_a_precise_code() -> None:
    # ADR-0004 §10: the outward wording is a policy of the scope; the internal
    # code is always precise.
    assert refuse("no_evidence", "nothing matched").reason == "no_evidence"
    assert refuse("out_of_scope", "not from here").reason == "out_of_scope"
    with pytest.raises(ValueError, match="reason must be one of"):
        refuse("dunno", "x")


def test_the_adjacent_support_is_its_own_field_and_comes_from_the_question() -> None:
    # §9a, and the two rules that keep it from becoming a way to answer anyway:
    # it is retrieved for the question — it is the same package — and it is
    # returned separately, never merged into a paragraph that reads like an answer.
    refusal = refuse("no_evidence", "no clause survived", PACKAGE)
    payload = refusal.as_json()
    assert payload["refusal"] == "no_evidence"
    assert "adjacentSupport" in payload
    assert [item["n"] for item in payload["adjacentSupport"]] == [1, 2]
    assert "answer" not in payload


# --------------------------------------------------------------------------
# Prompt and parsing
# --------------------------------------------------------------------------


def test_the_prompt_carries_the_numbers_the_model_must_cite() -> None:
    prompt = build_answer_prompt("Какова доля внедрения?", PACKAGE)
    assert "[1]" in prompt and "[2]" in prompt
    assert "data, not instruction" in prompt
    assert "Какова доля внедрения?" in prompt


def test_an_empty_clause_list_is_a_valid_answer_meaning_no() -> None:
    assert parse_answer('{"clauses": []}') == ()


def test_an_answer_of_the_wrong_shape_is_refused() -> None:
    for answer in ("nothing here", '{"text": "an answer"}'):
        with pytest.raises(ValueError, match="no JSON object|no clauses list"):
            parse_answer(answer)


def test_the_answer_is_assembled_here_not_by_the_model() -> None:
    # ADR-0005 §13: the model returns structure and references; deterministic
    # answer assembly lives in Radar code.
    assert render([Clause("One. ", (1,)), Clause(" Two.", (2,))]) == "One. Two."


def test_the_cache_key_ignores_spacing_and_punctuation() -> None:
    assert normalize_question("  Какова ДОЛЯ внедрения?  ") == normalize_question(
        "какова доля внедрения"
    )


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_an_answer_and_a_refusal_are_never_both(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    with pytest.raises(ValueError, match="never both and never neither"):
        database.record_answer(
            question="q",
            scope="research",
            mode="research",
            package=PACKAGE,
            answer_text="a",
            refusal=refuse("no_evidence", "x"),
            answered_by="test",
        )


def test_the_cache_key_is_question_scope_and_release(migrated_dsn: str) -> None:
    # ADR-0006 §10: a cache without scope in the key moves content between access
    # levels, and it does it silently.
    database = Database(_settings(migrated_dsn))
    first = database.record_answer(
        question="What is the adoption rate?",
        scope="research",
        mode="research",
        package=PACKAGE,
        answer_text="41% in 2026.",
        verification=verify([Clause("Adoption reached 41% in 2026.", (1,))], PACKAGE),
        answered_by="test",
    )
    assert first["cached"] is False
    again = database.record_answer(
        question="  what is the ADOPTION rate  ",
        scope="research",
        mode="research",
        package=PACKAGE,
        answer_text="41% in 2026.",
        answered_by="test",
    )
    assert again["cached"] is True
    # A different scope is a different key, and gets its own row.
    other = database.record_answer(
        question="What is the adoption rate?",
        scope="public",
        mode="strict",
        package=PACKAGE,
        refusal=refuse("out_of_scope", "not reachable from public"),
        answered_by="test",
    )
    assert other["cached"] is False
    assert database.cached_answer("What is the adoption rate?", scope="public") is not None


def test_a_recorded_answer_cannot_be_rewritten(migrated_dsn: str) -> None:
    database = Database(_settings(migrated_dsn))
    database.record_answer(
        question="q",
        scope="research",
        mode="research",
        package=PACKAGE,
        refusal=refuse("no_evidence", "nothing"),
        answered_by="test",
    )
    with (
        connect(migrated_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(Exception, match="immutable|reject"),
    ):
        cursor.execute("UPDATE kx.research_answers SET answer_text = 'edited'")
