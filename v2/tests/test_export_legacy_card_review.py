from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from packages.legacy_bridge.importer import deterministic_id
from tools.export_legacy_card_review import export_review

VERSION = "openclaw-card-summary-ru-v4"


def _legacy(path: Path, rows: list[tuple[str, str, str, str | None, str, str]]) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE material_llm_summaries(
              material_id TEXT PRIMARY KEY, issue_date TEXT, short_text TEXT, agpm_angle TEXT,
              model TEXT, status TEXT, prompt_version TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO material_llm_summaries VALUES (?, '2026-09-02', ?, ?, ?, ?, ?)", rows
        )
    return path


def _v2(path: Path, legacy_ids: list[str]) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE issues(issue_id TEXT, issue_date TEXT)")
        connection.execute("CREATE TABLE issue_materials(issue_id TEXT, material_id TEXT)")
        connection.execute("INSERT INTO issues VALUES ('iss_1', '2026-09-02')")
        connection.executemany(
            "INSERT INTO issue_materials VALUES ('iss_1', ?)",
            [(deterministic_id("material", legacy_id),) for legacy_id in legacy_ids],
        )
    return path


def test_only_new_texts_of_v2_materials_are_exported(tmp_path: Path) -> None:
    legacy = _legacy(
        tmp_path / "legacy.sqlite",
        [
            ("a", "Факты A", "Вывод A", "openai/gpt-5.5", "success", VERSION),
            ("b", "Факты B", "Вывод B", "minimax/MiniMax-M3", "success", VERSION),
            (
                "c",
                "Старый C",
                "Старый C",
                "openai/gpt-5.5",
                "success",
                "openclaw-card-summary-ru-v3",
            ),
            ("d", "", "", None, "fallback", VERSION),
            ("e", "Факты E", "Вывод E", "openai/gpt-5.5", "success", VERSION),
        ],
    )
    v2 = _v2(tmp_path / "v2.sqlite", ["a", "b", "c", "d"])

    review, stats = export_review(
        legacy_db=legacy, v2_db=v2, issue_date="2026-09-02", prompt_version=VERSION
    )

    cards = review["cards"]
    assert isinstance(cards, list)
    assert [card["materialId"] for card in cards if isinstance(card, dict)] == [
        deterministic_id("material", "a"),
        deterministic_id("material", "b"),
    ]
    assert review["model"] == "openai/gpt-5.5"
    assert review["promptVersion"] == VERSION
    assert stats == {"exported": 2, "not_in_v2_issue": 1, "v2_untouched": 2, "without_new_text": 2}


def test_issue_without_new_texts_is_refused(tmp_path: Path) -> None:
    legacy = _legacy(
        tmp_path / "legacy.sqlite",
        [("a", "Старый", "Старый", "openai/gpt-5.5", "success", "openclaw-card-summary-ru-v3")],
    )
    v2 = _v2(tmp_path / "v2.sqlite", ["a"])
    with pytest.raises(ValueError, match="no card of 2026-09-02 carries"):
        export_review(legacy_db=legacy, v2_db=v2, issue_date="2026-09-02", prompt_version=VERSION)


def test_absent_v2_issue_is_refused(tmp_path: Path) -> None:
    legacy = _legacy(tmp_path / "legacy.sqlite", [])
    v2 = _v2(tmp_path / "v2.sqlite", [])
    with pytest.raises(ValueError, match="V2 issue is absent"):
        export_review(legacy_db=legacy, v2_db=v2, issue_date="2026-01-01", prompt_version=VERSION)
