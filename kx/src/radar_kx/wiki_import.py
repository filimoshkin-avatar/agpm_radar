"""Reading the authored wiki into the store as concepts (P24, slice 2.5).

The pages are parsed out of a wiki snapshot that is already in KX, not off a
filesystem. That is deliberate: the snapshot is content-addressed and immutable
(slice 2.5a), so an import is reproducible from the store alone and a concept
version can name exactly which bytes it came from.

What is parsed, and what is refused:

* **Sections in the order the author wrote them**, each with an *optional*
  mapping onto one of the six ``SCHEMA.md`` conventions. Slice 1.5 measured three
  pages of sixty-three carrying all six, and 257 of 297 distinct second-level
  headings mapping to none. So an unmapped section is the ordinary case and is
  kept as it is; forcing the page into six would be rewriting the author's text
  (plan §16).
* **List items under claim-bearing sections** become atomic statements, marked
  ``list_item``. 34 of 63 authored pages carry their statements this way.
* **Nothing else.** The other 29 pages state their content in prose, and turning
  prose into statements is a model's job with a person confirming it - marked
  ``prose_model``, and this module does not do it. That is the point in the
  pipeline where "the machine rewrote what I wrote" would happen, so it is a
  separate step with a separate provenance value and a confirmation column.

Section conventions and the markdown reading come from :mod:`radar_kx.wiki_inventory`
so the import and the measurement of slice 1.5 cannot drift apart.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from radar_kx.identifiers import sha256_bytes
from radar_kx.language import language_of
from radar_kx.wiki_inventory import (
    AUTHORED_LAYERS,
    LIST_ITEM,
    fenced_lines,
    iter_headings,
    layer_for,
)

#: Which nature a statement gets from the section it was found under. SCHEMA.md
#: draws the division; the section is the only signal available without a model,
#: and guessing more finely from the sentence would be inventing provenance.
SECTION_NATURE = {
    "core_claims": "descriptive",
    "tensions": "descriptive",
    "implications": "implementation",
    "open_questions": "open_question",
}

#: A list item shorter than this is a label, a table of contents entry or a
#: fragment of a sentence, not a statement anything could be evidence for.
MIN_STATEMENT_CHARS = 25


class WikiImportError(ValueError):
    """A page cannot be read as a concept."""


@dataclass(frozen=True, slots=True)
class ParsedSection:
    ordinal: int
    heading: str
    heading_level: int
    convention: str | None
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    section_ordinal: int
    ordinal: int
    char_start: int
    char_end: int
    statement: str
    claim_nature: str

    @property
    def statement_sha256(self) -> str:
        return sha256_bytes(self.statement.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ParsedPage:
    relative_path: str
    layer: str
    title: str
    body: str
    sections: tuple[ParsedSection, ...]
    statements: tuple[ParsedStatement, ...]

    @property
    def body_sha256(self) -> str:
        return sha256_bytes(self.body.encode("utf-8"))

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    @property
    def language(self) -> str:
        return language_of(self.body)

    def concept_version_id(self, concept_id: str, snapshot_id: str) -> str:
        """Identity is the page, the wiki it came from, and its bytes."""
        return sha256_bytes(f"{concept_id}\n{snapshot_id}\n{self.body_sha256}".encode())

    def as_json(self) -> dict[str, Any]:
        return {
            "relativePath": self.relative_path,
            "layer": self.layer,
            "title": self.title,
            "wordCount": self.word_count,
            "language": self.language,
            "sections": len(self.sections),
            "mappedSections": sum(1 for item in self.sections if item.convention),
            "statements": len(self.statements),
        }


def _line_offsets(body: str) -> list[int]:
    """Character offset of the start of each line, one-indexed by line number."""
    offsets = [0, 0]
    position = 0
    for line in body.splitlines(keepends=True):
        position += len(line)
        offsets.append(position)
    return offsets


def parse_page(relative_path: str, body: str, *, perimeter: str = "agpm") -> ParsedPage:
    """Read one markdown page into sections and statements.

    ``relative_path`` is relative to the perimeter, as a snapshot stores it. The
    layer classifier of slice 1.5 works on paths that start with the root name,
    so the perimeter is put back for that one call rather than stored twice.
    """
    lines = body.splitlines()
    if not lines:
        raise WikiImportError(f"{relative_path} is empty")
    offsets = _line_offsets(body)
    fenced = fenced_lines(lines)
    headings = list(iter_headings(lines, fenced))

    title = next((item.text for item in headings if item.level == 1), relative_path)

    # A page may open with text before its first heading. That prologue is a
    # section too - dropping it would lose the statements some pages put there.
    boundaries = [item for item in headings if item.level >= 2]
    sections: list[ParsedSection] = []
    if not boundaries or boundaries[0].line > 1:
        end = offsets[boundaries[0].line] if boundaries else len(body)
        if end > 0:
            sections.append(
                ParsedSection(
                    ordinal=0,
                    heading=title,
                    heading_level=1,
                    convention=None,
                    char_start=0,
                    char_end=end,
                )
            )
    for index, heading in enumerate(boundaries):
        following = boundaries[index + 1].line if index + 1 < len(boundaries) else None
        sections.append(
            ParsedSection(
                ordinal=len(sections),
                heading=heading.text,
                heading_level=heading.level,
                convention=heading.section,
                char_start=offsets[heading.line],
                char_end=offsets[following] if following else len(body),
            )
        )

    statements: list[ParsedStatement] = []
    for section in sections:
        if section.convention not in SECTION_NATURE:
            continue
        nature = SECTION_NATURE[section.convention]
        for number, line in enumerate(lines, start=1):
            if number in fenced:
                continue
            start = offsets[number]
            if not (section.char_start <= start < section.char_end):
                continue
            match = LIST_ITEM.match(line)
            if match is None:
                continue
            text = unicodedata.normalize("NFC", match.group(1)).strip()
            if len(text) < MIN_STATEMENT_CHARS:
                continue
            item_start = start + line.index(match.group(1))
            statements.append(
                ParsedStatement(
                    section_ordinal=section.ordinal,
                    ordinal=len(statements),
                    char_start=item_start,
                    char_end=item_start + len(match.group(1)),
                    statement=text,
                    claim_nature=nature,
                )
            )

    return ParsedPage(
        relative_path=relative_path,
        layer=_layer(relative_path, perimeter),
        title=title,
        body=body,
        sections=tuple(sections),
        statements=tuple(statements),
    )


def _layer(relative_path: str, perimeter: str) -> str:
    layer = layer_for(f"{perimeter}/{relative_path}")
    return layer if layer in AUTHORED_LAYERS else "other"


def is_authored(relative_path: str, *, perimeter: str = "agpm") -> bool:
    """Whether a path is an authored page rather than a raw extract or bookkeeping.

    32 of the 93 files under ``agpm/`` are immutable raw extracts nobody wrote and
    nobody will bind to evidence, and 3 are bookkeeping. Importing them as
    concepts would put 35 pages into every "statements without evidence" report
    that were never statements.
    """
    return layer_for(f"{perimeter}/{relative_path}") in AUTHORED_LAYERS


#: Proposing a binding: which stored quotations look like they are about this
#: statement. Reciprocal rank fusion over the two language rankings, the same
#: k = 60 the document search uses (slice 1.4), so the two are comparable.
#:
#: The query is deliberately the OR form. A wiki statement is a sentence; asking
#: every one of its lexemes to appear in a quotation would match almost nothing,
#: and the point of a proposal is to put a short list in front of a person.
#:
#: Nothing here confirms anything. A proposal is a row with `confirmed_at IS NULL`,
#: and the "statements without evidence" report counts confirmed bindings only.
EVIDENCE_SEARCH_SQL = """
WITH scope AS (
{scope}
),
asked AS (
    SELECT replace(
               plainto_tsquery('pg_catalog.russian', %(statement)s)::text, ' & ', ' | '
           )::tsquery AS ru,
           replace(
               plainto_tsquery('pg_catalog.english', %(statement)s)::text, ' & ', ' | '
           )::tsquery AS en
),
scoped AS (
    SELECT evidence.claim_id, evidence.quote_text
    FROM kx.claim_evidence AS evidence
    JOIN kx.document_versions AS versions USING (version_id)
    JOIN scope ON scope.document_id = versions.document_id
    WHERE evidence.match_status = 'exact'
),
ranked_ru AS (
    SELECT scoped.claim_id,
           row_number() OVER (
               ORDER BY ts_rank(
                            to_tsvector('pg_catalog.russian', scoped.quote_text), asked.ru
                        ) DESC,
                        scoped.claim_id
           ) AS position
    FROM scoped, asked
    WHERE to_tsvector('pg_catalog.russian', scoped.quote_text) @@ asked.ru
),
ranked_en AS (
    SELECT scoped.claim_id,
           row_number() OVER (
               ORDER BY ts_rank(
                            to_tsvector('pg_catalog.english', scoped.quote_text), asked.en
                        ) DESC,
                        scoped.claim_id
           ) AS position
    FROM scoped, asked
    WHERE to_tsvector('pg_catalog.english', scoped.quote_text) @@ asked.en
)
SELECT coalesce(ranked_ru.claim_id, ranked_en.claim_id) AS claim_id,
       coalesce(1.0 / (%(k)s + ranked_ru.position), 0)
     + coalesce(1.0 / (%(k)s + ranked_en.position), 0) AS relevance
FROM ranked_ru
FULL OUTER JOIN ranked_en USING (claim_id)
ORDER BY relevance DESC
LIMIT %(limit)s
"""

#: Below this fused score a proposal is noise. One ranking placing a quotation
#: tenth gives 1/70 = 0.0143, which is the weakest thing worth showing anybody.
DEFAULT_RELEVANCE_FLOOR = 0.014
