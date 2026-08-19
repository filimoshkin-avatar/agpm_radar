"""Production-artifact CLI regression tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tools.build_production_artifact import ARTIFACT_NAME, ARTIFACT_PREFIX, main


def test_external_output_directory_is_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An absolute output path outside V2_ROOT must not fail after a valid build."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_production_artifact.py", "--check", "--output-dir", str(tmp_path)],
    )

    assert main() == 0

    manifest_path = tmp_path / f"{ARTIFACT_PREFIX}.manifest.json"
    assert (tmp_path / ARTIFACT_NAME).is_file()
    assert manifest_path.is_file()
    assert f"Manifest: {manifest_path}" in capsys.readouterr().out
