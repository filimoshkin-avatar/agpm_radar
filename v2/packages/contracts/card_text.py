"""Effective published description and takeaway, shared by search and observation."""

from __future__ import annotations

from typing import Any


def shown_texts(item: dict[str, Any]) -> tuple[str, str]:
    llm = item.get("llm")
    succeeded = isinstance(llm, dict) and llm.get("status") == "success"
    description = (str(item.get("llmShortText") or "") if succeeded else "") or (
        str(item.get("brief") or "") or str(item.get("summary") or "")
    )
    takeaway = (str(item.get("llmAgpmAngle") or "") if succeeded else "") or str(
        item.get("agpmTakeaway") or ""
    )
    return description, takeaway
