#!/usr/bin/env python3
"""Generate issue-level AgPM theses from selected Radar materials."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from radar_paths import DB_PATH, LLM_CLASSIFICATION_DIR, ensure_dirs


ISSUE_PROMPT_VERSION = "issue-theses-rules-v1"
DAILY_ANALYSIS_PROMPT_VERSION = "issue-daily-analysis-ru-v1"
PERIOD_PROMPT_VERSION = "period-theses-ru-v1"
PERIOD_WINDOWS = {"7d": 7, "30d": 30}


RUBRIC_NAMES = {
    "agpm_pmo_portfolio": "AgPM / PMO / портфели",
    "isup_coordination": "ИСУП и проектная координация",
    "governance_control": "governance и контроль агентов",
    "human_responsibility": "human-in-the-loop и ответственность",
    "workflow_orchestration": "agent workflow и orchestration",
    "security_access": "безопасность и права доступа",
    "mcp_gateways_infra": "MCP, gateways и инфраструктура",
    "enterprise_adoption": "enterprise adoption",
    "vendors_releases": "вендорские релизы",
    "research_methodology": "исследования и методология",
    "funding_ma": "финансирование и M&A",
}


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def load_issue_materials(conn: sqlite3.Connection, issue_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT m.*,
               group_concat(mr.rubric_id, '|') rubric_ids
        FROM materials m
        LEFT JOIN material_rubrics mr ON mr.material_id = m.id
        WHERE m.radar_issue_date = ?
        GROUP BY m.id
        ORDER BY m.key_material DESC, m.perimeter, m.title
        """,
        (issue_date,),
    ).fetchall()
    materials: list[dict[str, Any]] = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        item["rubrics"] = [value for value in (item.pop("rubric_ids") or "").split("|") if value]
        materials.append(item)
    return materials


def count_rubrics(materials: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for item in materials:
        counts.update(item.get("rubrics") or [])
    return counts


def count_flags(materials: list[dict[str, Any]]) -> Counter[str]:
    return Counter(
        {
            "governance": sum(int(item.get("governance_flag") or 0) for item in materials),
            "security": sum(int(item.get("security_flag") or 0) for item in materials),
            "human": sum(int(item.get("human_in_the_loop_flag") or 0) for item in materials),
            "pmo": sum(int(item.get("pmo_flag") or 0) for item in materials),
            "isup": sum(int(item.get("isup_flag") or 0) for item in materials),
            "mcp": sum(int(item.get("mcp_flag") or 0) for item in materials),
        }
    )


def keyword_count(materials: list[dict[str, Any]], terms: list[str]) -> int:
    count = 0
    for item in materials:
        text = " ".join(
            str(item.get(field) or "")
            for field in ["title", "summary", "agpm_takeaway", "source_name"]
        ).lower()
        if any(term in text for term in terms):
            count += 1
    return count


def top_rubric_names(rubrics: Counter[str], limit: int = 3) -> str:
    names = [RUBRIC_NAMES.get(rubric, rubric) for rubric, _ in rubrics.most_common(limit)]
    return ", ".join(names)


def period_label(period: str) -> str:
    return "7 выпусков" if period == "7d" else "30 выпусков"


def build_theses(materials: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not materials:
        return [
            {
                "lead": "В выпуске нет материалов после смыслового фильтра.",
                "rest": "Для AgPM это сигнал проверить поисковый контур и расширить источники близкого периметра.",
            }
        ]

    total = len(materials)
    perimeters = Counter(str(item.get("perimeter") or "unknown") for item in materials)
    rubrics = count_rubrics(materials)
    flags = count_flags(materials)
    workflow_count = rubrics["workflow_orchestration"] + keyword_count(materials, ["workflow", "orchestration", "business process", "операцион"])
    reliability_count = keyword_count(materials, ["durable", "audit", "observability", "monitoring", "trace", "журнал", "доказатель", "conflict"])
    access_count = flags["security"] + flags["mcp"] + keyword_count(materials, ["gateway", "access", "identity", "policy", "policies", "доступ", "права"])

    candidates: list[tuple[int, dict[str, str]]] = []

    if flags["governance"] or rubrics["governance_control"]:
        score = flags["governance"] + rubrics["governance_control"] + access_count
        candidates.append(
            (
                score,
                {
                    "lead": "Главный сигнал выпуска — управляемая агентность.",
                    "rest": (
                        f"В {total} материалах чаще всего повторяются права, политики, безопасность, журналирование и контроль действий; "
                        "для AgPM это подтверждает приоритет governance-слоя над простой демонстрацией автономности."
                    ),
                },
            )
        )

    if workflow_count:
        candidates.append(
            (
                workflow_count,
                {
                    "lead": "Агенты переходят из интерфейса в операционный контур.",
                    "rest": (
                        "Материалы про workflow, orchestration и enterprise-процессы показывают, что ценность агента возникает не в отдельном чате, "
                        "а в повторяемом управленческом сценарии с правилами, статусом и трассировкой."
                    ),
                },
            )
        )

    if perimeters["near"]:
        candidates.append(
            (
                perimeters["near"] + flags["pmo"],
                {
                    "lead": "Близкий периметр держится на прикладных PMO-сценариях.",
                    "rest": (
                        f"В выпуске {perimeters['near']} материала напрямую относятся к проектному управлению; их стоит использовать для операционализации AgPM: "
                        "статусы, поручения, риски, отчётность, встречи и портфельная видимость."
                    ),
                },
            )
        )

    if flags["human"] or rubrics["human_responsibility"]:
        candidates.append(
            (
                flags["human"] + rubrics["human_responsibility"],
                {
                    "lead": "Ответственность человека остаётся ограничителем автономии.",
                    "rest": (
                        "Даже в инфраструктурных и security-сюжетах агент рассматривается как исполнитель в заданных границах; "
                        "решения, риск-аппетит и эскалации должны оставаться в человеческом контуре."
                    ),
                },
            )
        )

    if reliability_count:
        candidates.append(
            (
                reliability_count,
                {
                    "lead": "Доказательная цепочка становится условием доверия.",
                    "rest": (
                        "Темы durable execution, monitoring, audit trail и разрешения конфликтов важны для агентного проектного офиса: "
                        "действие агента должно быть воспроизводимым, объяснимым и проверяемым."
                    ),
                },
            )
        )

    if rubrics:
        candidates.append(
            (
                sum(count for _, count in rubrics.most_common(3)),
                {
                    "lead": "Рубрики выпуска показывают смещение к управленческой инфраструктуре.",
                    "rest": (
                        f"Доминируют {top_rubric_names(rubrics)}; это полезный материал не для пересмотра канона AgPM, "
                        "а для уточнения практик внедрения и контроля."
                    ),
                },
            )
        )

    seen: set[str] = set()
    theses: list[dict[str, str]] = []
    for _, item in sorted(candidates, key=lambda row: row[0], reverse=True):
        if item["lead"] in seen:
            continue
        theses.append(item)
        seen.add(item["lead"])
        if len(theses) == 4:
            break
    return theses


def normalize_theses(value: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not isinstance(value, list):
        return result
    for item in value:
        if not isinstance(item, dict):
            continue
        lead = str(item.get("lead") or "").strip()
        rest = str(item.get("rest") or "").strip()
        if lead and rest:
            result.append({"lead": lead, "rest": rest})
        if len(result) == 4:
            break
    return result


def normalize_daily_analysis(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ["headline", "signal", "why_agpm", "watch_next"]:
        text = str(value.get(key) or "").strip()
        if text:
            result[key] = text
    evidence = value.get("evidence_titles")
    if isinstance(evidence, list):
        titles = [str(item).strip() for item in evidence if str(item).strip()]
        result["evidence_titles"] = titles[:5]
    if all(result.get(key) for key in ["headline", "signal", "why_agpm", "watch_next"]):
        return result
    return {}


def compact_material(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_date": item.get("radar_issue_date"),
        "title": item.get("title"),
        "perimeter": item.get("perimeter"),
        "verdict": item.get("verdict"),
        "source_name": item.get("source_name"),
        "summary": item.get("summary"),
        "agpm_takeaway": item.get("agpm_takeaway"),
        "rubrics": item.get("rubrics") or [],
        "flags": {
            "governance": bool(item.get("governance_flag")),
            "security": bool(item.get("security_flag")),
            "human_in_the_loop": bool(item.get("human_in_the_loop_flag")),
            "pmo": bool(item.get("pmo_flag")),
            "isup": bool(item.get("isup_flag")),
            "mcp": bool(item.get("mcp_flag")),
        },
    }


def existing_daily_analysis_current(conn: sqlite3.Connection, issue_date: str) -> bool:
    llm_ready = all(
        [
            os.environ.get("RADAR_LLM_BASE_URL", "").rstrip(),
            os.environ.get("RADAR_LLM_API_KEY", ""),
            os.environ.get("RADAR_LLM_MODEL", ""),
        ]
    )
    row = conn.execute(
        """
        SELECT status
        FROM issue_daily_analysis
        WHERE issue_date = ?
          AND prompt_version = ?
        """,
        (issue_date, DAILY_ANALYSIS_PROMPT_VERSION),
    ).fetchone()
    if not row:
        return False
    if row["status"] == "success":
        return True
    return row["status"] == "fallback" and not llm_ready


def llm_daily_analysis(
    issue_date: str,
    materials: list[dict[str, Any]],
    theses: list[dict[str, str]],
    brief: str,
) -> tuple[dict[str, Any], str, str, str, str] | None:
    base_url = os.environ.get("RADAR_LLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("RADAR_LLM_API_KEY", "")
    model = os.environ.get("RADAR_LLM_MODEL", "")
    if not base_url or not api_key or not model:
        return None

    prompt_dir = LLM_CLASSIFICATION_DIR / "issue-daily-analysis"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{issue_date}.{DAILY_ANALYSIS_PROMPT_VERSION}"
    request_path = prompt_dir / f"{stem}.request.json"
    response_path = prompt_dir / f"{stem}.response.json"
    context = {
        "issue_date": issue_date,
        "brief": brief,
        "theses": theses,
        "rubrics": RUBRIC_NAMES,
        "materials": [compact_material(item) for item in materials],
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты аналитик AgPM Radar. Сформируй детальный дневной разбор на русском языке "
                    "по уже включённым материалам выпуска. Не пересказывай новости поштучно; выбери главный "
                    "управленческий сюжет дня и объясни его значение для AgPM, PMO, ИСУП, governance, рисков "
                    "и операционной модели. Не добавляй факты, которых нет в данных."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Верни только JSON вида "
                    "{\"headline\":\"короткий заголовок\", \"signal\":\"главный сигнал 2-4 предложения\", "
                    "\"why_agpm\":\"почему это важно для AgPM 2-4 предложения\", "
                    "\"watch_next\":\"что отслеживать дальше 1-3 предложения\", "
                    "\"evidence_titles\":[\"название материала\"]}.\n"
                    "Текст должен быть на русском языке. Не заменяй 4 формальных тезиса выпуска; это дополнительный "
                    "раскрываемый аналитический блок. Опирайся только на данные выпуска.\n\n"
                    f"Данные выпуска: {json.dumps(context, ensure_ascii=False)}"
                ),
            },
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))
    response_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    content = data["choices"][0]["message"]["content"]
    parsed = normalize_daily_analysis(json.loads(content))
    if not parsed:
        return None
    return parsed, base_url, model, str(request_path), str(response_path)


def fallback_daily_analysis(materials: list[dict[str, Any]], theses: list[dict[str, str]]) -> dict[str, Any]:
    if not materials:
        return {
            "headline": "Выпуск без включённых материалов",
            "signal": "После смыслового фильтра в выпуск не попали материалы с достаточным управленческим сигналом.",
            "why_agpm": "Для AgPM это не методический вывод, а операционный сигнал: нужно проверить поисковый контур, источники близкого периметра и правила отсечения.",
            "watch_next": "В следующем выпуске стоит отдельно проверить PMO, ИСУП, governance, риски, ресурсы и корпоративные workflow.",
            "evidence_titles": [],
        }
    perimeters = Counter(str(item.get("perimeter") or "unknown") for item in materials)
    rubrics = count_rubrics(materials)
    lead = theses[0]["lead"].rstrip(".") if theses else "Главный сигнал выпуска сформирован по материалам радара"
    evidence = [str(item.get("title") or "").strip() for item in materials if item.get("title")][:5]
    return {
        "headline": lead,
        "signal": (
            f"В выпуске {len(materials)} материалов: близкий периметр — {perimeters['near']}, "
            f"средний — {perimeters['mid']}, дальний — {perimeters['far']}. "
            f"Доминирующие рубрики: {top_rubric_names(rubrics) or 'не определены'}."
        ),
        "why_agpm": (
            "Для AgPM это материал для прикладной операционализации: важно смотреть не на автономность агента саму по себе, "
            "а на управленческий процесс, границы прав, журнал действий, точки человеческого подтверждения и проверяемость результата."
        ),
        "watch_next": (
            "В следующих выпусках стоит отслеживать кейсы, где агент включён в статус, риск, поручение, ресурсное решение, "
            "портфельную видимость или эскалацию."
        ),
        "evidence_titles": evidence,
    }


def update_daily_analysis(
    conn: sqlite3.Connection,
    issue_date: str,
    materials: list[dict[str, Any]],
    theses: list[dict[str, str]],
    brief: str,
) -> None:
    if existing_daily_analysis_current(conn, issue_date):
        return
    status = "success"
    provider = "llm"
    model = ""
    request_path = ""
    response_path = ""
    error = ""
    try:
        llm_result = llm_daily_analysis(issue_date, materials, theses, brief)
    except Exception as exc:  # noqa: BLE001 - pipeline must keep publishing with fallback analysis.
        llm_result = None
        error = str(exc)
    if llm_result:
        analysis, provider, model, request_path, response_path = llm_result
    else:
        analysis = fallback_daily_analysis(materials, theses)
        status = "fallback"
        provider = "fallback"
        model = "rules-daily-analysis-v1"
    conn.execute(
        """
        INSERT INTO issue_daily_analysis(
          issue_date, headline, analysis_json, provider, model, prompt_version,
          request_path, response_path, status, error, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(issue_date) DO UPDATE SET
          headline = excluded.headline,
          analysis_json = excluded.analysis_json,
          provider = excluded.provider,
          model = excluded.model,
          prompt_version = excluded.prompt_version,
          request_path = excluded.request_path,
          response_path = excluded.response_path,
          status = excluded.status,
          error = excluded.error,
          updated_at = datetime('now')
        """,
        (
            issue_date,
            analysis.get("headline", ""),
            json.dumps(analysis, ensure_ascii=False),
            provider,
            model,
            DAILY_ANALYSIS_PROMPT_VERSION,
            request_path,
            response_path,
            status,
            error,
        ),
    )


def load_window_issue_dates(conn: sqlite3.Connection, as_of_issue_date: str, window: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT issue_date
        FROM issues
        WHERE issue_date <= ?
        ORDER BY issue_date DESC
        LIMIT ?
        """,
        (as_of_issue_date, window),
    ).fetchall()
    return [row["issue_date"] for row in reversed(rows)]


def load_period_materials(conn: sqlite3.Connection, issue_dates: list[str]) -> list[dict[str, Any]]:
    if not issue_dates:
        return []
    placeholders = ",".join("?" for _ in issue_dates)
    rows = conn.execute(
        f"""
        SELECT m.*,
               group_concat(mr.rubric_id, '|') rubric_ids
        FROM materials m
        LEFT JOIN material_rubrics mr ON mr.material_id = m.id
        WHERE m.radar_issue_date IN ({placeholders})
        GROUP BY m.id
        ORDER BY m.radar_issue_date DESC, m.key_material DESC, m.perimeter, m.title
        """,
        issue_dates,
    ).fetchall()
    materials: list[dict[str, Any]] = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        item["rubrics"] = [value for value in (item.pop("rubric_ids") or "").split("|") if value]
        materials.append(item)
    return materials


def load_window_stats(conn: sqlite3.Connection, issue_dates: list[str], materials: list[dict[str, Any]]) -> dict[str, int]:
    keys = ["viewed", "included", "cut", "near", "mid", "far", "core", "adjacent"]
    stats = {key: 0 for key in keys}
    if issue_dates:
        placeholders = ",".join("?" for _ in issue_dates)
        row = conn.execute(
            f"""
            SELECT
              sum(viewed) viewed,
              sum(included) included,
              sum(cut) cut,
              sum(near) near,
              sum(mid) mid,
              sum(far) far,
              sum(core) core,
              sum(adjacent) adjacent
            FROM daily_stats
            WHERE stat_date IN ({placeholders})
            """,
            issue_dates,
        ).fetchone()
        if row and row["viewed"] is not None:
            return {key: int(row[key] or 0) for key in keys}
    for item in materials:
        stats["viewed"] += 1
        stats["included"] += 1
        if item.get("perimeter") in {"near", "mid", "far"}:
            stats[str(item["perimeter"])] += 1
        if item.get("verdict") in {"core", "adjacent"}:
            stats[str(item["verdict"])] += 1
    return stats


def llm_period_theses(
    period: str,
    as_of_issue_date: str,
    issue_dates: list[str],
    materials: list[dict[str, Any]],
    stats: dict[str, int],
) -> tuple[list[dict[str, str]], str, str, str, str, str] | None:
    base_url = os.environ.get("RADAR_LLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("RADAR_LLM_API_KEY", "")
    model = os.environ.get("RADAR_LLM_MODEL", "")
    if not base_url or not api_key or not model:
        return None

    prompt_dir = LLM_CLASSIFICATION_DIR / "issue-period-theses"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{as_of_issue_date}.{period}.{PERIOD_PROMPT_VERSION}"
    request_path = prompt_dir / f"{stem}.request.json"
    response_path = prompt_dir / f"{stem}.response.json"
    top_materials = sorted(
        materials,
        key=lambda item: (
            0 if item.get("key_material") else 1,
            {"near": 0, "mid": 1, "far": 2}.get(str(item.get("perimeter")), 3),
            str(item.get("radar_issue_date") or ""),
        ),
    )[:24]
    context = {
        "period": period,
        "period_label": period_label(period),
        "as_of_issue_date": as_of_issue_date,
        "issue_dates": issue_dates,
        "stats": stats,
        "rubrics": RUBRIC_NAMES,
        "materials": [compact_material(item) for item in top_materials],
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты аналитик AgPM Radar. Сформируй 4 русскоязычных управленческих тезиса "
                    "по окну выпусков. Не пересказывай новости поштучно; выделяй паттерны, сдвиги, "
                    "сигналы для агентного управления проектами, PMO, ИСУП, governance, рисков и операционной модели. "
                    "Не добавляй факты, которых нет в данных."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Верни только JSON вида "
                    "{\"brief\":\"1 фраза\", \"theses\":[{\"lead\":\"короткий тезис\", \"rest\":\"обоснование 1-2 предложения\"}]}.\n"
                    "Тезисов должно быть ровно 4. Каждый тезис должен опираться на агрегат окна, а не на один случайный материал.\n\n"
                    f"Данные окна: {json.dumps(context, ensure_ascii=False)}"
                ),
            },
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))
    response_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    theses = normalize_theses(parsed.get("theses"))
    if len(theses) != 4:
        return None
    brief = str(parsed.get("brief") or "").strip()
    return theses, brief, base_url, model, str(request_path), str(response_path)


def fallback_period_theses(
    period: str,
    issue_dates: list[str],
    materials: list[dict[str, Any]],
    stats: dict[str, int],
) -> tuple[list[dict[str, str]], str]:
    label = period_label(period)
    issue_count = len(issue_dates)
    if not materials:
        theses = [
            {
                "lead": f"За последние {label} нет материалов после смыслового фильтра.",
                "rest": "Для AgPM это повод проверить поисковый контур, источники близкого периметра и правила отсечения.",
            },
            {
                "lead": "Пустое окно не даёт основания менять методическую рамку.",
                "rest": "Отсутствие включённых материалов лучше трактовать как операционный сигнал сбора, а не как изменение повестки AgPM.",
            },
            {
                "lead": "Нужна проверка источников с управленческой спецификой.",
                "rest": "В первую очередь стоит смотреть PMO, ИСУП, governance, риски, ресурсы и корпоративные workflow.",
            },
            {
                "lead": "Периодный слой требует накопления фактуры.",
                "rest": "Семидневные и тридцатидневные выводы должны строиться на повторяющихся сигналах, а не на единичных находках.",
            },
        ]
        return theses, f"За последние {label} нет включённых материалов."

    total = len(materials)
    perimeters = Counter(str(item.get("perimeter") or "unknown") for item in materials)
    rubrics = count_rubrics(materials)
    flags = count_flags(materials)
    workflow_count = rubrics["workflow_orchestration"] + keyword_count(materials, ["workflow", "orchestration", "business process", "операцион", "процесс"])
    reliability_count = keyword_count(materials, ["durable", "audit", "observability", "monitoring", "trace", "журнал", "доказатель", "conflict"])
    access_count = flags["security"] + flags["mcp"] + keyword_count(materials, ["gateway", "access", "identity", "policy", "policies", "доступ", "права"])
    period_text = f"за последние {label}"

    candidates: list[tuple[int, dict[str, str]]] = [
        (
            sum(count for _, count in rubrics.most_common(3)),
            {
                "lead": "Периодная повестка смещена к управленческой инфраструктуре.",
                "rest": (
                    f"В окне {issue_count} выпусков и {total} материалов доминируют {top_rubric_names(rubrics)}; "
                    "для AgPM это материал для уточнения практик внедрения, контроля и эксплуатации."
                ),
            },
        ),
        (
            perimeters["near"] * 3 + flags["pmo"] + flags["isup"],
            {
                "lead": "Близкий периметр показывает прикладной слой AgPM.",
                "rest": (
                    f"{perimeters['near']} материалов {period_text} напрямую связаны с PMO, ИСУП или проектной координацией; "
                    "их стоит использовать как основу для сценариев статусов, поручений, рисков, ресурсов и портфельной видимости."
                ),
            },
        ),
        (
            flags["governance"] + rubrics["governance_control"] + access_count,
            {
                "lead": "Governance остаётся главным условием масштабирования агентов.",
                "rest": (
                    "Повторяются права доступа, политики, безопасность, MCP/gateway и контроль действий; "
                    "значит, агентный контур требует управляемых границ, а не только автономного исполнения."
                ),
            },
        ),
        (
            workflow_count,
            {
                "lead": "Агентность закрепляется через повторяемые workflow.",
                "rest": (
                    "Сигналы про orchestration, business process и enterprise adoption показывают, что ценность возникает там, "
                    "где агент встроен в устойчивый управленческий процесс с правилами и трассировкой."
                ),
            },
        ),
        (
            flags["human"] + rubrics["human_responsibility"],
            {
                "lead": "Ответственность человека остаётся границей автономии.",
                "rest": (
                    "Human-in-the-loop появляется как управленческий ограничитель: решения, риск-аппетит, спорные действия "
                    "и эскалации должны оставаться в человеческом контуре."
                ),
            },
        ),
        (
            reliability_count,
            {
                "lead": "Доверие к агентам требует доказательной цепочки.",
                "rest": (
                    "Audit trail, monitoring, observability, durable execution и разрешение конфликтов нужны, чтобы действие агента "
                    "можно было проверить, объяснить и воспроизвести."
                ),
            },
        ),
        (
            perimeters["far"],
            {
                "lead": "Дальний периметр даёт ранние рыночные сигналы.",
                "rest": (
                    f"{perimeters['far']} материалов {period_text} относятся к платформам, вендорам и инфраструктуре; "
                    "их полезно держать как разведку, но не смешивать с прямыми AgPM-практиками."
                ),
            },
        ),
    ]

    theses: list[dict[str, str]] = []
    seen: set[str] = set()
    for score, item in sorted(candidates, key=lambda row: row[0], reverse=True):
        if score <= 0 or item["lead"] in seen:
            continue
        theses.append(item)
        seen.add(item["lead"])
        if len(theses) == 4:
            break
    for _, item in candidates:
        if len(theses) == 4:
            break
        if item["lead"] not in seen:
            theses.append(item)
            seen.add(item["lead"])

    brief = (
        f"За последние {label}: {stats.get('included', total)} включено из {stats.get('viewed', total)} просмотренных; "
        f"Б/С/Д — {stats.get('near', perimeters['near'])}/{stats.get('mid', perimeters['mid'])}/{stats.get('far', perimeters['far'])}."
    )
    return theses[:4], brief


def build_brief(materials: list[dict[str, Any]], theses: list[dict[str, str]]) -> str:
    if not materials:
        return "Выпуск без материалов после смыслового фильтра."
    perimeters = Counter(str(item.get("perimeter") or "unknown") for item in materials)
    top = theses[0]["lead"].rstrip(".") if theses else "Главный сигнал выпуска сформирован по материалам радара"
    return (
        f"{top}. В выпуске {len(materials)} материалов: "
        f"близкий периметр — {perimeters['near']}, средний — {perimeters['mid']}, дальний — {perimeters['far']}."
    )


def update_issue(conn: sqlite3.Connection, issue_date: str) -> None:
    materials = load_issue_materials(conn, issue_date)
    theses = build_theses(materials)
    brief = build_brief(materials, theses)
    conn.execute(
        """
        UPDATE issues
        SET theses_json = ?, brief = ?, updated_at = datetime('now')
        WHERE issue_date = ?
        """,
        (json.dumps(theses, ensure_ascii=False), brief, issue_date),
    )
    update_daily_analysis(conn, issue_date, materials, theses, brief)


def update_period_theses(conn: sqlite3.Connection, as_of_issue_date: str, period: str) -> None:
    issue_dates = load_window_issue_dates(conn, as_of_issue_date, PERIOD_WINDOWS[period])
    materials = load_period_materials(conn, issue_dates)
    stats = load_window_stats(conn, issue_dates, materials)
    llm_result = llm_period_theses(period, as_of_issue_date, issue_dates, materials, stats)
    if llm_result:
        theses, brief, provider, model, request_path, response_path = llm_result
        prompt_version = PERIOD_PROMPT_VERSION
    else:
        theses, brief = fallback_period_theses(period, issue_dates, materials, stats)
        provider = "fallback"
        model = "rules-period-v1"
        prompt_version = PERIOD_PROMPT_VERSION
        request_path = ""
        response_path = ""
    conn.execute(
        """
        INSERT INTO issue_period_theses(
          as_of_issue_date, period, start_issue_date, end_issue_date, issue_count,
          material_count, stats_json, theses_json, brief, provider, model,
          prompt_version, request_path, response_path, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(as_of_issue_date, period) DO UPDATE SET
          start_issue_date = excluded.start_issue_date,
          end_issue_date = excluded.end_issue_date,
          issue_count = excluded.issue_count,
          material_count = excluded.material_count,
          stats_json = excluded.stats_json,
          theses_json = excluded.theses_json,
          brief = excluded.brief,
          provider = excluded.provider,
          model = excluded.model,
          prompt_version = excluded.prompt_version,
          request_path = excluded.request_path,
          response_path = excluded.response_path,
          updated_at = datetime('now')
        """,
        (
            as_of_issue_date,
            period,
            issue_dates[0] if issue_dates else as_of_issue_date,
            issue_dates[-1] if issue_dates else as_of_issue_date,
            len(issue_dates),
            len(materials),
            json.dumps(stats, ensure_ascii=False),
            json.dumps(theses, ensure_ascii=False),
            brief,
            provider,
            model,
            prompt_version,
            request_path,
            response_path,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate issue-level theses for the public Radar site.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--issue-date", help="Limit generation to one issue date, YYYY-MM-DD.")
    args = parser.parse_args()

    ensure_dirs()
    conn = connect(args.db)
    try:
        if args.issue_date:
            issue_dates = [args.issue_date]
        else:
            issue_dates = [row["issue_date"] for row in conn.execute("SELECT issue_date FROM issues ORDER BY issue_date")]
        for issue_date in issue_dates:
            update_issue(conn, issue_date)
            for period in PERIOD_WINDOWS:
                update_period_theses(conn, issue_date, period)
        conn.commit()
    finally:
        conn.close()
    print(f"Generated theses for {len(issue_dates)} issues and {len(issue_dates) * len(PERIOD_WINDOWS)} period windows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
