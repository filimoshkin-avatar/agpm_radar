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

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
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
