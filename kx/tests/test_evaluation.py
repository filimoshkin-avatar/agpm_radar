"""Retrieval measurement: what it counts, and what it refuses to conclude."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from radar_kx.evaluation import (
    GoldQuestion,
    GoldSetError,
    _percentile,
    evaluate,
    load_gold_set,
    summarize,
)

PROBES = (
    Path(__file__).resolve().parents[1] / "data" / "eval" / "vertical-slice-probes-2026-08-22.json"
)


@dataclass(frozen=True)
class _Hit:
    document_id: str


def _searcher(answers: dict[str, list[str]]) -> Any:
    def search(query: str, *, scope: str, limit: int) -> list[_Hit]:
        return [_Hit(document) for document in answers.get(query, [])][:limit]

    return search


def _question(
    question_id: str, question: str, expected: list[str], kind: str = "probe"
) -> GoldQuestion:
    return GoldQuestion(
        question_id=question_id,
        kind=kind,
        scope="current",
        question=question,
        expected_documents=tuple(expected),
    )


def test_rank_is_the_position_of_the_first_expected_document() -> None:
    results, _ = evaluate(
        _searcher({"q": ["other", "other2", "wanted"]}),
        [_question("one", "q", ["wanted"])],
    )
    assert results[0].rank == 3
    assert results[0].found is True


def test_repeated_documents_do_not_inflate_the_rank() -> None:
    # Search returns chunks, and several chunks of one document is normal. Rank is
    # over documents, or a document that answers in three places looks like three
    # documents' worth of noise before the answer.
    results, _ = evaluate(
        _searcher({"q": ["a", "a", "a", "wanted"]}),
        [_question("one", "q", ["wanted"])],
    )
    assert results[0].rank == 2


def test_a_question_with_no_hit_is_not_found_rather_than_rank_zero() -> None:
    results, summary = evaluate(_searcher({"q": ["nothing"]}), [_question("one", "q", ["wanted"])])
    assert results[0].rank is None
    assert results[0].found is False
    assert summary["byKind"]["probe"]["notFound"] == ["one"]


def test_probes_and_authored_questions_are_never_averaged_together() -> None:
    # A probe measures the index; a question measures the system. One number over
    # both would let a perfect probe score hide a broken question score.
    results, summary = evaluate(
        _searcher({"p": ["wanted"], "q": ["nothing"]}),
        [
            _question("p1", "p", ["wanted"], kind="probe"),
            _question("q1", "q", ["wanted"], kind="question"),
        ],
    )
    assert summary["byKind"]["probe"]["recallAt10"] == 1.0
    assert summary["byKind"]["question"]["recallAt10"] == 0.0
    assert "recallAt10" not in summary
    assert len(results) == 2


def test_the_summary_states_no_threshold() -> None:
    # P29: the bar is the owner's, set from the measurement. A default here would
    # quietly become that bar.
    summary = summarize((), k=10)
    assert "owner" in summary["thresholds"]
    assert "pass" not in summary


def test_mrr_divides_by_every_question_not_only_the_found_ones() -> None:
    _, summary = evaluate(
        _searcher({"a": ["wanted"], "b": []}),
        [_question("a", "a", ["wanted"]), _question("b", "b", ["wanted"])],
    )
    assert summary["byKind"]["probe"]["mrr"] == 0.5


@pytest.mark.parametrize(
    ("values", "fraction", "expected"),
    [
        ([1.0], 0.95, 1.0),
        ([1.0, 2.0, 3.0, 4.0], 0.5, 2.0),
        ([float(index) for index in range(1, 21)], 0.95, 19.0),
    ],
)
def test_percentile_is_nearest_rank(values: list[float], fraction: float, expected: float) -> None:
    assert _percentile(values, fraction) == expected


def test_a_gold_set_without_expected_documents_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(
            {
                "questions": [
                    {"questionId": "a", "kind": "probe", "question": "q", "expectedDocuments": []}
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GoldSetError, match="expectedDocuments"):
        load_gold_set(path)


def test_an_unknown_question_kind_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(
            {
                "questions": [
                    {
                        "questionId": "a",
                        "kind": "vibes",
                        "question": "q",
                        "expectedDocuments": ["d"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GoldSetError, match="probe or question"):
        load_gold_set(path)


def test_a_repeated_question_id_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "gold.json"
    entry = {"questionId": "a", "kind": "probe", "question": "q", "expectedDocuments": ["d"]}
    path.write_text(json.dumps({"questions": [entry, dict(entry)]}), encoding="utf-8")
    with pytest.raises(GoldSetError, match="repeated id"):
        load_gold_set(path)


def test_the_shipped_probe_set_loads() -> None:
    name, questions = load_gold_set(PROBES)
    assert name == "vertical-slice-probes-2026-08-22"
    assert len(questions) >= 20
    assert {question.kind for question in questions} == {"probe"}
    assert {question.scope for question in questions} == {"current"}
    assert len({question.question_id for question in questions}) == len(questions)


def test_a_probe_phrase_is_taken_whole_from_one_line() -> None:
    # The generator's whole job is that the phrase is a literal substring. The three
    # ways an earlier version broke that - collapsing whitespace, cutting mid-word,
    # crossing a chunk boundary - all produced a gold set that measured itself.
    from build_probe_gold_set import phrase_from_line

    chunk = (
        "Short heading\n"
        "Agentic project management changes what a project manager does every day, "
        "and the change is not only in tooling but in who decides.\n"
        "Another line."
    )
    phrase = phrase_from_line(chunk)
    assert phrase is not None
    assert phrase in chunk
    assert "\n" not in phrase
    assert not phrase.startswith(" ") and not phrase.endswith(" ")


def test_a_chunk_of_short_lines_yields_no_probe() -> None:
    from build_probe_gold_set import phrase_from_line

    # The YouTube footer: every line is one or two words.
    assert phrase_from_line("Info\nPresse\nUrheberrecht\nKontakt\n(c) 2026 Google LLC") is None
