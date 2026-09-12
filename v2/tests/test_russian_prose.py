"""Regressions for untranslated non-English source text and source-title isolation."""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from packages.contracts.russian_prose import (
    brand_words,
    foreign_script_fragments,
    require_russian_prose,
)
from tools.build_stage14_daily import _material

TITLE = "PKSHAグループ、PMO業務を変革する「AI Powered PMO」"


@pytest.mark.parametrize(
    "text",
    [
        "Агент собирает и整理ует данные.",
        "агент分析ирует",
        "Обновление かな",
        "Данные 한글",
        "Ответ مرحبا",
        "Текст עזרה",
        "Сводка Δοκιμή",
        "Данные 𠀀",
        "Агент выполняет governance и workflows.",
    ],
)
def test_untranslated_prose_is_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="RUSSIAN_PROSE_GATE"):
        require_russian_prose(text, "summary")


def test_proper_names_and_russian_transcription_pass() -> None:
    require_russian_prose(
        "PKSHA Technology и X Capital предлагают AI Powered PMO. Сатоси Накамото описал механизм. Компания monday выпускает monday sidekick.",
        "summary",
        brand_words("https://monday.com/blog"),
    )
    assert not foreign_script_fragments("Проектный офис упорядочивает данные. ИИ проверяет задачи.")


def test_daily_gate_preserves_japanese_title_but_rejects_prose() -> None:
    raw: dict[str, object] = {
        "id": "pksha",
        "url": "https://example.org/pksha",
        "title": TITLE,
        "summary": "PKSHA Technology представила сервис для проектного офиса.",
    }
    assert _material(raw, 1)["title"] == TITLE
    for field in ("summary", "brief", "agpm_takeaway"):
        with pytest.raises(ValueError, match="RUSSIAN_PROSE_GATE"):
            _material(raw | {field: "Агент整理ует данные."}, 1)
    with pytest.raises(ValueError, match="RUSSIAN_PROSE_GATE"):
        _material(
            raw
            | {
                "llm_summary": {
                    "status": "success",
                    "short_text": "Агент整理ует данные.",
                    "agpm_angle": "Проверяйте качество сведений.",
                }
            },
            1,
        )


def test_live_legacy_language_and_report_paths() -> None:
    script = """
import sys
sys.path.insert(0, sys.argv[1])
import agpm_radar_openclaw_analysis as cards
import agpm_radar_report as report
source = 'PKSHA Technology X Capital 2026 1,313'
title = 'PKSHAグループ、PMO業務を変革する「AI Powered PMO」'
card = {'short_text': 'PKSHA Technology и X Capital с августа 2026 года предлагают сервис AI Powered PMO. Агент整理ует сведения из задач, протоколов и чатов.', 'agpm_angle': 'Проверьте, какие сведения нужны руководителям для принятия решений и кто отвечает за качество данных. Начните с одного проекта и сравните задержки.'}
try:
    cards.validate_card_text(card, source_text=source, title=title)
except RuntimeError as error:
    assert 'RUSSIAN_PROSE_GATE' in str(error)
else:
    raise AssertionError('mixed Japanese/Russian word passed')
card['short_text'] = card['short_text'].replace('Агент整理ует', 'Агент упорядочивает')
cards.validate_card_text(card, source_text=source, title=title)
for summary in ('日本語のみ', 'Агент整理ует сведения.', '', 'Материал «' + title + '» относится к проектному управлению.'):
    item = {'title': title, 'summary': summary, 'url': 'https://example.org/pksha'}
    paragraphs = report.general_material_summary(item)
    text = ' '.join(paragraphs)
    assert title not in text, text
    assert not report.foreign_script_fragments(text), text
    assert not report.foreign_latin_words(text), text
assert report.needs_russian_summary('Агент использует governance для контроля workflows.')
assert 'только в отдельном названии статьи' in cards.card_prompt({'id': 'pksha'}, source, [])
"""
    completed = subprocess.run(  # noqa: S603
        [
            "/usr/bin/python3",
            "-c",
            script,
            str(Path(__file__).resolve().parents[2] / "pipeline/scripts"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_japanese_sources_can_ground_russian_theses_without_quoted_titles() -> None:
    from typing import cast

    from packages.domain.snapshot import JsonObject
    from tools.generate_v2_analysis import _thesis_violations

    materials = [cast(JsonObject, {"materialId": "mat_pksha", "title": TITLE, "perimeter": "near"})]
    rest = (
        "PKSHA Technology передаёт сбор данных агенту. Он проверяет задачи, протоколы и переписку. "
        "Для руководителя проекта это означает необходимость определить, кто проверяет качество сведений, "
        "подтверждает изменения и отвечает за своевременное информирование смежных команд. "
        "Пилот должен показать, насколько сократились задержки передачи информации, пропуски обновлений "
        "и ручной труд сотрудников проектного офиса."
    )
    theses = [
        {"lead": f"Вывод {i}.", "rest": rest, "evidence_material_ids": ["mat_pksha"]}
        for i in range(4)
    ]
    assert _thesis_violations(theses, materials=materials) == []
    theses[0]["evidence_material_ids"] = ["mat_outside"]
    assert any(
        "unknown material" in error for error in _thesis_violations(theses, materials=materials)
    )
