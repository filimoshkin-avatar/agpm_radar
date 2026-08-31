from __future__ import annotations

import pytest
from packages.domain.snapshot import JsonObject
from tools.v2_period_analysis import (
    PeriodAnalysisError,
    _validate,
    period_blocks,
    strip_period_blocks,
)


def _theses(prefix: str) -> list[dict[str, str]]:
    subjects = (
        "контур полномочий и согласований",
        "наблюдаемость исполнения и журнал действий",
        "экономика проверки результата",
        "границы человеческой ответственности",
    )
    return [
        {
            "lead": f"{prefix}: {subjects[index - 1]} определяет масштабирование.",
            "rest": (
                f"Опора тезиса {index} находится в отдельной группе материалов окна. "
                f"Их общий предмет — {subjects[index - 1]}. Для AgPM этот сигнал задаёт "
                "самостоятельное требование к роли агента, контрольной точке и доказуемому результату."
            ),
        }
        for index in range(1, 5)
    ]


def test_validate_requires_four_non_duplicate_substantive_theses() -> None:
    result = _validate({"theses": _theses("Оперативный сигнал")})  # type: ignore[dict-item]
    assert len(result) == 4
    with pytest.raises(PeriodAnalysisError):
        _validate({"theses": _theses("Оперативный сигнал")[:3]})  # type: ignore[dict-item]


def test_period_blocks_round_trip_and_replace_old_periods() -> None:
    results = {
        "7d": {
            "attempts": 1,
            "error": None,
            "model": "openai/gpt-5.5",
            "period": "7d",
            "promptVersion": "v2-period-analysis-ru-v1",
            "provider": "openai",
            "status": "success",
            "theses": _theses("Неделя"),
        },
        "30d": {
            "attempts": 2,
            "error": None,
            "model": "openai/gpt-5.5",
            "period": "30d",
            "promptVersion": "v2-period-analysis-ru-v1",
            "provider": "openai",
            "status": "success",
            "theses": _theses("Месяц"),
        },
    }
    blocks = period_blocks(results)  # type: ignore[arg-type]
    assert len(blocks) == 10
    assert sum(str(block["title"]).endswith("метаданные") for block in blocks) == 2
    daily: JsonObject = {"kind": "overview", "title": "Сигнал", "text": "Дневной текст"}
    assert strip_period_blocks([daily, *blocks]) == [daily]
