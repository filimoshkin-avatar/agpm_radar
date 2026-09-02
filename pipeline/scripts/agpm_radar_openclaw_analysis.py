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
from itertools import combinations
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
from radar_paths import DB_PATH, LLM_CLASSIFICATION_DIR, WORKSPACE_CORPUS, ensure_dirs


OPENCLAW_DAILY_PROMPT_VERSION = "openclaw-daily-analysis-ru-v1"
OPENCLAW_ISSUE_THESES_PROMPT_VERSION = "openclaw-issue-theses-ru-v1"
OPENCLAW_CARD_SUMMARY_PROMPT_VERSION = "openclaw-card-summary-ru-v4"
CARD_SIMILARITY_THRESHOLD = 0.72
CARD_LEADING_WORDS = 8
CARD_SOURCE_TEXT_CHARS = 12000
CARD_MIN_TEXT_CHARS = 80
# The prompt asks for 600 and 500 characters; the check leaves room for a long sentence.
CARD_MAX_TEXT_CHARS = {"short_text": 720, "agpm_angle": 600}
# Openings of the rule-based card texts, normalised the way card_text_words() does it.
# A model that was shown the article and still writes one of these is paraphrasing
# the template, not the source.
CARD_TEMPLATE_PHRASES = (
    "описывает переход от отдельных",
    "переход от отдельных ai помощников",
    "усиливает governance линию",
    "для agpm это важно",
    "материал близкого периметра",
    "материал среднего периметра",
    "сигнал дальнего периметра",
    "нужно читать как сигнал",
)


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


def card_text_words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", text.casefold())


def card_text_shingles(text: str, width: int = 3) -> set[tuple[str, ...]]:
    words = card_text_words(text)
    if len(words) < width:
        return {tuple(words)} if words else set()
    return {tuple(words[index : index + width]) for index in range(len(words) - width + 1)}


def card_text_similarity(left: str, right: str) -> float:
    left_shingles = card_text_shingles(left)
    right_shingles = card_text_shingles(right)
    union = left_shingles | right_shingles
    return len(left_shingles & right_shingles) / len(union) if union else 0.0


def card_terms(text: str) -> set[str]:
    return {word for word in card_text_words(text) if len(word) >= 4 or word.isdigit()}


def card_binding_terms(short_text: str, source_text: str, title: str) -> set[str]:
    """Terms the description takes from the article body rather than from its title."""
    return (card_terms(short_text) & card_terms(source_text)) - card_terms(title)


def card_template_phrase(text: str) -> str | None:
    words = " ".join(card_text_words(text))
    for phrase in CARD_TEMPLATE_PHRASES:
        if phrase in words:
            return phrase
    return None


def card_texts_repeat(left: str, right: str) -> tuple[bool, float]:
    same_lead = (
        card_text_words(left)[:CARD_LEADING_WORDS] == card_text_words(right)[:CARD_LEADING_WORDS]
    )
    return same_lead, card_text_similarity(left, right)


def repeated_card_pair(cards: list[dict[str, str]]) -> tuple[str, str, str, str] | None:
    """First pair of cards sharing an opening or most of their wording: field, left id, right id, why."""
    for field in ("short_text", "agpm_angle"):
        for left, right in combinations(cards, 2):
            same_lead, similarity = card_texts_repeat(left[field], right[field])
            if same_lead or similarity >= CARD_SIMILARITY_THRESHOLD:
                return field, left["id"], right["id"], f"similarity={similarity:.2f}, same_lead={same_lead}"
    return None


def normalize_card(value: Any) -> dict[str, str]:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, dict):
        raise RuntimeError("card JSON is not an object")
    short_text = str(value.get("short_text") or "").strip()
    agpm_angle = str(value.get("agpm_angle") or "").strip()
    if not short_text or not agpm_angle:
        raise RuntimeError("card JSON lacks short_text or agpm_angle")
    return {"short_text": short_text, "agpm_angle": agpm_angle}


def validate_card_text(card: dict[str, str], *, source_text: str, title: str) -> None:
    for field in ("short_text", "agpm_angle"):
        if len(card[field]) < CARD_MIN_TEXT_CHARS:
            raise RuntimeError(f"{field} is too short: {len(card[field])} chars")
        if len(card[field]) > CARD_MAX_TEXT_CHARS[field]:
            raise RuntimeError(
                f"{field} is too long: {len(card[field])} chars, at most {CARD_MAX_TEXT_CHARS[field]}"
            )
        phrase = card_template_phrase(card[field])
        if phrase:
            raise RuntimeError(f"{field} uses a template phrase: «{phrase}»")
    if not card_binding_terms(card["short_text"], source_text, title):
        raise RuntimeError("short_text names nothing from the article body beyond its title")
    same_lead, similarity = card_texts_repeat(card["short_text"], card["agpm_angle"])
    if same_lead or similarity >= CARD_SIMILARITY_THRESHOLD:
        raise RuntimeError(
            f"agpm_angle repeats short_text (similarity={similarity:.2f}, same_lead={same_lead})"
        )


def load_card_source_text(material: dict[str, Any]) -> tuple[str, str]:
    """Article body for one material from the shared fulltext cache.

    A miss is fetched; a cached failure is retried once, so a transient error does not
    leave the card without its article for good. Returns the text and the fetch status.
    """
    # Pulls requests and BeautifulSoup; imported here so the module stays importable without them.
    from agpm_radar_report import fetch_fulltext, fulltext_cache_path

    payload = fetch_fulltext(material, WORKSPACE_CORPUS)
    if payload and payload.get("status") != "resolved":
        cache = fulltext_cache_path(WORKSPACE_CORPUS, material.get("url"))
        if cache.exists():
            cache.unlink()
            payload = fetch_fulltext(material, WORKSPACE_CORPUS)
    if not payload:
        return "", "no_url"
    text = str(payload.get("text") or "").strip()
    status = str(payload.get("status") or "unresolved")
    if status != "resolved" or len(text) < 300:
        return "", status
    return text[:CARD_SOURCE_TEXT_CHARS], status


def card_prompt(material: dict[str, Any], source_text: str, feedback: list[str]) -> str:
    article = {
        "title": material.get("title"),
        "source_name": material.get("source_name"),
        "url": material.get("url"),
        "published_at": material.get("published_at"),
        "perimeter": material.get("perimeter"),
        "rubrics": [RUBRIC_NAMES.get(rubric, rubric) for rubric in material.get("rubrics") or []],
        "source_text": source_text,
    }
    repair = (
        f"\n\nПредыдущий ответ отклонён: {feedback[-1]}. Напиши оба текста заново с учётом этого."
        if feedback
        else ""
    )
    return (
        "Ты редактор AgPM Radar. AgPM — агентное управление проектами: применение ИИ-агентов "
        "в проектном управлении, PMO и ИСУП.\n"
        "Ниже одна статья из выпуска радара: заголовок, источник и текст. "
        "Подготовь по ней два самостоятельных текста на русском языке.\n"
        "short_text — 2–3 коротких предложения, не длиннее 600 знаков, о том, что конкретно в статье: "
        "кто и что сделал или предложил, какие продукты, компании, цифры, сроки и механизмы названы. "
        "Только то, что есть в тексте. Выбери 2–4 самых важных факта, а не перечисляй всё. "
        "Не пересказывай заголовок и не подменяй содержание общими словами об агентах и workflow.\n"
        "agpm_angle — 2–3 предложения, не длиннее 500 знаков, с управленческим выводом для AgPM, PMO "
        "или ИСУП: что из этой статьи стоит взять в практику проектного управления, где риск, что "
        "проверить у себя. Вывод опирается на факты из short_text, а не на общие слова об "
        "управляемости и governance. Начинай его с сути, а не с «Для PMO» или «Для AgPM».\n"
        "Запрещено: универсальные заготовки («для AgPM это важно», «усиливает governance-линию», "
        "«переход от помощников к агентным workflow»), факты не из статьи, повтор одного текста в другом.\n"
        "Ответ будет отклонён, если short_text не содержит ни одного названия, числа или термина "
        "из текста статьи, или если любой из текстов состоит из заготовок.\n"
        'Верни только JSON вида {"short_text": "...", "agpm_angle": "..."}.'
        f"{repair}\n\nСтатья: {json.dumps(article, ensure_ascii=False)}"
    )


def generate_card_text(
    material: dict[str, Any],
    source_text: str,
    *,
    issue_date: str,
    model: str,
    timeout: int,
    feedback: list[str],
) -> tuple[dict[str, str], str, str]:
    """One model call for one card. A rejected answer leaves its reason in `feedback` for the next attempt."""
    stem = f"{issue_date}.{OPENCLAW_CARD_SUMMARY_PROMPT_VERSION}.{material['id']}"
    try:
        parsed, request_path, response_path = openclaw_json(
            card_prompt(material, source_text, feedback), model=model, stem=stem, timeout=timeout
        )
    except json.JSONDecodeError as exc:
        feedback.append("ответ не разобран как JSON")
        raise RuntimeError(f"card JSON is invalid: {exc}") from exc
    try:
        card = normalize_card(parsed)
        validate_card_text(card, source_text=source_text, title=str(material.get("title") or ""))
    except RuntimeError as exc:
        feedback.append(str(exc))
        raise
    return card, request_path, response_path


def write_card_summary(conn: sqlite3.Connection, issue_date: str, card: dict[str, str]) -> None:
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
            card["id"],
            issue_date,
            card["short_text"],
            card["agpm_angle"],
            card["model"],
            OPENCLAW_CARD_SUMMARY_PROMPT_VERSION,
            card["request_path"],
            card["response_path"],
        ),
    )


def generate_card_summaries(
    conn: sqlite3.Connection,
    issue_date: str,
    materials: list[dict[str, Any]],
    *,
    models: list[str],
    delays: list[float],
    timeout: int,
) -> tuple[int, list[str]]:
    """LLM texts for every material whose article body is available; the rest keep the rule-based text.

    Each card is one model call with the article inside the prompt, retried with the rejection reason
    across the model chain. Cards that repeat each other are regenerated once more. Returns the number
    of cards written and the failures that were persisted as errors.
    """
    by_id = {str(item.get("id")): item for item in materials if item.get("id")}
    sources: dict[str, str] = {}
    cards: dict[str, dict[str, str]] = {}
    failures: list[str] = []

    def generate(material_id: str, reason: str | None) -> None:
        feedback = [reason] if reason else []
        try:
            (card, request_path, response_path), model = run_with_model_fallback(
                f"card {material_id}",
                lambda model: generate_card_text(
                    by_id[material_id],
                    sources[material_id],
                    issue_date=issue_date,
                    model=model,
                    timeout=timeout,
                    feedback=feedback,
                ),
                models=models,
                delays=delays,
            )
        except Exception as exc:  # noqa: BLE001 - persist the error and go on with the other cards.
            cards.pop(material_id, None)
            mark_card_errors(conn, issue_date, {material_id}, models[-1], str(exc))
            conn.commit()
            failures.append(f"card {material_id}: {exc}")
            return
        cards[material_id] = {
            **card,
            "id": material_id,
            "model": model,
            "request_path": request_path,
            "response_path": response_path,
        }

    for material_id, material in by_id.items():
        text, status = load_card_source_text(material)
        if not text:
            mark_card_without_source(conn, issue_date, material_id, status)
            conn.commit()
            print(f"card {material_id}: no article text ({status}); rule-based text stays")
            continue
        sources[material_id] = text
        generate(material_id, None)

    for _ in range(len(cards)):
        pair = repeated_card_pair(list(cards.values()))
        if pair is None:
            break
        field, left_id, right_id, detail = pair
        print(f"card {right_id}: {field} repeats card {left_id} ({detail}); regenerating")
        generate(
            right_id,
            f"{field} повторяет текст другой карточки этого выпуска: «{cards[left_id][field][:120]}». "
            "Нужны другое начало и формулировки по фактам именно этой статьи",
        )
    pair = repeated_card_pair(list(cards.values()))
    if pair is not None:
        field, left_id, right_id, detail = pair
        cards.pop(right_id)
        error = f"{field} still repeats card {left_id} ({detail})"
        mark_card_errors(conn, issue_date, {right_id}, models[-1], error)
        failures.append(f"card {right_id}: {error}")

    for card in cards.values():
        write_card_summary(conn, issue_date, card)
    conn.commit()
    return len(cards), failures


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
            WHERE material_llm_summaries.status != 'success'
            """,
            (material_id, issue_date, model, OPENCLAW_CARD_SUMMARY_PROMPT_VERSION, error),
        )


def mark_card_without_source(
    conn: sqlite3.Connection,
    issue_date: str,
    material_id: str,
    fetch_status: str,
) -> None:
    """Record that no model was asked because the article body is unavailable; a stored success stays."""
    conn.execute(
        """
        INSERT INTO material_llm_summaries(
          material_id, issue_date, short_text, agpm_angle, provider, model,
          prompt_version, request_path, response_path, status, error, updated_at
        )
        VALUES (?, ?, '', '', 'openclaw', NULL, ?, '', '', 'fallback', ?, datetime('now'))
        ON CONFLICT(material_id) DO UPDATE SET
          issue_date = excluded.issue_date,
          model = excluded.model,
          prompt_version = excluded.prompt_version,
          status = excluded.status,
          error = excluded.error,
          updated_at = datetime('now')
        WHERE material_llm_summaries.status != 'success'
        """,
        (material_id, issue_date, OPENCLAW_CARD_SUMMARY_PROMPT_VERSION, f"no source text: {fetch_status}"),
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
        "--only-card-summaries",
        action="store_true",
        help="Regenerate only the card texts; the daily analysis and the theses are left as they are.",
    )
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

        if not args.only_card_summaries:
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
            selected_ids = {str(item.get("id")) for item in context["materials"] if item.get("id")}
            card_count, card_failures = generate_card_summaries(
                conn,
                issue_date,
                [item for item in materials if str(item.get("id")) in selected_ids],
                models=models,
                delays=delays,
                timeout=args.timeout,
            )
            failures.extend(card_failures)
            print(f"card summaries: {card_count} written, {len(card_failures)} failed")

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
