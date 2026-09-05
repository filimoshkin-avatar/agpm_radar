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


def test_no_gazette_issue_is_shipped_with_the_application() -> None:
    """An issue is published content, and nothing about it lives in a release.

    It used to live in three lists at once - `apps/web`, `WEB_PATHS` and
    `_BUNDLED_GAZETTE_ISSUES` - and naming it in one and not another cost two
    releases: the file sat in the checkout, index.html linked it, the web role
    shipped without it, and every test stayed green because the tests read the
    checkout and the reader reads the artifact. Since 2026-09-05 an issue is
    published like any other content, `/api/gazettes` says which one is current,
    and there is no list to disagree with another. This is the gate that keeps
    one from growing back.
    """
    from packages.deployment.artifacts import WEB_PATHS

    web = Path(__file__).resolve().parents[1] / "apps/web"
    assert not list(web.glob("gazette-*.html")), "a gazette issue is back in apps/web"
    assert not [path for path in WEB_PATHS if "/gazette-" in path], (
        "a gazette issue is back in the web role"
    )
    import re

    html = (web / "index.html").read_text(encoding="utf-8")
    # A year inside the substring would blind this half of the gate from 2027.
    assert not re.search(r"gazette-\d{8}", html), (
        "index.html names an issue instead of asking the API"
    )
    # And no month typed in by hand: the archive button is filled from the API
    # answer, and a month written into the markup would outlive every new issue.
    assert re.search(r'id="gazetteIssue"[^>]*>\s*<i[^>]*></i>\s*<b></b>', html), (
        "the archive button names a month instead of taking it from the API"
    )
