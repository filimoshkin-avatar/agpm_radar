from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any

from radar_kx.artifact_import import (
    import_artifact,
    load_artifact_manifest,
    load_provenance_corrections,
    record_provenance_corrections,
)
from radar_kx.cache_import import import_caches
from radar_kx.canon_corpus import canon_summary, import_canon, scan_canon
from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.evaluation import evaluate, load_gold_set
from radar_kx.issue_perimeter import load_perimeter_export
from radar_kx.manifest import load_manifest
from radar_kx.reconciliation import load_inventory
from radar_kx.search import MATCH_MODES, SCOPES
from radar_kx.vertical_slice import load_candidates
from radar_kx.vertical_slice import select as select_slice
from radar_kx.worker import run_until_idle


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-kx")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_manifest_parser = subparsers.add_parser("import-manifest")
    import_manifest_parser.add_argument("path", type=Path)
    import_manifest_parser.add_argument("--source-name", default="materials.jsonl")

    # Rung seven of the acquisition ladder: material that arrived as a file.
    import_artifact_parser = subparsers.add_parser("import-artifact")
    import_artifact_parser.add_argument("--manifest", type=Path, required=True)

    # Load the AgPM canon and the external standards as their own corpus.
    import_canon_parser = subparsers.add_parser("import-canon")
    import_canon_parser.add_argument("--raw-dir", type=Path, required=True)
    import_canon_parser.add_argument(
        "--originals-dir",
        type=Path,
        help="where the Word and PDF originals live for sources held only as an excerpt",
    )
    import_canon_parser.add_argument("--source-name", default="agpm-canon")
    import_canon_parser.add_argument("--provided-by", default="project-manager")
    import_canon_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan and report what would be imported, without touching the store",
    )

    # Append provenance to versions that already exist, without touching them.
    record_provenance_parser = subparsers.add_parser("record-provenance")
    record_provenance_parser.add_argument("--file", type=Path, required=True)

    import_cache_parser = subparsers.add_parser("import-cache")
    import_cache_parser.add_argument("--metadata-dir", type=Path, required=True)
    import_cache_parser.add_argument("--fulltext-dir", type=Path, required=True)

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--scope", choices=sorted(SCOPES), default="current")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--match", choices=list(MATCH_MODES), default="all")

    subparsers.add_parser("coverage-report")

    # Compare a file-store inventory with KX and record the difference (P28).
    reconcile_parser = subparsers.add_parser("reconcile-stores")
    reconcile_parser.add_argument("--inventory", type=Path, required=True)

    # Choose the documents the vertical slice runs on, from the extract that
    # scripts/vertical_slice_candidates.sql produced.
    slice_parser = subparsers.add_parser("vertical-slice")
    slice_parser.add_argument("--candidates", type=Path, required=True)
    slice_parser.add_argument("--size", type=int, default=24)

    # Measure retrieval against a gold set. Prints numbers, never a pass mark.
    eval_parser = subparsers.add_parser("eval-retrieval")
    eval_parser.add_argument("--gold-set", type=Path, required=True)
    eval_parser.add_argument("--k", type=int, default=10)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--workers", type=int, default=8)

    subparsers.add_parser("status")

    failures_parser = subparsers.add_parser("failures")
    failures_parser.add_argument("--limit", type=int, default=100)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--full", action="store_true")

    requeue_parser = subparsers.add_parser("requeue-failed")
    requeue_parser.add_argument("--error-code")

    perimeter_import_parser = subparsers.add_parser("import-perimeter")
    perimeter_import_parser.add_argument("path", type=Path)

    subparsers.add_parser("perimeter-status")

    perimeter_gaps_parser = subparsers.add_parser("perimeter-gaps")
    perimeter_gaps_parser.add_argument("--limit", type=int, default=500)

    prepare_parser = subparsers.add_parser("perimeter-prepare")
    prepare_parser.add_argument("--robots-override-reason")
    prepare_parser.add_argument("--body-limit-bytes", type=int)
    prepare_parser.add_argument("--requeue", action="store_true")

    reparse_parser = subparsers.add_parser("perimeter-reparse")
    reparse_parser.add_argument("--reason", required=True)

    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = Settings.from_environment()
    database = Database(settings)

    if args.command == "import-manifest":
        manifest = load_manifest(args.path)
        _print_json(database.import_manifest(manifest, source_name=args.source_name))
        return
    if args.command == "import-artifact":
        artifact = load_artifact_manifest(args.manifest)
        _print_json(import_artifact(database, artifact).as_json())
        return
    if args.command == "import-canon":
        canon = scan_canon(args.raw_dir, originals_directory=args.originals_dir)
        if args.dry_run:
            _print_json(canon_summary(canon))
            return
        _print_json(
            import_canon(
                database,
                canon,
                source_name=args.source_name,
                recorded_by=f"radar-kx-import-canon:{args.source_name}",
                provided_by=args.provided_by,
            )
        )
        return
    if args.command == "record-provenance":
        recorded_by, corrections = load_provenance_corrections(args.file)
        _print_json(
            record_provenance_corrections(
                database, recorded_by=recorded_by, corrections=corrections
            )
        )
        return
    if args.command == "import-cache":
        cache_result = import_caches(
            database,
            metadata_dir=args.metadata_dir,
            fulltext_dir=args.fulltext_dir,
        )
        _print_json(dataclasses.asdict(cache_result))
        return
    if args.command == "run":
        _print_json(run_until_idle(settings, workers=args.workers))
        return
    if args.command == "search":
        _print_json(
            {
                "query": args.query,
                "scope": args.scope,
                "hits": [
                    hit.as_json()
                    for hit in database.search(
                        args.query, scope=args.scope, limit=args.limit, match=args.match
                    )
                ],
            }
        )
        return
    if args.command == "vertical-slice":
        payload = json.loads(args.candidates.read_text(encoding="utf-8"))
        _print_json(select_slice(load_candidates(payload), size=args.size).as_json())
        return
    if args.command == "eval-retrieval":
        name, questions = load_gold_set(args.gold_set)
        results, summary = evaluate(database.search, questions, k=args.k)
        _print_json(
            {
                "goldSet": name,
                "summary": summary,
                "results": [item.as_json() for item in results],
            }
        )
        return
    if args.command == "reconcile-stores":
        scope, entries, source = load_inventory(args.inventory)
        _print_json(
            database.record_store_reconciliation(
                scope,
                entries,
                source=source,
                generated_by=f"radar_kx reconcile-stores:{settings.release_id}",
            )
        )
        return
    if args.command == "coverage-report":
        coverage = database.coverage_report()
        _print_json(coverage)
        raise SystemExit(0 if coverage["status"] == "ok" else 1)
    if args.command == "status":
        _print_json(database.status())
        return
    if args.command == "failures":
        _print_json(list(database.iter_failures(limit=args.limit)))
        return
    if args.command == "verify":
        verification = database.verify(full=args.full)
        _print_json(verification)
        raise SystemExit(0 if verification["status"] == "ok" else 1)
    if args.command == "requeue-failed":
        _print_json(
            {
                "requeued": database.requeue_failed(error_code=args.error_code),
                "errorCode": args.error_code,
            }
        )
        return
    if args.command == "import-perimeter":
        _print_json(database.import_issue_perimeter(load_perimeter_export(args.path)))
        return
    if args.command == "perimeter-status":
        _print_json(database.perimeter_status())
        return
    if args.command == "perimeter-gaps":
        _print_json(list(database.iter_perimeter_gaps(limit=args.limit)))
        return
    if args.command == "perimeter-prepare":
        _print_json(
            database.prepare_perimeter(
                robots_override_reason=args.robots_override_reason,
                body_limit_bytes=args.body_limit_bytes,
                requeue=args.requeue,
            )
        )
        return
    if args.command == "perimeter-reparse":
        _print_json(
            database.reparse_perimeter_gaps(
                reason=args.reason,
                min_text_chars=settings.min_text_chars,
            )
        )
        return
    raise AssertionError(f"unhandled command: {args.command}")
