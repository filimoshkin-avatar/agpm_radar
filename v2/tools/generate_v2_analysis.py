"""Generate a daily analysis from the final, immutable V2 issue composition."""

# ruff: noqa: RUF001,S603,S607

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import cast

from packages.contracts.analysis import clean_evidence_material_ids, issue_content_hash
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.safe_files import atomic_write_new

MODEL = "openai/gpt-5.5"
PROMPT_VERSION = "v2-daily-analysis-ru-v2"
MAX_ATTEMPTS = 3
MIN_ANALYTIC_PARAGRAPHS = 3
MAX_ANALYTIC_PARAGRAPHS = 5
MIN_ANALYTIC_CHARS = 1_200
MIN_WATCH_SENTENCES = 2
MAX_WATCH_SENTENCES = 4


class V2AnalysisError(RuntimeError):
    """The V2-native analysis could not be generated or verified."""


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?…])\s+", text.strip()) if part.strip()]


def _quality_violations(raw: JsonObject) -> list[str]:
    violations: list[str] = []
    for key in ("signal", "why_agpm"):
        text = str(raw.get(key) or "").strip()
        paragraph_count = len(_paragraphs(text))
        if not MIN_ANALYTIC_PARAGRAPHS <= paragraph_count <= MAX_ANALYTIC_PARAGRAPHS:
            violations.append(
                f"{key}: требуется {MIN_ANALYTIC_PARAGRAPHS}–{MAX_ANALYTIC_PARAGRAPHS} "
                f"абзацев, получено {paragraph_count}"
            )
        if len(text) < MIN_ANALYTIC_CHARS:
            violations.append(
                f"{key}: требуется не менее {MIN_ANALYTIC_CHARS} знаков, получено {len(text)}"
            )
    watch_next = str(raw.get("watch_next") or "").strip()
    sentence_count = len(_sentences(watch_next))
    if not MIN_WATCH_SENTENCES <= sentence_count <= MAX_WATCH_SENTENCES:
        violations.append(
            f"watch_next: требуется {MIN_WATCH_SENTENCES}–{MAX_WATCH_SENTENCES} предложения, "
            f"получено {sentence_count}"
        )
    return violations


def _parse_json(text: str) -> JsonObject:
    value: object = json.loads(text)
    if not isinstance(value, dict):
        raise V2AnalysisError("model response is not a JSON object")
    return cast(JsonObject, value)


def _model_payload(stdout: str) -> JsonObject:
    outer = _parse_json(stdout)
    outputs = outer.get("outputs")
    if not isinstance(outputs, list) or not outputs or not isinstance(outputs[0], dict):
        raise V2AnalysisError("OpenClaw returned no model output")
    return _parse_json(str(cast(dict[str, object], outputs[0]).get("text") or ""))


def validate_v2_analysis(
    raw: JsonObject, *, materials: list[JsonObject], content_hash: str
) -> JsonObject:
    required_text = ("headline", "signal", "why_agpm", "watch_next")
    for key in required_text:
        if not isinstance(raw.get(key), str) or not str(raw[key]).strip():
            raise V2AnalysisError(f"analysis field {key} is empty")
    if raw.get("input_content_hash") != content_hash:
        raise V2AnalysisError("analysis input_content_hash differs from final V2 composition")
    evidence_ids = clean_evidence_material_ids(raw.get("evidence_material_ids"))
    included = {str(item["materialId"]): item for item in materials}
    if not evidence_ids:
        raise V2AnalysisError("successful analysis has no evidence material ids")
    unknown = [material_id for material_id in evidence_ids if material_id not in included]
    if unknown:
        raise V2AnalysisError(f"analysis cites materials outside the V2 issue: {unknown}")
    quality_violations = _quality_violations(raw)
    if quality_violations:
        raise V2AnalysisError("analysis quality gate failed: " + "; ".join(quality_violations))
    return cast(
        JsonObject,
        {
            "headline": str(raw["headline"]).strip(),
            "signal": str(raw["signal"]).strip(),
            "why_agpm": str(raw["why_agpm"]).strip(),
            "watch_next": str(raw["watch_next"]).strip(),
            "evidence_material_ids": evidence_ids,
            "evidence_titles": [str(included[item_id]["title"]) for item_id in evidence_ids],
            "input_content_hash": content_hash,
        },
    )


def generate_v2_analysis(
    *, issue_date: str, materials: list[JsonObject], artifacts_root: Path, timeout: int = 180
) -> JsonObject:
    """Call OpenClaw only after V2 eligibility has fixed the included materials."""
    content_hash = issue_content_hash(materials)
    context = {
        "issue_date": issue_date,
        "issue_content_hash": content_hash,
        "materials": [
            {
                "material_id": item["materialId"],
                "title": item["title"],
                "summary": item["summary"],
                "agpm_takeaway": item["agpmTakeaway"],
                "perimeter": item["perimeter"],
                "rubrics": item["rubrics"],
            }
            for item in materials
        ],
    }
    base_prompt = (
        "Ты аналитик AgPM Radar V2. Сформируй подробный дневной разбор на русском языке.\n"
        "Опирайся только на материалы из входного JSON. Не упоминай исключённые или внешние "
        "материалы и не придумывай факты.\n"
        "Не заменяй четыре формальных тезиса: это отдельный раскрываемый аналитический блок.\n"
        "Верни только JSON с полями headline, signal, why_agpm, watch_next, "
        "evidence_material_ids, input_content_hash.\n"
        "signal и why_agpm должны быть развёрнутыми: по 3–5 связных абзацев и не менее "
        "1 200 знаков каждый. Раскрой управленческий смысл для AgPM, PMO, ИСУП, governance, "
        "рисков и операционной модели. Отделяй факты источников от аналитических выводов.\n"
        "watch_next: 2–4 предложения о том, что проверять в следующих выпусках. "
        "evidence_material_ids: 2–10 идентификаторов только из входного списка. "
        "input_content_hash верни без изменений.\n\n"
        f"Данные выпуска: {json.dumps(context, ensure_ascii=False)}"
    )
    artifacts_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    failures: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        repair = ""
        if failures:
            repair = (
                "\n\nПредыдущий ответ отклонён контролем качества. Исправь все нарушения:\n- "
                + "\n- ".join(failures[-1].split("; "))
            )
        prompt = base_prompt + repair
        request = canonical_json_line(
            {
                "attempt": attempt,
                "model": MODEL,
                "prompt": prompt,
                "promptVersion": PROMPT_VERSION,
            }
        )
        atomic_write_new(artifacts_root / f"request-attempt-{attempt}.json", request, mode=0o600)
        if attempt == 1:
            atomic_write_new(artifacts_root / "request.json", request, mode=0o600)
        completed = subprocess.run(
            ["openclaw", "infer", "model", "run", "--model", MODEL, "--json", "--prompt", prompt],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
        response = canonical_json_line(
            {
                "attempt": attempt,
                "returncode": completed.returncode,
                "stderr": completed.stderr,
                "stdout": completed.stdout,
            }
        )
        atomic_write_new(artifacts_root / f"response-attempt-{attempt}.json", response, mode=0o600)
        if attempt == 1:
            atomic_write_new(artifacts_root / "response.json", response, mode=0o600)
        if completed.returncode != 0:
            failures.append(f"OpenClaw inference failed with code {completed.returncode}")
            continue
        try:
            return validate_v2_analysis(
                _model_payload(completed.stdout), materials=materials, content_hash=content_hash
            )
        except (V2AnalysisError, json.JSONDecodeError) as exc:
            failures.append(str(exc))
    raise V2AnalysisError(
        f"analysis failed after {MAX_ATTEMPTS} attempts: " + " | ".join(failures)
    )
