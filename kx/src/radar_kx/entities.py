"""Who and what a statement names, so UC-05 has something to draw.

`entities` has been in the schema since migration 001 and empty ever since, and
the feasibility measurement says plainly what that costs: "Организаций, авторов,
платформ, стандартов и отраслей в базе не существует", and of UC-05's six modes
only two are reachable - the evidential one and the one by source.

The types below are derived from what the repository actually records: the
feasibility document names five (organisation, person, platform, standard,
industry), UC-05's remaining modes name three more (role, risk, control) and the
engineering plan's P23 names practice alongside products and roles. The owner's
own use-case document says thirteen; four of those are not written down anywhere
here, so this list is nine and says so rather than inventing four.

One model call reads a batch of statements and names what each one mentions. The
model does not decide what an entity *is* - the type list is closed, a value
outside it is dropped and counted, and a name the model returns empty is not an
entity.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The closed list. A type outside it is a dropped row, never a new type: the
#: whole point of a graph mode is that "organisation" means one thing.
ENTITY_TYPES = (
    "organisation",
    "person",
    "platform",
    "standard",
    "industry",
    "role",
    "risk",
    "control",
    "practice",
)

#: How a name stands in the sentence. A statement *about* Gartner and one that
#: cites Gartner are different facts and the graph would draw them the same way.
ROLES = ("subject", "mentioned")

#: Statements per call. Smaller than the reading pass's ten: naming entities
#: produces several rows per statement, and a long answer is a truncated answer.
BATCH = 8

#: Entities per statement. A sentence naming more than six things is a list, and
#: a list is what the reading pass admits as `rejected`.
MAX_ENTITIES = 6

#: Longest name kept. Anything longer is a phrase the model wrote instead of a
#: name, and a phrase cannot be merged with its next occurrence.
NAME_CHARS = 120

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

#: Names that are not names. The reading pass learned the same lesson about
#: `primary_source`: a model asked for an organisation will happily answer
#: "аналитики" unless told that is not one.
EMPTY_NAMES = frozenset(
    {
        "",
        "-",
        "—",
        "н/д",
        "нет",
        "none",
        "null",
        "n/a",
        "аналитики",
        "эксперты",
        "исследование",
        "компания",
        "организация",
        "组织",
    }
)


class EntityError(ValueError):
    """The model's answer cannot be used."""


@dataclass(frozen=True, slots=True)
class Mention:
    """One entity named by one statement."""

    claim_id: str
    entity_type: str
    canonical_name: str
    surface_form: str
    role: str


INSTRUCTIONS = "\n".join(
    [
        "Ты извлекаешь из утверждений базы знаний по агентному управлению проектами",
        "названные в них сущности. Отвечаешь только тем, что в тексте действительно",
        "названо. Ничего не додумываешь и не обобщаешь.",
        "",
        "ТИПЫ (type), ровно одно значение из девяти:",
        "  organisation — компания, вендор, аналитический дом, регулятор, институт",
        '                 ("Gartner", "OpenAI", "Еврокомиссия");',
        '  person       — человек: автор, исследователь, руководитель ("Andrew Ng");',
        '  platform     — продукт, платформа, инструмент ("Copilot", "Jira", "LangChain");',
        '  standard     — стандарт, регламент, закон, методология ("ISO/IEC 42001",',
        '                 "EU AI Act", "PMBOK");',
        '  industry     — отрасль ("финансы", "здравоохранение", "госсектор");',
        '  role         — роль в организации или проекте ("PMO", "руководитель проекта",',
        '                 "продуктовый аналитик", "агент-оркестратор");',
        '  risk         — названный риск ("галлюцинации модели", "утечка данных");',
        '  control      — мера, контроль, ограничение ("порог автономии",',
        '                 "человек в контуре", "аудит решений");',
        '  practice     — практика или метод ("парное ревью", "ретроспектива",',
        '                 "делегированная автономия").',
        "",
        "РОЛЬ В ПРЕДЛОЖЕНИИ (role):",
        "  subject   — утверждение именно об этой сущности;",
        "  mentioned — она упомянута, но утверждение о другом.",
        "",
        "ИМЯ (name): каноническое, как принято писать, в именительном падеже.",
        '  "Гартнер" и "Gartner" — одно и то же: пиши "Gartner".',
        "  form: те слова, которыми сущность названа в самом тексте.",
        "",
        "ПРАВИЛА:",
        "1. Пустой список — нормальный ответ. Многие утверждения не называют ничего.",
        f"2. Не больше {MAX_ENTITIES} сущностей на утверждение.",
        '3. Не выдумывай общих слов: "аналитики", "эксперты", "компания" — не имена.',
        "4. Роль (role) — это должность или функция, а не конкретный человек.",
        "5. Отвечай только массивом JSON, по объекту на утверждение, в том же порядке:",
        '   [{"item":1,"entities":[{"type":"organisation","name":"Gartner",',
        '     "form":"Гартнер","role":"mentioned"}]}]',
        "6. Никакого текста до или после массива.",
    ]
)


def build_payload(claims: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for index, claim in enumerate(claims, start=1):
        text = " ".join(str(claim["quote_text"]).split())[:400]
        lines.append(f"{index}. {text}")
    return "\n\n".join(lines)


def _clean(value: Any, *, limit: int = NAME_CHARS) -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, str | int | float):
        return ""
    return " ".join(str(value).split())[:limit]


def parse_mentions(
    answer: str, claims: Sequence[Mapping[str, Any]]
) -> tuple[tuple[Mention, ...], dict[str, int]]:
    """Read the answer back, keeping only what the closed lists allow.

    Everything discarded is counted, because a model that invents half its types
    and one that answers cleanly must not produce the same-looking result.
    """
    text = _FENCE.sub("", answer).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise EntityError(f"answer is not JSON: {error}") from error
    if not isinstance(parsed, list):
        raise EntityError("answer is not a list")

    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    found: list[Mention] = []
    for row in parsed:
        if not isinstance(row, dict):
            drop("notAnObject")
            continue
        try:
            ordinal = int(row.get("item", 0))
        except (TypeError, ValueError):
            drop("badOrdinal")
            continue
        if not 1 <= ordinal <= len(claims):
            drop("unknownItem")
            continue
        claim_id = str(claims[ordinal - 1]["claim_id"])
        named = row.get("entities")
        if named is None:
            continue
        if not isinstance(named, list):
            drop("entitiesNotAList")
            continue
        seen: set[tuple[str, str]] = set()
        for item in named[:MAX_ENTITIES]:
            if not isinstance(item, dict):
                drop("notAnObject")
                continue
            entity_type = _clean(item.get("type"), limit=40).lower()
            if entity_type not in ENTITY_TYPES:
                drop("unknownType")
                continue
            name = _clean(item.get("name"))
            if name.lower() in EMPTY_NAMES:
                drop("emptyName")
                continue
            role = _clean(item.get("role"), limit=20).lower()
            if role not in ROLES:
                role = "mentioned"
            key = (entity_type, name.casefold())
            if key in seen:
                drop("duplicate")
                continue
            seen.add(key)
            found.append(
                Mention(
                    claim_id=claim_id,
                    entity_type=entity_type,
                    canonical_name=name,
                    surface_form=_clean(item.get("form")) or name,
                    role=role,
                )
            )
    return tuple(found), dropped


def summarize(mentions: Sequence[Mention], dropped: Mapping[str, int]) -> dict[str, Any]:
    """What a pass found, counted the way the owner will want to check it."""
    from collections import Counter

    types: Counter[str] = Counter()
    names: Counter[str] = Counter()
    for mention in mentions:
        types[mention.entity_type] += 1
        names[f"{mention.entity_type}/{mention.canonical_name}"] += 1
    return {
        "mentions": len(mentions),
        "distinctEntities": len(names),
        "byType": dict(types.most_common()),
        "mostNamed": dict(names.most_common(10)),
        "dropped": dict(dropped),
    }
