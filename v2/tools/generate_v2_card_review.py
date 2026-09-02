"""Regenerate V2 card descriptions for one accepted historical issue."""

# ruff: noqa: RUF001,S603,S607

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from itertools import combinations
from pathlib import Path
from typing import cast

from packages.contracts.json_types import JsonValue
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.safe_files import atomic_write_new
from packages.validation.public_issue import build_public_issue_from_views

MODEL = "openai/gpt-5.5"
PROMPT_VERSION = "v2-card-review-ru-v1"
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 240
SIMILARITY_THRESHOLD = 0.72
LEADING_WORDS = 8


class CardReviewError(RuntimeError):
    """The historical card review could not be generated or verified."""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", text.casefold())


def _shingles(text: str, width: int = 3) -> set[tuple[str, ...]]:
    words = _words(text)
    if len(words) < width:
        return {tuple(words)} if words else set()
    return {tuple(words[index : index + width]) for index in range(len(words) - width + 1)}


def _text_similarity(left: str, right: str) -> float:
    left_shingles = _shingles(left)
    right_shingles = _shingles(right)
    union = left_shingles | right_shingles
    return len(left_shingles & right_shingles) / len(union) if union else 0.0


def _reject_similar_texts(cards: list[JsonObject], field: str) -> None:
    for left, right in combinations(cards, 2):
        left_text = str(left[field])
        right_text = str(right[field])
        same_lead = _words(left_text)[:LEADING_WORDS] == _words(right_text)[:LEADING_WORDS]
        similarity = _text_similarity(left_text, right_text)
        if same_lead or similarity >= SIMILARITY_THRESHOLD:
            raise CardReviewError(
                f"semantically repetitive {field}: {left['materialId']} and "
                f"{right['materialId']} (similarity={similarity:.2f}, same_lead={same_lead})"
            )


def _strip_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _json_object(text: str) -> JsonObject:
    cleaned = _strip_fence(text)
    try:
        value: object = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise CardReviewError("model response is not a JSON object")
    return cast(JsonObject, value)


def _model_payload(stdout: str) -> JsonObject:
    outer = _json_object(stdout)
    outputs = outer.get("outputs")
    if not isinstance(outputs, list) or not outputs or not isinstance(outputs[0], dict):
        raise CardReviewError("OpenClaw returned no model output")
    return _json_object(str(cast(dict[str, object], outputs[0]).get("text") or ""))


def _validate(raw: JsonObject, expected_ids: set[str]) -> list[JsonObject]:
    cards = raw.get("cards")
    if not isinstance(cards, list):
        raise CardReviewError("cards is not an array")
    result: list[JsonObject] = []
    seen: set[str] = set()
    for value in cards:
        if not isinstance(value, dict):
            continue
        material_id = str(value.get("materialId") or "").strip()
        short_text = str(value.get("shortText") or "").strip()
        agpm_angle = str(value.get("agpmAngle") or "").strip()
        if material_id not in expected_ids or material_id in seen:
            continue
        if len(short_text) < 120 or len(agpm_angle) < 120:
            continue
        seen.add(material_id)
        result.append({"materialId": material_id, "shortText": short_text, "agpmAngle": agpm_angle})
    missing = expected_ids - seen
    if missing:
        raise CardReviewError(f"missing or incomplete cards: {sorted(missing)}")
    if len({str(item["shortText"]) for item in result}) != len(result):
        raise CardReviewError("duplicate shortText values")
    if len({str(item["agpmAngle"]) for item in result}) != len(result):
        raise CardReviewError("duplicate agpmAngle values")
    _reject_similar_texts(result, "shortText")
    _reject_similar_texts(result, "agpmAngle")
    return result


def generate(*, database: Path, issue_date: str, output: Path) -> JsonObject:
    with sqlite3.connect(database) as connection:
        public = build_public_issue_from_views(connection, issue_date=issue_date)
    materials = cast(list[dict[str, object]], public["materials"])
    expected_ids = {str(item["id"]) for item in materials}
    context = {
        "issueDate": issue_date,
        "materials": [
            {
                "materialId": item["id"],
                "title": item["title"],
                "summary": item["summary"],
                "brief": item["brief"],
                "agpmTakeaway": item["agpmTakeaway"],
                "perimeter": item["perimeter"],
                "rubrics": item["rubrics"],
            }
            for item in materials
        ],
    }
    base_prompt = (
        "Ты редактор AgPM Radar V2. Пересмотри описания всех карточек исторического выпуска.\n"
        "Для каждой карточки подготовь два самостоятельных текста на русском языке. shortText — "
        "2–3 конкретных предложения о фактах и механизме именно этого материала. agpmAngle — "
        "2–3 предложения с отдельным управленческим выводом для AgPM, PMO или ИСУП.\n"
        "Опирайся только на входные данные, не добавляй внешних фактов. Отделяй факт источника от "
        "аналитического вывода. Не повторяй заголовок как описание. Не используй универсальные "
        "заготовки и одинаковые начала для разных карточек.\n"
        'Верни только JSON вида {"cards":[{"materialId":"...","shortText":"...",'
        '"agpmAngle":"..."}]}. Оба текста обязательны для каждого materialId.\n\n'
        f"Данные выпуска: {json.dumps(context, ensure_ascii=False)}"
    )
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    failures: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        repair = ""
        if failures:
            repair = f"\n\nПредыдущий ответ отклонён: {failures[-1]}. Исправь ответ полностью."
        prompt = base_prompt + repair
        request = {
            "attempt": attempt,
            "model": MODEL,
            "prompt": prompt,
            "promptVersion": PROMPT_VERSION,
        }
        atomic_write_new(
            output / f"request-attempt-{attempt}.json",
            canonical_json_line(cast(JsonObject, request)),
            mode=0o600,
        )
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
                check=False,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
            response = {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
            atomic_write_new(
                output / f"response-attempt-{attempt}.json",
                canonical_json_line(cast(JsonObject, response)),
                mode=0o600,
            )
            if completed.returncode != 0:
                raise CardReviewError(f"OpenClaw exit code {completed.returncode}")
            cards = _validate(_model_payload(completed.stdout), expected_ids)
            result: JsonObject = {
                "cards": cast(list[JsonValue], cards),
                "issueDate": issue_date,
                "model": MODEL,
                "promptVersion": PROMPT_VERSION,
                "status": "success",
            }
            atomic_write_new(output / "result.json", canonical_json_line(result), mode=0o600)
            return result
        except (CardReviewError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
            failures.append(str(error))
    raise CardReviewError("all attempts failed: " + " | ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--issue-date", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = generate(database=args.database, issue_date=args.issue_date, output=args.output)
    print(canonical_json_line(result).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
