from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from packages.domain.snapshot import JsonObject
from tools.publish_v2_card_review import _apply_review


def _projection(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE issues(issue_id TEXT, issue_date TEXT)")
        connection.execute("CREATE TABLE issue_materials(issue_id TEXT, material_id TEXT)")
        connection.execute(
            """
            CREATE TABLE material_analysis(
              issue_id TEXT, material_id TEXT, short_text TEXT, agpm_angle TEXT, llm_status TEXT,
              requested_model TEXT, effective_model TEXT, provider TEXT, prompt_version TEXT,
              updated_at TEXT
            )
            """
        )
        connection.execute("INSERT INTO issues VALUES ('iss_1', '2026-09-02')")
        for material_id in ("mat_a", "mat_b", "mat_c"):
            connection.execute("INSERT INTO issue_materials VALUES ('iss_1', ?)", (material_id,))
            connection.execute(
                "INSERT INTO material_analysis VALUES ('iss_1', ?, 'старое', 'старое', 'success',"
                " 'openai/gpt-5.5', 'openai/gpt-5.5', 'openai', 'candidate-v1', '2026-09-01T00:00:00Z')",
                (material_id,),
            )
    return path


def _review(cards: list[JsonObject]) -> JsonObject:
    return {
        "cards": list(cards),
        "issueDate": "2026-09-02",
        "model": "openai/gpt-5.5",
        "promptVersion": "openclaw-card-summary-ru-v4",
        "status": "success",
    }


def test_partial_review_updates_its_cards_and_leaves_the_rest(tmp_path: Path) -> None:
    projection = _projection(tmp_path / "projection.sqlite")
    review = _review(
        [
            {"materialId": "mat_a", "shortText": "факты A", "agpmAngle": "вывод A"},
            {
                "materialId": "mat_b",
                "shortText": "факты B",
                "agpmAngle": "вывод B",
                "model": "minimax/MiniMax-M3",
            },
        ]
    )

    assert _apply_review(projection, review) == (2, 1)

    with sqlite3.connect(projection) as connection:
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT material_id, short_text, effective_model, prompt_version"
                " FROM material_analysis ORDER BY material_id"
            )
        }
    assert rows["mat_a"] == ("факты A", "openai/gpt-5.5", "openclaw-card-summary-ru-v4")
    assert rows["mat_b"] == ("факты B", "minimax/MiniMax-M3", "openclaw-card-summary-ru-v4")
    assert rows["mat_c"] == ("старое", "openai/gpt-5.5", "candidate-v1")


def test_card_outside_the_issue_is_refused(tmp_path: Path) -> None:
    projection = _projection(tmp_path / "projection.sqlite")
    review = _review([{"materialId": "mat_zzz", "shortText": "x", "agpmAngle": "y"}])
    with pytest.raises(ValueError, match="outside the issue"):
        _apply_review(projection, review)


def test_empty_review_is_refused(tmp_path: Path) -> None:
    projection = _projection(tmp_path / "projection.sqlite")
    with pytest.raises(ValueError, match="outside the issue"):
        _apply_review(projection, _review([]))
