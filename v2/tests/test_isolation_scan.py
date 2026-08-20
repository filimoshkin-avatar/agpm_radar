"""Regression checks for text and pinned binary isolation scanning."""

from __future__ import annotations

from pathlib import Path

from tools.check_isolation import ALLOWED_BINARY_ASSETS, ALLOWED_WEB_URLS, scan_workspace


def test_pinned_social_image_is_allowed_without_weakening_isolation_scan() -> None:
    assert {
        Path("apps/web/og-image-20260803.png"): (
            "1805d2711f4f7a4dd6118afc9900a314472383ace8ad9c0c98c26281f0c2b430"
        )
    } == ALLOWED_BINARY_ASSETS
    failures, scanned, fixtures = scan_workspace()
    assert failures == []
    assert scanned > 0
    assert fixtures > 0


def test_only_canonical_social_metadata_urls_are_allowlisted() -> None:
    assert {
        "https://radar.agpm.space/",
        "https://radar.agpm.space/og-image-20260803.png",
    } == ALLOWED_WEB_URLS
