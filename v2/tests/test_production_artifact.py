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


def test_every_gazette_issue_is_shipped_routed_and_linked() -> None:
    """The three places a gazette issue has to be named, checked against each other.

    Naming an issue in one list and not the other has now cost two releases: the
    file sits in apps/web, index.html links it, and the web role ships without it,
    so the reader gets a 404 while every test stays green - the tests read the
    checkout, not the artifact. A comment above the list asked twice; this asks in
    the gate.
    """
    import re

    from apps.api.application import _BUNDLED_GAZETTE_ISSUES
    from packages.deployment.artifacts import WEB_PATHS

    web = Path(__file__).resolve().parents[1] / "apps/web"
    on_disk = {path.name for path in web.glob("gazette-*.html")}
    shipped = {path.rsplit("/", 1)[-1] for path in WEB_PATHS if "/gazette-" in path}
    routed = {path.removeprefix("/") for path in _BUNDLED_GAZETTE_ISSUES}
    html = (web / "index.html").read_text(encoding="utf-8")
    linked = set(re.findall(r"gazette-[0-9A-Za-z-]+\.html", html)) | {
        f"gazette-{issue}.html" for issue in re.findall(r'data-gazette-issue="([^"]+)"', html)
    }

    assert shipped == on_disk, "WEB_PATHS and apps/web disagree about which issues exist"
    assert routed == on_disk, "_BUNDLED_GAZETTE_ISSUES and apps/web disagree"
    assert linked <= shipped, (
        f"index.html links issues the web role does not ship: {linked - shipped}"
    )
