"""Regression tests for Stage 14/15 daily narrative reconciliation."""

# ruff: noqa: RUF001

from typing import cast

import pytest
from tools.build_stage14_correction import _flag_names
from tools.build_stage14_daily import _analysis, _reconcile_narrative


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
