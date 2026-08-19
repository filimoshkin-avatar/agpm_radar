"""Synthetic fixture provenance tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from apps.api import status_payload

V2_ROOT = Path(__file__).resolve().parents[1]


def test_every_fixture_is_explicitly_synthetic() -> None:
    fixture_paths = sorted((V2_ROOT / "fixtures").rglob("*.json"))
    assert fixture_paths
    for path in fixture_paths:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        fixture = cast(dict[str, object], parsed)
        assert fixture["fixtureKind"] == "synthetic"
        assert fixture["containsProductionData"] is False


def test_component_fixture_uses_only_synthetic_identity() -> None:
    path = V2_ROOT / "fixtures/synthetic/component-status.json"
    parsed = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    assert parsed["payload"] == status_payload()
