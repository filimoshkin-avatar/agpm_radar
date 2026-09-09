"""The public repository and browser consume the same independently authored examples."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from apps.api.public_data import PublicDataRepository, _card_search_text
from packages.contracts.json_types import JsonObject


def test_search_matches_compact_rubric_labels_instead_of_hidden_full_titles() -> None:
    card: JsonObject = {"rubrics": ["agpm_pmo_portfolio", "mcp_gateways_infra"]}
    text = _card_search_text(
        card,
        {
            "agpm_pmo_portfolio": "AgPM, PMO и портфели",
            "mcp_gateways_infra": "Инфраструктура агентов и MCP",
        },
    )
    assert "agpm / pmo" in text
    assert "mcp / инфраструктура" in text
    assert "портфели" not in text


def test_all_query_fragments_match_only_visible_card_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/synthetic/search-matching.json").read_text()
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE pub_material_rubrics_v1 (rubric_id TEXT, title TEXT)")
        repository = PublicDataRepository(connection)

        def materials(_period: str) -> list[JsonObject]:
            return [cast(JsonObject, fixture["card"])]

        monkeypatch.setattr(repository, "_period_materials", materials)
        for case in fixture["queries"]:
            items, cursor = repository.materials(
                period="30d", perimeter=None, rubric=None, query=case["q"], offset=0, limit=100
            )
            assert bool(items) == case["matches"], case["q"]
            assert cursor is None
    finally:
        connection.close()
