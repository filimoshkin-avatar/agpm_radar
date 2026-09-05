"""Explicit published DTO queries for the Radar V2 public API."""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from math import log
from typing import Final, cast

from packages.contracts.json_types import JsonObject, JsonValue
from packages.validation.public_issue import (
    PublicIssueValidationError,
    build_public_issue_from_views,
    validate_public_value,
)

_GAZETTE_PERIOD: Final = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
_ZERO_STATS: Final[dict[str, int]] = {
    "adjacent": 0,
    "core": 0,
    "cut": 0,
    "far": 0,
    "included": 0,
    "mid": 0,
    "near": 0,
    "viewed": 0,
}


class PublicDataError(RuntimeError):
    """Published projections are missing or violate the frozen API contract."""


class PublishedResourceNotFoundError(PublicDataError):
    """A requested published DTO does not exist."""


class PublicDataInputError(PublicDataError):
    """A bounded request value the caller can correct: the boundary answers 400, not 503."""


def _public_text(value: object, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise PublicDataError(f"{label} is not bounded public text")
    try:
        validate_public_value(value, label=label)
    except PublicIssueValidationError as error:
        raise PublicDataError(f"{label} contains non-public content") from error
    return value


def _normalize_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PublicDataError("published timestamp is not text")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        try:
            legacy = date.fromisoformat(value)
        except ValueError as error:
            raise PublicDataError("published timestamp is invalid") from error
        if legacy.isoformat() != value:
            raise PublicDataError("published timestamp is not canonical") from None
        return f"{value}T00:00:00Z"


def _period_bounds(anchor: date, period: str) -> tuple[str, str]:
    if period == "day":
        start = anchor
    elif period == "yesterday":
        start = anchor - timedelta(days=1)
        anchor = start
    elif period == "7d":
        start = anchor - timedelta(days=6)
    elif period == "30d":
        start = anchor - timedelta(days=29)
    else:
        raise PublicDataError("unsupported period")
    return start.isoformat(), anchor.isoformat()


def _issue_date(row: tuple[object, ...] | None) -> date | None:
    if row is None or row[0] is None:
        return None
    value = str(row[0])
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise PublicDataError("published issue date is invalid") from error
    if parsed.isoformat() != value:
        raise PublicDataError("published issue date is not canonical")
    return parsed


def _latest_date(connection: sqlite3.Connection) -> date | None:
    return _issue_date(connection.execute("SELECT MAX(issue_date) FROM pub_issues_v1").fetchone())


def _issue_date_before(connection: sqlite3.Connection, moment: date) -> date | None:
    return _issue_date(
        connection.execute(
            "SELECT MAX(issue_date) FROM pub_issues_v1 WHERE issue_date < ?",
            (moment.isoformat(),),
        ).fetchone()
    )


def _period_dates(connection: sqlite3.Connection, period: str) -> tuple[str, ...]:
    anchor = _latest_date(connection)
    if anchor is None:
        return ()
    start, end = _period_bounds(anchor, period)
    return tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT issue_date
            FROM pub_issues_v1
            WHERE issue_date BETWEEN ? AND ?
            ORDER BY issue_date DESC
            LIMIT 31
            """,
            (start, end),
        )
    )


def _rubric_anchor(connection: sqlite3.Connection, anchor_date: str | None) -> date | None:
    latest = _latest_date(connection)
    if latest is None:
        return None
    if anchor_date is None:
        return latest
    try:
        anchor = date.fromisoformat(anchor_date)
    except ValueError as error:
        raise PublicDataInputError("rubric anchor date is invalid") from error
    if anchor.isoformat() != anchor_date or anchor > latest:
        raise PublicDataInputError("rubric anchor date is outside the published range")
    return anchor


def _rubric_window(
    connection: sqlite3.Connection,
    period: str,
    anchor: date,
) -> tuple[date, date, date | None, date | None]:
    days = {"day": 1, "yesterday": 1, "7d": 7, "30d": 30}.get(period)
    if days is None:
        raise PublicDataError("unsupported rubric period")
    if period == "yesterday":
        anchor -= timedelta(days=1)
    current_start = anchor - timedelta(days=days - 1)
    if days == 1:
        # One issue is compared with the previous published day, not with the
        # calendar yesterday: the archive has gaps, and an empty day would hand
        # every rubric a rise.
        previous = _issue_date_before(connection, current_start)
        return current_start, anchor, previous, previous
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return current_start, anchor, previous_start, previous_end


def _rubric_confidence(*, support: int, window: int, rubric_total: int) -> str:
    """How far the index can be trusted.

    The index is a ratio of shares, and the weaker of the two windows bounds it -
    not their sum. While the smaller window holds fewer materials than the catalog
    holds rubrics, the smoothing prior outweighs the data and there is nothing to
    compare. The former `current + previous` rule could not see that: a rubric with
    nothing at all in the previous window earned "high" on current volume alone.
    """
    if window < rubric_total:
        return "low"
    if support >= 10 and window >= 30:
        return "high"
    if support >= 4 and window >= 10:
        return "medium"
    return "low"


def _page(items: list[JsonObject], offset: int, limit: int) -> tuple[list[JsonObject], int | None]:
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    return selected, next_offset if next_offset < len(items) else None


def _shown_texts(item: JsonObject) -> tuple[str, str]:
    """The description and takeaway a card shows, so search hits are always visible.

    The model's texts when its analysis succeeded, the rule-based ones otherwise: the same
    rule as cardView() in apps/web/app.mjs and pub_search_documents_v1.
    """
    llm = item.get("llm")
    succeeded = isinstance(llm, dict) and llm.get("status") == "success"
    description = (str(item.get("llmShortText") or "") if succeeded else "") or (
        str(item.get("brief") or "") or str(item.get("summary") or "")
    )
    takeaway = (str(item.get("llmAgpmAngle") or "") if succeeded else "") or str(
        item.get("agpmTakeaway") or ""
    )
    return description, takeaway


# The card's signal labels and the abbreviated Russian month names of the browser's
# ru-RU locale (CLDR), which fmtDate() in apps/web/app.mjs prints without the dot.
_SIGNAL_LABELS: Final = {"strong": "Сильный сигнал", "context": "Контекст", "watch": "Наблюдение"}
_MONTHS_SHORT: Final = (
    "янв",
    "февр",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сент",
    "окт",
    "нояб",
    "дек",
)


def _source_host(url: str) -> str:
    """The host the card prints for a material: sourceHost() in apps/web/app.mjs."""
    host = urllib.parse.urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _date_label(item: JsonObject) -> str:
    """The date line of the card: materialDateLabel() with the compact fmtDate()."""

    def compact(value: object) -> str:
        text = str(value or "")[:10]
        try:
            day, month = int(text[8:10]), int(text[5:7])
        except ValueError:
            return ""
        return f"{day} {_MONTHS_SHORT[month - 1]}" if 1 <= month <= 12 else ""

    if item.get("publishedAt"):
        return f"опубл. {compact(item['publishedAt'])}"
    issue_date = compact(item.get("issueDate"))
    if issue_date:
        return f"дата публикации не найдена · выпуск {issue_date}"
    return "дата публикации не найдена"


def _card_search_text(item: JsonObject, rubric_titles: Mapping[str, str]) -> str:
    """Everything the card shows as text, casefolded: cardSearchText() in apps/web/app.mjs.

    Signal label, source host, date line, title, description, takeaway and the names of
    the first three rubric tags. Nothing the card does not show, so a hit is always visible.
    """
    description, takeaway = _shown_texts(item)
    strength = str(
        item.get("signalStrength") or ("strong" if item.get("verdict") == "core" else "context")
    )
    signal = _SIGNAL_LABELS.get(strength, _SIGNAL_LABELS["strong"])
    host = (
        _source_host(str(item.get("url") or "")) or str(item.get("sourceName") or "") or "источник"
    )
    rubrics = [
        rubric_titles.get(str(rubric), str(rubric))
        for rubric in cast(list[JsonValue], item.get("rubrics") or [])[:3]
    ]
    return " ".join(
        (
            signal,
            host,
            _date_label(item),
            str(item.get("title") or ""),
            description,
            takeaway,
            *rubrics,
        )
    ).casefold()


class PublicDataRepository:
    """Map allowlisted published views to exact public DTOs."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        issue_cache: dict[str, JsonObject] | None = None,
    ) -> None:
        self.connection = connection
        #: Issue documents already built from this release, by date. A release
        #: never changes, so a built document is a fact about it; the dict is the
        #: caller's and must die with the release it was built from. A cached
        #: document is served as it is, never edited.
        self.issue_cache = issue_cache if issue_cache is not None else {}

    def _issue(self, issue_date: str) -> JsonObject:
        cached = self.issue_cache.get(issue_date)
        if cached is None:
            cached = build_public_issue_from_views(self.connection, issue_date=issue_date)
            self.issue_cache[issue_date] = cached
        return cached

    def latest_issue(self) -> JsonObject:
        latest = _latest_date(self.connection)
        if latest is None:
            raise PublishedResourceNotFoundError("no published issue exists")
        return self._issue(latest.isoformat())

    def issue(self, issue_date: str) -> JsonObject:
        row = self.connection.execute(
            "SELECT 1 FROM pub_issues_v1 WHERE issue_date = ?",
            (issue_date,),
        ).fetchone()
        if row is None:
            raise PublishedResourceNotFoundError("published issue not found")
        return self._issue(issue_date)

    def issues(
        self,
        *,
        limit: int,
        before_date: str | None,
    ) -> tuple[list[JsonObject], str | None]:
        rows = self.connection.execute(
            """
            SELECT issue_date
            FROM pub_issues_v1
            WHERE (? IS NULL OR issue_date < ?)
            ORDER BY issue_date DESC
            LIMIT ?
            """,
            (before_date, before_date, limit + 1),
        ).fetchall()
        items: list[JsonObject] = []
        for row in rows[:limit]:
            issue = self._issue(str(row[0]))
            items.append(
                {
                    "brief": issue["brief"],
                    "issueDate": issue["issueDate"],
                    "issueNumber": issue["issueNumber"],
                    "llm": issue["llm"],
                    "materialCount": issue["materialCount"],
                    "publishedAt": issue["publishedAt"],
                    "title": issue["title"],
                }
            )
        next_date = str(rows[limit - 1][0]) if len(rows) > limit else None
        return items, next_date

    def _period_materials(self, period: str) -> list[JsonObject]:
        materials: list[JsonObject] = []
        for issue_date in _period_dates(self.connection, period):
            issue = self._issue(issue_date)
            issue_materials = cast(list[JsonValue], issue["materials"])
            materials.extend(cast(JsonObject, item) for item in issue_materials)
        return materials

    def materials(
        self,
        *,
        period: str,
        perimeter: str | None,
        rubric: str | None,
        query: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[JsonObject], int | None]:
        materials = self._period_materials(period)
        if perimeter is not None:
            materials = [item for item in materials if item["perimeter"] == perimeter]
        if rubric is not None:
            materials = [
                item for item in materials if rubric in cast(list[JsonValue], item["rubrics"])
            ]
        if query is not None:
            tokens = tuple(part.casefold() for part in query.split() if part)
            rubric_titles = {
                str(row[0]): str(row[1])
                for row in self.connection.execute(
                    "SELECT DISTINCT rubric_id, title FROM pub_material_rubrics_v1"
                )
            }
            materials = [
                item
                for item in materials
                if all(token in _card_search_text(item, rubric_titles) for token in tokens)
            ]
        return _page(materials, offset, limit)

    def stats(self, period: str) -> JsonObject:
        dates = _period_dates(self.connection, period)
        if not dates:
            return cast(JsonObject, dict(_ZERO_STATS))
        placeholders = ",".join("?" for _date_value in dates)
        row = self.connection.execute(
            f"""
            SELECT SUM(viewed), SUM(included), SUM(cut), SUM(near), SUM(mid),
                   SUM(far), SUM(core), SUM(adjacent)
            FROM pub_stats_v1 AS s
            JOIN pub_issues_v1 AS i ON i.issue_id = s.issue_id
            WHERE i.issue_date IN ({placeholders})
            """,  # noqa: S608 -- placeholders are generated, never user-controlled
            dates,
        ).fetchone()
        if row is None or row[0] is None:
            return cast(JsonObject, dict(_ZERO_STATS))
        keys = ("viewed", "included", "cut", "near", "mid", "far", "core", "adjacent")
        return cast(JsonObject, {key: int(value) for key, value in zip(keys, row, strict=True)})

    def timeseries(self, *, days: int, basis: str) -> list[JsonObject]:
        rows = self.connection.execute(
            """
            SELECT i.issue_date, i.published_at, s.viewed, s.included, s.cut,
                   s.near, s.mid, s.far, s.core, s.adjacent
            FROM pub_issues_v1 AS i
            JOIN pub_stats_v1 AS s ON s.issue_id = i.issue_id
            ORDER BY i.issue_date DESC
            LIMIT 90
            """
        ).fetchall()
        points: list[JsonObject] = []
        for row in rows:
            selected_date = str(row[0])
            if basis == "publication" and row[1] is not None:
                normalized = _normalize_timestamp(row[1])
                if normalized is not None:
                    selected_date = normalized[:10]
            points.append(
                {
                    "adjacent": int(row[9]),
                    "core": int(row[8]),
                    "cut": int(row[4]),
                    "date": selected_date,
                    "far": int(row[7]),
                    "included": int(row[3]),
                    "mid": int(row[6]),
                    "near": int(row[5]),
                    "viewed": int(row[2]),
                }
            )
        points.sort(key=lambda item: cast(str, item["date"]))
        return points[-days:]

    def rubrics(self, period: str, anchor_date: str | None = None) -> list[JsonObject]:
        anchor = _rubric_anchor(self.connection, anchor_date)
        if anchor is None:
            return []
        current_start, current_end, previous_start, previous_end = _rubric_window(
            self.connection, period, anchor
        )
        catalog = self.connection.execute(
            """
            SELECT rubric_id, MAX(title)
            FROM pub_material_rubrics_v1
            GROUP BY rubric_id
            """
        ).fetchall()

        def counts(start: date, end: date) -> tuple[dict[str, int], int]:
            values = (start.isoformat(), end.isoformat())
            rows = self.connection.execute(
                """
                SELECT r.rubric_id, COUNT(*)
                FROM pub_material_rubrics_v1 AS r
                JOIN pub_issues_v1 AS i ON i.issue_id = r.issue_id
                WHERE i.issue_date BETWEEN ? AND ?
                GROUP BY r.rubric_id
                """,
                values,
            ).fetchall()
            total_row = self.connection.execute(
                """
                SELECT COUNT(*)
                FROM pub_issue_materials_v1 AS m
                JOIN pub_issues_v1 AS i ON i.issue_id = m.issue_id
                WHERE i.issue_date BETWEEN ? AND ?
                """,
                values,
            ).fetchone()
            return {str(row[0]): int(row[1]) for row in rows}, int(total_row[0] or 0)

        current, current_total = counts(current_start, current_end)
        previous: dict[str, int] = {}
        previous_total = 0
        if previous_start is not None and previous_end is not None:
            previous, previous_total = counts(previous_start, previous_end)
        rubric_total = max(1, len(catalog))
        result: list[JsonObject] = []
        for raw_id, raw_title in catalog:
            rubric_id = _public_text(raw_id, "rubric id", maximum=80)
            current_count = current.get(rubric_id, 0)
            previous_count = previous.get(rubric_id, 0)
            # A rubric empty in both windows carries neither a count nor a trend:
            # a zero row with an arrow would state exactly what the data does not.
            if not current_count and not previous_count:
                continue
            current_share = (current_count + 1) / (current_total + rubric_total)
            if previous_start is None:
                # Nothing to compare with - there is no earlier issue. That is
                # "no data", not growth: the index stays at zero, the arrow flat.
                previous_share = None
                index = 0.0
                direction = "flat"
                confidence = "low"
            else:
                previous_share = (previous_count + 1) / (previous_total + rubric_total)
                index = 100.0 * log(current_share / previous_share)
                direction = "up" if index > 10 else "down" if index < -10 else "flat"
                confidence = _rubric_confidence(
                    support=min(current_count, previous_count),
                    window=min(current_total, previous_total),
                    rubric_total=rubric_total,
                )
            result.append(
                {
                    "anchorDate": current_end.isoformat(),
                    "confidence": confidence,
                    "count": current_count,
                    "currentCount": current_count,
                    "currentShare": round(current_share, 6),
                    "currentTotal": current_total,
                    "direction": direction,
                    "id": rubric_id,
                    "index": round(index, 2),
                    "period": "day" if period in {"day", "yesterday"} else period,
                    "previousCount": previous_count,
                    "previousShare": (None if previous_share is None else round(previous_share, 6)),
                    "previousTotal": previous_total,
                    "title": _public_text(raw_title, "rubric title", maximum=500),
                }
            )
        # Ordered by count, because bar length encodes count: a list sorted by an
        # index the reader cannot see reads as a broken histogram.
        return sorted(
            result,
            key=lambda item: (
                -int(cast(int, item["currentCount"])),
                str(item["title"]),
                str(item["id"]),
            ),
        )

    def sources(self, period: str) -> list[JsonObject]:
        dates = _period_dates(self.connection, period)
        if not dates:
            return []
        placeholders = ",".join("?" for _date_value in dates)
        rows = self.connection.execute(
            f"""
            SELECT m.source_name, COUNT(*)
            FROM pub_issue_materials_v1 AS m
            JOIN pub_issues_v1 AS i ON i.issue_id = m.issue_id
            WHERE i.issue_date IN ({placeholders})
              AND m.source_name IS NOT NULL AND m.source_name <> ''
            GROUP BY m.source_name
            ORDER BY COUNT(*) DESC, m.source_name
            """,  # noqa: S608 -- placeholders are generated, never user-controlled
            dates,
        ).fetchall()
        return [
            {
                "included": int(row[1]),
                "name": _public_text(row[0], "source name", maximum=500),
            }
            for row in rows
        ]

    def _gazette_entrypoint(self, gazette_id: str, period: str) -> str:
        """The URL of the issue itself, not of the directory it lives in.

        A gazette is one HTML file plus whatever it embeds, and its asset path
        carries the digest of its bytes, because the route is served immutable
        for a year. So the address of an issue changes exactly when the issue
        does, and the reader who has the old one cached is not shown it as new.
        """
        rows = self.connection.execute(
            """
            SELECT relative_path
            FROM pub_gazette_assets_v1
            WHERE gazette_id = ? AND media_type = 'text/html'
            ORDER BY relative_path
            """,
            (gazette_id,),
        ).fetchall()
        if len(rows) != 1:
            raise PublicDataError("published gazette does not have exactly one HTML entrypoint")
        path = _public_text(rows[0][0], "gazette asset path", maximum=512)
        # Two shapes live in the database: the Stage 11 seed wrote
        # `gazettes/<period>/index.html`, the fixtures write a bare `index.html`.
        # `_gazette_asset` in apps/api/application.py already looks a route up by
        # all three forms; this is the same tolerance on the way out.
        name = path.removeprefix(f"gazettes/{period}/").removeprefix(f"{period}/")
        if not name or "/" in name:
            raise PublicDataError("published gazette entrypoint is outside its period")
        return f"/gazettes/{period}/{name}"

    def gazettes(
        self,
        *,
        limit: int,
        before: tuple[str, str] | None,
    ) -> tuple[list[JsonObject], tuple[str, str] | None]:
        before_period, before_id = before if before is not None else (None, None)
        rows = self.connection.execute(
            """
            SELECT gazette_id, period, title, published_at
            FROM pub_gazettes_v1
            WHERE (? IS NULL OR period < ? OR (period = ? AND gazette_id < ?))
            ORDER BY period DESC, gazette_id DESC
            LIMIT ?
            """,
            (before_period, before_period, before_period, before_id, limit + 1),
        ).fetchall()
        items: list[JsonObject] = []
        for row in rows[:limit]:
            period = str(row[1])
            if _GAZETTE_PERIOD.fullmatch(period) is None:
                raise PublicDataError("published gazette period is unsafe")
            gazette_id = _public_text(row[0], "gazette id", maximum=128)
            items.append(
                {
                    "id": gazette_id,
                    "period": period,
                    "publishedAt": _normalize_timestamp(row[3]),
                    "title": _public_text(row[2], "gazette title", maximum=1_000),
                    "url": self._gazette_entrypoint(gazette_id, period),
                }
            )
        next_value = (
            (str(rows[limit - 1][1]), str(rows[limit - 1][0])) if len(rows) > limit else None
        )
        return items, next_value


__all__ = [
    "PublicDataError",
    "PublicDataInputError",
    "PublicDataRepository",
    "PublishedResourceNotFoundError",
]
