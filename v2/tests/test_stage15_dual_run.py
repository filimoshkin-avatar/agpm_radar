"""Stage 15 post-Legacy comparison helper regressions."""

from packages.domain.snapshot import JsonObject
from tools.run_stage15_dual import _issue_date, _llm_status, _urls


def test_legacy_and_v2_public_shapes_share_comparison_surface() -> None:
    legacy: JsonObject = {
        "issue": {"issue_date": "2026-08-20"},
        "daily_analysis": {"status": "success"},
        "materials": [
            {"canonical_url": "https://example.test/a"},
            {"url": "https://example.test/b"},
        ],
    }
    v2: JsonObject = {
        "issueDate": "2026-08-20",
        "analysis": {"status": "success"},
        "llm": {"effectiveModel": "gpt-5.5", "status": "success"},
        "materials": [
            {"canonicalUrl": "https://example.test/a"},
            {"url": "https://example.test/b"},
        ],
    }
    assert _issue_date(legacy) == _issue_date(v2) == "2026-08-20"
    assert _urls(legacy) == _urls(v2)
    assert _llm_status(legacy) == _llm_status(v2) == "success"
