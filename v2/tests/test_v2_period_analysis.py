from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from packages.domain.snapshot import JsonObject
from tools import v2_period_analysis
from tools.v2_period_analysis import (
    PeriodAnalysisError,
    _validate,
    generate_period,
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


def test_an_over_long_prompt_falls_back_without_a_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured 2026-09-05: the 30-day prompt stood at 85 % of the argv ceiling.

    Past it the kernel refuses the argument before the model sees it; that is the
    period's fallback, recorded as one attempt, not a traceback ending the day.
    """

    def never(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("the model must not be asked with a prompt the kernel refuses")

    monkeypatch.setattr(subprocess, "run", never)
    monkeypatch.setattr(v2_period_analysis, "_window_documents", lambda *args, **kwargs: [])
    monkeypatch.setattr(v2_period_analysis, "_prompt", lambda *args, **kwargs: "ы" * 70_000)
    result = generate_period(
        database=tmp_path / "absent.sqlite",
        anchor="2026-09-05",
        period="7d",
        artifacts_root=tmp_path / "period",
    )

    assert result["status"] == "fallback"
    assert result["attempts"] == 1
    assert "argv limit" in str(result["error"])
