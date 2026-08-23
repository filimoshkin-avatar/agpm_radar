"""Putting a quotation back on its sentence boundaries (stage 0a).

The atom of this product is the quotation, and 3 481 of the 13 876 stored ones
begin or end in the middle of a sentence. Not because anything was recorded
wrongly - every span reproduces itself out of the canonical text, and the trigger
on `claim_evidence` proves it on every write - but because the extraction prompt
was free to choose where to cut, and it cut where the assertion ended rather than
where the sentence did. A reader who opens the evidence and finds

    five of the six US military branches had formally adopted GenAI.mil

instead of

    By February 2026, five of the six US military branches had formally adopted
    GenAI.mil as their enterprise AI platform, with over 1.1 million unique users
    logged within weeks of launch.

has lost the date, the scope and the figure, and has been given a reason to
distrust the next quotation too.

**Why this is a repair and not a re-extraction.** `claim_evidence` carries one
trigger, `claim_evidence_exact_span`, and it fires `BEFORE INSERT OR UPDATE`.
There is no immutability guard. So a boundary can be widened in place: the
`claim_id` survives, everything keyed to it survives, and the trigger re-proves
that the widened quotation is still verbatim. Re-parsing, the other way to reach
the same text, mints new versions and new claim ids and orphans the base.

**What it refuses to do.** Three rules keep the repair from becoming a rewrite:

* *Only a torn side moves.* A span that already starts after a full stop, or at
  the top of its own list item, or at the edge of a table cell, is not touched -
  on that side. Roughly three quarters of the base never moves at all.
* *Never past the block.* A heading is a heading, a list item is a list item and
  a table cell is a cell. Widening stops at the edge of the structural unit the
  span sits in, which for prose is the paragraph. This is the owner's ceiling and
  it is also `publication.within_one_paragraph`, so a widened quotation cannot
  fall out of the publication rule it was inside before.
* *Only outwards.* The result contains the original span. A repair that could
  shorten evidence would be a different and much more dangerous operation.

**What it cannot fix.** A quotation torn by the PDF parser rather than by the
prompt - `consistentlydegradesperforma`, a line break inside a word, a catalogue
of standards glued into one line with no punctuation at all - has no sentence
boundary to be moved to. Those spans come back unchanged with the reason saying
so. Fixing them means re-parsing, and re-parsing is the irreversible one.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: The publication rule's backstop (`publication.MAX_QUOTE_CHARS`), repeated here
#: as a ceiling rather than imported: this module must not be able to produce a
#: quotation that the publication rule would then refuse.
MAX_QUOTE_CHARS = 1500

#: How much one side may gain. Derived, not chosen: a sentence in this corpus is
#: 115 characters at the median, 229 at the ninetieth percentile and 281 at the
#: ninety-fifth, measured over 47 782 of them. A side that has to travel further
#: than 300 characters to find a full stop is not completing a sentence - it is
#: crossing a glued table, a diagram source or a slide of unpunctuated bullets,
#: and those are the parse defect, which this repair cannot touch.
MAX_SIDE_GROWTH = 300

#: How full a line has to be, against the widest line of its own block, to read
#: as a wrap rather than as a row somebody ended. Self-calibrating on purpose: a
#: fixed character width would be wrong for both a 60-column PDF and a source
#: that never wraps at all.
WRAP_RATIO = 0.8

#: What ends a sentence. A semicolon does not: it is the punctuation of a list,
#: and 273 spans are cut at one precisely because the list continues.
TERMINATORS = ".!?…"

#: Punctuation that belongs to the sentence it closes and therefore travels with
#: the terminator rather than starting the next quotation.
CLOSERS = "\"»”'’)]"

#: A full stop after one of these is an abbreviation, not an end. The list is
#: short on purpose: every entry is a word that actually occurs in this corpus,
#: and a guessed one would only make the rule harder to check.
ABBREVIATIONS = frozenset(
    """
    mr mrs ms dr prof inc ltd co corp vs etc cf al fig no vol st jr sr ed eds
    jan feb mar apr jun jul aug sep sept oct nov dec approx est dept univ gov
    т е д п к др рис табл см стр гг вв руб млн млрд тыс им ул проф акад англ
    напр обл н э
    """.split()
)

_LIST_MARKER = re.compile(r"^[ \t]*(?:[-*•+‣]|\d+[.)]|[a-zа-я][.)])[ \t]+")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+")
_BLOCKQUOTE = re.compile(r"^[ \t]*>[ \t]?")
_TABLE_ROW = re.compile(r"^[ \t]*\|")
_WORD_BEFORE_DOT = re.compile(r"([0-9]+|[^\W\d_]+)$", re.UNICODE)

#: Reasons a side can carry. Stated as constants because the dry run counts them
#: and a report that groups by a typo is a report nobody can read.
UNCHANGED_BOUNDARY = "already on a boundary"
EXPANDED_TO_SENTENCE = "widened to the sentence"
EXPANDED_TO_BLOCK = "widened to the edge of the block"
KEPT_STRUCTURAL = "kept: the source is built that way"
KEPT_CROSSES_PARAGRAPH = "kept: the span already crosses a paragraph break"
KEPT_OVER_CAP = "kept: the sentence is longer than the publication cap"
KEPT_OUT_OF_REACH = "kept: no sentence boundary within a sentence's reach"


@dataclass(frozen=True, slots=True)
class Block:
    """The structural unit a span sits in, and what kind of unit it is."""

    start: int
    end: int
    kind: str
    #: The longest line in the block. A line that stops well short of it stopped
    #: because somebody ended it, not because it ran out of width.
    wrap_width: int = 0


@dataclass(frozen=True, slots=True)
class Expansion:
    """Where the span should sit, and why each side did or did not move."""

    start: int
    end: int
    block_kind: str
    left_reason: str
    right_reason: str

    @property
    def changed(self) -> bool:
        widened = (EXPANDED_TO_SENTENCE, EXPANDED_TO_BLOCK)
        return self.left_reason in widened or self.right_reason in widened


def _line_bounds(text: str, position: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return start, len(text) if end < 0 else end


def _opens_a_block(line: str) -> bool:
    """Whether this line starts something rather than continuing the line above.

    A blank line, a list marker, a heading, a blockquote or a table row all end
    the previous unit. Everything else is a wrap: this corpus comes largely out
    of PDFs, where a paragraph is many hard-wrapped lines and a line break in the
    middle of one carries no meaning.
    """
    return bool(
        not line.strip()
        or _LIST_MARKER.match(line)
        or _HEADING.match(line)
        or _BLOCKQUOTE.match(line)
        or _TABLE_ROW.match(line)
    )


def block_of(text: str, start: int, end: int) -> Block:
    """The structural unit around the span, and how far widening may go.

    Bounded by `MAX_QUOTE_CHARS` on each side: a span can never grow past the cap,
    so walking a 900 000-character document to find where its only paragraph ends
    would be work thrown away.
    """
    first_start, first_end = _line_bounds(text, start)
    first_line = text[first_start:first_end]
    _, last_end = _line_bounds(text, max(start, end - 1))

    heading = _HEADING.match(first_line)
    if heading:
        return Block(first_start + heading.end(), first_end, "heading")

    if _TABLE_ROW.match(first_line):
        opening = text.rfind("|", first_start, start)
        closing = text.find("|", end, last_end)
        return Block(
            opening + 1 if opening >= 0 else first_start,
            closing if closing >= 0 else last_end,
            "table cell",
        )

    marker = _LIST_MARKER.match(first_line)
    left = first_start + marker.end() if marker else first_start
    kind = "list item" if marker else "paragraph"

    if not marker:
        floor = max(0, end - MAX_QUOTE_CHARS)
        while left > floor:
            previous_start, _ = _line_bounds(text, left - 1)
            if _opens_a_block(text[previous_start : left - 1]):
                break
            left = previous_start
        left = max(left, floor)

    ceiling = min(len(text), start + MAX_QUOTE_CHARS)
    right = last_end
    while right < ceiling:
        following_start, following_end = _line_bounds(text, right + 1)
        if _opens_a_block(text[following_start:following_end]):
            break
        right = following_end
    right = min(right, ceiling)
    widest = max((len(line) for line in text[left:right].split("\n")), default=0)
    return Block(left, right, kind, widest)


def is_a_wrap(text: str, newline: int, block: Block) -> bool:
    """Whether the line break at `newline` is a wrap or somebody's decision.

    Half of this corpus arrives as PDF text, where a paragraph is a stack of
    hard-wrapped lines and the break inside it means nothing. The other half of
    the line breaks mean everything: a table flattened into plain text, a slide
    of bullets, a catalogue of standards - one row per line, no punctuation
    anywhere. Widening cannot tell them apart by looking at the break itself, but
    it can look at the line that ends there. A wrapped line ends full; a row ends
    where its content ended.
    """
    line_start = text.rfind("\n", block.start, newline) + 1
    return newline - max(line_start, block.start) >= WRAP_RATIO * block.wrap_width


def is_terminator(text: str, index: int) -> bool:
    """Whether the character at `index` ends a sentence rather than an abbreviation.

    Only the neighbourhood is read - forty characters back, eight forward - so the
    check costs the same on a one-line note and on a one-megabyte standard.
    """
    char = text[index]
    if char in "!?…":
        return True
    if char != ".":
        return False
    before = text[max(0, index - 40) : index]
    after = text[index + 1 : index + 8]
    if before[-1:].isdigit() and after[:1].isdigit():
        return False
    word = _WORD_BEFORE_DOT.search(before)
    if word:
        found = word.group(0)
        if len(found) == 1 and not found.isdigit():
            return False
        if found.lower() in ABBREVIATIONS:
            return False
    tail = after.lstrip(CLOSERS)
    if tail == "" or tail[0].isspace():
        return True
    # `воздействий.Примечание` - the PDF parser lost the space, not the sentence.
    # Reading the full stop as real is what stops a widening from running on
    # through the rest of the standard.
    return tail[0].isupper()


def _terminates_before(text: str, position: int, floor: int) -> bool:
    """Whether the text ending at `position` finishes a sentence."""
    index = position - 1
    while index >= floor and (text[index].isspace() or text[index] in CLOSERS):
        index -= 1
    return index >= floor and text[index] in TERMINATORS and is_terminator(text, index)


def left_is_clean(text: str, start: int, block: Block) -> bool:
    """Whether the span already begins where something begins."""
    head = text[block.start : start]
    if start <= block.start or not head.strip():
        return True
    if _terminates_before(text, start, block.start):
        return True
    break_at = text.rfind("\n", block.start, start)
    return (
        break_at >= 0
        and not text[break_at + 1 : start].strip()
        and not is_a_wrap(text, break_at, block)
    )


def right_is_clean(text: str, end: int, block: Block) -> bool:
    """Whether the span already ends where something ends."""
    tail = text[end : block.end]
    if end >= block.end or not tail.strip():
        return True
    if _terminates_before(text, end, block.start):
        return True
    break_at = text.find("\n", end, block.end)
    return break_at >= 0 and not text[end:break_at].strip() and not is_a_wrap(text, break_at, block)


def _sentence_start(text: str, position: int, block: Block) -> tuple[int, str]:
    """Walk left to the start of the sentence, no further than one sentence's reach.

    Two other things end the walk as legitimately as a full stop does: the top of
    the block, and a line break that is not a wrap. Both are places where the text
    itself starts something.
    """
    floor = max(block.start, position - MAX_SIDE_GROWTH)
    index = position - 1
    while index >= floor:
        char = text[index]
        if char in TERMINATORS and is_terminator(text, index):
            candidate = index + 1
            while candidate < position and (
                text[candidate].isspace() or text[candidate] in CLOSERS
            ):
                candidate += 1
            return candidate, EXPANDED_TO_SENTENCE
        if char == "\n" and not is_a_wrap(text, index, block):
            candidate = index + 1
            while candidate < position and text[candidate].isspace():
                candidate += 1
            return candidate, EXPANDED_TO_BLOCK
        index -= 1
    if floor > block.start:
        return position, KEPT_OUT_OF_REACH
    while floor < position and text[floor].isspace():
        floor += 1
    return floor, EXPANDED_TO_BLOCK


def _sentence_end(text: str, position: int, block: Block) -> tuple[int, str]:
    ceiling = min(block.end, position + MAX_SIDE_GROWTH)
    index = position
    while index < ceiling:
        char = text[index]
        if char in TERMINATORS and is_terminator(text, index):
            candidate = index + 1
            while candidate < ceiling and text[candidate] in CLOSERS:
                candidate += 1
            return candidate, EXPANDED_TO_SENTENCE
        if char == "\n" and not is_a_wrap(text, index, block):
            while index > position and text[index - 1].isspace():
                index -= 1
            return index, EXPANDED_TO_BLOCK
        index += 1
    if ceiling < block.end:
        return position, KEPT_OUT_OF_REACH
    while ceiling > position and text[ceiling - 1].isspace():
        ceiling -= 1
    return ceiling, EXPANDED_TO_BLOCK


def expand(text: str, start: int, end: int) -> Expansion:
    """Where this span should sit once both of its sides are on a boundary."""
    if "\n\n" in text[start:end]:
        return Expansion(start, end, "paragraph", KEPT_CROSSES_PARAGRAPH, KEPT_CROSSES_PARAGRAPH)

    found = block_of(text, start, end)
    if found.kind in ("heading", "table cell"):
        return Expansion(start, end, found.kind, KEPT_STRUCTURAL, KEPT_STRUCTURAL)

    # A span may reach outside the unit its first line belongs to. The block is
    # widened to hold it rather than allowed to cut it.
    block = Block(min(found.start, start), max(found.end, end), found.kind, found.wrap_width)

    if left_is_clean(text, start, block):
        new_start, left_reason = start, UNCHANGED_BOUNDARY
    else:
        new_start, left_reason = _sentence_start(text, start, block)

    if right_is_clean(text, end, block):
        new_end, right_reason = end, UNCHANGED_BOUNDARY
    else:
        new_end, right_reason = _sentence_end(text, end, block)

    new_start, new_end = min(new_start, start), max(new_end, end)
    if "\n\n" in text[new_start:new_end] or new_end - new_start > MAX_QUOTE_CHARS:
        return Expansion(start, end, block.kind, KEPT_OVER_CAP, KEPT_OVER_CAP)
    return Expansion(new_start, new_end, block.kind, left_reason, right_reason)


@dataclass(frozen=True, slots=True)
class Repair:
    """One quotation, where it sat and where it should sit."""

    claim_id: str
    version_id: str
    old_start: int
    old_end: int
    quote: str
    widened: str
    expansion: Expansion

    @property
    def changed(self) -> bool:
        return self.expansion.changed

    @property
    def added(self) -> int:
        return len(self.widened) - len(self.quote)

    def as_example(self) -> dict[str, Any]:
        """One before-and-after, small enough to read a page of them."""
        return {
            "claimId": self.claim_id,
            "blockKind": self.expansion.block_kind,
            "leftBoundary": self.expansion.left_reason,
            "rightBoundary": self.expansion.right_reason,
            "added": self.added,
            "gainedOnTheLeft": self.widened[: self.old_start - self.expansion.start],
            "quote": self.quote,
            "gainedOnTheRight": self.widened[
                len(self.widened) - (self.expansion.end - self.old_end) :
            ]
            if self.expansion.end > self.old_end
            else "",
        }


def _percentiles(values: list[int]) -> dict[str, int]:
    if not values:
        return {}
    values.sort()
    return {
        "p50": values[len(values) // 2],
        "p90": values[min(len(values) - 1, int(0.90 * len(values)))],
        "p99": values[min(len(values) - 1, int(0.99 * len(values)))],
        "max": values[-1],
    }


def summarize(repairs: Sequence[Repair]) -> dict[str, Any]:
    """What a run did, counted by the reason each side gave.

    The dry run and the applied run produce the same shape on purpose: what the
    owner approved and what then happened have to be comparable line by line.
    """
    by_kind: Counter[str] = Counter()
    by_left: Counter[str] = Counter()
    by_right: Counter[str] = Counter()
    added: list[int] = []
    for repair in repairs:
        by_kind[repair.expansion.block_kind] += 1
        by_left[repair.expansion.left_reason] += 1
        by_right[repair.expansion.right_reason] += 1
        if repair.changed:
            added.append(repair.added)
    return {
        "examined": len(repairs),
        "changed": len(added),
        "unchanged": len(repairs) - len(added),
        "byBlockKind": dict(by_kind.most_common()),
        "byLeftBoundary": dict(by_left.most_common()),
        "byRightBoundary": dict(by_right.most_common()),
        "charactersAdded": _percentiles(added),
    }
