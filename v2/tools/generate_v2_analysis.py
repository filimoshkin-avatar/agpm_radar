"""Generate a daily analysis from the final, immutable V2 issue composition."""

# ruff: noqa: RUF001,S603,S607

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

from packages.contracts.analysis import clean_evidence_material_ids, issue_content_hash
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.safe_files import atomic_write_new

MODEL = "openai/gpt-5.5"
PROMPT_VERSION = "v2-daily-analysis-ru-v1"


class V2AnalysisError(RuntimeError):
    """The V2-native analysis could not be generated or verified."""


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
    prompt = (
        "Ты аналитик AgPM Radar V2. Сформируй подробный дневной разбор на русском языке.\n"
        "Опирайся только на материалы из входного JSON. Не упоминай исключённые или внешние "
        "материалы и не придумывай факты.\n"
        "Верни только JSON с полями headline, signal, why_agpm, watch_next, "
        "evidence_material_ids, input_content_hash.\n"
        "signal и why_agpm: по 3–5 связных абзацев. watch_next: 2–4 предложения. "
        "evidence_material_ids: 2–10 идентификаторов только из входного списка. "
        "input_content_hash верни без изменений.\n\n"
        f"Данные выпуска: {json.dumps(context, ensure_ascii=False)}"
    )
    artifacts_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    atomic_write_new(
        artifacts_root / "request.json",
        canonical_json_line({"model": MODEL, "prompt": prompt, "promptVersion": PROMPT_VERSION}),
        mode=0o600,
    )
    completed = subprocess.run(
        ["openclaw", "infer", "model", "run", "--model", MODEL, "--json", "--prompt", prompt],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    atomic_write_new(
        artifacts_root / "response.json",
        canonical_json_line(
            {
                "returncode": completed.returncode,
                "stderr": completed.stderr,
                "stdout": completed.stdout,
            }
        ),
        mode=0o600,
    )
    if completed.returncode != 0:
        raise V2AnalysisError(f"OpenClaw inference failed with code {completed.returncode}")
    return validate_v2_analysis(
        _model_payload(completed.stdout), materials=materials, content_hash=content_hash
    )
