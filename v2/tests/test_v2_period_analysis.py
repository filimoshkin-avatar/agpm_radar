from __future__ import annotations

import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import cast

import pytest
from packages.domain.snapshot import JsonObject
from tools import v2_period_analysis
from tools.v2_period_analysis import (
    PROMPT_BUDGET_BYTES,
    TEXT_CAPS,
    PeriodAnalysisError,
    _fit_context,
    _prompt,
    _shorten,
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
    # Zero, not one: the model was never asked. `attempts` counts paid calls,
    # and the owner counts them too.
    assert result["attempts"] == 0
    assert "argv limit" in str(result["error"])


def _window(issues: int, per_issue: int, text_chars: int) -> list[JsonObject]:
    """A window of `issues` days, each carrying `per_issue` materials."""
    perimeters = ("near", "mid", "far")
    start = date(2026, 6, 1)
    documents: list[JsonObject] = []
    for day in range(issues):
        materials = [
            {
                "title": f"Материал {day:02d}-{index:02d} про агентное управление",
                "llmShortText": "Факт из статьи. " * (text_chars // 16 + 1),
                "llmAgpmAngle": "Вывод для AgPM. " * (text_chars // 16 + 1),
                "summary": "",
                "agpmTakeaway": "",
                "brief": "",
                "perimeter": perimeters[index % 3],
                "rubrics": ["governance_control"],
                "sourceName": f"Источник {index}",
            }
            for index in range(per_issue)
        ]
        issue_date = (start + timedelta(days=day)).isoformat()
        documents.append(cast(JsonObject, {"issueDate": issue_date, "materials": materials}))
    return documents


def test_the_whole_window_reaches_the_model_under_the_argv_ceiling() -> None:
    """Measured 2026-09-05: the 30-day window held 196 materials and the model saw 60.

    The prompt is one argv string, so the ceiling is real; what it bounds is now
    how much of each material travels, not how many of them do.
    """
    documents = _window(issues=30, per_issue=7, text_chars=900)
    context = _fit_context(documents, "30d", "2026-06-30", None)
    prompt = _prompt(context, "30d", None)

    assert context["materialCount"] == 210
    assert context["shownMaterialCount"] == 210
    assert context["omittedMaterialCount"] == 0
    assert len(prompt.encode("utf-8")) <= PROMPT_BUDGET_BYTES
    # Every title, not the first sixty.
    for document in documents:
        for material in cast(list[dict[str, object]], document["materials"]):
            assert str(material["title"]) in prompt
    # It fits because the texts were shortened, and the prompt says which way.
    assert (context["textCap"], context["angleCap"]) in TEXT_CAPS
    assert int(cast(int, context["textCap"])) < TEXT_CAPS[0][0]
    assert int(cast(int, context["textCap"])) > 0
    assert "обрезаны до" in prompt


def test_a_small_window_keeps_the_longest_texts() -> None:
    documents = _window(issues=7, per_issue=4, text_chars=300)
    context = _fit_context(documents, "7d", "2026-06-07", None)

    assert (context["textCap"], context["angleCap"]) == TEXT_CAPS[0]
    assert context["shownMaterialCount"] == 28
    assert len(_prompt(context, "7d", None).encode("utf-8")) <= PROMPT_BUDGET_BYTES


def test_the_seven_day_theses_travel_inside_the_same_ceiling() -> None:
    """The 30-day prompt carries the week's theses too; the budget covers them."""
    documents = _window(issues=30, per_issue=7, text_chars=900)
    previous = cast(list[JsonObject], _theses("Неделя"))
    context = _fit_context(documents, "30d", "2026-06-30", previous)

    assert context["shownMaterialCount"] == 210
    assert len(_prompt(context, "30d", previous).encode("utf-8")) <= PROMPT_BUDGET_BYTES


def test_a_window_too_large_even_for_titles_says_what_it_dropped() -> None:
    documents = _window(issues=60, per_issue=40, text_chars=0)
    context = _fit_context(documents, "30d", "2026-07-30", None)
    prompt = _prompt(context, "30d", None)

    assert context["materialCount"] == 2400
    assert 0 < int(cast(int, context["shownMaterialCount"])) < 2400
    assert context["omittedMaterialCount"] == 2400 - int(cast(int, context["shownMaterialCount"]))
    assert len(prompt.encode("utf-8")) <= PROMPT_BUDGET_BYTES
    assert "Показано" in prompt


def test_a_shortened_text_ends_on_a_word_and_says_it_continues() -> None:
    assert _shorten("короткий текст", 100) == "короткий текст"
    cut = _shorten("одно два три четыре пять шесть семь восемь", 20)
    assert cut.endswith("…")
    assert len(cut) <= 21
    assert "четыр" not in cut or cut.startswith("одно два три четыре")
    assert _shorten("что угодно", 0) == ""
    assert _shorten(None, 100) == ""
