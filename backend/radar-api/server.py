#!/usr/bin/env python3
"""Small stdlib JSON API for radar.aipractice.space."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("RADAR_DB", "/mnt/vdd/Radar/data/db/radar.sqlite"))
HOST = os.environ.get("RADAR_BACKEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("RADAR_BACKEND_PORT", "8765"))
PIPELINE_SCRIPTS = Path("/mnt/vdd/Radar/pipeline/scripts")
if str(PIPELINE_SCRIPTS) not in sys.path:
    sys.path.append(str(PIPELINE_SCRIPTS))

from agpm_radar_signal_strength import signal_label


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def parse_json_list(value: str | None) -> list[Any]:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def parse_json_dict(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def period_start(period: str) -> str | None:
    today = date.today()
    if period == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    if period == "day":
        return today.isoformat()
    if period == "7d":
        return (today - timedelta(days=6)).isoformat()
    if period == "30d":
        return (today - timedelta(days=29)).isoformat()
    return None


def rejected_facts(conn: sqlite3.Connection, period: str) -> list[dict[str, Any]]:
    start = period_start(period)
    if not start:
        return []
    facts: list[dict[str, Any]] = []
    for row in conn.execute("SELECT radar_issue_date, source_json FROM rejected_materials_internal"):
        try:
            data = json.loads(row["source_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        published = str(data.get("published_at") or row["radar_issue_date"] or "")[:10]
        if not published:
            continue
        if period in {"day", "yesterday"} and published != start:
            continue
        if period not in {"day", "yesterday"} and published < start:
            continue
        facts.append({
            "published_at": published,
            "source_name": data.get("source_name") or "Внутренний отсев",
        })
    return facts


def empty_stats() -> dict[str, int]:
    return {key: 0 for key in ["viewed", "included", "cut", "near", "mid", "far", "core", "adjacent"]}


def add_material_counts(acc: dict[str, int], row: sqlite3.Row) -> None:
    acc["included"] += 1
    acc["viewed"] += 1
    perimeter = row["perimeter"]
    verdict = row["verdict"]
    if perimeter in {"near", "mid", "far"}:
        acc[perimeter] += 1
    if verdict in {"core", "adjacent"}:
        acc[verdict] += 1


def add_rejected_count(acc: dict[str, int]) -> None:
    acc["viewed"] += 1
    acc["cut"] += 1


def issue_stats(conn: sqlite3.Connection, issue_date: str) -> dict[str, int]:
    row = conn.execute("SELECT * FROM daily_stats WHERE stat_date = ?", (issue_date,)).fetchone()
    if row:
        return {key: int(row[key]) for key in ["viewed", "included", "cut", "near", "mid", "far", "core", "adjacent"]}
    stats = empty_stats()
    for material in conn.execute("SELECT perimeter, verdict FROM materials WHERE radar_issue_date = ?", (issue_date,)):
        add_material_counts(stats, material)
    return stats


def material_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = row_to_dict(row)
    item["signal_label"] = signal_label(item.get("signal_strength"))
    item["theses"] = parse_json_list(item.pop("theses_json", "[]"))
    item["rubrics"] = [
        r["rubric_id"]
        for r in conn.execute("SELECT rubric_id FROM material_rubrics WHERE material_id = ? ORDER BY confidence DESC", (item["id"],))
    ]
    quality = conn.execute(
        """
        SELECT source_host, issue_date_delta_days, severity, review_status, reason
        FROM material_date_quality
        WHERE material_id = ?
        """,
        (item["id"],),
    ).fetchone()
    item["date_quality"] = row_to_dict(quality) if quality else None
    llm_summary = conn.execute(
        """
        SELECT short_text, agpm_angle, provider, model, prompt_version, status, updated_at
        FROM material_llm_summaries
        WHERE material_id = ?
        """,
        (item["id"],),
    ).fetchone()
    item["llm_summary"] = row_to_dict(llm_summary) if llm_summary else None
    return item


def fts_query(value: str) -> str:
    terms = re.findall(r"[\w]+", value.lower(), flags=re.UNICODE)[:8]
    return " OR ".join(f"{term}*" for term in terms)


def material_rows(conn: sqlite3.Connection, params: dict[str, list[str]]) -> list[dict[str, Any]]:
    where = ["1=1"]
    values: list[Any] = []
    order = "COALESCE(m.published_at, m.radar_issue_date) DESC, m.key_material DESC, m.title"
    join = ""
    period = (params.get("period") or [""])[0]
    start = period_start(period)
    if start:
        if period in {"day", "yesterday"}:
            where.append("date(radar_issue_date) = ?")
        else:
            where.append("radar_issue_date >= ?")
        values.append(start)
    perimeter = (params.get("perimeter") or [""])[0]
    if perimeter in {"near", "mid", "far"}:
        where.append("perimeter = ?")
        values.append(perimeter)
    verdict = (params.get("verdict") or [""])[0]
    if verdict in {"core", "adjacent"}:
        where.append("verdict = ?")
        values.append(verdict)
    date_status = (params.get("date_status") or [""])[0]
    if date_status in {"resolved", "low_confidence", "unresolved"}:
        where.append("publication_date_status = ?")
        values.append(date_status)
    q = (params.get("q") or [""])[0].strip().lower()
    if q:
        match = fts_query(q)
        if match:
            join = "JOIN materials_fts f ON f.material_id = m.id AND materials_fts MATCH ?"
            values.insert(0, match)
            order = "bm25(materials_fts), " + order
        else:
            where.append("(lower(title) LIKE ? OR lower(summary) LIKE ? OR lower(agpm_takeaway) LIKE ? OR lower(source_name) LIKE ? OR lower(url) LIKE ? OR lower(canonical_url) LIKE ?)")
            like = f"%{q}%"
            values.extend([like, like, like, like, like, like])
    rubric = (params.get("rubric") or [""])[0]
    if rubric:
        join += " JOIN material_rubrics mr ON mr.material_id = m.id"
        where.append("mr.rubric_id = ?")
        values.append(rubric)
    limit = min(int((params.get("limit") or ["500"])[0]), 500)
    sql = f"""
    SELECT m.* FROM materials m
    {join}
    WHERE {' AND '.join(where)}
    ORDER BY {order}
    LIMIT ?
    """
    try:
        rows = conn.execute(sql, [*values, limit]).fetchall()
    except sqlite3.OperationalError:
        if not q:
            raise
        like = f"%{q}%"
        fallback_where = [clause for clause in where if "materials_fts" not in clause]
        fallback_where.append("(lower(m.title) LIKE ? OR lower(m.summary) LIKE ? OR lower(m.agpm_takeaway) LIKE ? OR lower(m.source_name) LIKE ? OR lower(m.url) LIKE ? OR lower(m.canonical_url) LIKE ?)")
        rows = conn.execute(
            f"""
            SELECT m.* FROM materials m
            {'JOIN material_rubrics mr ON mr.material_id = m.id' if rubric else ''}
            WHERE {' AND '.join(fallback_where)}
            ORDER BY COALESCE(m.published_at, m.radar_issue_date) DESC, m.key_material DESC, m.title
            LIMIT ?
            """,
            [*values[1:], like, like, like, like, like, like, limit],
        ).fetchall()
    return [material_payload(conn, row) for row in rows]


def stats(conn: sqlite3.Connection, period: str) -> dict[str, int]:
    start = period_start(period)
    if not start:
        return empty_stats()
    if period in {"day", "yesterday"}:
        return issue_stats(conn, start)
    row = conn.execute(
        """
        SELECT
          sum(viewed) viewed,
          sum(included) included,
          sum(cut) cut,
          sum(near) near,
          sum(mid) mid,
          sum(far) far,
          sum(core) core,
          sum(adjacent) adjacent
        FROM daily_stats
        WHERE stat_date >= ?
        """,
        (start,),
    ).fetchone()
    if row and row["viewed"] is not None:
        return {key: int(row[key] or 0) for key in ["viewed", "included", "cut", "near", "mid", "far", "core", "adjacent"]}
    return empty_stats()


def stats_for_publication_day(conn: sqlite3.Connection, day: str) -> dict[str, int]:
    summary = empty_stats()
    for material in conn.execute(
        "SELECT perimeter, verdict FROM materials m WHERE date(m.published_at) = ?",
        (day,),
    ):
        add_material_counts(summary, material)
    for row in conn.execute("SELECT radar_issue_date, source_json FROM rejected_materials_internal"):
        try:
            data = json.loads(row["source_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        published = str(data.get("published_at") or row["radar_issue_date"] or "")[:10]
        if published == day:
            add_rejected_count(summary)
    return summary


def publication_timeseries(conn: sqlite3.Connection, days: int) -> list[dict[str, Any]]:
    days = max(1, min(days, 90))
    today = date.today()
    start = today - timedelta(days=days - 1)
    result: list[dict[str, Any]] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        row = stats_for_publication_day(conn, day)
        row["stat_date"] = day
        result.append(row)
    return result


def issue_timeseries(conn: sqlite3.Connection, days: int) -> list[dict[str, Any]]:
    days = max(1, min(days, 90))
    today = date.today()
    start = today - timedelta(days=days - 1)
    result: list[dict[str, Any]] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        row = issue_stats(conn, day)
        row["stat_date"] = day
        result.append(row)
    return result


def source_rows(conn: sqlite3.Connection, period: str) -> list[dict[str, Any]]:
    period_sql, period_values = period_filter_sql("m", period)
    where = f"WHERE {period_sql}" if period_sql else ""
    rows = conn.execute(
        f"SELECT source_name name, count(*) included FROM materials m {where} GROUP BY source_name",
        period_values,
    ).fetchall()
    merged: dict[str, dict[str, int | str]] = {}
    for row in rows:
        name = row["name"] or "Неизвестный источник"
        merged[name] = {"name": name, "included": int(row["included"] or 0), "cut": 0, "collected": int(row["included"] or 0)}
    return sorted(merged.values(), key=lambda row: (-int(row["collected"]), -int(row["included"]), str(row["name"])))[:20]


def period_theses_payload(conn: sqlite3.Connection, as_of_issue_date: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT *
        FROM issue_period_theses
        WHERE as_of_issue_date = ?
        ORDER BY period
        """,
        (as_of_issue_date,),
    ).fetchall()
    payload: dict[str, Any] = {}
    for row in rows:
        item = row_to_dict(row)
        period = item["period"]
        item["theses"] = parse_json_list(item.pop("theses_json") or "[]")
        item["stats"] = parse_json_dict(item.pop("stats_json") or "{}")
        payload[period] = item
    return payload


def daily_analysis_payload(conn: sqlite3.Connection, issue_date: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM issue_daily_analysis
        WHERE issue_date = ?
        """,
        (issue_date,),
    ).fetchone()
    if not row:
        return None
    item = row_to_dict(row)
    item["analysis"] = parse_json_dict(item.pop("analysis_json") or "{}")
    return item


def issue_llm_theses_payload(conn: sqlite3.Connection, issue_date: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM issue_llm_theses
        WHERE issue_date = ?
        """,
        (issue_date,),
    ).fetchone()
    if not row:
        return None
    item = row_to_dict(row)
    item["theses"] = parse_json_list(item.pop("theses_json") or "[]")
    return item


def period_filter_sql(alias: str, period: str) -> tuple[str, list[Any]]:
    start = period_start(period)
    if not start:
        return "", []
    field = f"{alias}.radar_issue_date"
    if period in {"day", "yesterday"}:
        return f"date({field}) = ?", [start]
    return f"{field} >= ?", [start]


def date_quality_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT publication_date_status, severity, review_status, count(*) count
        FROM material_date_quality
        GROUP BY publication_date_status, severity, review_status
        """
    ).fetchall()
    summary: dict[str, Any] = {
        "materials_total": 0,
        "queued_for_review": 0,
        "by_publication_date_status": {},
        "by_review_status": {},
        "by_severity": {},
    }
    for row in rows:
        count = int(row["count"])
        summary["materials_total"] += count
        summary["by_publication_date_status"][row["publication_date_status"]] = (
            summary["by_publication_date_status"].get(row["publication_date_status"], 0) + count
        )
        summary["by_review_status"][row["review_status"]] = summary["by_review_status"].get(row["review_status"], 0) + count
        summary["by_severity"][row["severity"]] = summary["by_severity"].get(row["severity"], 0) + count
        if row["review_status"] == "queued":
            summary["queued_for_review"] += count
    hosts = conn.execute(
        """
        SELECT source_host host, count(*) count
        FROM material_date_quality
        WHERE review_status = 'queued'
        GROUP BY source_host
        ORDER BY count DESC, source_host
        LIMIT 20
        """
    ).fetchall()
    summary["top_review_hosts"] = [row_to_dict(row) for row in hosts]
    return summary


def date_quality_queue(conn: sqlite3.Connection, params: dict[str, list[str]]) -> list[dict[str, Any]]:
    limit = min(int((params.get("limit") or ["100"])[0]), 500)
    severity = (params.get("severity") or [""])[0]
    where = ["q.review_status = 'queued'"]
    values: list[Any] = []
    if severity in {"high", "medium", "low"}:
        where.append("q.severity = ?")
        values.append(severity)
    rows = conn.execute(
        f"""
        SELECT q.material_id, m.title, m.url, m.source_name, m.published_at, m.radar_issue_date,
               q.source_host, q.publication_date_status, q.issue_date_delta_days,
               q.severity, q.review_status, q.reason
        FROM material_date_quality q
        JOIN materials m ON m.id = q.material_id
        WHERE {' AND '.join(where)}
        ORDER BY
          CASE q.severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
          abs(COALESCE(q.issue_date_delta_days, 0)) DESC,
          m.title
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def latest(conn: sqlite3.Connection) -> dict[str, Any]:
    issue = conn.execute("SELECT * FROM issues ORDER BY issue_date DESC LIMIT 1").fetchone()
    if not issue:
        return {"issue": None, "materials": [], "stats": {}}
    issue_dict = row_to_dict(issue)
    issue_dict["theses"] = json.loads(issue_dict.pop("theses_json") or "[]")
    materials = []
    for row in conn.execute("SELECT * FROM materials WHERE radar_issue_date = ? ORDER BY key_material DESC, title", (issue["issue_date"],)):
        materials.append(material_payload(conn, row))
    return {
        "site": {"title": "Радар агентного проектного управления"},
        "issue": issue_dict,
        "daily_analysis": daily_analysis_payload(conn, issue["issue_date"]),
        "issue_llm_theses": issue_llm_theses_payload(conn, issue["issue_date"]),
        "issue_stats": issue_stats(conn, issue["issue_date"]),
        "stats": {
            "yesterday": stats(conn, "yesterday"),
            "day": stats(conn, "day"),
            "7d": stats(conn, "7d"),
            "30d": stats(conn, "30d"),
        },
        "period_theses": period_theses_payload(conn, issue["issue_date"]),
        "date_quality": date_quality_summary(conn),
        "materials": materials,
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        conn = connect()
        try:
            if parsed.path == "/api/health":
                self.send_json({"ok": True, "db": str(DB_PATH)})
            elif parsed.path in {"/api/latest", "/api/issue/latest"}:
                self.send_json(latest(conn))
            elif parsed.path == "/api/materials":
                self.send_json({"materials": material_rows(conn, params)})
            elif parsed.path == "/api/search":
                self.send_json({"materials": material_rows(conn, params)})
            elif parsed.path == "/api/stats":
                period = (params.get("period") or ["30d"])[0]
                self.send_json(stats(conn, period))
            elif parsed.path == "/api/internal/date-quality":
                self.send_json({"summary": date_quality_summary(conn), "queue": date_quality_queue(conn, params)})
            elif parsed.path == "/api/timeseries":
                days = int((params.get("days") or ["30"])[0])
                basis = (params.get("basis") or ["issue"])[0]
                if basis == "publication":
                    self.send_json({"timeseries": publication_timeseries(conn, days)})
                else:
                    self.send_json({"timeseries": issue_timeseries(conn, days)})
            elif parsed.path == "/api/rubrics":
                period = (params.get("period") or ["30d"])[0]
                period_sql, period_values = period_filter_sql("m", period)
                date_join = f"AND {period_sql}" if period_sql else ""
                rows = conn.execute(
                    f"""
                    SELECT r.id, r.title, count(m.id) count, avg(CASE WHEN m.id IS NOT NULL THEN mr.confidence END) confidence
                    FROM rubrics r
                    LEFT JOIN material_rubrics mr ON mr.rubric_id = r.id
                    LEFT JOIN materials m ON m.id = mr.material_id {date_join}
                    GROUP BY r.id, r.title, r.sort_order
                    ORDER BY r.sort_order
                    """,
                    period_values,
                ).fetchall()
                self.send_json({"rubrics": [row_to_dict(row) for row in rows]})
            elif parsed.path == "/api/sources":
                period = (params.get("period") or ["30d"])[0]
                self.send_json({"sources": source_rows(conn, period)})
            elif parsed.path == "/api/period-theses":
                issue_date = (params.get("issue_date") or [""])[0]
                if not issue_date:
                    issue = conn.execute("SELECT issue_date FROM issues ORDER BY issue_date DESC LIMIT 1").fetchone()
                    issue_date = issue["issue_date"] if issue else ""
                period = (params.get("period") or [""])[0]
                payload = period_theses_payload(conn, issue_date) if issue_date else {}
                self.send_json(payload.get(period) if period in payload else {"period_theses": payload})
            elif parsed.path == "/api/issues":
                limit = int((params.get("limit") or ["20"])[0])
                rows = conn.execute("SELECT * FROM issues ORDER BY issue_date DESC LIMIT ?", (limit,)).fetchall()
                self.send_json({"issues": [row_to_dict(row) for row in rows]})
            elif parsed.path.startswith("/api/issue/"):
                issue_date = parsed.path.rsplit("/", 1)[-1]
                issue = conn.execute("SELECT * FROM issues WHERE issue_date = ?", (issue_date,)).fetchone()
                if not issue:
                    self.send_json({"error": "issue not found"}, 404)
                else:
                    issue_dict = row_to_dict(issue)
                    issue_dict["theses"] = parse_json_list(issue_dict.pop("theses_json") or "[]")
                    materials = [
                        material_payload(conn, row)
                        for row in conn.execute("SELECT * FROM materials WHERE radar_issue_date = ? ORDER BY key_material DESC, title", (issue_date,))
                    ]
                    self.send_json({
                        "issue": issue_dict,
                        "daily_analysis": daily_analysis_payload(conn, issue_date),
                        "issue_llm_theses": issue_llm_theses_payload(conn, issue_date),
                        "materials": materials,
                    })
            else:
                self.send_json({"error": "not found"}, 404)
        finally:
            conn.close()

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Radar API listening on http://{HOST}:{PORT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
