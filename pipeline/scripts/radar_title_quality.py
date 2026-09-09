"""Legacy transport/provenance adapter; title policy lives in V2 contracts."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import requests

# Collection also runs from the deployed OpenClaw workspace scripts directory.
repository = Path(__file__).resolve().parents[2]
if not (repository / "v2/packages/contracts/title_quality.py").is_file():
    repository = Path(os.environ.get("RADAR_ROOT", "/mnt/vdd/Radar"))
sys.path.insert(0, str(repository / "v2"))
from packages.contracts.title_quality import (  # noqa: E402,F401
    TitleCandidate, TitleQualityError, extract_title_candidates, reliable_page_title,
    resolve_title_candidate, title_reference_problem,
    repair_title_reference, require_title, resolve_title, title_problem,
)


def apply_title_markup(item: dict[str, Any], markup: str) -> None:
    resolution = resolve_title(item.get("title"), markup, str(item.get("url") or ""))
    apply_title_resolution(item, resolution)


def apply_cached_title(item: dict[str, Any], payload: dict[str, Any]) -> None:
    cached = payload.get("page_title")
    candidate = TitleCandidate(**cached) if isinstance(cached, dict) else None
    resolution = resolve_title_candidate(item.get("title"), candidate, str(item.get("url") or ""))
    apply_title_resolution(item, resolution)


def apply_title_resolution(item: dict[str, Any], resolution: Any) -> None:
    old = item.get("title")
    if not resolution.reason:
        return
    for key in ("summary", "brief", "raw_excerpt", "agpm_takeaway", "llm_short_text", "llm_agpm_angle"):
        if isinstance(item.get(key), str):
            item[key] = repair_title_reference(item[key], str(old or ""), resolution.title)
    if isinstance(item.get("llm_summary"), dict):
        for key in ("short_text", "agpm_angle"):
            if isinstance(item["llm_summary"].get(key), str):
                item["llm_summary"][key] = repair_title_reference(item["llm_summary"][key], str(old or ""), resolution.title)
    item["title"] = resolution.title
    item["title_quality"] = {
        "version": 1, "reason": resolution.reason, "source": resolution.source,
        "original_title": str(old or "")[:2000], "resolved_title": resolution.title,
        "url": item.get("url"),
    }


def recover_web_title(item: dict[str, Any]) -> str:
    markup = ""
    url = str(item.get("url") or "")
    if url.startswith(("http://", "https://")):
        try:
            response = requests.get(url, timeout=20, headers={"User-Agent": "AgPM Radar title quality/1.0"})
            try:
                if response.ok and "text/html" in response.headers.get("content-type", "").lower():
                    markup = response.text
            finally:
                response.close()
        except requests.RequestException:
            pass
    apply_title_markup(item, markup)
    return markup
