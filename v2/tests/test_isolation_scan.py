"""Regression checks for text and pinned binary isolation scanning."""

from __future__ import annotations

from pathlib import Path

from tools.check_isolation import ALLOWED_BINARY_ASSETS, ALLOWED_WEB_URLS, scan_workspace


def test_pinned_social_image_is_allowed_without_weakening_isolation_scan() -> None:
    assert {
        Path("apps/web/fonts/GolosText[wght].ttf"): (
            "17bb58fb69aec2dfb047a2ebf52534023e9b688c97a6b7ac795b0a72912c2063"
        ),
        Path("apps/web/fonts/PTMono-Regular.ttf"): (
            "cbe732b3b8fd211fd986ebdfc9b870ddeca4faab0bb5425fc509b37f9b4ac804"
        ),
        Path("apps/web/og-image-20260803.png"): (
            "1805d2711f4f7a4dd6118afc9900a314472383ace8ad9c0c98c26281f0c2b430"
        ),
    } == ALLOWED_BINARY_ASSETS
    failures, scanned, fixtures = scan_workspace()
    assert failures == []
    assert scanned > 0
    assert fixtures > 0


def test_only_canonical_social_metadata_urls_are_allowlisted() -> None:
    assert {
        "http://www.w3.org/2000/svg",
        "https://fonts.googleapis.com",
        "https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;0,900;1,500&family=Ruslan+Display&family=Roboto+Condensed:wght@700&display=swap",
        "https://fonts.gstatic.com",
        "https://radar.agpm.space/",
        "https://radar.agpm.space/og-image-20260803.png",
    } == ALLOWED_WEB_URLS
