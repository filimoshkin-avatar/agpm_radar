"""Grounding invariants for analysis generated from the final V2 composition."""

from __future__ import annotations

from typing import cast

import pytest
from packages.contracts.analysis import issue_content_hash
from packages.domain.snapshot import JsonObject
from tools.generate_v2_analysis import V2AnalysisError, validate_v2_analysis


def _materials() -> list[JsonObject]:
    return [
        cast(
            JsonObject,
            {
                "materialId": f"mat_{index}",
                "title": f"Материал {index}",
                "summary": f"Содержание {index}",
                "rubrics": ["governance_control"],
                "perimeter": "mid",
            },
        )
        for index in range(1, 7)
    ]


def _long_block(label: str) -> str:
    paragraph = f"{label} " + ("Содержательный управленческий вывод для проектного контура. " * 8)
    return "\n\n".join([paragraph, paragraph, paragraph])


def test_analysis_is_bound_to_final_six_material_composition() -> None:
    materials = _materials()
    content_hash = issue_content_hash(materials)
    result = validate_v2_analysis(
        cast(
            JsonObject,
            {
                "headline": "Заголовок",
                "signal": _long_block("Сигнал."),
                "why_agpm": _long_block("Значение."),
                "watch_next": "Проверить развитие сигнала. Сверить выводы и методику.",
                "evidence_material_ids": ["mat_2", "mat_5"],
                "input_content_hash": content_hash,
            },
        ),
        materials=materials,
        content_hash=content_hash,
    )
    assert result["evidence_material_ids"] == ["mat_2", "mat_5"]
    assert result["evidence_titles"] == ["Материал 2", "Материал 5"]


def test_analysis_rejects_evidence_from_excluded_legacy_material() -> None:
    materials = _materials()
    content_hash = issue_content_hash(materials)
    with pytest.raises(V2AnalysisError, match="outside the V2 issue"):
        validate_v2_analysis(
            cast(
                JsonObject,
                {
                    "headline": "Заголовок",
                    "signal": "Сигнал",
                    "why_agpm": "Значение",
                    "watch_next": "Наблюдение",
                    "evidence_material_ids": ["mat_legacy_excluded"],
                    "input_content_hash": content_hash,
                },
            ),
            materials=materials,
            content_hash=content_hash,
        )


def test_analysis_rejects_stale_composition_hash() -> None:
    materials = _materials()
    content_hash = issue_content_hash(materials)
    with pytest.raises(V2AnalysisError, match="input_content_hash"):
        validate_v2_analysis(
            cast(
                JsonObject,
                {
                    "headline": "Заголовок",
                    "signal": "Сигнал",
                    "why_agpm": "Значение",
                    "watch_next": "Наблюдение",
                    "evidence_material_ids": ["mat_1"],
                    "input_content_hash": "0" * 64,
                },
            ),
            materials=materials,
            content_hash=content_hash,
        )


def test_analysis_rejects_compact_model_response() -> None:
    materials = _materials()
    content_hash = issue_content_hash(materials)
    with pytest.raises(V2AnalysisError, match="quality gate failed"):
        validate_v2_analysis(
            cast(
                JsonObject,
                {
                    "headline": "Заголовок",
                    "signal": "Один короткий абзац.",
                    "why_agpm": "Один короткий абзац.",
                    "watch_next": "Одно предложение.",
                    "evidence_material_ids": ["mat_1", "mat_2"],
                    "input_content_hash": content_hash,
                },
            ),
            materials=materials,
            content_hash=content_hash,
        )


def test_analysis_rejects_material_ids_in_reader_facing_text() -> None:
    materials = _materials()
    content_hash = issue_content_hash(materials)
    with pytest.raises(V2AnalysisError, match="material_id запрещены"):
        validate_v2_analysis(
            cast(
                JsonObject,
                {
                    "headline": "Заголовок",
                    "signal": _long_block("Материалы mat_1 и mat_2 подтверждают сигнал."),
                    "why_agpm": _long_block("Значение."),
                    "watch_next": "Проверить развитие сигнала. Сверить выводы и методику.",
                    "evidence_material_ids": ["mat_1", "mat_2"],
                    "input_content_hash": content_hash,
                },
            ),
            materials=materials,
            content_hash=content_hash,
        )
