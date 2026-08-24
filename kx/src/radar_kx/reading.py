"""Reading a statement: what kind it is, whose it is, and whether it belongs (stage 0b).

Four of the owner's decisions are answered by one pass over a claim, so they are
one prompt rather than four:

* **decision 7 - what kind of material it is.** A fact and a forecast are not the
  same evidence, and the reader has to see which one is under a conclusion.
* **decision 1 - whose claim it originally was, and whether this is a retelling.**
  Four outlets repeating one Gartner forecast are four hosts and one observation.
  The independence gate could not see the difference; the reader now can.
* **decision 3 - where it belongs.** A statement about a class enters the base, a
  product launch goes to the observatory as market chronicle, a vendor's list of
  connectors is dropped. What is dropped is written down with a reason.
* **decision 11 - how long it stays current**, computed here from the rule for
  its kind rather than asked of the model: an expiry is arithmetic on a date, and
  a model guessing at it would be a worse answer that looks the same.

The subject comes along in the same call (`claim_topics`) because the model is
already reading the sentence, and a statement the backbone has no place for
becomes a row in the gap map rather than a silence (decision 8).

**What crosses the boundary.** One line per claim - the statement as the store
normalised it, its quotation, and the name of the corpus it came from - plus the
rubricator, which is the owner's own writing. Never the document. Ten claims per
call: enough that the model sees a batch rather than a stream, small enough that
one unusable answer costs ten rows and not two hundred.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

#: Her six kinds of material (§5, decision 7).
MATERIAL_KINDS = ("fact", "opinion", "case", "forecast", "product_release", "incident")

#: Where a statement is admitted (decision 3).
ADMISSIONS = ("knowledge", "observatory", "rejected")

#: How many claims one call reads. Ten, because each answer carries six fields and
#: a subject list, and a batch of twenty-five - the size the topic pass uses -
#: produces an answer long enough that the tail of it starts drifting.
BATCH = 10

#: How many subjects one statement may carry. The same cap as the document pass:
#: a statement about everything is a statement placed nowhere.
MAX_TOPICS = 3

#: What the gap map records when a statement was placed nowhere and the reader
#: did not say what was missing. A silence and a named gap are different answers,
#: and the queue has to be able to tell them apart.
NO_SUBJECT_NAMED = "предмет не назван: тема не выбрана и причина не указана"

#: The other way a statement ends up with no subject: the model named subjects
#: that are not in the backbone. The keys it asked for go into the line, because
#: that is the useful half - a gap row saying "not named" would be false, and the
#: names are a candidate subject list rather than noise.
NOT_IN_THE_BACKBONE = "названных тем нет в скелете: {keys}"


def not_in_the_backbone(keys: Sequence[str]) -> str:
    """The gap line for a reading whose every named subject was invented."""
    if not keys:
        return NO_SUBJECT_NAMED
    return NOT_IN_THE_BACKBONE.format(keys=", ".join(keys))


#: Longest quotation sent with a statement. A widened quotation is a sentence or
#: two; beyond that the model is reading the article, which is what the egress
#: rule forbids.
QUOTE_CHARS = 400

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class ReadingError(ValueError):
    """The model's answer cannot be used."""


@dataclass(frozen=True, slots=True)
class ReadableClaim:
    """One statement as the reader sees it."""

    claim_id: str
    statement: str
    quote: str
    #: `issue perimeter` or `canon`. Which corpus it came out of changes what a
    #: sensible admission is, and the model is told rather than left to guess.
    corpus: str
    #: The day this statement is dated to, for the expiry arithmetic: the day the
    #: document was published, or - when the source said nothing - the day the
    #: radar first saw it. `document_dates.shown_on` is NOT NULL, so a statement
    #: whose document has been dated always has one of the two. `None` means no
    #: date row at all, and then nothing is measured (see `valid_until`).
    dated_on: datetime | None


@dataclass(frozen=True, slots=True)
class Reading:
    """What one pass determined about one statement."""

    claim_id: str
    material_kind: str
    primary_source: str
    is_retelling: bool
    admission: str
    admission_note: str | None
    topic_keys: tuple[str, ...]
    missing: str | None
    confidence: float | None


def build_instructions(topics: Sequence[Mapping[str, Any]]) -> str:
    """The instruction block: the owner's rules, and her backbone to place things on."""
    lines = [
        "Ты читаешь утверждения базы знаний по агентному управлению проектами.",
        "На каждое утверждение отвечаешь пятью решениями. Правила заданы автором базы.",
        "",
        "1. ВИД МАТЕРИАЛА (kind), ровно одно значение:",
        "   fact — проверяемое положение дел;",
        "   opinion — суждение, оценка, позиция автора;",
        "   case — описание конкретного внедрения или опыта организации;",
        "   forecast — утверждение о будущем, прогноз, ожидание;",
        "   product_release — запуск, релиз, обновление продукта или платформы;",
        "   incident — сбой, инцидент, авария, утечка, отзыв.",
        "",
        "2. ПЕРВОИСТОЧНИК (source) и ПЕРЕСКАЗ (retelling).",
        "   Если утверждение пересказывает чужое исследование, прогноз или заявление —",
        "   retelling = true, а source = ИМЯ того, кто сказал это первым:",
        '   организация, издание, автор или стандарт ("Gartner", "McKinsey",',
        '   "OpenAI", "ISO/IEC 42001", "Abada and Lambin"). Не пиши общих слов',
        '   вроде "аналитики", "исследование", "эксперты" — если имени в тексте нет,',
        "   ставь retelling = false и пустой source.",
        "   Если издание говорит от себя — retelling = false, source = пустая строка.",
        "",
        "3. ДОПУСК (admission), ровно одно значение:",
        "   knowledge — утверждение о КЛАССЕ явлений: принцип, закономерность, метод,",
        "     риск, практика, измерение. Идёт в основную базу;",
        "   observatory — событие рынка: запуск продукта, сделка, релиз, назначение,",
        "     инцидент у конкретной компании. Идёт в хронику, не в знание;",
        "   rejected — перечень функций или коннекторов вендора, рекламный текст,",
        "     навигационный обрывок, бессодержательный фрагмент.",
        "   Для rejected обязательно короткое note — почему.",
        "",
        "4. ТЕМЫ (topics): от 0 до 3 ключей из рубрикатора ниже.",
        "   Ключ обязан быть из списка. Ничего не придумывай и не переводи.",
        "   Если предмета утверждения в рубрикаторе нет — верни пустой список и напиши",
        "   в missing одной строкой, какой темы не хватило.",
        "",
        "5. УВЕРЕННОСТЬ (confidence): число от 0 до 1.",
        "",
        "Рубрикатор (ключ — тема — раздел):",
    ]
    for topic in topics:
        path = str(topic.get("path") or topic["title"])
        parent = path.split(" / ")[0]
        lines.append(f"- {topic['topic_key']} — {topic['title']} — {parent}")
    lines += [
        "",
        "Формат ответа: только массив JSON, по объекту на каждый пронумерованный",
        "элемент, в том же порядке. Никакого текста до или после массива.",
        '[{"item":1,"kind":"fact","source":"","retelling":false,"admission":"knowledge",'
        '"note":null,"topics":["ключ"],"missing":null,"confidence":0.8}]',
    ]
    return "\n".join(lines)


def build_payload(claims: Sequence[ReadableClaim]) -> str:
    """One numbered line per statement: what it says, and what it stands on."""
    lines = []
    for index, claim in enumerate(claims, start=1):
        statement = " ".join(claim.statement.split())
        quote = " ".join(claim.quote.split())[:QUOTE_CHARS]
        lines.append(f"{index}. [{claim.corpus}] {statement}\n   цитата: {quote}")
    return "\n".join(lines)


def _clean(value: Any, *, limit: int = 200) -> str:
    """A model's field as a line of text, or nothing.

    Only strings and numbers become text. A JSON `false` for "who originally said
    this" used to pass through `str()` and be stored as the primary source
    "False" - a value that reads as a name, satisfies the retelling constraint,
    and is not one. Anything that is not a scalar is the model failing to answer.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, str | int | float):
        return ""
    return " ".join(str(value).split())[:limit]


def parse_readings(
    answer: str, claims: Sequence[ReadableClaim], allowed_topics: frozenset[str]
) -> tuple[tuple[Reading, ...], dict[str, int]]:
    """Read the answer back, keeping only what the rules actually allow.

    Everything thrown away is counted. A model that invents half its values and a
    model that answers cleanly must not produce the same-looking result.
    """
    text = _FENCE.sub("", answer).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ReadingError("the answer contains no JSON array")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReadingError(f"the answer is not JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ReadingError("the answer is not a list")

    dropped = {"unknownItem": 0, "unknownKind": 0, "unknownAdmission": 0, "unknownTopic": 0}
    readings: list[Reading] = []
    seen: set[int] = set()
    for row in parsed:
        if not isinstance(row, dict):
            dropped["unknownItem"] += 1
            continue
        try:
            ordinal = int(row.get("item", 0))
        except (TypeError, ValueError):
            ordinal = 0
        if not 1 <= ordinal <= len(claims) or ordinal in seen:
            dropped["unknownItem"] += 1
            continue
        seen.add(ordinal)

        kind = _clean(row.get("kind"), limit=40).lower()
        if kind not in MATERIAL_KINDS:
            dropped["unknownKind"] += 1
            continue
        admission = _clean(row.get("admission"), limit=40).lower()
        if admission not in ADMISSIONS:
            dropped["unknownAdmission"] += 1
            continue

        keys: list[str] = []
        raw_topics = row.get("topics")
        for value in raw_topics if isinstance(raw_topics, list) else []:
            key = _clean(value, limit=80)
            if key not in allowed_topics:
                dropped["unknownTopic"] += 1
                continue
            if key not in keys:
                keys.append(key)
        keys = keys[:MAX_TOPICS]

        # A retelling that cannot say what it retells records nothing useful, and
        # the table's own constraint refuses it. Downgrading here rather than
        # dropping the row keeps the other four answers.
        source = _clean(row.get("source"))
        retelling = bool(row.get("retelling")) and bool(source)

        note = _clean(row.get("note"), limit=400) or None
        readings.append(
            Reading(
                claim_id=claims[ordinal - 1].claim_id,
                material_kind=kind,
                primary_source=source,
                is_retelling=retelling,
                admission=admission,
                admission_note=note,
                topic_keys=tuple(keys),
                missing=(_clean(row.get("missing"), limit=400) or None) if not keys else None,
                confidence=_confidence(row.get("confidence")),
            )
        )
    dropped["unknownItem"] += len(claims) - len(seen)
    return tuple(readings), dropped


def _confidence(value: Any) -> float | None:
    if not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(min(1.0, max(0.0, number)), 3)


def valid_until(
    kind: str, dated_on: datetime | None, freshness: Mapping[str, timedelta | None]
) -> datetime | None:
    """When this statement joins the review queue, by the rule for its kind.

    Arithmetic, not judgement: `material_kind_freshness` holds the interval the
    owner set for each kind, and expiry only queues a review - it changes nothing
    on its own (decision 11). A kind with no interval never expires.

    Measured from the *document*, never from the clock. Falling back to "now" put
    the anchor on the day the reading pass happened to run: 6 625 of 13 876
    statements on production - every one whose document carried no published date
    - were given a `valid_until` exactly one interval after the pass, so their
    freshness clocks started months late and would have expired together on its
    anniversary. A statement nobody can date cannot have its freshness measured,
    and saying so with `None` is the honest answer; measuring from today is a
    number that looks like knowledge and is a record of when the job ran.
    """
    interval = freshness.get(kind)
    if interval is None or dated_on is None:
        return None
    return dated_on + interval


def summarize(readings: Sequence[Reading], dropped: Mapping[str, int]) -> dict[str, Any]:
    """What a pass read, counted the way the owner will want to check it."""
    from collections import Counter

    kinds: Counter[str] = Counter()
    admissions: Counter[str] = Counter()
    retold = 0
    without_topic = 0
    for reading in readings:
        kinds[reading.material_kind] += 1
        admissions[reading.admission] += 1
        retold += 1 if reading.is_retelling else 0
        without_topic += 0 if reading.topic_keys else 1
    return {
        "read": len(readings),
        "byMaterialKind": dict(kinds.most_common()),
        "byAdmission": dict(admissions.most_common()),
        "retellings": retold,
        "withoutASubject": without_topic,
        "dropped": {key: value for key, value in dropped.items() if value},
    }
