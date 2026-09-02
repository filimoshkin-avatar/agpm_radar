# ruff: noqa: RUF001

"""The search projection carries exactly the texts a card shows.

A card shows the model's short text and angle when its analysis succeeded, and the
rule-based brief and takeaway otherwise. Migration 0003 makes the search view follow the
same rule, so a search hit is always visible on the card that produced it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from packages.storage.hashing import rebuild_and_check_fts
from packages.storage.migrations import create_database

STAMP = "2026-09-02T18:00:00Z"
HASH = "0" * 64


def _seed(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO issues(issue_id, issue_date, issue_number, title, brief, lifecycle_status,"
            " published_at, publication_origin, empty_reason, content_hash, created_at, updated_at)"
            " VALUES ('iss_1', '2026-09-02', 1, 'Выпуск', NULL, 'published', NULL,"
            " 'legacy_inferred', NULL, ?, ?, ?)",
            (HASH, STAMP, STAMP),
        )
        for material_id, title in (
            ("mat_llm", "Статья с текстом модели"),
            ("mat_rule", "Статья без"),
        ):
            connection.execute(
                "INSERT INTO materials(material_id, title, url, canonical_url, source_name,"
                " published_at, publication_date_status, summary, agpm_takeaway, brief,"
                " content_hash, created_at, updated_at)"
                " VALUES (?, ?, ?, NULL, 'Источник', NULL, 'resolved', 'общее описание',"
                " 'общий вывод', NULL, ?, ?, ?)",
                (material_id, title, f"https://example.test/{material_id}", HASH, STAMP, STAMP),
            )
        for order, material_id in enumerate(("mat_llm", "mat_rule")):
            connection.execute(
                "INSERT INTO issue_materials(issue_id, material_id, sort_order, perimeter, verdict,"
                " summary, agpm_takeaway, brief, theses_json, trend_notes, flags_json,"
                " key_material, signal_score, signal_strength, created_at, updated_at)"
                " VALUES ('iss_1', ?, ?, 'near', 'core', 'шаблон описания', 'шаблон вывода',"
                " 'краткий шаблон', '[]', NULL, '{}', 0, NULL, 'strong', ?, ?)",
                (material_id, order, STAMP, STAMP),
            )
        connection.execute(
            "INSERT INTO material_analysis(issue_id, material_id, short_text, agpm_angle,"
            " llm_status, requested_model, effective_model, provider, prompt_version, updated_at)"
            " VALUES ('iss_1', 'mat_llm', 'Klarna передала агенту 2,3 млн чатов',"
            " 'Выбирать первый сценарий там, где есть baseline', 'success', 'openai/gpt-5.5',"
            " 'openai/gpt-5.5', 'openai', 'openclaw-card-summary-ru-v4', ?)",
            (STAMP,),
        )
        connection.execute(
            "INSERT INTO material_analysis(issue_id, material_id, short_text, agpm_angle,"
            " llm_status, requested_model, effective_model, provider, prompt_version, updated_at)"
            " VALUES ('iss_1', 'mat_rule', NULL, NULL, 'fallback', NULL, NULL, NULL,"
            " 'openclaw-card-summary-ru-v4', ?)",
            (STAMP,),
        )
        connection.commit()


def test_search_documents_carry_the_shown_texts(tmp_path: Path) -> None:
    database = tmp_path / "search.sqlite"
    create_database(database, applied_at=STAMP)
    _seed(database)
    with sqlite3.connect(database) as connection:
        assert rebuild_and_check_fts(connection) == 2
        rows = {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT material_id, summary, agpm_takeaway FROM published_materials_fts"
            )
        }
        hits = {
            str(row[0])
            for row in connection.execute(
                "SELECT material_id FROM published_materials_fts WHERE published_materials_fts"
                " MATCH 'Klarna'"
            )
        }
    assert rows["mat_llm"] == (
        "Klarna передала агенту 2,3 млн чатов",
        "Выбирать первый сценарий там, где есть baseline",
    )
    assert rows["mat_rule"] == ("краткий шаблон", "шаблон вывода")
    assert hits == {"mat_llm"}
