#!/usr/bin/env python3
"""Export deterministic, sanitized Legacy Radar fixtures from a read-only SQLite DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_CASES = {
    "normal-latest": "2026-08-19",
    "deterministic-fallback": "2026-08-15",
    "empty-issue": "2026-07-26",
    "high-volume": "2026-08-04",
}


def parse_json(value: str | None, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback
    return parsed


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    result = conn.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {result[0] if result else 'no result'}")
    return conn


def issue_payload(conn: sqlite3.Connection, issue_date: str) -> dict[str, Any]:
    issue = conn.execute(
        """
        SELECT issue_date, issue_number, title, brief, theses_json,
               status, published_at, created_at, updated_at
        FROM issues
        WHERE issue_date = ?
        """,
        (issue_date,),
    ).fetchone()
    if issue is None:
        raise RuntimeError(f"Fixture issue does not exist: {issue_date}")

    issue_data = dict(issue)
    issue_data["theses"] = parse_json(issue_data.pop("theses_json"), [])

    materials: list[dict[str, Any]] = []
    material_rows = conn.execute(
        """
        SELECT id, title, url, canonical_url, source_name, source_id,
               published_at, first_seen_at, radar_issue_date,
               publication_date_source, publication_date_confidence,
               publication_date_status, perimeter, verdict, summary,
               agpm_takeaway, governance_flag, security_flag,
               human_in_the_loop_flag, pmo_flag, isup_flag, mcp_flag,
               key_material, brief, theses_json, trend_notes,
               signal_score, signal_strength, created_at, updated_at
        FROM materials
        WHERE radar_issue_date = ?
        ORDER BY key_material DESC, title, id
        """,
        (issue_date,),
    ).fetchall()

    for material_row in material_rows:
        material = dict(material_row)
        material["theses"] = parse_json(material.pop("theses_json"), [])
        material["rubrics"] = [
            dict(row)
            for row in conn.execute(
                """
                SELECT rubric_id, confidence, source
                FROM material_rubrics
                WHERE material_id = ?
                ORDER BY confidence DESC, rubric_id
                """,
                (material["id"],),
            )
        ]
        quality = conn.execute(
            """
            SELECT source_host, publication_date_status,
                   issue_date_delta_days, severity, review_status, reason
            FROM material_date_quality
            WHERE material_id = ?
            """,
            (material["id"],),
        ).fetchone()
        material["date_quality"] = row_dict(quality)
        llm_summary = conn.execute(
            """
            SELECT issue_date, short_text, agpm_angle, provider, model,
                   prompt_version, status, error, created_at, updated_at
            FROM material_llm_summaries
            WHERE material_id = ?
            """,
            (material["id"],),
        ).fetchone()
        material["llm_summary"] = row_dict(llm_summary)
        materials.append(material)

    stats = conn.execute(
        """
        SELECT stat_date, viewed, included, cut, near, mid, far,
               core, adjacent, updated_at
        FROM daily_stats
        WHERE stat_date = ?
        """,
        (issue_date,),
    ).fetchone()

    daily_analysis = conn.execute(
        """
        SELECT issue_date, headline, analysis_json, provider, model,
               prompt_version, status, error, created_at, updated_at
        FROM issue_daily_analysis
        WHERE issue_date = ?
        """,
        (issue_date,),
    ).fetchone()
    daily_analysis_data = row_dict(daily_analysis)
    if daily_analysis_data is not None:
        daily_analysis_data["analysis"] = parse_json(
            daily_analysis_data.pop("analysis_json"), {}
        )

    llm_theses = conn.execute(
        """
        SELECT issue_date, theses_json, brief, provider, model,
               prompt_version, status, error, created_at, updated_at
        FROM issue_llm_theses
        WHERE issue_date = ?
        """,
        (issue_date,),
    ).fetchone()
    llm_theses_data = row_dict(llm_theses)
    if llm_theses_data is not None:
        llm_theses_data["theses"] = parse_json(
            llm_theses_data.pop("theses_json"), []
        )

    period_theses: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT as_of_issue_date, period, start_issue_date, end_issue_date,
               issue_count, material_count, stats_json, theses_json, brief,
               provider, model, prompt_version, created_at, updated_at
        FROM issue_period_theses
        WHERE as_of_issue_date = ?
        ORDER BY period
        """,
        (issue_date,),
    ):
        item = dict(row)
        item["stats"] = parse_json(item.pop("stats_json"), {})
        item["theses"] = parse_json(item.pop("theses_json"), [])
        period_theses.append(item)

    return {
        "fixtureContractVersion": 1,
        "issue": issue_data,
        "stats": row_dict(stats),
        "dailyAnalysis": daily_analysis_data,
        "issueLlmTheses": llm_theses_data,
        "periodTheses": period_theses,
        "materials": materials,
    }


def write_json(path: Path, payload: Any) -> str:
    body = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return sha256_bytes(body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", type=Path, default=Path("/mnt/vdd/Radar/data/db/radar.sqlite")
    )
    parser.add_argument(
        "--out", type=Path, default=Path("fixtures/legacy-baseline")
    )
    args = parser.parse_args()

    db_bytes = args.db.read_bytes()
    conn = connect_read_only(args.db)
    try:
        files: list[dict[str, Any]] = []
        for case, issue_date in DEFAULT_CASES.items():
            filename = f"{case}-{issue_date}.json"
            payload = issue_payload(conn, issue_date)
            digest = write_json(args.out / filename, payload)
            files.append(
                {
                    "case": case,
                    "issueDate": issue_date,
                    "path": filename,
                    "sha256": digest,
                    "materialCount": len(payload["materials"]),
                    "dailyAnalysisStatus": (
                        payload["dailyAnalysis"] or {}
                    ).get("status", "missing"),
                    "issueLlmStatus": (
                        payload["issueLlmTheses"] or {}
                    ).get("status", "missing"),
                }
            )

        migrations = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        db_summary = dict(
            conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM issues) AS issues,
                  (SELECT count(*) FROM materials) AS materials,
                  (SELECT count(*) FROM daily_stats) AS dailyStats,
                  (SELECT min(issue_date) FROM issues) AS firstIssueDate,
                  (SELECT max(issue_date) FROM issues) AS lastIssueDate,
                  (SELECT max(updated_at) FROM issues) AS maxIssueUpdatedAt
                """
            ).fetchone()
        )
        manifest = {
            "fixtureSetVersion": 1,
            "source": {
                "sqliteVersion": sqlite3.sqlite_version,
                "databaseSha256": sha256_bytes(db_bytes),
                "databaseBytes": len(db_bytes),
                "schemaMigrations": migrations,
                "summary": db_summary,
            },
            "sanitization": {
                "excluded": [
                    "report_md_path",
                    "report_docx_path",
                    "docx_source_path",
                    "md_source_path",
                    "request_path",
                    "response_path",
                    "diagnostic_json",
                    "rejected_materials_internal",
                    "raw provider payloads",
                    "secrets and OAuth data",
                ]
            },
            "files": files,
        }
        write_json(args.out / "manifest.json", manifest)
    finally:
        conn.close()

    print(f"Exported {len(DEFAULT_CASES)} fixtures to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
