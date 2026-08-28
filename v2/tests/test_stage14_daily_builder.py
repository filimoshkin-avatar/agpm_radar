"""Regression tests for Stage 14/15 daily narrative reconciliation."""

# ruff: noqa: RUF001

from typing import cast

import pytest
from packages.domain.candidates import validate_llm_outcome
from tools.build_stage14_correction import _flag_names
from tools.build_stage14_daily import _analysis, _llm_outcome, _reconcile_narrative


def test_filtered_material_reconciles_counts_and_perimeters() -> None:
    stats = {
        "adjacent": 0,
        "core": 9,
        "cut": 150,
        "far": 5,
        "included": 9,
        "mid": 0,
        "near": 4,
        "viewed": 159,
    }
    text = (
        "Агенты переходят в контур. В выпуске 10 материалов: близкий периметр — 4, "
        "средний — 1, дальний — 5. В 10 материалах повторяется governance."
    )
    reconciled = _reconcile_narrative(text, legacy_count=10, stats=stats)
    assert reconciled == (
        "Агенты переходят в контур. В выпуске 9 материалов: близкий периметр — 4, "
        "дальний периметр — 5. В 9 материалах повторяется governance."
    )
    assert "10 материал" not in reconciled
    assert "средний" not in reconciled


def test_analysis_reconciles_inherited_legacy_claims() -> None:
    document: dict[str, object] = {
        "daily_analysis": {
            "headline": "Сигнал",
            "analysis": {"signal": "В 10 материалах повторяется governance."},
        },
        "issue": {
            "brief": "В выпуске 10 материалов: близкий периметр — 4, средний — 1, дальний — 5.",
            "theses": [{"lead": "Вывод", "rest": "Выбрано 10 материалов."}],
        },
    }
    stats = {
        "adjacent": 0,
        "core": 9,
        "cut": 150,
        "far": 5,
        "included": 9,
        "mid": 0,
        "near": 4,
        "viewed": 159,
    }
    analysis = _analysis(document, legacy_count=10, stats=stats)
    assert analysis["brief"] == (
        "В выпуске 9 материалов: близкий периметр — 4, дальний периметр — 5."
    )
    blocks = cast(list[dict[str, object]], analysis["blocks"])
    theses = cast(list[dict[str, object]], analysis["theses"])
    assert blocks[0]["text"] == "В 9 материалах повторяется governance."
    assert theses[0]["rest"] == "Выбрано 9 материалов."


def test_analysis_prefers_successful_llm_theses_over_deterministic_issue_theses() -> None:
    document: dict[str, object] = {
        "daily_analysis": {"headline": "LLM", "analysis": {"signal": "Сигнал"}},
        "issue": {"brief": "Кратко", "theses": [{"lead": "Rules", "rest": "Fallback"}]},
        "issue_llm_theses": {
            "status": "success",
            "theses": [{"lead": "LLM lead", "rest": "LLM rest"}],
        },
    }
    stats = {
        "adjacent": 1,
        "core": 0,
        "cut": 0,
        "far": 1,
        "included": 1,
        "mid": 0,
        "near": 0,
        "viewed": 1,
    }

    analysis = _analysis(document, legacy_count=1, stats=stats)

    assert analysis["theses"] == [{"lead": "LLM lead", "rest": "LLM rest"}]


def test_correction_accepts_imported_and_native_flag_shapes() -> None:
    assert _flag_names('{"security":true,"pmo":false}', material_id="mat_1") == ["security"]
    assert _flag_names('["security","governance"]', material_id="mat_1") == [
        "governance",
        "security",
    ]
    with pytest.raises(ValueError, match="accepted material flags are invalid"):
        _flag_names('["security",1]', material_id="mat_1")


def _zero_stats() -> dict[str, int]:
    return {
        "adjacent": 0,
        "core": 0,
        "cut": 0,
        "far": 0,
        "included": 1,
        "mid": 0,
        "near": 0,
        "viewed": 1,
    }


def test_analysis_maps_watch_next_to_actions_and_invents_nothing() -> None:
    """Three Legacy fields, three blocks - and no phantom `risks`/`actions` keys."""
    document: dict[str, object] = {
        "daily_analysis": {
            "headline": "Сигнал",
            "analysis": {
                "signal": "Открытый сигнал.",
                "why_agpm": "Почему это важно.",
                "watch_next": "Что смотреть дальше.",
                "risks": "мусор-риски",
                "actions": "мусор-действия",
            },
        },
        "issue": {"brief": "", "theses": []},
    }
    analysis = _analysis(document, legacy_count=1, stats=_zero_stats())
    blocks = cast(list[dict[str, object]], analysis["blocks"])
    assert [(block["kind"], block["title"]) for block in blocks] == [
        ("overview", "Сигнал"),
        ("signals", "Почему это важно для AgPM"),
        ("actions", "Что смотреть дальше"),
    ]
    assert blocks[2]["text"] == "Что смотреть дальше."
    assert all("мусор" not in str(block) for block in blocks)


def test_analysis_carries_evidence_titles_as_the_llm_chose_them() -> None:
    """The LLM's own list, in its own order: empties dropped, dupes stable."""
    document: dict[str, object] = {
        "daily_analysis": {
            "headline": "Сигнал",
            "analysis": {
                "signal": "Сигнал.",
                "evidence_titles": [
                    "Второй источник",
                    "",
                    "Первый источник",
                    "Второй источник",
                    "  Третий  ",
                    7,
                    None,
                ],
            },
        },
        "issue": {"brief": "", "theses": []},
    }
    analysis = _analysis(document, legacy_count=1, stats=_zero_stats())
    assert analysis["evidenceTitles"] == ["Второй источник", "Первый источник", "Третий"]


def test_the_native_day_records_the_model_that_wrote_the_analysis() -> None:
    outcome = _llm_outcome(native=True)

    validate_llm_outcome(outcome)
    assert outcome["status"] == "success"
    assert outcome["effective"] == {"model": "gpt-5.5", "provider": "openai"}


def test_the_fallback_day_does_not_claim_a_model_wrote_the_analysis() -> None:
    # "fallback" in this schema means a second model answered; a deterministic
    # substitute is "unavailable". The record used to be a success literal either
    # way, so a Legacy-analysis day claimed a model that never accepted anything.
    outcome = _llm_outcome(native=False)

    validate_llm_outcome(outcome)
    assert outcome["status"] == "unavailable"
    assert outcome["effective"] is None
    assert outcome["deterministicFallback"] == {
        "implementation": "legacy-analysis-import",
        "version": "1",
    }
