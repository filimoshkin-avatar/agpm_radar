"""What the knowledge base is organised around, and who decided that (slice 2.5в).

The owner asked what backbone the base is built on. The honest answer was: none.
Statements were matched to claims by shared words, with nothing requiring a match
to be about the same subject, and that is the root of the connection quality they
noticed.

A backbone does exist - twice, in prose, in two wiki pages that do not quite
agree, and neither is in the store:

``wiki/overview/ontological-structure.md``
    four categories at level 1, and a statement that levels 2 and 3 exist without
    enumerating them.
``wiki/overview/agpm-overview.md``
    seven layers of the model. The wiki's own directory layout half-matches it,
    and the five directories with no pages at all are layers of exactly this list.

This module reads both out of the stored wiki snapshot rather than off a
filesystem, so what the owner is shown is what the base actually holds, and
proposes them as candidates. It does not choose. A topic is an editorial fact
about what the field is made of; inferring it from a corpus is how a knowledge
base ends up organised around whatever happened to be written about most.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCES = ("agpm_ontology", "agpm_model_layers", "wiki_sections", "authored")

#: The pages the two skeletons live on.
ONTOLOGY_PAGE = "wiki/overview/ontological-structure.md"
MODEL_PAGE = "wiki/overview/agpm-overview.md"

#: The heading under which each page keeps its list.
ONTOLOGY_HEADING = "four categories"
MODEL_HEADING = "базовая структура модели"

_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_BOLD_LEAD = re.compile(r"^\*\*(.+?)\*\*[:.]?\s*(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class SkeletonElement:
    ordinal: int
    title: str
    description: str = ""

    @property
    def topic_key(self) -> str:
        latin = unicodedata.normalize("NFKD", self.title.casefold())
        slug = _SLUG.sub("-", latin).strip("-")
        return (slug or f"topic-{self.ordinal}")[:80]

    def as_json(self) -> dict[str, Any]:
        return {"ordinal": self.ordinal, "title": self.title, "description": self.description}


@dataclass(frozen=True, slots=True)
class SkeletonCandidate:
    source: str
    title: str
    origin: str
    note: str
    elements: tuple[SkeletonElement, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "origin": self.origin,
            "note": self.note,
            "elements": [element.as_json() for element in self.elements],
        }


def _section(body: str, heading_text: str) -> list[str]:
    """Lines under the heading whose text starts with ``heading_text``."""
    lines = body.splitlines()
    start = None
    level = 0
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match is None:
            continue
        if start is None and match.group(2).strip().casefold().startswith(heading_text):
            start, level = index + 1, len(match.group(1))
            continue
        if start is not None and len(match.group(1)) <= level:
            return lines[start:index]
    return lines[start:] if start is not None else []


def _elements(lines: Sequence[str]) -> tuple[SkeletonElement, ...]:
    found: list[SkeletonElement] = []
    for line in lines:
        match = _NUMBERED.match(line) or _BULLET.match(line)
        if match is None:
            continue
        text = _LINK.sub(r"\1", match.group(1)).strip()
        lead = _BOLD_LEAD.match(text)
        if lead:
            # "**Layer**, which does X" leaves the description starting with a
            # comma; it is a continuation of the title, not a sentence.
            found.append(
                SkeletonElement(
                    len(found) + 1,
                    lead.group(1).strip(),
                    lead.group(2).strip().lstrip(", ").strip(),
                )
            )
        else:
            # A list item with no bold lead is its own title, cut at the first
            # sentence so a topic name does not become a paragraph.
            head, _, rest = text.partition(". ")
            found.append(SkeletonElement(len(found) + 1, head.strip(" .:"), rest.strip()))
    return tuple(found)


def candidates(
    *, pages: dict[str, str], section_counts: dict[str, int]
) -> tuple[SkeletonCandidate, ...]:
    """The skeletons that exist today, read out of the stored wiki."""
    found: list[SkeletonCandidate] = []

    ontology = pages.get(ONTOLOGY_PAGE, "")
    if ontology:
        found.append(
            SkeletonCandidate(
                source="agpm_ontology",
                title="Онтология AgPM — четыре категории",
                origin=ONTOLOGY_PAGE,
                note=(
                    "Уровень 1 из трёхуровневой декомпозиции. Уровни 2 (подгруппы) и 3 "
                    "(конкретные элементы) страница объявляет, но не перечисляет — их "
                    "придётся дописать или вывести из существующих страниц."
                ),
                elements=_elements(_section(ontology, ONTOLOGY_HEADING)),
            )
        )

    model = pages.get(MODEL_PAGE, "")
    if model:
        found.append(
            SkeletonCandidate(
                source="agpm_model_layers",
                title="Базовая структура модели — семь слоёв",
                origin=MODEL_PAGE,
                note=(
                    "Более подробный список, и именно ему наполовину соответствует "
                    "структура каталогов wiki. Пять каталогов пусты, и это ровно слои "
                    "из этого списка."
                ),
                elements=_elements(_section(model, MODEL_HEADING)),
            )
        )

    if section_counts:
        found.append(
            SkeletonCandidate(
                source="wiki_sections",
                title="Фактическая структура каталогов wiki",
                origin="agpm/wiki/",
                note=(
                    "Не проект, а то, что есть. Пустые каталоги — это места, где модель "
                    "утверждает раздел, а страниц нет ни одной."
                ),
                elements=tuple(
                    SkeletonElement(
                        ordinal=index,
                        title=name,
                        description=(
                            f"{count} страниц" if count else "пусто — раздел объявлен, страниц нет"
                        ),
                    )
                    for index, (name, count) in enumerate(
                        sorted(section_counts.items(), key=lambda item: (-item[1], item[0])),
                        start=1,
                    )
                ),
            )
        )
    return tuple(found)


# ---------------------------------------------------------------------------
# The authored backbone (owner, 2026-08-23)
# ---------------------------------------------------------------------------
#
# The three candidates above were read out of the wiki and all three were
# rejected: none of them is the backbone, and the owner wrote their own. What
# arrives is a composition, not a corpus reading, so it arrives in a file that
# says who decided it - the same shape `load_family_batch` uses, and for the same
# reason. A topic nobody signed is a topic nobody can be asked about later.
#
# Only the subject dimension becomes topics. The authored document has four:
# subject ontology ("what is this knowledge about?"), authority status, genre and
# scope of applicability. Only the first answers the question a topic answers,
# and the other three are recorded in the file's `notLoaded` block rather than
# flattened into the same table - mixing the axes is exactly the defect the
# rubricator already has.


class SkeletonError(ValueError):
    """The authored skeleton cannot be read, or cannot be trusted."""


#: The key shape migration 019 enforces. Checked here too, so a bad file is
#: refused before a transaction is opened rather than half-way through one.
_TOPIC_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")

#: Levels 1-3 from `ontological-structure.md`: category, subgroup, element.
MAX_LEVEL = 3


@dataclass(frozen=True, slots=True)
class AuthoredTopic:
    key: str
    title: str
    level: int
    parent_key: str | None
    description: str = ""


@dataclass(frozen=True, slots=True)
class AuthoredSkeleton:
    skeleton_key: str
    title: str
    decided_by: str
    note: str
    #: Sections of the authored document that are deliberately not topics, with
    #: the reason each one is not. Carried so the exclusions stay reviewable.
    not_loaded: tuple[dict[str, str], ...]
    #: Flattened, parents always before their children, so one pass can insert.
    topics: tuple[AuthoredTopic, ...]

    def by_level(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for topic in self.topics:
            counts[topic.level] = counts.get(topic.level, 0) + 1
        return counts


def load_authored_skeleton(path: Path) -> AuthoredSkeleton:
    """Read the owner's composition. Anything ambiguous is refused, never guessed."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkeletonError(f"skeleton is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise SkeletonError("skeleton must be an object")
    decided_by = str(payload.get("decidedBy") or "").strip()
    if not decided_by:
        raise SkeletonError("skeleton must name who decided it")
    skeleton_key = str(payload.get("skeletonKey") or "").strip()
    if not skeleton_key:
        raise SkeletonError("skeleton must have a skeletonKey")
    raw = payload.get("topics")
    if not isinstance(raw, list) or not raw:
        raise SkeletonError("skeleton has no topics")

    flat: list[AuthoredTopic] = []
    seen: set[str] = set()
    _walk(raw, level=1, parent_key=None, into=flat, seen=seen)
    return AuthoredSkeleton(
        skeleton_key=skeleton_key,
        title=str(payload.get("title") or skeleton_key),
        decided_by=decided_by,
        note=str(payload.get("note") or ""),
        not_loaded=tuple(
            {str(key): str(value) for key, value in item.items()}
            for item in payload.get("notLoaded", [])
            if isinstance(item, dict)
        ),
        topics=tuple(flat),
    )


def _walk(
    items: Any,
    *,
    level: int,
    parent_key: str | None,
    into: list[AuthoredTopic],
    seen: set[str],
) -> None:
    if not isinstance(items, list):
        raise SkeletonError(f"children of {parent_key!r} must be a list")
    for item in items:
        if not isinstance(item, dict):
            raise SkeletonError(f"every topic under {parent_key!r} must be an object")
        key = str(item.get("key") or "").strip()
        if not _TOPIC_KEY.match(key):
            raise SkeletonError(f"{key!r} is not a usable topic key")
        if key in seen:
            raise SkeletonError(f"{key} appears twice in one skeleton")
        seen.add(key)
        title = str(item.get("title") or "").strip()
        if not 1 <= len(title) <= 200:
            raise SkeletonError(f"{key}: a topic needs a title of 1-200 characters")
        if level > MAX_LEVEL:
            raise SkeletonError(f"{key}: the skeleton is three levels deep, not {level}")
        into.append(
            AuthoredTopic(
                key=key,
                title=title,
                level=level,
                parent_key=parent_key,
                description=str(item.get("description") or "").strip(),
            )
        )
        children = item.get("children")
        if children:
            _walk(children, level=level + 1, parent_key=key, into=into, seen=seen)
