"""Grounding invariants for analysis generated from the final V2 composition."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
from packages.contracts.analysis import issue_content_hash
from packages.domain.snapshot import JsonObject
from tools.generate_v2_analysis import (
    MAX_ATTEMPTS,
    V2AnalysisError,
    _model_payload,
    generate_v2_analysis,
    validate_v2_analysis,
)


def _materials() -> list[JsonObject]:
    return [
        cast(
            JsonObject,
            {
                "materialId": f"mat_{index}",
                "title": f"Материал {index}",
                "summary": f"Содержание {index}",
                "agpmTakeaway": f"Вывод {index}",
                "rubrics": ["governance_control"],
                "perimeter": "mid",
            },
        )
        for index in range(1, 7)
    ]


def _long_block(label: str) -> str:
    paragraph = f"{label} " + ("Содержательный управленческий вывод для проектного контура. " * 8)
    return "\n\n".join([paragraph, paragraph, paragraph])


def _theses() -> list[dict[str, str]]:
    return [
        {"lead": f"Тезис {index}.", "rest": "Основание по среднему периметру."}
        for index in range(1, 5)
    ]


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
                "theses": _theses(),
                "evidence_material_ids": ["mat_2", "mat_5"],
                "input_content_hash": content_hash,
            },
        ),
        materials=materials,
        content_hash=content_hash,
    )
    assert result["evidence_material_ids"] == ["mat_2", "mat_5"]
    assert result["evidence_titles"] == ["Материал 2", "Материал 5"]
    assert len(cast(list[object], result["theses"])) == 4


def test_analysis_rejects_thesis_about_absent_perimeter() -> None:
    materials = _materials()
    content_hash = issue_content_hash(materials)
    theses = _theses()
    theses[1]["rest"] = "Два материала близкого периметра подтверждают вывод."
    with pytest.raises(V2AnalysisError, match="absent V2 perimeters"):
        validate_v2_analysis(
            cast(
                JsonObject,
                {
                    "signal": _long_block("Сигнал."),
                    "why_agpm": _long_block("Значение."),
                    "watch_next": "Проверить развитие сигнала. Сверить выводы и методику.",
                    "theses": theses,
                    "evidence_material_ids": ["mat_1", "mat_2"],
                    "input_content_hash": content_hash,
                },
            ),
            materials=materials,
            content_hash=content_hash,
        )


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


def _envelope(text: str) -> str:
    """The shape `openclaw infer model run --json` actually returns."""
    return json.dumps({"ok": True, "outputs": [{"mediaUrl": None, "text": text}]})


def _good_answer(content_hash: str) -> str:
    return json.dumps(
        {
            "headline": "Заголовок",
            "signal": _long_block("Сигнал."),
            "why_agpm": _long_block("Значение."),
            "watch_next": "Проверить развитие сигнала. Сверить выводы и методику.",
            "theses": _theses(),
            "evidence_material_ids": ["mat_1", "mat_2"],
            "input_content_hash": content_hash,
        },
        ensure_ascii=False,
    )


def test_a_fenced_answer_is_read_not_rejected() -> None:
    # What a real call returned: the model wrapped its JSON in ```json even though
    # the prompt asked for JSON only. Legacy strips this; the V2 port had not.
    payload = _model_payload(_envelope('```json\n{"headline": "ok"}\n```'))

    assert payload["headline"] == "ok"


def test_prose_around_the_object_is_read_not_rejected() -> None:
    payload = _model_payload(_envelope('Here is the analysis:\n{"headline": "ok"}\nDone.'))

    assert payload["headline"] == "ok"


def test_a_hung_attempt_costs_one_attempt_not_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materials = _materials()
    content_hash = issue_content_hash(materials)
    calls: list[int] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, 180)
        return subprocess.CompletedProcess(command, 0, _envelope(_good_answer(content_hash)), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = generate_v2_analysis(
        issue_date="2026-08-28",
        materials=materials,
        artifacts_root=tmp_path / "llm-analysis",
    )

    assert len(calls) == 2
    assert result["evidence_material_ids"] == ["mat_1", "mat_2"]
    assert (tmp_path / "llm-analysis" / "response-attempt-1.json").exists()


def test_every_attempt_timing_out_is_a_bounded_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def always_hangs(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 180)

    monkeypatch.setattr(subprocess, "run", always_hangs)
    with pytest.raises(V2AnalysisError, match=f"after {MAX_ATTEMPTS} attempts"):
        generate_v2_analysis(
            issue_date="2026-08-28",
            materials=_materials(),
            artifacts_root=tmp_path / "llm-analysis",
        )
