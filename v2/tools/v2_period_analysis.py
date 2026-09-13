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

from packages.contracts.json_types import JsonValue
from packages.contracts.russian_prose import (
    RUSSIAN_PROSE_PROMPT,
    brand_words,
    require_russian_prose,
)
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.safe_files import atomic_write_new
from packages.validation.public_issue import build_public_issue_from_views

from tools.generate_v2_analysis import MAX_PROMPT_ARGV_BYTES, prompt_argv_overflow

PRIMARY_MODEL = "openai/gpt-5.5"
# All three routes passed live OpenClaw probes on 2026-09-13. Providers differ
# so one provider's outage does not consume the whole recovery policy.
MODEL_CHAIN = (PRIMARY_MODEL, "minimax/MiniMax-M3", "zai/glm-5.2")
ATTEMPTS_PER_MODEL = 2
ATTEMPT_MODELS = tuple(model for model in MODEL_CHAIN for _ in range(ATTEMPTS_PER_MODEL))
PROMPT_VERSION = "v2-period-analysis-ru-v5"
MAX_ATTEMPTS = len(ATTEMPT_MODELS)
TIMEOUT_SECONDS = 180
WORST_CASE_SECONDS = MAX_ATTEMPTS * TIMEOUT_SECONDS
PERIODS = ("7d", "30d")
MARKER = "Период AgPM"

#: How large a prompt may be built. The kernel refuses a single argument above
#: `MAX_PROMPT_ARGV_BYTES`; a repair attempt appends the model's own rejection
#: text, whose length nobody here chooses, so the reserve is taken up front.
#: `prompt_argv_overflow` stays the last line of defence.
PROMPT_BUDGET_BYTES = MAX_PROMPT_ARGV_BYTES - 8_192

#: How much of one material may travel, in rungs of (description, AgPM angle).
#: The former context took the first sixty materials and stopped there: the
#: 30-day window on 2026-09-05 held 196, and 136 of them - the whole far
#: perimeter and everything older - the model never saw at all, while the
#: theses promised a month. The owner's decision of 2026-09-05: the whole
#: window travels, and it is each material that gets shorter.
#:
#: The angle runs out before the description and disappears on the narrow
#: rungs. The description carries the fact the material is in the window for;
#: the angle is interpretation, and producing it is the model's own job rather
#: than a retelling of somebody else's.
#:
#: The rungs are deliberately close together. With coarse ones the 30-day
#: window landed on 120 characters and left a third of the budget unspent;
#: measured 2026-09-05 against the production release, 196 materials.
TEXT_CAPS = (
    (700, 350),
    (500, 250),
    (360, 180),
    (260, 130),
    (200, 0),
    (160, 0),
    (120, 0),
    (80, 0),
    (0, 0),
)

_PERIMETER_ORDER = {"near": 0, "mid": 1, "far": 2}


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


def _validate(raw: JsonObject, allowed: frozenset[str] = frozenset()) -> list[JsonObject]:
    theses = raw.get("theses")
    if not isinstance(theses, list) or len(theses) != 4:
        raise PeriodAnalysisError("требуется ровно четыре тезиса")
    result: list[JsonObject] = []
    language_errors: list[str] = []
    for index, value in enumerate(theses):
        if not isinstance(value, dict):
            raise PeriodAnalysisError(f"тезис {index + 1} не является объектом")
        lead = str(value.get("lead") or "").strip()
        rest = str(value.get("rest") or "").strip()
        try:
            require_russian_prose(f"{lead} {rest}", f"theses[{index}]", allowed)
        except ValueError as exc:
            language_errors.append(str(exc))
        if len(lead) < 35 or len(rest) < 100:
            raise PeriodAnalysisError(f"тезис {index + 1} недостаточно содержателен")
        result.append({"lead": lead, "rest": rest})
    if language_errors:
        raise PeriodAnalysisError(" | ".join(language_errors))
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


def _shorten(value: object, cap: int) -> str:
    """One material's text under a ceiling, cut on a word boundary.

    A word cut in half reads to a model as a fact that is not there; the
    ellipsis says the text continues, and the prompt forbids completing it.
    """
    text = " ".join(str(value or "").split())
    if cap <= 0 or not text:
        return ""
    if len(text) <= cap:
        return text
    cut = text[:cap]
    space = cut.rfind(" ")
    if space > cap // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def _material_rows(documents: list[JsonObject]) -> list[dict[str, object]]:
    """Every material of the window, ordered by what the theses need first.

    Near perimeter before far, newer before older inside a perimeter, and the
    title breaks ties: the order has to be deterministic, because the prompt is
    retained beside the answer and is what a later check reads.
    """
    rows: list[dict[str, object]] = []
    for document in documents:
        for raw in cast(list[dict[str, object]], document["materials"]):
            rows.append(
                {
                    "issueDate": document["issueDate"],
                    "title": raw["title"],
                    "text": raw.get("llmShortText") or raw.get("summary") or raw.get("brief") or "",
                    "angle": raw.get("llmAgpmAngle") or raw.get("agpmTakeaway") or "",
                    "perimeter": raw["perimeter"],
                    "rubrics": raw["rubrics"],
                    "sourceName": raw["sourceName"],
                }
            )
    rows.sort(
        key=lambda item: (
            _PERIMETER_ORDER.get(str(item["perimeter"]), 3),
            -date.fromisoformat(str(item["issueDate"])).toordinal(),
            str(item["title"]),
        )
    )
    return rows


def _context(
    rows: list[dict[str, object]],
    period: str,
    anchor: str,
    *,
    issue_count: int,
    caps: tuple[int, int],
    total: int,
) -> JsonObject:
    """The window as the model will see it: every material, texts under `caps`."""
    text_cap, angle_cap = caps
    materials: list[dict[str, object]] = []
    for row in rows:
        material: dict[str, object] = {
            "issueDate": row["issueDate"],
            "title": row["title"],
            "perimeter": row["perimeter"],
            "rubrics": row["rubrics"],
        }
        summary = _shorten(row["text"], text_cap)
        angle = _shorten(row["angle"], angle_cap)
        # An empty field is bytes that say nothing and an invitation to decide
        # the material is empty. It is simply absent instead.
        if summary:
            material["summary"] = summary
        if angle:
            material["agpmAngle"] = angle
        materials.append(material)
    return cast(
        JsonObject,
        {
            "anchorDate": anchor,
            "angleCap": angle_cap,
            "issueCount": issue_count,
            "materialCount": total,
            "materials": materials,
            "omittedMaterialCount": total - len(materials),
            "period": period,
            "shownMaterialCount": len(materials),
            "textCap": text_cap,
        },
    )


def _fit_context(
    documents: list[JsonObject],
    period: str,
    anchor: str,
    previous: list[JsonObject] | None,
    *,
    repair: str = "",
) -> JsonObject:
    """The most detailed context that still fits one command-line argument.

    Texts shrink first, down to titles alone. Only if the titles of the whole
    window do not fit either - which takes hundreds of materials - is the
    window cut from the end of the priority order, and the number dropped goes
    into the metadata: a prompt that silently showed a part would misstate what
    the theses rest on.
    """
    rows = _material_rows(documents)
    total = len(rows)
    issue_count = len(documents)

    def fits(context: JsonObject) -> bool:
        return (
            len((_prompt(context, period, previous) + repair).encode("utf-8"))
            <= PROMPT_BUDGET_BYTES
        )

    context = _context(rows, period, anchor, issue_count=issue_count, caps=(0, 0), total=total)
    for caps in TEXT_CAPS:
        candidate = _context(rows, period, anchor, issue_count=issue_count, caps=caps, total=total)
        if fits(candidate):
            return candidate
        context = candidate
    shown = total
    while shown > 1:
        shown = max(1, shown * 3 // 4)
        context = _context(
            rows[:shown], period, anchor, issue_count=issue_count, caps=(0, 0), total=total
        )
        if fits(context):
            return context
    return context


def _prompt(context: JsonObject, period: str, previous: list[JsonObject] | None) -> str:
    task = (
        "Найди оперативные изменения: новые сигналы, ускорение или ослабление тем, инциденты и "
        "изменения относительно предшествующей повестки. Не выдавай устойчивый фон за новость."
        if period == "7d"
        else "Найди устойчивые паттерны и структурные сдвиги: повторяемость сигналов, зрелость практик, "
        "накопленные риски и последствия для операционной модели AgPM, PMO и ИСУП."
    )
    shown = int(cast(int, context["shownMaterialCount"]))
    total = int(cast(int, context["materialCount"]))
    cap = int(cast(int, context["textCap"]))
    # The prompt says what it carries. A model that was not told a text is cut
    # completes it, and writes the completion down as a fact.
    if cap:
        angle_cap = int(cast(int, context["angleCap"]))
        carried = (
            f"Материалов в окне {total}, все они ниже. Описания обрезаны до {cap} знаков"
            + (f", выводы — до {angle_cap}" if angle_cap else ", выводов для AgPM нет")
            + "; многоточие в конце значит, что текст продолжается. "
            "Не достраивай обрезанное и не выдавай обрывок за законченную мысль.\n"
        )
    else:
        carried = (
            f"Материалов в окне {total}, все они ниже — заголовками, без текстов: окно "
            "слишком велико, чтобы тексты поместились. Опирайся на состав и повторяемость "
            "тем, а не на содержание отдельного материала.\n"
        )
    if shown != total:
        carried += (
            f"Показано {shown} из {total}: остальные не поместились. Не утверждай ничего "
            "о материалах, которых здесь нет.\n"
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
        f"{RUSSIAN_PROSE_PROMPT}"
        "Переводи и технические термины: egress — исходящие соединения, "
        "agent-first — ориентированный на работу агентов. Перед ответом проверь язык всех четырёх тезисов.\n"
        f"{task}\n"
        f"{carried}"
        "Опирайся только на входные данные. Не перечисляй новости по одной, не добавляй внешние факты и "
        "не используй универсальные заготовки. Каждый rest должен показывать опору в корпусе окна и значение "
        "для агентного управления проектами.\n"
        'Верни только JSON: {"theses":[{"lead":"...","rest":"..."}]}.\n'
        f"{distinction}\nДанные окна: {json.dumps(context, ensure_ascii=False)}"
    )


def period_fallback_theses(period: str) -> list[JsonObject]:
    """Reader notice for every exhausted period run; diagnostics stay in metadata."""
    label = "7 дней" if period == "7d" else "30 дней"
    return [
        {
            "lead": f"Обзор за {label} пока недоступен.",
            "rest": (
                "Не удалось подготовить общий текст. Материалы за выбранный период доступны "
                "в ленте: их можно читать, отбирать по темам и открывать первоисточники."
            ),
        }
    ]


def _fallback(period: str, context: JsonObject, error: str, *, attempts: int) -> JsonObject:
    count = int(cast(int, context["materialCount"]))
    return cast(
        JsonObject,
        {
            "attempts": attempts,
            "error": error,
            "issueCount": context["issueCount"],
            "materialCount": count,
            "model": "rules-period-v2",
            "period": period,
            "promptVersion": PROMPT_VERSION,
            "provider": "fallback",
            "shownMaterialCount": context["shownMaterialCount"],
            "status": "fallback",
            "textCap": context["textCap"],
            "angleCap": context["angleCap"],
            "theses": period_fallback_theses(period),
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
    context = _fit_context(documents, period, anchor, previous)
    root = artifacts_root / period
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    failures: list[str] = []
    attempted_models: list[str] = []
    rejected: JsonObject | None = None
    #: How many times the model was asked. Not the attempt number: an attempt
    #: burnt on the argv ceiling never reaches the model, and "1 attempt" in the
    #: owner's report would name a paid call that was never made.
    calls = 0
    for attempt, model in enumerate(ATTEMPT_MODELS, 1):
        repair = (
            "\nИсправь ответ с учётом всех замечаний предыдущих попыток: "
            + " | ".join(failures)
            + ". Верни полный JSON с четырьмя тезисами.\n"
            if failures
            else ""
        )
        if rejected is not None:
            repair += (
                "Ниже отклонённый черновик, а не источник фактов. Исправь нарушения, "
                "сохраняя подтверждённое данными содержание и корректные формулировки. "
                "Не добавляй новые иностранные термины.\n"
                + json.dumps(rejected, ensure_ascii=False)
            )
        if repair:
            context = _fit_context(documents, period, anchor, previous, repair=repair)
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
            calls += 1
            attempted_models.append(model)
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
            except subprocess.TimeoutExpired as error:
                # str(TimeoutExpired) contains the entire argv, including the
                # corpus. Keep it out of notifications and bounded metadata.
                atomic_write_new(
                    root / f"response-attempt-{attempt}.json",
                    canonical_json_line({"timedOutAfterSeconds": TIMEOUT_SECONDS}),
                    mode=0o600,
                )
                raise PeriodAnalysisError(
                    f"OpenClaw превысил время ожидания {TIMEOUT_SECONDS} с"
                ) from error
            except OSError as error:
                # The process could not be started: a missing binary, an argument
                # the kernel refused. One failed attempt, like a hung model.
                # Narrow, around this one call: in the broad `except` this also
                # caught the artifact write, so a full disk after a good answer
                # threw the finished theses away and bought two more.
                atomic_write_new(
                    root / f"response-attempt-{attempt}.json",
                    canonical_json_line({"startError": str(error)}),
                    mode=0o600,
                )
                raise PeriodAnalysisError(f"OpenClaw не удалось запустить: {error}") from error
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
            rejected = _model_payload(completed.stdout)
            theses = _validate(rejected, brand_words(json.dumps(context)))
            if previous:
                left = " ".join(f"{x['lead']} {x['rest']}" for x in previous)
                right = " ".join(f"{x['lead']} {x['rest']}" for x in theses)
                if _similarity(left, right) >= 0.72:
                    raise PeriodAnalysisError("окна 7 и 30 дней слишком похожи")
            result = cast(
                JsonObject,
                {
                    "attempts": calls,
                    "attemptModels": cast(list[object], attempted_models),
                    "error": None,
                    "evidenceTitles": [
                        str(item["title"]) for item in cast(list[JsonObject], context["materials"])
                    ],
                    "issueCount": len(documents),
                    "materialCount": context["materialCount"],
                    "model": model,
                    "period": period,
                    "promptVersion": PROMPT_VERSION,
                    "provider": model.split("/", 1)[0],
                    "angleCap": context["angleCap"],
                    "shownMaterialCount": context["shownMaterialCount"],
                    "status": "success",
                    "textCap": context["textCap"],
                    "theses": cast(list[object], theses),
                },
            )
            atomic_write_new(root / "result.json", canonical_json_line(result), mode=0o600)
            return result
        except (json.JSONDecodeError, PeriodAnalysisError) as error:
            diagnostic = f"{model}: {error}"
            atomic_write_new(
                root / f"rejection-attempt-{attempt}.json",
                canonical_json_line({"attempt": attempt, "model": model, "error": diagnostic}),
                mode=0o600,
            )
            # Six errors must fit the 10 KB metadata block contract. Full
            # diagnostics remain in the private per-attempt artifacts above.
            failures.append(diagnostic[:600])
    result = _fallback(period, context, " | ".join(failures), attempts=calls)
    result["attemptModels"] = cast(list[JsonValue], attempted_models)
    atomic_write_new(root / "result.json", canonical_json_line(result), mode=0o600)
    return result


def period_blocks(results: Mapping[str, JsonObject]) -> list[JsonObject]:
    blocks: list[JsonObject] = []
    for period in PERIODS:
        result = results[period]
        # Also protects reused/older results containing diagnostics in their prose.
        # Never render arbitrary fallback text supplied by a failed model or importer.
        theses = (
            period_fallback_theses(period)
            if result.get("status") == "fallback"
            else cast(list[JsonObject], result["theses"])
        )
        for index, thesis in enumerate(theses, 1):
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
