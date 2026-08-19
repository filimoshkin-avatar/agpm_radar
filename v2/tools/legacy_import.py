"""Create a disposable Radar V2 database and run the one-shot Legacy bootstrap import."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
from pathlib import Path

from packages.legacy_bridge.importer import GazetteInput, ImportReport, import_legacy
from packages.storage.hashing import file_sha256
from packages.storage.migrations import create_database


def _report_json(report: ImportReport, database: Path) -> dict[str, object]:
    result = dataclasses.asdict(report)
    result["database_file_sha256"] = file_sha256(database)
    result["database_bytes"] = database.stat().st_size
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-db", required=True, type=Path)
    parser.add_argument("--target-db", required=True, type=Path)
    parser.add_argument("--evidence-manifest", required=True, type=Path)
    parser.add_argument("--evidence-manifest-sha256", required=True)
    parser.add_argument("--imported-at", required=True)
    parser.add_argument("--deferred-queue", type=Path)
    parser.add_argument("--gazette-asset", type=Path)
    parser.add_argument("--gazette-relative-path")
    parser.add_argument("--gazette-period")
    parser.add_argument("--gazette-title")
    parser.add_argument("--gazette-published-at")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    gazette_values = (
        args.gazette_asset,
        args.gazette_relative_path,
        args.gazette_period,
        args.gazette_title,
        args.gazette_published_at,
    )
    if any(value is not None for value in gazette_values) and not all(
        value is not None for value in gazette_values
    ):
        parser.error("all five gazette arguments must be supplied together")
    gazette = (
        GazetteInput(
            path=args.gazette_asset,
            relative_path=args.gazette_relative_path,
            period=args.gazette_period,
            title=args.gazette_title,
            published_at=args.gazette_published_at,
        )
        if args.gazette_asset is not None
        else None
    )
    create_database(args.target_db, applied_at=args.imported_at)
    with sqlite3.connect(args.target_db) as target:
        report = import_legacy(
            legacy_db=args.legacy_db,
            target=target,
            evidence_manifest=args.evidence_manifest,
            expected_manifest_sha256=args.evidence_manifest_sha256,
            imported_at=args.imported_at,
            deferred_queue=args.deferred_queue,
            gazette=gazette,
        )
    for suffix in ("-journal", "-wal", "-shm"):
        if Path(str(args.target_db) + suffix).exists():
            raise RuntimeError(f"sealed database has forbidden sidecar: {suffix}")
    rendered = (
        json.dumps(
            _report_json(report, args.target_db), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
