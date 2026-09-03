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
PROMPT_VERSION = "v2-daily-analysis-ru-v5"
MAX_ATTEMPTS = 3
ATTEMPT_TIMEOUT_SECONDS = 180
#: This module's worst case: every attempt runs to its own ceiling. The caller must
#: allow the candidate build at least this much - `run_stage15_dual` derives its cap
#: from here rather than remembering a number. Its former 300 seconds were less than
#: 3 x 180, and the run died on a `TimeoutExpired` nobody caught.
WORST_CASE_SECONDS = MAX_ATTEMPTS * ATTEMPT_TIMEOUT_SECONDS
MIN_ANALYTIC_PARAGRAPHS = 3
MAX_ANALYTIC_PARAGRAPHS = 5
MIN_ANALYTIC_CHARS = 1_200
MIN_WATCH_SENTENCES = 2
MAX_WATCH_SENTENCES = 4
MIN_THESIS_REST_CHARS = 320
MIN_THESIS_REST_SENTENCES = 3


class V2AnalysisError(RuntimeError):
    """The V2-native analysis could not be generated or verified."""


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?…])\s+", text.strip()) if part.strip()]


def _quality_violations(raw: JsonObject) -> list[str]:
    violations: list[str] = []
    for key in ("signal", "why_agpm", "watch_next"):
        identifiers = sorted(set(re.findall(r"\bmat_[A-Za-z0-9_]+\b", str(raw.get(key) or ""))))
        if identifiers:
            violations.append(
                f"{key}: технические material_id запрещены в пользовательском тексте; "
                f"замени их точными названиями материалов: {', '.join(identifiers)}"
            )
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


def _thesis_violations(raw: object, *, materials: list[JsonObject]) -> list[str]:
    """Every defect of the four theses at once, so one repair prompt names them all.

    The first version raised on the first defect: the model fixed one rule per
    attempt, and three attempts are fewer than nine rules over four theses. The
    messages go back to the model verbatim.
    """
    if not isinstance(raw, list) or len(raw) != 4:
        return ["analysis theses must contain exactly 4 items"]
    available = {str(item["perimeter"]) for item in materials}
    perimeter_words = {
        "near": re.compile(r"\bблизк\w*\s+периметр\w*", re.IGNORECASE),
        "mid": re.compile(r"\bсредн\w*\s+периметр\w*", re.IGNORECASE),
        "far": re.compile(r"\bдальн\w*\s+периметр\w*", re.IGNORECASE),
    }
    titles = [str(item["title"]).casefold() for item in materials]
    violations: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            violations.append(f"analysis theses[{index}] is not an object")
            continue
        lead = str(item.get("lead") or "").strip()
        rest = str(item.get("rest") or "").strip()
        if not lead or not rest:
            violations.append(f"analysis theses[{index}] has an empty lead or rest")
            continue
        text = f"{lead} {rest}"
        if len(rest) < MIN_THESIS_REST_CHARS:
            violations.append(
                f"analysis theses[{index}] rest is too short: {len(rest)} < {MIN_THESIS_REST_CHARS}"
            )
        sentence_count = len(_sentences(rest))
        if sentence_count < MIN_THESIS_REST_SENTENCES:
            violations.append(
                f"analysis theses[{index}] needs at least {MIN_THESIS_REST_SENTENCES} "
                f"sentences, got {sentence_count}"
            )
        folded_rest = rest.casefold()
        evidence_gap = folded_rest.startswith("материалы выпуска не отвечают на вопрос")
        if not evidence_gap and not any(title in folded_rest for title in titles):
            violations.append(
                f"analysis theses[{index}] must cite an included title or state an evidence gap"
            )
        identifiers = sorted(set(re.findall(r"\bmat_[A-Za-z0-9_]+\b", text)))
        if identifiers:
            violations.append(
                f"analysis theses[{index}] contains reader-facing material_id: {identifiers}"
            )
        # The cited title is the model's obligation, not its own words: a source called
        # "Weekly Summary" or priced "$0.75" in its title must not fail the thesis citing it.
        own_words = text.casefold()
        for title in titles:
            own_words = own_words.replace(title, " ")
        internal_fields = [
            field
            for field in ("llm_short_text", "llm_agpm_angle", "summary", "agpm_takeaway")
            if field in own_words
        ]
        if internal_fields:
            violations.append(
                f"analysis theses[{index}] exposes internal fields: {internal_fields}"
            )
        if re.search(r"\$\d+\.\d+", own_words):
            violations.append(
                f"analysis theses[{index}] uses a dot as a currency decimal separator"
            )
        unsupported = [
            perimeter
            for perimeter, pattern in perimeter_words.items()
            if perimeter not in available and pattern.search(text)
        ]
        if unsupported:
            violations.append(f"analysis theses[{index}] cites absent V2 perimeters: {unsupported}")
    return violations


def _clean_theses(raw: object) -> list[JsonObject]:
    return [
        cast(JsonObject, {"lead": str(item["lead"]).strip(), "rest": str(item["rest"]).strip()})
        for item in cast(list[dict[str, object]], raw)
    ]


def _strip_json_fence(value: str) -> str:
    """Strip the markdown fence a model wraps JSON in even when asked not to.

    The Legacy pipeline calls this very same `openclaw infer` and has stripped the
    fence since 2026-07 (`pipeline/scripts/agpm_radar_openclaw_analysis.py`). The V2
    port dropped that line, and the first fenced answer took the daily chain down.
    Retrying does not cover it: the repair prompt carries "Expecting value: line 1
    column 1", from which no model infers that the fence is the problem.
    """
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json(text: str) -> JsonObject:
    cleaned = _strip_json_fence(text)
    try:
        value: object = json.loads(cleaned)
    except json.JSONDecodeError:
        # Legacy's second line of defence: take everything between the outermost
        # braces. A sentence before or after the object is not worth losing a day for.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
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
    required_text = ("signal", "why_agpm", "watch_next")
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
    quality_violations = _quality_violations(raw) + _thesis_violations(
        raw.get("theses"), materials=materials
    )
    if quality_violations:
        raise V2AnalysisError("analysis quality gate failed: " + "; ".join(quality_violations))
    return cast(
        JsonObject,
        {
            "signal": str(raw["signal"]).strip(),
            "why_agpm": str(raw["why_agpm"]).strip(),
            "watch_next": str(raw["watch_next"]).strip(),
            "theses": _clean_theses(raw["theses"]),
            "evidence_material_ids": evidence_ids,
            "evidence_titles": [str(included[item_id]["title"]) for item_id in evidence_ids],
            "input_content_hash": content_hash,
        },
    )


def generate_v2_analysis(
    *,
    issue_date: str,
    materials: list[JsonObject],
    artifacts_root: Path,
    timeout: int = ATTEMPT_TIMEOUT_SECONDS,
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
                "llm_short_text": item.get("llmShortText"),
                "llm_agpm_angle": item.get("llmAgpmAngle"),
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
        "Сформируй также ровно четыре содержательных тезиса по финальному составу V2. "
        "Не упоминай периметр, которого нет во входном списке.\n"
        "Верни только JSON с полями signal, why_agpm, watch_next, theses, "
        "evidence_material_ids, input_content_hash.\n"
        "signal и why_agpm должны быть развёрнутыми: по 3–5 связных абзацев и не менее "
        "1 200 знаков каждый. Раскрой управленческий смысл для AgPM, PMO, ИСУП, governance, "
        "рисков и операционной модели. Отделяй факты источников от аналитических выводов.\n"
        "watch_next: 2–4 предложения о том, что проверять в следующих выпусках. "
        "theses: массив ровно из четырёх объектов с непустыми полями lead и rest. "
        "lead — конкретный вывод, rest — 3–5 предложений и не менее 320 знаков. "
        "В каждом rest назови точный заголовок хотя бы одного входного материала и приведи "
        "проверяемую конкретную фактологию прежде всего из llm_short_text и llm_agpm_angle: "
        "продукт, действие, число, срок, интеграцию или ограничение. Поля summary и agpm_takeaway "
        "используй только как резервный контекст и не пересказывай их общими фразами. Затем отдели "
        "факт источника от интерпретации для AgPM. Если входные материалы не дают конкретных "
        "фактов для вывода, начни rest "
        "словами «Материалы выпуска не отвечают на вопрос…» и прямо укажи, каких данных "
        "не хватает; "
        "не заполняй пробел общими рассуждениями. Не называй читателю поля JSON: llm_short_text, "
        "llm_agpm_angle, summary, agpm_takeaway. Соблюдай русскую типографику, включая запятую "
        "как десятичный разделитель. "
        "evidence_material_ids: 2–10 идентификаторов только из входного списка. "
        "Технические идентификаторы mat_* используй только в evidence_material_ids. "
        "В signal, why_agpm и watch_next называй материалы только по их точным "
        "человекочитаемым заголовкам из поля title; не показывай material_id читателю. "
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
        try:
            completed = subprocess.run(
                [
                    "openclaw",
                    "infer",
                    "model",
                    "run",
                    "--model",
                    MODEL,
                    "--json",
                    "--prompt",
                    prompt,
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # A hung model costs one attempt. This exception used to travel past
            # V2AnalysisError and up, where nothing caught it either.
            failures.append(f"OpenClaw inference timed out after {timeout} s")
            atomic_write_new(
                artifacts_root / f"response-attempt-{attempt}.json",
                canonical_json_line({"attempt": attempt, "timedOutAfterSeconds": timeout}),
                mode=0o600,
            )
            continue
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
    raise V2AnalysisError(f"analysis failed after {MAX_ATTEMPTS} attempts: " + " | ".join(failures))
