"""Generate grounded V2-native 7/30 day AgPM theses with an explicit fallback."""

# ruff: noqa: E501,RUF001,S603,S607

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections.abc import Mapping
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import cast

from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.safe_files import atomic_write_new
from packages.validation.public_issue import build_public_issue_from_views

from tools.generate_v2_analysis import prompt_argv_overflow

PRIMARY_MODEL = "openai/gpt-5.5"
FALLBACK_MODEL = "openai/gpt-5.4"
PROMPT_VERSION = "v2-period-analysis-ru-v1"
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 240
PERIODS = ("7d", "30d")
MARKER = "Период AgPM"


class PeriodAnalysisError(RuntimeError):
    """A period analysis response is missing, unsafe, or insufficiently distinct."""


def _json_object(text: str) -> JsonObject:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise PeriodAnalysisError("ответ модели не является JSON-объектом")
    return cast(JsonObject, parsed)


def _model_payload(stdout: str) -> JsonObject:
    outer = _json_object(stdout)
    outputs = outer.get("outputs")
    if not isinstance(outputs, list) or not outputs or not isinstance(outputs[0], dict):
        raise PeriodAnalysisError("OpenClaw не вернул результат модели")
    return _json_object(str(cast(dict[str, object], outputs[0]).get("text") or ""))


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-zа-яё0-9 ]", " ", value.lower()).split())


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()


def _validate(raw: JsonObject) -> list[JsonObject]:
    theses = raw.get("theses")
    if not isinstance(theses, list) or len(theses) != 4:
        raise PeriodAnalysisError("требуется ровно четыре тезиса")
    result: list[JsonObject] = []
    for index, value in enumerate(theses):
        if not isinstance(value, dict):
            raise PeriodAnalysisError(f"тезис {index + 1} не является объектом")
        lead = str(value.get("lead") or "").strip()
        rest = str(value.get("rest") or "").strip()
        if len(lead) < 35 or len(rest) < 100:
            raise PeriodAnalysisError(f"тезис {index + 1} недостаточно содержателен")
        result.append({"lead": lead, "rest": rest})
    combined = [f"{item['lead']} {item['rest']}" for item in result]
    if len({_normalize(str(item["lead"])) for item in result}) != 4:
        raise PeriodAnalysisError("начала тезисов повторяются")
    if any(_similarity(combined[i], combined[j]) >= 0.82 for i in range(4) for j in range(i)):
        raise PeriodAnalysisError("внутри окна найдены смысловые дубли")
    return result


def _window_documents(
    database: Path,
    *,
    anchor: str,
    period: str,
    current_issue: JsonObject | None,
) -> list[JsonObject]:
    anchor_day = date.fromisoformat(anchor)
    days = 7 if period == "7d" else 30
    start = (anchor_day - timedelta(days=days - 1)).isoformat()
    documents: list[JsonObject] = []
    # The source release is read, never touched: the same read-only URI the daily
    # builder uses for Legacy, so a stray statement cannot reach the pinned bytes.
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        dates = [
            str(row[0])
            for row in connection.execute(
                "SELECT issue_date FROM pub_issues_v1 WHERE issue_date BETWEEN ? AND ? ORDER BY issue_date",
                (start, anchor),
            )
        ]
        for issue_date in dates:
            documents.append(build_public_issue_from_views(connection, issue_date=issue_date))
    if current_issue is not None and not any(item["issueDate"] == anchor for item in documents):
        documents.append(current_issue)
    return documents


def _context(documents: list[JsonObject], period: str, anchor: str) -> JsonObject:
    materials: list[dict[str, object]] = []
    for document in documents:
        for raw in cast(list[dict[str, object]], document["materials"]):
            materials.append(
                {
                    "issueDate": document["issueDate"],
                    "title": raw["title"],
                    "summary": raw.get("llmShortText") or raw.get("summary") or raw.get("brief"),
                    "agpmAngle": raw.get("llmAgpmAngle") or raw.get("agpmTakeaway"),
                    "perimeter": raw["perimeter"],
                    "rubrics": raw["rubrics"],
                    "sourceName": raw["sourceName"],
                }
            )
    total = len(materials)
    materials.sort(
        key=lambda item: (
            {"near": 0, "mid": 1, "far": 2}.get(str(item["perimeter"]), 3),
            -date.fromisoformat(str(item["issueDate"])).toordinal(),
        )
    )
    sample = materials[:60]
    return cast(
        JsonObject,
        {
            "anchorDate": anchor,
            "issueCount": len(documents),
            "materialCount": total,
            "materials": sample,
            "period": period,
            "sampledMaterialCount": len(sample),
        },
    )


def _prompt(context: JsonObject, period: str, previous: list[JsonObject] | None) -> str:
    task = (
        "Найди оперативные изменения: новые сигналы, ускорение или ослабление тем, инциденты и "
        "изменения относительно предшествующей повестки. Не выдавай устойчивый фон за новость."
        if period == "7d"
        else "Найди устойчивые паттерны и структурные сдвиги: повторяемость сигналов, зрелость практик, "
        "накопленные риски и последствия для операционной модели AgPM, PMO и ИСУП."
    )
    distinction = ""
    if previous:
        distinction = (
            "\nТексты окна 7 дней уже сформированы ниже. Тезисы 30 дней не должны повторять их "
            "формулировки или масштаб анализа. Если повестка объективно совпадает, объясни, что именно "
            "делает сигнал устойчивым на месячном горизонте.\n"
            f"Окно 7 дней: {json.dumps(previous, ensure_ascii=False)}\n"
        )
    return (
        "Ты аналитик AgPM Radar V2. Подготовь четыре доказательных управленческих тезиса на русском языке.\n"
        f"{task}\n"
        "Опирайся только на входные данные. Не перечисляй новости по одной, не добавляй внешние факты и "
        "не используй универсальные заготовки. Каждый rest должен показывать опору в корпусе окна и значение "
        "для агентного управления проектами.\n"
        'Верни только JSON: {"theses":[{"lead":"...","rest":"..."}]}.\n'
        f"{distinction}\nДанные окна: {json.dumps(context, ensure_ascii=False)}"
    )


def _fallback(period: str, context: JsonObject, error: str, *, attempts: int) -> JsonObject:
    count = int(cast(int, context["materialCount"]))
    label = "7 дней" if period == "7d" else "30 дней"
    leads = [
        f"За {label} LLM-анализ недоступен; показан резервный срез.",
        "Периодный вывод требует повторной проверки после восстановления модели.",
        "Корпус сохранён и может быть пересчитан без повторного сбора источников.",
        "Резервный текст не используется как основание для изменения методики AgPM.",
    ]
    theses = [
        {
            "lead": lead,
            "rest": f"В окне сохранено {count} материалов. Причина перехода на fallback: {error}",
        }
        for lead in leads
    ]
    return cast(
        JsonObject,
        {
            "attempts": attempts,
            "error": error,
            "model": "rules-period-v2",
            "period": period,
            "promptVersion": PROMPT_VERSION,
            "provider": "fallback",
            "status": "fallback",
            "theses": theses,
        },
    )


def generate_period(
    *,
    database: Path,
    anchor: str,
    period: str,
    artifacts_root: Path,
    current_issue: JsonObject | None = None,
    previous: list[JsonObject] | None = None,
) -> JsonObject:
    documents = _window_documents(
        database, anchor=anchor, period=period, current_issue=current_issue
    )
    context = _context(documents, period, anchor)
    root = artifacts_root / period
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    failures: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        model = PRIMARY_MODEL if attempt < MAX_ATTEMPTS else FALLBACK_MODEL
        repair = (
            f"\nПредыдущий ответ отклонён: {failures[-1]}. Перепиши полностью." if failures else ""
        )
        prompt = _prompt(context, period, previous) + repair
        overflow = prompt_argv_overflow(prompt)
        if overflow is not None:
            failures.append(overflow)
            break
        atomic_write_new(
            root / f"request-attempt-{attempt}.json",
            canonical_json_line(
                {
                    "attempt": attempt,
                    "model": model,
                    "prompt": prompt,
                    "promptVersion": PROMPT_VERSION,
                }
            ),
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
                    model,
                    "--json",
                    "--prompt",
                    prompt,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
            atomic_write_new(
                root / f"response-attempt-{attempt}.json",
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
                raise PeriodAnalysisError(f"OpenClaw завершился с кодом {completed.returncode}")
            theses = _validate(_model_payload(completed.stdout))
            if previous:
                left = " ".join(f"{x['lead']} {x['rest']}" for x in previous)
                right = " ".join(f"{x['lead']} {x['rest']}" for x in theses)
                if _similarity(left, right) >= 0.72:
                    raise PeriodAnalysisError("окна 7 и 30 дней слишком похожи")
            result = cast(
                JsonObject,
                {
                    "attempts": attempt,
                    "error": None,
                    "evidenceTitles": [
                        str(item["title"]) for item in cast(list[JsonObject], context["materials"])
                    ],
                    "issueCount": len(documents),
                    "materialCount": context["materialCount"],
                    "model": model,
                    "period": period,
                    "promptVersion": PROMPT_VERSION,
                    "provider": "openai",
                    "status": "success",
                    "theses": cast(list[object], theses),
                },
            )
            atomic_write_new(root / "result.json", canonical_json_line(result), mode=0o600)
            return result
        except (
            OSError,
            json.JSONDecodeError,
            PeriodAnalysisError,
            subprocess.TimeoutExpired,
        ) as error:
            failures.append(str(error))
    result = _fallback(period, context, " | ".join(failures), attempts=len(failures))
    atomic_write_new(root / "result.json", canonical_json_line(result), mode=0o600)
    return result


def period_blocks(results: Mapping[str, JsonObject]) -> list[JsonObject]:
    blocks: list[JsonObject] = []
    for period in PERIODS:
        result = results[period]
        for index, thesis in enumerate(cast(list[dict[str, object]], result["theses"]), 1):
            blocks.append(
                {
                    "kind": "signals",
                    "title": f"{MARKER} · {period} · {index:02d}",
                    "text": f"{thesis['lead']}\n\n{thesis['rest']}",
                }
            )
        metadata = {
            key: value for key, value in result.items() if key not in {"theses", "evidenceTitles"}
        }
        blocks.append(
            {
                "kind": "actions",
                "title": f"{MARKER} · {period} · метаданные",
                "text": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            }
        )
    return blocks


def strip_period_blocks(blocks: list[JsonObject]) -> list[JsonObject]:
    return [block for block in blocks if not str(block.get("title") or "").startswith(MARKER)]
