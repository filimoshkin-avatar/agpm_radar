# ruff: noqa: RUF001

from __future__ import annotations

import pytest
from packages.domain.snapshot import JsonObject
from tools.generate_v2_card_review import CardReviewError, _validate


def _card(material_id: str, short_text: str, agpm_angle: str) -> JsonObject:
    return {
        "materialId": material_id,
        "shortText": short_text,
        "agpmAngle": agpm_angle,
    }


def test_validate_rejects_matching_leads() -> None:
    common = "Материал показывает переход от отдельных помощников к агентным процессам"
    raw: JsonObject = {
        "cards": [
            _card(
                "a",
                f"{common}, где контролируются расходы. " * 3,
                "Первый вывод для управления. " * 8,
            ),
            _card(
                "b",
                f"{common}, где координируются операции. " * 3,
                "Второй вывод для портфеля. " * 8,
            ),
        ]
    }

    with pytest.raises(CardReviewError, match="semantically repetitive shortText"):
        _validate(raw, {"a", "b"})


def test_validate_accepts_distinct_card_texts() -> None:
    raw: JsonObject = {
        "cards": [
            _card(
                "a",
                "Исследование разбирает контроль затрат автономных операций и связывает лимиты с журналом действий. "
                * 2,
                "PMO следует учитывать стоимость каждого подтверждённого результата и заранее задавать финансовые ограничения. "
                * 2,
            ),
            _card(
                "b",
                "Кейс описывает портфельную разведку: система собирает сигналы компаний и обновляет представление фонда. "
                * 2,
                "Для AgPM здесь важны границы автономии при изменении портфельной оценки и человеческое подтверждение эскалаций. "
                * 2,
            ),
        ]
    }

    assert len(_validate(raw, {"a", "b"})) == 2
