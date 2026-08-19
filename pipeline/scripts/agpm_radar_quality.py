#!/usr/bin/env python3
"""Build Radar data-quality diagnostics and search index."""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

from radar_paths import DB_PATH, JSON_CACHE_DIR, ensure_dirs


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def source_host(url: str | None) -> str:
    if not url:
        return ""
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def delta_days(conn: sqlite3.Connection, published_at: str | None, issue_date: str | None) -> int | None:
    if not published_at or not issue_date:
        return None
    row = conn.execute("SELECT CAST(julianday(?) - julianday(?) AS INTEGER)", (published_at, issue_date)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def diagnose(row: sqlite3.Row, delta: int | None, host: str, domain_rule: str | None) -> tuple[str, str, str]:
    status = row["publication_date_status"] or "unresolved"
    confidence = float(row["publication_date_confidence"] or 0)
    published_at = row["published_at"]
    issue_date = row["radar_issue_date"]

    if not published_at or status == "unresolved":
        return "high", "queued", "Дата публикации первоисточника не найдена."
    if status == "low_confidence" or confidence < 0.7:
        return "medium", "queued", "Дата найдена с низкой уверенностью."
    if delta is not None and delta > 0:
        return "high", "queued", "Дата публикации позднее даты выпуска радара."
    if delta is not None and abs(delta) > 365:
        return "medium", "queued", "Дата публикации отличается от даты выпуска больше чем на год."
    if delta is not None and abs(delta) > 90:
        return "low", "monitor", "Дата публикации сильно отличается от даты выпуска."
    if domain_rule and "manual" in domain_rule:
        return "low", "monitor", "Для домена задана ручная или полуавтоматическая проверка дат."
    return "ok", "ok", "Диагностических замечаний по дате нет."


def rebuild_fts_compat(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("DELETE FROM materials_fts")
    except sqlite3.OperationalError:
        return
    rows = conn.execute(
        """
        SELECT id, title, summary, agpm_takeaway, source_name, url
        FROM materials
        """
    ).fetchall()
    conn.executemany(
        """
        INSERT INTO materials_fts(material_id, title, summary, agpm_takeaway, source_name, url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["id"],
                row["title"] or "",
                row["summary"] or "",
                row["agpm_takeaway"] or "",
                row["source_name"] or "",
                row["url"] or "",
            )
            for row in rows
        ],
    )


def rebuild_quality(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT m.*
        FROM materials m
        ORDER BY COALESCE(m.published_at, m.radar_issue_date) DESC, m.title
        """
    ).fetchall()
    severity_counts: Counter[str] = Counter()
    review_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    host_counts: Counter[str] = Counter()
    queued = 0

    conn.execute("DELETE FROM material_date_quality")
    for row in rows:
        host = source_host(row["canonical_url"] or row["url"])
        rule = conn.execute("SELECT date_strategy FROM source_domain_rules WHERE host = ?", (host,)).fetchone()
        domain_rule = rule["date_strategy"] if rule else None
        delta = delta_days(conn, row["published_at"], row["radar_issue_date"])
        severity, review_status, reason = diagnose(row, delta, host, domain_rule)
        diagnostic = {
            "title": row["title"],
            "url": row["url"],
            "published_at": row["published_at"],
            "radar_issue_date": row["radar_issue_date"],
            "date_source": row["publication_date_source"],
            "confidence": row["publication_date_confidence"],
            "domain_rule": domain_rule,
        }
        conn.execute(
            """
            INSERT INTO material_date_quality(
              material_id, source_host, publication_date_status, issue_date_delta_days,
              severity, review_status, reason, diagnostic_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                row["id"],
                host,
                row["publication_date_status"] or "unresolved",
                delta,
                severity,
                review_status,
                reason,
                json.dumps(diagnostic, ensure_ascii=False),
            ),
        )
        severity_counts[severity] += 1
        review_counts[review_status] += 1
        status_counts[row["publication_date_status"] or "unresolved"] += 1
        if review_status == "queued":
            queued += 1
            host_counts[host or row["source_name"] or "unknown"] += 1

    return {
        "materials_total": len(rows),
        "queued_for_review": queued,
        "by_publication_date_status": dict(status_counts),
        "by_review_status": dict(review_counts),
        "by_severity": dict(severity_counts),
        "top_review_hosts": [{"host": host, "count": count} for host, count in host_counts.most_common(20)],
    }


def export_quality_summary(conn: sqlite3.Connection, summary: dict[str, Any], out_dir: Path) -> None:
    internal = out_dir / "internal"
    internal.mkdir(parents=True, exist_ok=True)
    queue = conn.execute(
        """
        SELECT q.material_id, m.title, m.url, m.source_name, m.published_at, m.radar_issue_date,
               q.source_host, q.publication_date_status, q.issue_date_delta_days,
               q.severity, q.review_status, q.reason
        FROM material_date_quality q
        JOIN materials m ON m.id = q.material_id
        WHERE q.review_status = 'queued'
        ORDER BY
          CASE q.severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
          abs(COALESCE(q.issue_date_delta_days, 0)) DESC,
          m.title
        LIMIT 500
        """
    ).fetchall()
    payload = {
        "summary": summary,
        "queue": [row_dict(row) for row in queue],
    }
    (internal / "date-quality.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--out", type=Path, default=JSON_CACHE_DIR)
    args = parser.parse_args()

    ensure_dirs()
    conn = connect(args.db)
    try:
        summary = rebuild_quality(conn)
        rebuild_fts_compat(conn)
        conn.commit()
        export_quality_summary(conn, summary, args.out)
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
