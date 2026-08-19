#!/usr/bin/env python3
"""Public signal strength labels for AgPM Radar materials."""

from __future__ import annotations

from typing import Any


SIGNAL_LABELS = {
    "strong": "Сильный сигнал",
    "context": "Контекст",
    "watch": "Наблюдать",
}

WATCH_TERMS = [
    "top-10",
    "top 10",
    "best ai agents",
    "лучших ии-агентов",
    "лучшие ии-агенты",
    "маркетплейс",
    "marketplace",
    "agent economy",
    "agents economy",
    "brief",
    "digest",
]

PMO_TERMS = [
    "pmo",
    "project management",
    "project portfolio",
    "portfolio management",
    "project coordination",
    "jira",
    "asana",
    "atlassian",
    "исуп",
    "проектн",
    "портфел",
]

GOVERNANCE_TERMS = [
    "governance",
    "policy",
    "policies",
    "permission",
    "access",
    "audit",
    "compliance",
    "security",
    "risk",
    "human-in-the-loop",
    "human in the loop",
    "accountability",
    "oversight",
    "контрол",
    "безопас",
    "ответствен",
    "доступ",
    "аудит",
]

ENTERPRISE_TERMS = [
    "enterprise",
    "business process",
    "workflow",
    "operations",
    "corporate",
    "platform",
    "компани",
    "корпоратив",
    "бизнес-процесс",
]


def signal_strength_from_score(score: int | None, item: dict[str, Any] | None = None) -> str:
    """Return strong/context/watch for the public card label.

    The score comes from the existing Radar relevance review. Extra caps/floors
    keep the public label aligned with the editorial meaning of AgPM signals.
    """
    item = item or {}
    try:
        value = int(score) if score is not None else 0
    except (TypeError, ValueError):
        value = 0

    text = " ".join(
        str(item.get(key) or "")
        for key in ["title", "summary", "agpm_takeaway", "brief", "source_name"]
    ).lower()
    rubrics = set(item.get("rubrics") or [])
    perimeter = str(item.get("perimeter") or "")

    if value >= 13:
        strength = "strong"
    elif value >= 10:
        strength = "context"
    else:
        strength = "watch"

    has_pmo_signal = perimeter == "near" or bool(rubrics & {"agpm_pmo_portfolio", "isup_coordination"}) or any(
        term in text for term in PMO_TERMS
    )
    has_governance_signal = bool(rubrics & {"governance_control", "security_access", "human_responsibility"}) or any(
        term in text for term in GOVERNANCE_TERMS
    )
    has_enterprise_signal = bool(rubrics & {"workflow_orchestration", "enterprise_adoption"}) or any(
        term in text for term in ENTERPRISE_TERMS
    )
    is_generic_market_signal = any(term in text for term in WATCH_TERMS)

    if has_pmo_signal and strength == "watch":
        strength = "context"
    if has_governance_signal and has_enterprise_signal and value >= 10:
        strength = "strong"
    if is_generic_market_signal and not (has_pmo_signal or has_governance_signal):
        strength = "watch"

    return strength


def signal_label(strength: str | None) -> str:
    return SIGNAL_LABELS.get(strength or "", SIGNAL_LABELS["strong"])
