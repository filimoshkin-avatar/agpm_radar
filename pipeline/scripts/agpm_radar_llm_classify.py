#!/usr/bin/env python3
"""Classify Radar materials into Russian rubrics.

Uses an OpenAI-compatible chat completions endpoint when configured via env.
Falls back to deterministic rules so the pipeline remains runnable without keys.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any

from radar_paths import DB_PATH, LLM_CLASSIFICATION_DIR, ensure_dirs


PROMPT_VERSION = "rubrics-ru-v6"

RUBRICS = {
    "agpm_pmo_portfolio": "AgPM, PMO и портфели",
    "isup_coordination": "ИСУП и проектная координация",
    "governance_control": "Governance и контроль",
    "human_responsibility": "Ответственность человека",
    "workflow_orchestration": "Процессы и оркестрация",
    "security_access": "Безопасность и доступ",
    "mcp_gateways_infra": "Инфраструктура агентов и MCP",
    "enterprise_adoption": "Внедрение в enterprise",
    "vendors_releases": "Вендоры и продуктовые релизы",
    "research_methodology": "Исследования и методология",
    "funding_ma": "Инвестиции и сделки",
}

RUBRIC_ORDER = {rubric_id: index for index, rubric_id in enumerate(RUBRICS)}


def has_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def fallback_classify(material: dict[str, Any]) -> dict[str, Any]:
    signal_text = " ".join(str(material.get(key) or "") for key in ["title", "summary", "url", "canonical_url", "source_name"]).lower()
    content_text = " ".join(str(material.get(key) or "") for key in ["title", "summary", "source_name"]).lower()
    takeaway_text = str(material.get("agpm_takeaway") or "").lower()
    context_text = f"{signal_text} {takeaway_text}"
    isup_tool_terms = [
        "исуп",
        "pmf",
        "пм форсайт",
        "jira",
        "asana",
        "atlassian",
        "clickup",
        "smartsheet",
        "monday.com",
        "monday ",
        "ms project",
        "microsoft project",
        "planner",
        "teamwork.com",
        "zoho projects",
        "aha!",
    ]
    coordination_terms = [
        "project management",
        "project workflow",
        "project workflows",
        "project coordination",
        "projectbeheer",
        "gestión de proyectos",
        "task tracking",
        "tasks",
        "tickets",
        "issue tracking",
        "backlog",
        "sprint",
        "standup",
        "status reporting",
        "weekly reports",
        "meeting minutes",
        "risk marking",
        "documentation",
        "координац",
        "проектн",
        "задач",
        "поручен",
        "статус",
        "отчет",
        "отчёт",
        "спринт",
        "бэклог",
        "тикет",
        "документац",
    ]
    pmo_terms = [
        "agpm",
        "agentic project management",
        "agentic pmo",
        "pmo",
        "project management",
        "portfolio management",
        "project portfolio",
        "project manager agent",
        "управление проект",
        "проектн",
        "проектный офис",
        "портфел",
    ]
    governance_terms = [
        "governance",
        "policy framework",
        "policy",
        "compliance",
        "controls",
        "control framework",
        "oversight",
        "audit",
        "accountability framework",
        "regulation",
        "регулирован",
        "политик",
        "контроль",
        "надзор",
    ]
    human_terms = [
        "human-in-the-loop",
        "human in the loop",
        "human oversight",
        "human approval",
        "human review",
        "human accountability",
        "accountability",
        "responsibility",
        "responsible ai",
        "approval",
        "review board",
        "ответствен",
        "согласован",
        "утвержден",
        "человек в контуре",
    ]
    workflow_terms = [
        "workflow",
        "orchestration",
        "business process",
        "process automation",
        "automation",
        "agentic operations",
        "операцион",
        "бизнес-процесс",
        "автоматизац",
        "оркестрац",
        "процесс",
    ]
    security_terms = [
        "security",
        "cybersecurity",
        "access control",
        "permission",
        "identity",
        "privacy",
        "data protection",
        "secure",
        "risk mitigation",
        "insurance",
        "безопас",
        "доступ",
        "права доступа",
        "риски",
        "страхован",
    ]
    mcp_terms = [
        "mcp",
        "model context protocol",
        "agent gateway",
        "agent gateways",
        "gateway",
        "a2a",
        "tool calling",
        "agentic infrastructure",
        "инфраструктур",
        "шлюз",
    ]
    enterprise_terms = [
        "enterprise",
        "corporate",
        "production",
        "deployment",
        "adoption",
        "implementation",
        "scale",
        "scaling",
        "operating model",
        "корпоратив",
        "внедрен",
        "масштаб",
        "эксплуатац",
        "операционн",
    ]
    vendor_terms = [
        "launch",
        "launches",
        "unveils",
        "product",
        "platform",
        "marketplace",
        "vendor",
        "startup",
        "customer.io",
        "outsystems",
        "redpanda",
        "huawei",
        "huawei cloud",
        "tencent",
        "yandex",
        "alice ai",
        "selectel",
        "just ai",
        "мтс",
        "яндекс",
        "алиса",
        "запуст",
        "выпуст",
        "представ",
        "платформ",
        "маркетплейс",
    ]
    research_terms = [
        "research",
        "study",
        "survey",
        "report",
        "review",
        "gartner says",
        "ey report",
        "bcg",
        "pwc",
        "white paper",
        "исслед",
        "опрос",
        "отчёт",
        "отчет",
        "аналит",
        "обзор",
    ]
    funding_terms = [
        "funding",
        "m&a",
        "acquisition",
        "investment",
        "investor",
        "venture",
        "vc ",
        "fund ",
        "raises",
        "raised",
        "round",
        "merger",
        "инвести",
        "фонд",
        "слиян",
        "поглощ",
        "венчур",
    ]
    explicit_isup = has_any(signal_text, ["исуп", "pmf", "пм форсайт"])
    project_tool_context = has_any(signal_text, isup_tool_terms) and has_any(signal_text, coordination_terms)
    flags = {
        "governance": has_any(signal_text, governance_terms),
        "security": has_any(signal_text, security_terms),
        "human_in_the_loop": has_any(signal_text, human_terms),
        "pmo": has_any(signal_text, pmo_terms),
        "isup": explicit_isup or project_tool_context,
        "mcp": has_any(signal_text, mcp_terms),
    }

    scores = {rubric_id: 0 for rubric_id in RUBRICS}
    if flags["pmo"]:
        scores["agpm_pmo_portfolio"] += 5
    if material.get("perimeter") == "near" and has_any(context_text, ["pmo", "project", "проект", "портфел"]):
        scores["agpm_pmo_portfolio"] += 2
    if flags["isup"]:
        scores["isup_coordination"] += 7
    if flags["governance"]:
        scores["governance_control"] += 6
    if flags["human_in_the_loop"]:
        scores["human_responsibility"] += 5
    if has_any(signal_text, workflow_terms):
        scores["workflow_orchestration"] += 5
    if flags["security"]:
        scores["security_access"] += 5
    if flags["mcp"]:
        scores["mcp_gateways_infra"] += 6
    if has_any(content_text, enterprise_terms):
        scores["enterprise_adoption"] += 4
    if has_any(content_text, vendor_terms):
        scores["vendors_releases"] += 6
    if has_any(content_text, research_terms) or "methodolog" in content_text or "методолог" in content_text:
        scores["research_methodology"] += 5
    if has_any(content_text, funding_terms):
        scores["funding_ma"] += 7

    if material.get("perimeter") == "far":
        scores["vendors_releases"] += 1
        scores["enterprise_adoption"] += 1

    rubrics = [
        rubric_id
        for rubric_id, score in sorted(scores.items(), key=lambda item: (-item[1], RUBRIC_ORDER[item[0]]))
        if score > 0
    ][:3] or ["enterprise_adoption"]
    confidence = 0.58 if len(rubrics) == 1 else 0.66
    return {
        "rubrics": rubrics,
        "verdict": material.get("verdict") or "core",
        "confidence": confidence,
        "explanation_ru": "Классификация выполнена fallback-правилами: LLM-провайдер не настроен.",
        "flags": flags,
        "key_candidate": bool(material.get("key_material") or material.get("perimeter") == "near" or flags["governance"]),
    }


def llm_classify(material: dict[str, Any]) -> dict[str, Any] | None:
    base_url = os.environ.get("RADAR_LLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("RADAR_LLM_API_KEY", "")
    model = os.environ.get("RADAR_LLM_MODEL", "")
    if not base_url or not api_key or not model:
        return None
    prompt = {
        "role": "user",
        "content": (
            "Классифицируй материал радара AgPM. Верни только JSON с полями: "
            "rubrics (ids из списка), verdict core|adjacent, confidence 0..1, explanation_ru, "
            "flags {governance, security, human_in_the_loop, pmo, isup, mcp}, key_candidate.\n\n"
            "Не ставь Human-in-the-loop только из-за общей фразы про ответственность в редакционном выводе; "
            "нужен явный сигнал про человеческое согласование, oversight, accountability или контур принятия решений. "
            "Не ставь Security только из-за общих слов про enterprise: нужен сигнал про безопасность, доступ, privacy, "
            "identity, cyber-риск или страхование риска. Если материал является релизом, запуском платформы, "
            "маркетплейсом или продуктовой новостью, не теряй rubrics vendors_releases из-за governance/workflow.\n\n"
            "Рубрику isup_coordination ставь не только при буквальном слове ИСУП, но и когда материал "
            "говорит об агентности внутри проектных платформ и контуров координации: Jira, Asana, "
            "Atlassian, monday.com, ClickUp, Smartsheet, MS Project/Planner, backlog, sprint, task tracking, "
            "status reporting, meeting minutes, standups, project workflows, управление задачами и поручениями.\n\n"
            f"Рубрики: {json.dumps(RUBRICS, ensure_ascii=False)}\n\n"
            f"Материал: {json.dumps(material, ensure_ascii=False)}"
        ),
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Ты русскоязычный классификатор материалов для радара агентного проектного управления."},
            prompt,
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def normalize(result: dict[str, Any]) -> dict[str, Any]:
    rubrics = [rubric for rubric in result.get("rubrics", []) if rubric in RUBRICS][:3]
    if not rubrics:
        rubrics = ["enterprise_adoption"]
    flags = result.get("flags") or {}
    return {
        "rubrics": rubrics,
        "verdict": result.get("verdict") if result.get("verdict") in {"core", "adjacent"} else "core",
        "confidence": float(result.get("confidence") or 0),
        "explanation_ru": str(result.get("explanation_ru") or ""),
        "flags": {key: bool(flags.get(key)) for key in ["governance", "security", "human_in_the_loop", "pmo", "isup", "mcp"]},
        "key_candidate": bool(result.get("key_candidate")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    ensure_dirs()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT m.* FROM materials m
        WHERE NOT EXISTS (
          SELECT 1 FROM llm_classifications c
          WHERE c.material_id = m.id AND c.prompt_version = ? AND c.status = 'ok'
        )
        OR NOT EXISTS (
          SELECT 1 FROM material_rubrics mr
          WHERE mr.material_id = m.id
        )
        ORDER BY COALESCE(m.published_at, m.radar_issue_date) DESC, m.title
        """,
        (PROMPT_VERSION,),
    ).fetchall()
    done = 0
    for row in rows:
        material = dict(row)
        request_path = LLM_CLASSIFICATION_DIR / f"{material['id']}.{PROMPT_VERSION}.request.json"
        response_path = LLM_CLASSIFICATION_DIR / f"{material['id']}.{PROMPT_VERSION}.response.json"
        request_path.write_text(json.dumps(material, ensure_ascii=False, indent=2), encoding="utf-8")
        provider = "fallback"
        model = "rules-v1"
        try:
            raw = llm_classify(material)
            if raw is None:
                raw = fallback_classify(material)
            else:
                provider = os.environ.get("RADAR_LLM_BASE_URL", "openai-compatible")
                model = os.environ.get("RADAR_LLM_MODEL", "")
            result = normalize(raw)
            response_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            conn.execute("DELETE FROM material_rubrics WHERE material_id = ?", (material["id"],))
            for rubric_id in result["rubrics"]:
                conn.execute(
                    "INSERT OR REPLACE INTO material_rubrics(material_id, rubric_id, confidence, source) VALUES (?, ?, ?, ?)",
                    (material["id"], rubric_id, result["confidence"], provider),
                )
            flags = result["flags"]
            conn.execute(
                """
                UPDATE materials SET verdict = ?, governance_flag = ?, security_flag = ?,
                  human_in_the_loop_flag = ?, pmo_flag = ?, isup_flag = ?, mcp_flag = ?,
                  key_material = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    result["verdict"],
                    int(flags["governance"]),
                    int(flags["security"]),
                    int(flags["human_in_the_loop"]),
                    int(flags["pmo"]),
                    int(flags["isup"]),
                    int(flags["mcp"]),
                    int(result["key_candidate"]),
                    material["id"],
                ),
            )
            conn.execute(
                """
                INSERT INTO llm_classifications(
                  material_id, provider, model, prompt_version, request_path, response_path,
                  normalized_json, confidence, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok')
                """,
                (
                    material["id"],
                    provider,
                    model,
                    PROMPT_VERSION,
                    str(request_path),
                    str(response_path),
                    json.dumps(result, ensure_ascii=False),
                    result["confidence"],
                ),
            )
            done += 1
            if args.limit and done >= args.limit:
                break
            time.sleep(0.05)
        except Exception as exc:
            conn.execute(
                """
                INSERT INTO llm_classifications(material_id, provider, model, prompt_version, request_path, status, error)
                VALUES (?, ?, ?, ?, ?, 'error', ?)
                """,
                (material["id"], provider, model, PROMPT_VERSION, str(request_path), str(exc)),
            )
    conn.commit()
    conn.close()
    print(f"Classified {done} materials")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
