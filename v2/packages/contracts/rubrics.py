"""Presentation vocabulary shared by public search and every rubric control.

Membership remains the published material's rubric IDs. This catalogue is
independent of period statistics, including rubrics with no current materials.
"""

from typing import Final

from packages.contracts.json_types import JsonObject

# ID, compact label, explanatory title, group (also the existing visual tone).
RUBRICS: Final = (
    ("agpm_pmo_portfolio", "AgPM / PMO", "AgPM, PMO и портфели", "near"),
    ("isup_coordination", "ИСУП", "ИСУП и проектная координация", "near"),
    ("governance_control", "Governance", "Governance и контроль", "mid"),
    ("human_responsibility", "Ответственность", "Ответственность человека", "mid"),
    ("workflow_orchestration", "Оркестрация", "Процессы и оркестрация", "mid"),
    ("security_access", "Безопасность", "Безопасность и доступ", "mid"),
    ("mcp_gateways_infra", "MCP / инфраструктура", "Инфраструктура агентов и MCP", "far"),
    ("enterprise_adoption", "Внедрение", "Внедрение в enterprise", "far"),
    ("vendors_releases", "Вендоры", "Вендоры и продуктовые релизы", "far"),
    ("research_methodology", "Исследования", "Исследования и методология", "far"),
    ("funding_ma", "Инвестиции", "Инвестиции и сделки", "far"),
)
RUBRIC_LABELS: Final = {row[0]: row[1] for row in RUBRICS}


def rubric_catalog() -> list[JsonObject]:
    """Return fresh public metadata without querying the content database."""
    return [
        {"id": key, "label": label, "title": title, "group": group, "order": order}
        for order, (key, label, title, group) in enumerate(RUBRICS)
    ]
