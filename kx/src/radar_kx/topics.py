"""Putting a subject on a statement and on a document (slice 2.5в, second half).

The backbone is loaded; nothing yet says which part of it a given wiki statement
or a given stored document is about, and without that a binding is still free to
match a sentence about autonomy thresholds to a quotation about procurement
because both used the word "process".

**Why a model and not the embedder.** The comparison this exists for asks whether
restricting evidence to one subject helps the lexical method or the semantic one
more. Assigning the subjects with the embedder would let the semantic method draw
its own partition and then be measured inside it, and the answer would be a
property of the instrument rather than of the corpus. A model reads the words,
which is what a person does, and neither method under test gets to define the
boundary it is scored against.

What crosses the boundary is the rubricator - Radar's own writing - plus one
title and a lede-length snippet per document, or one wiki statement per statement.
The model answers with keys from the list it was given; anything else it says is
dropped here, counted, and never becomes a row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: How many topics one item may carry. A document is usually about one or two
#: things; letting the model tag six would make the restriction meaningless,
#: because every document would sit in every subject.
MAX_TOPICS_PER_ITEM = 3

#: Characters of a document's own text sent alongside its title. A lede is what
#: says what an article is about, and it is no longer than a published quotation.
LEDE_CHARS = 300

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class TopicAssignmentError(ValueError):
    """The model's answer cannot be used."""


@dataclass(frozen=True, slots=True)
class AssignableItem:
    """One thing to be given a subject."""

    key: str
    text: str


@dataclass(frozen=True, slots=True)
class Assignment:
    key: str
    topic_keys: tuple[str, ...]


def build_rubricator(topics: Sequence[Mapping[str, Any]]) -> str:
    """The instruction block: the backbone itself, and the rules for using it."""
    lines = [
        "Ты размечаешь материалы базы знаний по агентному управлению.",
        "Рубрикатор задан автором и закрыт: выбирать можно только из него.",
        "",
        "Темы (ключ — тема — раздел, к которому она относится):",
    ]
    for topic in topics:
        path = str(topic.get("path") or topic["title"])
        parent = path.split(" / ")[0]
        lines.append(f"- {topic['topic_key']} — {topic['title']} — {parent}")
    lines += [
        "",
        "Правила:",
        f"1. Каждому пронумерованному элементу поставь от 0 до {MAX_TOPICS_PER_ITEM}"
        " ключей, описывающих его предмет.",
        "2. Ключ обязан быть из списка выше. Ничего не придумывай и не переводи.",
        "3. Если предмет элемента рубрикатором не покрыт — верни пустой список."
        " Пустой список — нормальный ответ, а не ошибка.",
        "4. Отвечай только массивом JSON, по одному объекту на элемент, в порядке"
        ' элементов: [{"item": 1, "topics": ["ключ", "ключ"]}]',
        "5. Никакого текста до или после массива.",
    ]
    return "\n".join(lines)


def build_payload(items: Sequence[AssignableItem]) -> str:
    return "\n".join(f"{index}. {item.text}" for index, item in enumerate(items, start=1))


def parse_assignment(
    answer: str, items: Sequence[AssignableItem], allowed: frozenset[str]
) -> tuple[tuple[Assignment, ...], dict[str, int]]:
    """Read the answer back, keeping only what the rubricator actually contains.

    Returns the usable assignments and a count of what was thrown away, because a
    model that invents half its keys and a model that answers cleanly must not
    produce the same-looking result.
    """
    text = _FENCE.sub("", answer).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise TopicAssignmentError("the answer contains no JSON array")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise TopicAssignmentError(f"the answer is not JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise TopicAssignmentError("the answer is not a list")

    dropped = {"unknownTopic": 0, "unknownItem": 0, "overCap": 0}
    found: list[Assignment] = []
    for row in parsed:
        if not isinstance(row, dict):
            dropped["unknownItem"] += 1
            continue
        try:
            ordinal = int(row.get("item", 0))
        except (TypeError, ValueError):
            ordinal = 0
        if not 1 <= ordinal <= len(items):
            dropped["unknownItem"] += 1
            continue
        raw = row.get("topics")
        keys: list[str] = []
        for value in raw if isinstance(raw, list) else []:
            key = str(value).strip()
            if key not in allowed:
                dropped["unknownTopic"] += 1
                continue
            if key not in keys:
                keys.append(key)
        if len(keys) > MAX_TOPICS_PER_ITEM:
            dropped["overCap"] += len(keys) - MAX_TOPICS_PER_ITEM
            keys = keys[:MAX_TOPICS_PER_ITEM]
        found.append(Assignment(key=items[ordinal - 1].key, topic_keys=tuple(keys)))
    return tuple(found), dropped


def document_item(*, document_id: str, title: str, lede: str) -> AssignableItem:
    """A document as the model sees it: its title, and enough text to place it."""
    head = " ".join(lede.split())[:LEDE_CHARS]
    label = " ".join(title.split()) or "(без заголовка)"
    return AssignableItem(key=document_id, text=f"{label} — {head}" if head else label)
