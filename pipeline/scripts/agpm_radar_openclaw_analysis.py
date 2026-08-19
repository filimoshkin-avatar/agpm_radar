#!/usr/bin/env python3
"""Generate Russian LLM analysis for AgPM Radar through OpenClaw CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agpm_radar_issue_theses import (
    DAILY_ANALYSIS_PROMPT_VERSION,
    RUBRIC_NAMES,
    build_brief,
    compact_material,
    load_issue_materials,
    normalize_daily_analysis,
    normalize_theses,
)
from radar_paths import DB_PATH, LLM_CLASSIFICATION_DIR, ensure_dirs


OPENCLAW_DAILY_PROMPT_VERSION = "openclaw-daily-analysis-ru-v1"
OPENCLAW_ISSUE_THESES_PROMPT_VERSION = "openclaw-issue-theses-ru-v1"
OPENCLAW_CARD_SUMMARY_PROMPT_VERSION = "openclaw-card-summary-ru-v1"


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def model_chain(primary: str, fallbacks: str) -> list[str]:
    result: list[str] = []
    for value in [primary, *fallbacks.split(",")]:
        model = value.strip()
        if model and model not in result:
            result.append(model)
    return result


def retry_delays(value: str) -> list[float]:
    delays = [float(item.strip()) for item in value.split(",") if item.strip()]
    if not delays:
        raise ValueError("At least one retry delay is required")
    if any(delay < 0 for delay in delays):
        raise ValueError("Retry delays must be non-negative")
    return delays


def run_with_model_fallback(
    label: str,
    operation: Any,
    *,
    models: list[str],
    delays: list[float],
) -> tuple[Any, str]:
    errors: list[str] = []
    for model in models:
        for attempt, delay_seconds in enumerate(delays, start=1):
            if delay_seconds:
                print(f"{label}: waiting {delay_seconds:g}s before attempt {attempt} with {model}")
                time.sleep(delay_seconds)
            try:
                print(f"{label}: attempt {attempt}/{len(delays)} with {model}")
                return operation(model), model
            except Exception as exc:  # noqa: BLE001 - try the next attempt or model.
                message = f"{model} attempt {attempt}/{len(delays)}: {exc}"
                errors.append(message)
                print(f"{label}: {message}")
    raise RuntimeError(f"{label} failed across all models: {' | '.join(errors)}")


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_model_json(text: str) -> Any:
    cleaned = strip_json_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = min([pos for pos in [cleaned.find("{"), cleaned.find("[")] if pos >= 0], default=-1)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if start >= 0 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def openclaw_json(
    prompt: str,
    *,
    model: str,
    stem: str,
    timeout: int,
) -> tuple[Any, str, str]:
    prompt_dir = LLM_CLASSIFICATION_DIR / "openclaw-analysis"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    request_path = prompt_dir / f"{stem}.{utc_stamp()}.request.json"
    response_path = prompt_dir / f"{stem}.{utc_stamp()}.response.json"
    request_payload = {
        "command": "openclaw infer model run",
        "model": model,
        "prompt": prompt,
    }
    request_path.write_text(json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    completed = subprocess.run(
        ["openclaw", "infer", "model", "run", "--model", model, "--json", "--prompt", prompt],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    raw = {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    response_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"openclaw infer failed with code {completed.returncode}: {completed.stderr.strip()}")
    outer = parse_model_json(completed.stdout)
    outputs = outer.get("outputs") or []
    if not outputs or not isinstance(outputs[0], dict):
        raise RuntimeError("openclaw infer returned no text output")
    return parse_model_json(str(outputs[0].get("text") or "")), str(request_path), str(response_path)


def load_issue_theses(conn: sqlite3.Connection, issue_date: str) -> list[dict[str, str]]:
    row = conn.execute("SELECT theses_json FROM issues WHERE issue_date = ?", (issue_date,)).fetchone()
    if not row:
        return []
    try:
        data = json.loads(row["theses_json"] or "[]")
    except json.JSONDecodeError:
        return []
    return normalize_theses(data)


def latest_issue_date(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT issue_date FROM issues ORDER BY issue_date DESC LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("No Radar issues found")
    return str(row["issue_date"])


def issue_context(
    conn: sqlite3.Connection,
    issue_date: str,
    max_materials: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str, dict[str, Any]]:
    materials = load_issue_materials(conn, issue_date)
    theses = load_issue_theses(conn, issue_date)
    brief = build_brief(materials, theses)
    selected = sorted(
        materials,
        key=lambda item: (
            0 if item.get("key_material") else 1,
            {"near": 0, "mid": 1, "far": 2}.get(str(item.get("perimeter")), 3),
            str(item.get("title") or ""),
        ),
    )[:max_materials]
    context = {
        "issue_date": issue_date,
        "brief": brief,
        "formal_theses": theses,
        "rubrics": RUBRIC_NAMES,
        "materials": [compact_material(item) | {"id": item.get("id"), "url": item.get("url")} for item in selected],
    }
    return materials, theses, brief, context


def generate_daily_analysis(
    conn: sqlite3.Connection,
    issue_date: str,
    context: dict[str, Any],
    *,
    model: str,
    timeout: int,
) -> tuple[str, str]:
    prompt = (
        "Ты аналитик AgPM Radar. Сформируй подробный дневной разбор выпуска на русском языке.\n"
        "Опирайся только на включённые в выпуск материалы. Не придумывай факты, цифры и источники.\n"
        "Не заменяй четыре формальных тезиса: это отдельный раскрываемый аналитический блок.\n"
        "Верни только JSON с полями: headline, signal, why_agpm, watch_next, evidence_titles.\n"
        "signal и why_agpm должны быть развёрнутыми: 3-5 связных абзацев каждый, с управленческим смыслом для AgPM, PMO, ИСУП, governance, рисков и операционной модели.\n"
        "watch_next: 2-4 предложения о том, что отслеживать в следующих выпусках.\n"
        "evidence_titles: 5-10 названий материалов из выпуска.\n\n"
        f"Данные выпуска: {json.dumps(context, ensure_ascii=False)}"
    )
    parsed, request_path, response_path = openclaw_json(
        prompt,
        model=model,
        stem=f"{issue_date}.{OPENCLAW_DAILY_PROMPT_VERSION}",
        timeout=timeout,
    )
    analysis = normalize_daily_analysis(parsed)
    if not analysis:
        raise RuntimeError("OpenClaw daily analysis JSON failed validation")
    conn.execute(
        """
        INSERT INTO issue_daily_analysis(
          issue_date, headline, analysis_json, provider, model, prompt_version,
          request_path, response_path, status, error, updated_at
        )
        VALUES (?, ?, ?, 'openclaw', ?, ?, ?, ?, 'success', '', datetime('now'))
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
            model,
            DAILY_ANALYSIS_PROMPT_VERSION,
            request_path,
            response_path,
        ),
    )
    return request_path, response_path


def generate_issue_llm_theses(
    conn: sqlite3.Connection,
    issue_date: str,
    context: dict[str, Any],
    *,
    model: str,
    timeout: int,
) -> tuple[str, str]:
    prompt = (
        "Ты аналитик AgPM Radar. Сформируй LLM-версию четырёх главных тезисов выпуска на русском языке.\n"
        "Это аналитический слой рядом с формальными тезисами, а не замена текущего deterministic-блока.\n"
        "Каждый тезис должен быть управленческим выводом для AgPM, а не пересказом одной новости.\n"
        "Верни только JSON вида {\"brief\":\"одна фраза\", \"theses\":[{\"lead\":\"короткий тезис\", \"rest\":\"обоснование 1-2 предложения\"}]}.\n"
        "Тезисов должно быть ровно 4. Опирайся только на данные выпуска.\n\n"
        f"Данные выпуска: {json.dumps(context, ensure_ascii=False)}"
    )
    parsed, request_path, response_path = openclaw_json(
        prompt,
        model=model,
        stem=f"{issue_date}.{OPENCLAW_ISSUE_THESES_PROMPT_VERSION}",
        timeout=timeout,
    )
    theses = normalize_theses(parsed.get("theses") if isinstance(parsed, dict) else None)
    if len(theses) != 4:
        raise RuntimeError("OpenClaw issue theses JSON must contain exactly 4 valid theses")
    brief = str(parsed.get("brief") or "").strip() if isinstance(parsed, dict) else ""
    conn.execute(
        """
        INSERT INTO issue_llm_theses(
          issue_date, theses_json, brief, provider, model, prompt_version,
          request_path, response_path, status, error, updated_at
        )
        VALUES (?, ?, ?, 'openclaw', ?, ?, ?, ?, 'success', '', datetime('now'))
        ON CONFLICT(issue_date) DO UPDATE SET
          theses_json = excluded.theses_json,
          brief = excluded.brief,
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
            json.dumps(theses, ensure_ascii=False),
            brief,
            model,
            OPENCLAW_ISSUE_THESES_PROMPT_VERSION,
            request_path,
            response_path,
        ),
    )
    return request_path, response_path


def normalize_card_summaries(value: Any, material_ids: set[str]) -> list[dict[str, str]]:
    if isinstance(value, dict):
        value = value.get("summaries")
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        material_id = str(item.get("id") or item.get("material_id") or "").strip()
        short_text = str(item.get("short_text") or "").strip()
        agpm_angle = str(item.get("agpm_angle") or "").strip()
        if material_id in material_ids and short_text:
            result.append({"id": material_id, "short_text": short_text, "agpm_angle": agpm_angle})
    return result


def generate_card_summaries(
    conn: sqlite3.Connection,
    issue_date: str,
    context: dict[str, Any],
    *,
    model: str,
    timeout: int,
) -> tuple[int, str, str]:
    material_ids = {str(item.get("id")) for item in context["materials"] if item.get("id")}
    prompt = (
        "Ты редактор AgPM Radar. Для каждой карточки выпуска подготовь короткий русский LLM-текст.\n"
        "Не меняй факты и не добавляй внешние сведения. Текст должен помогать читателю понять, почему материал важен для AgPM.\n"
        "Верни только JSON вида {\"summaries\":[{\"id\":\"material_id\", \"short_text\":\"1-2 предложения\", \"agpm_angle\":\"краткий управленческий угол\"}]}.\n"
        "Верни запись для каждого material_id из входных данных.\n\n"
        f"Материалы выпуска: {json.dumps(context['materials'], ensure_ascii=False)}"
    )
    parsed, request_path, response_path = openclaw_json(
        prompt,
        model=model,
        stem=f"{issue_date}.{OPENCLAW_CARD_SUMMARY_PROMPT_VERSION}",
        timeout=timeout,
    )
    summaries = normalize_card_summaries(parsed, material_ids)
    returned_ids = {item["id"] for item in summaries}
    missing_ids = material_ids - returned_ids
    if missing_ids:
        raise RuntimeError(
            f"OpenClaw card summaries JSON is incomplete: {len(missing_ids)} of {len(material_ids)} material(s) missing"
        )
    for item in summaries:
        conn.execute(
            """
            INSERT INTO material_llm_summaries(
              material_id, issue_date, short_text, agpm_angle, provider, model,
              prompt_version, request_path, response_path, status, error, updated_at
            )
            VALUES (?, ?, ?, ?, 'openclaw', ?, ?, ?, ?, 'success', '', datetime('now'))
            ON CONFLICT(material_id) DO UPDATE SET
              issue_date = excluded.issue_date,
              short_text = excluded.short_text,
              agpm_angle = excluded.agpm_angle,
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
                item["id"],
                issue_date,
                item["short_text"],
                item.get("agpm_angle", ""),
                model,
                OPENCLAW_CARD_SUMMARY_PROMPT_VERSION,
                request_path,
                response_path,
            ),
        )
    return len(summaries), request_path, response_path


def mark_error(conn: sqlite3.Connection, table: str, issue_date: str, model: str, error: str) -> None:
    if table == "issue_daily_analysis":
        conn.execute(
            """
            INSERT INTO issue_daily_analysis(issue_date, provider, model, prompt_version, status, error, updated_at)
            VALUES (?, 'openclaw', ?, ?, 'error', ?, datetime('now'))
            ON CONFLICT(issue_date) DO UPDATE SET
              provider = excluded.provider, model = excluded.model, prompt_version = excluded.prompt_version,
              status = excluded.status, error = excluded.error, updated_at = datetime('now')
            """,
            (issue_date, model, DAILY_ANALYSIS_PROMPT_VERSION, error),
        )
    elif table == "issue_llm_theses":
        conn.execute(
            """
            INSERT INTO issue_llm_theses(issue_date, provider, model, prompt_version, status, error, updated_at)
            VALUES (?, 'openclaw', ?, ?, 'error', ?, datetime('now'))
            ON CONFLICT(issue_date) DO UPDATE SET
              provider = excluded.provider, model = excluded.model, prompt_version = excluded.prompt_version,
              status = excluded.status, error = excluded.error, updated_at = datetime('now')
            """,
            (issue_date, model, OPENCLAW_ISSUE_THESES_PROMPT_VERSION, error),
        )


def mark_card_errors(
    conn: sqlite3.Connection,
    issue_date: str,
    material_ids: set[str],
    model: str,
    error: str,
) -> None:
    for material_id in material_ids:
        conn.execute(
            """
            INSERT INTO material_llm_summaries(
              material_id, issue_date, short_text, agpm_angle, provider, model,
              prompt_version, request_path, response_path, status, error, updated_at
            )
            VALUES (?, ?, '', '', 'openclaw', ?, ?, '', '', 'error', ?, datetime('now'))
            ON CONFLICT(material_id) DO UPDATE SET
              issue_date = excluded.issue_date,
              provider = excluded.provider,
              model = excluded.model,
              prompt_version = excluded.prompt_version,
              status = excluded.status,
              error = excluded.error,
              updated_at = datetime('now')
            """,
            (material_id, issue_date, model, OPENCLAW_CARD_SUMMARY_PROMPT_VERSION, error),
        )


def mark_empty_issue(conn: sqlite3.Connection, issue_date: str, model: str) -> None:
    conn.execute(
        """
        INSERT INTO issue_llm_theses(
          issue_date, theses_json, brief, provider, model, prompt_version,
          request_path, response_path, status, error, updated_at
        )
        VALUES (?, '[]', 'Выпуск без материалов после смыслового фильтра.', 'openclaw', ?, ?, '', '', 'skipped', '', datetime('now'))
        ON CONFLICT(issue_date) DO UPDATE SET
          theses_json = excluded.theses_json,
          brief = excluded.brief,
          provider = excluded.provider,
          model = excluded.model,
          prompt_version = excluded.prompt_version,
          request_path = excluded.request_path,
          response_path = excluded.response_path,
          status = excluded.status,
          error = excluded.error,
          updated_at = datetime('now')
        """,
        (issue_date, model, OPENCLAW_ISSUE_THESES_PROMPT_VERSION),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate OpenClaw LLM analysis for one Radar issue.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--issue-date", help="Issue date, YYYY-MM-DD. Defaults to latest issue.")
    parser.add_argument("--model", default=os.getenv("RADAR_OPENCLAW_MODEL", "openai/gpt-5.5"))
    parser.add_argument(
        "--fallback-models",
        default=os.getenv("RADAR_OPENCLAW_FALLBACK_MODELS", "openai/gpt-5.6-sol,minimax/MiniMax-M3"),
        help="Comma-separated fallback models, tried after the primary model.",
    )
    parser.add_argument(
        "--retry-delays",
        default=os.getenv("RADAR_OPENCLAW_RETRY_DELAYS", "0,5,20"),
        help="Comma-separated delays in seconds; one attempt is made for each value and model.",
    )
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--max-materials", type=int, default=20)
    parser.add_argument("--skip-card-summaries", action="store_true")
    parser.add_argument(
        "--require-all",
        action="store_true",
        help="Return a non-zero exit code if any LLM layer fails after all retries and fallbacks.",
    )
    args = parser.parse_args()

    ensure_dirs()
    conn = connect(args.db)
    try:
        issue_date = args.issue_date or latest_issue_date(conn)
        materials, _, _, context = issue_context(conn, issue_date, args.max_materials)
        if not materials:
            mark_empty_issue(conn, issue_date, args.model)
            conn.commit()
            print(f"OpenClaw LLM analysis skipped for {issue_date}: issue has no materials")
            return 0
        print(f"Generating OpenClaw LLM analysis for {issue_date}: {len(context['materials'])} materials")
        models = model_chain(args.model, args.fallback_models)
        delays = retry_delays(args.retry_delays)
        failures: list[str] = []

        try:
            _, daily_model = run_with_model_fallback(
                "daily analysis",
                lambda model: generate_daily_analysis(
                    conn, issue_date, context, model=model, timeout=args.timeout
                ),
                models=models,
                delays=delays,
            )
            conn.commit()
            print(f"daily analysis: success with {daily_model}")
        except Exception as exc:  # noqa: BLE001 - persist error and continue with other layers.
            mark_error(conn, "issue_daily_analysis", issue_date, models[-1], str(exc))
            conn.commit()
            failures.append(str(exc))

        try:
            _, theses_model = run_with_model_fallback(
                "issue theses",
                lambda model: generate_issue_llm_theses(
                    conn, issue_date, context, model=model, timeout=args.timeout
                ),
                models=models,
                delays=delays,
            )
            conn.commit()
            print(f"issue theses: success with {theses_model}")
        except Exception as exc:  # noqa: BLE001
            mark_error(conn, "issue_llm_theses", issue_date, models[-1], str(exc))
            conn.commit()
            failures.append(str(exc))

        card_count = 0
        if not args.skip_card_summaries:
            try:
                card_result, card_model = run_with_model_fallback(
                    "card summaries",
                    lambda model: generate_card_summaries(
                        conn, issue_date, context, model=model, timeout=args.timeout
                    ),
                    models=models,
                    delays=delays,
                )
                card_count = int(card_result[0])
                conn.commit()
                print(f"card summaries: success with {card_model}")
            except Exception as exc:  # noqa: BLE001
                material_ids = {str(item.get("id")) for item in context["materials"] if item.get("id")}
                mark_card_errors(conn, issue_date, material_ids, models[-1], str(exc))
                conn.commit()
                failures.append(str(exc))

        conn.commit()
        print(f"OpenClaw LLM analysis saved for {issue_date}; card summaries: {card_count}")
        if failures:
            print(
                f"OpenClaw LLM analysis incomplete for {issue_date}: {len(failures)} layer(s) failed; "
                "deterministic fallbacks remain available"
            )
            if args.require_all:
                return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
