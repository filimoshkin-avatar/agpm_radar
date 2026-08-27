"""Explicit published DTO queries for the Radar V2 public API."""

from __future__ import annotations

import re
import sqlite3
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


def _latest_date(connection: sqlite3.Connection) -> date | None:
    row = connection.execute("SELECT MAX(issue_date) FROM pub_issues_v1").fetchone()
    if row is None or row[0] is None:
        return None
    value = str(row[0])
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise PublicDataError("latest published issue date is invalid") from error
    if parsed.isoformat() != value:
        raise PublicDataError("latest published issue date is not canonical")
    return parsed


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
        raise PublicDataError("rubric anchor date is invalid") from error
    if anchor.isoformat() != anchor_date or anchor > latest:
        raise PublicDataError("rubric anchor date is outside the published range")
    return anchor


def _rubric_window(period: str, anchor: date) -> tuple[date, date, date | None, date | None]:
    days = {"day": 1, "yesterday": 1, "7d": 7, "30d": 30}.get(period)
    if days is None:
        raise PublicDataError("unsupported rubric period")
    if period == "yesterday":
        anchor -= timedelta(days=1)
    current_start = anchor - timedelta(days=days - 1)
    if days == 1:
        return current_start, anchor, None, None
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return current_start, anchor, previous_start, previous_end


def _page(items: list[JsonObject], offset: int, limit: int) -> tuple[list[JsonObject], int | None]:
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    return selected, next_offset if next_offset < len(items) else None


class PublicDataRepository:
    """Map allowlisted published views to exact public DTOs."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def latest_issue(self) -> JsonObject:
        latest = _latest_date(self.connection)
        if latest is None:
            raise PublishedResourceNotFoundError("no published issue exists")
        return build_public_issue_from_views(self.connection, issue_date=latest.isoformat())

    def issue(self, issue_date: str) -> JsonObject:
        row = self.connection.execute(
            "SELECT 1 FROM pub_issues_v1 WHERE issue_date = ?",
            (issue_date,),
        ).fetchone()
        if row is None:
            raise PublishedResourceNotFoundError("published issue not found")
        return build_public_issue_from_views(self.connection, issue_date=issue_date)

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
            issue = build_public_issue_from_views(self.connection, issue_date=str(row[0]))
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
            issue = build_public_issue_from_views(self.connection, issue_date=issue_date)
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
            filtered: list[JsonObject] = []
            for item in materials:
                searchable = " ".join(
                    str(item.get(field) or "")
                    for field in ("title", "summary", "agpmTakeaway", "sourceName")
                ).casefold()
                if all(token in searchable for token in tokens):
                    filtered.append(item)
            materials = filtered
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
        current_start, current_end, previous_start, previous_end = _rubric_window(period, anchor)
        catalog = self.connection.execute(
            """
            SELECT rubric_id, MAX(title), MIN(sort_order)
            FROM pub_material_rubrics_v1
            GROUP BY rubric_id
            ORDER BY MIN(sort_order), MAX(title), rubric_id
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
        for raw_id, raw_title, _sort_order in catalog:
            rubric_id = _public_text(raw_id, "rubric id", maximum=80)
            current_count = current.get(rubric_id, 0)
            previous_count = previous.get(rubric_id, 0)
            current_share = (current_count + 1) / (current_total + rubric_total)
            if previous_start is None:
                index = 100.0 if current_count else 0.0
                direction = "up" if current_count else "flat"
            else:
                previous_share = (previous_count + 1) / (previous_total + rubric_total)
                index = 100.0 * log(current_share / previous_share)
                direction = "up" if index > 10 else "down" if index < -10 else "flat"
            evidence = current_count + previous_count
            confidence = "high" if evidence >= 10 else "medium" if evidence >= 4 else "low"
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
                    "previousShare": (
                        None
                        if previous_start is None
                        else round((previous_count + 1) / (previous_total + rubric_total), 6)
                    ),
                    "previousTotal": previous_total,
                    "title": _public_text(raw_title, "rubric title", maximum=500),
                }
            )
        return sorted(
            result,
            key=lambda item: (
                -float(cast(float, item["index"])),
                -int(cast(int, item["currentCount"])),
                str(item["title"]),
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
            items.append(
                {
                    "id": _public_text(row[0], "gazette id", maximum=128),
                    "period": period,
                    "publishedAt": _normalize_timestamp(row[3]),
                    "title": _public_text(row[2], "gazette title", maximum=1_000),
                    "url": f"/gazettes/{period}/",
                }
            )
        next_value = (
            (str(rows[limit - 1][1]), str(rows[limit - 1][0])) if len(rows) > limit else None
        )
        return items, next_value


__all__ = [
    "PublicDataError",
    "PublicDataRepository",
    "PublishedResourceNotFoundError",
]
