"""Reading a publication date out of what the source said (stage 0a).

The observatory the owner asked for is a cut by class of event over a period, so
it needs a day for every document. The store had none. What it has is text: the
radar copied whatever the material claimed into `published_raw` and never parsed
it, and 7 986 of 8 346 materials carry something.

Almost all of it is already ISO - inside the issue perimeter it is ISO without a
single exception - so this is a small parser with a short list of shapes, each of
which occurs in the store and none of which was invented for completeness:

    2026-08-20              7 860   a day
    2026-08-20T00:00:00Z            a day, with a time nobody needs
    April 13, 2026             83   a day, written out
    2026-06 / 2026-06-??       13   a month
    2026 / 2025-??-??           5   a year
    2 hours ago                25   nothing this can use

**A month is not a day.** A partial date is stored as the first day of the period
it names, and `precision` is what keeps that convention from being read back as a
real day. Sorting a chronicle is the one thing the first-of-the-month form is for.

**"2 hours ago" is deliberately not parsed.** It could be resolved against the
capture time, and the result would be a publication date that is really an
arithmetic on a crawl. The fallback already handles those materials correctly and
labels them honestly: what is shown is the day the radar found them, and the
reader is told so.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

#: How precise the source was. `none` means it gave nothing this could read.
PRECISIONS = ("day", "month", "year", "none")

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}

_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ]|$)")
#: `2026-06`, `2026-06-??` and the malformed `2026-06-xx` / `2026-06-2026` a few
#: sources emit - all of them name a month and nothing finer.
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})(?:-(?:\?\?|xx|\d{4}))?$", re.IGNORECASE)
_ISO_YEAR = re.compile(r"^(\d{4})(?:-\?\?)*$")
_WRITTEN_OUT = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})$")


def parse_published(raw: str | None) -> tuple[date | None, str]:
    """What the source said, as a date and the precision it was given at."""
    text = (raw or "").strip()
    if not text:
        return None, "none"

    match = _ISO_DAY.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        found = _safe_date(year, month, day)
        if found is not None:
            return found, "day"

    match = _WRITTEN_OUT.match(text)
    if match:
        named = _MONTHS.get(match.group(1).lower())
        if named is not None:
            found = _safe_date(int(match.group(3)), named, int(match.group(2)))
            if found is not None:
                return found, "day"

    match = _ISO_MONTH.match(text)
    if match:
        found = _safe_date(int(match.group(1)), int(match.group(2)), 1)
        if found is not None:
            return found, "month"

    match = _ISO_YEAR.match(text)
    if match:
        found = _safe_date(int(match.group(1)), 1, 1)
        if found is not None:
            return found, "year"

    return None, "none"


def _safe_date(year: int, month: int, day: int) -> date | None:
    """A date, or nothing. `2026-02-31` is a typo, not a day in February."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ResolvedDate:
    """One document's date, and the honesty label that travels with it."""

    document_id: str
    published_raw: str | None
    raw_source: str
    published_on: date | None
    date_precision: str
    shown_on: date
    shown_kind: str


def resolve(
    *,
    document_id: str,
    issue_raw: str | None,
    material_raw: str | None,
    found_at: datetime,
) -> ResolvedDate:
    """The date to show for one document, and which of the two dates it is.

    The issue perimeter wins over the corpus row when both say something: the
    perimeter is the radar's own record of the material it selected, written at
    selection time, while the corpus row is an older and coarser import.
    """
    for source, raw in (("issue_perimeter", issue_raw), ("source_material", material_raw)):
        published_on, precision = parse_published(raw)
        if published_on is not None:
            return ResolvedDate(
                document_id=document_id,
                published_raw=raw,
                raw_source=source,
                published_on=published_on,
                date_precision=precision,
                shown_on=published_on,
                shown_kind="published",
            )

    unread = issue_raw or material_raw
    return ResolvedDate(
        document_id=document_id,
        published_raw=unread,
        raw_source="issue_perimeter"
        if issue_raw
        else ("source_material" if material_raw else "none"),
        published_on=None,
        date_precision="none",
        shown_on=found_at.date(),
        shown_kind="first_seen",
    )


def summarize(dates: Sequence[ResolvedDate]) -> dict[str, Any]:
    """What a pass found, counted the way the caveat will have to be worded."""
    by_kind: Counter[str] = Counter()
    by_precision: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    unread: Counter[str] = Counter()
    published: list[date] = []
    for resolved in dates:
        by_kind[resolved.shown_kind] += 1
        by_precision[resolved.date_precision] += 1
        by_source[resolved.raw_source] += 1
        if resolved.published_on is not None:
            published.append(resolved.published_on)
        elif resolved.published_raw:
            unread[resolved.published_raw.strip()[:40]] += 1
    return {
        "documents": len(dates),
        "byShownDate": dict(by_kind.most_common()),
        "byPrecision": dict(by_precision.most_common()),
        "byRawSource": dict(by_source.most_common()),
        "publishedRange": {
            "earliest": min(published).isoformat() if published else None,
            "latest": max(published).isoformat() if published else None,
        },
        # What the parser was handed and could not read. Named rather than
        # counted: a shape nobody can see is a shape nobody will ever add.
        "unreadable": dict(unread.most_common(10)),
    }
