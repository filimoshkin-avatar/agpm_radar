"""Stage 2 API identity tests."""

from __future__ import annotations

import json

import pytest
from apps.api import status_payload
from apps.api.__main__ import main


def test_status_payload_is_deterministic() -> None:
    assert status_payload() == {
        "application": "radar-v2-api",
        "contractFamily": "radar-v2/1",
        "contractVersion": "1.0.0",
        "stage": "stage-2-skeleton",
        "status": "skeleton",
    }


def test_preflight_cli_prints_identity(capsys: pytest.CaptureFixture[str]) -> None:
    assert main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == status_payload()
