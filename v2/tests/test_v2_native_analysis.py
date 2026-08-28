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


def test_analysis_is_bound_to_final_six_material_composition() -> None:
    materials = _materials()
    content_hash = issue_content_hash(materials)
    result = validate_v2_analysis(
        cast(
            JsonObject,
            {
                "headline": "Заголовок",
                "signal": "Сигнал",
                "why_agpm": "Значение",
                "watch_next": "Наблюдение",
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
