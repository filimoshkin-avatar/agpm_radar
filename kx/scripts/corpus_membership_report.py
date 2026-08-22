#!/usr/bin/env python3
"""Reconcile every store that counts Radar materials and print the corpus-membership report.

Read-only everywhere: Legacy is opened with ``mode=ro`` plus ``query_only``, the V2 release
with ``immutable=1``, the file stores are only read, and KX arrives as the JSON that
``corpus_membership_kx_extract.sql`` produced under a read-only transaction. Nothing here
can write to a production store.

    python3 scripts/corpus_membership_report.py \
        --legacy-db /mnt/vdd/Radar/data/db/radar.sqlite \
        --v2-content-root /var/lib/radar-v2/content \
        --knowledge-root /root/.openclaw-projectmanager/workspace/knowledge/agpm-radar \
        --kx-extract kx-extract.json \
        --output corpus-membership-report.json

Exits non-zero when any check fails, so it can gate a commit or a scheduled reconciliation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from radar_kx.corpus_membership import (
    active_release_database,
    build_report,
    load_discovery,
    load_fulltext,
    load_legacy,
    load_legacy_source_metadata_urls,
    load_v2_release,
)


def _summary(report: dict[str, Any]) -> str:
    layers = report["layers"]
    lines = [
        "Radar corpus membership",
        "",
        f"  discovery registry   {layers['discovery']['records']:>6} records"
        f"  ({layers['discovery']['distinctCanonicalUrls']} distinct URLs)",
        f"  legacy editorial     {layers['legacy']['rows']:>6} rows"
        f"  ({layers['legacy']['distinctCanonicalUrls']} distinct URLs)",
        f"  v2 release materials {layers['v2Release']['materials']:>6}",
        f"  v2 issue selection   {layers['v2Release']['selectionRows']:>6} rows"
        f"  ({layers['v2Release']['selectionDistinctCanonicalUrls']} distinct URLs)",
    ]
    if layers["kx"] is not None:
        lines.append(
            f"  kx perimeter         {layers['kx']['currentPerimeterDocuments']:>6} documents"
            f"  (corpus {layers['kx']['counts']['documents']})"
        )
    lines.append(f"  full-text cache      {layers['fulltext']['files']:>6} files")
    lines.append("")
    for check in report["checks"]:
        mark = "ok  " if check["ok"] else "FAIL"
        lines.append(
            f"  [{mark}] {check['name']}: expected {check['expected']}, got {check['actual']}"
        )
    lines.append("")
    lines.append(f"  status: {report['status']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(prog="corpus_membership_report")
    parser.add_argument("--legacy-db", required=True, type=Path)
    release = parser.add_mutually_exclusive_group(required=True)
    release.add_argument("--v2-content-root", type=Path)
    release.add_argument("--v2-release-db", type=Path)
    parser.add_argument("--knowledge-root", required=True, type=Path)
    parser.add_argument("--kx-extract", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    release_db = (
        args.v2_release_db
        if args.v2_release_db is not None
        else active_release_database(args.v2_content_root)
    )
    discovery_path = args.knowledge_root / "data" / "materials.jsonl"
    fulltext_dir = args.knowledge_root / "data" / "source-fulltext"

    legacy = load_legacy(args.legacy_db)
    legacy_metadata_urls = load_legacy_source_metadata_urls(args.legacy_db)
    release_content = load_v2_release(release_db)
    discovery = load_discovery(discovery_path)
    fulltext = load_fulltext(fulltext_dir)
    kx = json.loads(args.kx_extract.read_text(encoding="utf-8")) if args.kx_extract else None

    report = build_report(
        legacy=legacy,
        legacy_metadata_urls=legacy_metadata_urls,
        release=release_content,
        discovery=discovery,
        fulltext=fulltext,
        kx=kx,
        inputs={
            "legacyDb": str(args.legacy_db),
            "v2ReleaseDb": str(release_db),
            "discoveryRegistry": str(discovery_path),
            "fullTextDirectory": str(fulltext_dir),
            "kxExtract": str(args.kx_extract) if args.kx_extract else "(absent)",
        },
    )
    if args.output is not None:
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if not args.quiet:
        print(_summary(report))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
