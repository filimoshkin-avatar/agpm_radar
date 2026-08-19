#!/usr/bin/env python3
"""Export public Radar API cache files from SQLite."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from radar_paths import DB_PATH, JSON_CACHE_DIR, ensure_dirs
from agpm_radar_signal_strength import signal_label


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def validate_rubric_links(conn: sqlite3.Connection) -> None:
    materials_count = conn.execute("SELECT count(*) FROM materials").fetchone()[0]
    links_count = conn.execute("SELECT count(*) FROM material_rubrics").fetchone()[0]
    if materials_count and not links_count:
        raise RuntimeError("materials exist, but material_rubrics is empty; run agpm_radar_llm_classify.py before export")


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
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
    summary["top_review_hosts"] = [row_dict(row) for row in hosts]
    return summary


def material_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = row_dict(row)
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
    item["date_quality"] = row_dict(quality) if quality else None
    llm_summary = conn.execute(
        """
        SELECT short_text, agpm_angle, provider, model, prompt_version, status, updated_at
        FROM material_llm_summaries
        WHERE material_id = ?
        """,
        (item["id"],),
    ).fetchone()
    item["llm_summary"] = row_dict(llm_summary) if llm_summary else None
    return item


def empty_stats() -> dict[str, int]:
    return {key: 0 for key in ["viewed", "included", "cut", "near", "mid", "far", "core", "adjacent"]}


def rejected_facts(conn: sqlite3.Connection, start: str | None, exact_day: bool = False) -> list[dict[str, Any]]:
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
        if exact_day and published != start:
            continue
        if not exact_day and published < start:
            continue
        facts.append({
            "published_at": published,
            "source_name": data.get("source_name") or "Внутренний отсев",
        })
    return facts


def add_material_counts(acc: dict[str, int], row: sqlite3.Row) -> None:
    acc["included"] += 1
    acc["viewed"] += 1
    if row["perimeter"] in {"near", "mid", "far"}:
        acc[row["perimeter"]] += 1
    if row["verdict"] in {"core", "adjacent"}:
        acc[row["verdict"]] += 1


def add_rejected_count(acc: dict[str, int]) -> None:
    acc["viewed"] += 1
    acc["cut"] += 1


def publication_stats(conn: sqlite3.Connection, period: str) -> dict[str, int]:
    summary = empty_stats()
    period_sql, period_values = publication_filter_sql("m", period)
    if not period_sql:
        return summary
    for material in conn.execute(
        f"SELECT perimeter, verdict FROM materials m WHERE {period_sql}",
        period_values,
    ):
        add_material_counts(summary, material)
    start = period_start(period)
    for _ in rejected_facts(conn, start, exact_day=period in {"day", "yesterday"}):
        add_rejected_count(summary)
    return summary


def publication_stats_for_day(conn: sqlite3.Connection, day: str) -> dict[str, int]:
    summary = empty_stats()
    for material in conn.execute(
        "SELECT perimeter, verdict FROM materials m WHERE date(m.published_at) = ?",
        (day,),
    ):
        add_material_counts(summary, material)
    for _ in rejected_facts(conn, day, exact_day=True):
        add_rejected_count(summary)
    return summary


def issue_stats(conn: sqlite3.Connection, issue_date: str) -> dict[str, int]:
    row = conn.execute("SELECT * FROM daily_stats WHERE stat_date = ?", (issue_date,)).fetchone()
    if row:
        return {key: int(row[key]) for key in ["viewed", "included", "cut", "near", "mid", "far", "core", "adjacent"]}
    summary = empty_stats()
    for material in conn.execute("SELECT perimeter, verdict FROM materials WHERE radar_issue_date = ?", (issue_date,)):
        add_material_counts(summary, material)
    return summary


def period_issue_stats(conn: sqlite3.Connection, period: str) -> dict[str, int]:
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


def publication_timeseries(conn: sqlite3.Connection, days: int) -> list[dict[str, Any]]:
    days = max(1, min(days, 90))
    today = date.today()
    start = today - timedelta(days=days - 1)
    result: list[dict[str, Any]] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        row = publication_stats_for_day(conn, day)
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
    source_filter, source_values = period_filter_sql("m", period)
    source_where = f"WHERE {source_filter}" if source_filter else ""
    rows = conn.execute(
        f"SELECT source_name name, count(*) included FROM materials m {source_where} GROUP BY source_name",
        source_values,
    ).fetchall()
    merged: dict[str, dict[str, int | str]] = {}
    for row in rows:
        name = row["name"] or "Неизвестный источник"
        included = int(row["included"] or 0)
        merged[name] = {"name": name, "included": included, "cut": 0, "collected": included}
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
        item = row_dict(row)
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
    item = row_dict(row)
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
    item = row_dict(row)
    item["theses"] = parse_json_list(item.pop("theses_json") or "[]")
    return item


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


def period_filter_sql(alias: str, period: str) -> tuple[str, list[Any]]:
    start = period_start(period)
    if not start:
        return "", []
    field = f"{alias}.radar_issue_date"
    if period in {"day", "yesterday"}:
        return f"date({field}) = ?", [start]
    return f"{field} >= ?", [start]


def publication_filter_sql(alias: str, period: str) -> tuple[str, list[Any]]:
    start = period_start(period)
    if not start:
        return "", []
    field = f"{alias}.published_at"
    if period in {"day", "yesterday"}:
        return f"date({field}) = ?", [start]
    return f"{field} IS NOT NULL AND date({field}) >= ?", [start]


def materials_for_issue(conn: sqlite3.Connection, issue_date: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM materials
        WHERE radar_issue_date = ?
        ORDER BY key_material DESC, perimeter, title
        """,
        (issue_date,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(material_payload(conn, row))
    return result


def issue_payload(conn: sqlite3.Connection, issue_date: str) -> dict[str, Any] | None:
    issue = conn.execute("SELECT * FROM issues WHERE issue_date = ?", (issue_date,)).fetchone()
    if not issue:
        return None
    issue_data = row_dict(issue)
    issue_data["theses"] = parse_json_list(issue_data.pop("theses_json") or "[]")
    return {
        "issue": issue_data,
        "daily_analysis": daily_analysis_payload(conn, issue_date),
        "issue_llm_theses": issue_llm_theses_payload(conn, issue_date),
        "materials": materials_for_issue(conn, issue_date),
    }


def export(db: Path, out_dir: Path) -> None:
    ensure_dirs()
    out_dir.mkdir(parents=True, exist_ok=True)
    issues_dir = out_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        validate_rubric_links(conn)
        latest = conn.execute("SELECT issue_date FROM issues ORDER BY issue_date DESC LIMIT 1").fetchone()
        if latest:
            payload = issue_payload(conn, latest["issue_date"]) or {}
            today = date.today()
            payload["site"] = {"title": "Радар агентного проектного управления"}
            payload["issue_stats"] = issue_stats(conn, latest["issue_date"])
            payload["stats"] = {
                "yesterday": period_issue_stats(conn, "yesterday"),
                "day": period_issue_stats(conn, "day"),
                "7d": period_issue_stats(conn, "7d"),
                "30d": period_issue_stats(conn, "30d"),
            }
            payload["period_theses"] = period_theses_payload(conn, latest["issue_date"])
            payload["date_quality"] = date_quality_summary(conn)
            (out_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            (out_dir / "period-theses.json").write_text(json.dumps(payload["period_theses"], ensure_ascii=False, indent=2), encoding="utf-8")
        for row in conn.execute("SELECT issue_date FROM issues ORDER BY issue_date"):
            payload = issue_payload(conn, row["issue_date"])
            if payload:
                (issues_dir / f"{row['issue_date']}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "timeseries.json").write_text(json.dumps(issue_timeseries(conn, 30), ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "publication-timeseries.json").write_text(json.dumps(publication_timeseries(conn, 30), ensure_ascii=False, indent=2), encoding="utf-8")
        rubric_filter, rubric_values = period_filter_sql("m", "30d")
        date_join = f"AND {rubric_filter}" if rubric_filter else ""
        rubrics = conn.execute(
            f"""
            SELECT r.id, r.title, count(m.id) count, avg(CASE WHEN m.id IS NOT NULL THEN mr.confidence END) confidence
            FROM rubrics r
            LEFT JOIN material_rubrics mr ON mr.rubric_id = r.id
            LEFT JOIN materials m ON m.id = mr.material_id {date_join}
            GROUP BY r.id, r.title, r.sort_order
            ORDER BY r.sort_order
            """,
            rubric_values,
        ).fetchall()
        (out_dir / "rubrics.json").write_text(json.dumps([row_dict(row) for row in rubrics], ensure_ascii=False, indent=2), encoding="utf-8")
        sources = source_rows(conn, "30d")
        (out_dir / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "date-quality-summary.json").write_text(json.dumps(date_quality_summary(conn), ensure_ascii=False, indent=2), encoding="utf-8")
        index = {
            "issues": [row["issue_date"] for row in conn.execute("SELECT issue_date FROM issues ORDER BY issue_date DESC")],
            "generated_from": str(db),
        }
        (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--out", type=Path, default=JSON_CACHE_DIR)
    args = parser.parse_args()
    export(args.db, args.out)
    print(f"Exported public cache to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
