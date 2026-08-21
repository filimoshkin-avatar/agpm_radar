from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any

from radar_kx.cache_import import import_caches
from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.manifest import load_manifest
from radar_kx.worker import run_until_idle


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-kx")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_manifest_parser = subparsers.add_parser("import-manifest")
    import_manifest_parser.add_argument("path", type=Path)
    import_manifest_parser.add_argument("--source-name", default="materials.jsonl")

    import_cache_parser = subparsers.add_parser("import-cache")
    import_cache_parser.add_argument("--metadata-dir", type=Path, required=True)
    import_cache_parser.add_argument("--fulltext-dir", type=Path, required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--workers", type=int, default=8)

    subparsers.add_parser("status")

    failures_parser = subparsers.add_parser("failures")
    failures_parser.add_argument("--limit", type=int, default=100)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--full", action="store_true")

    requeue_parser = subparsers.add_parser("requeue-failed")
    requeue_parser.add_argument("--error-code")

    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = Settings.from_environment()
    database = Database(settings)

    if args.command == "import-manifest":
        manifest = load_manifest(args.path)
        _print_json(database.import_manifest(manifest, source_name=args.source_name))
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
    raise AssertionError(f"unhandled command: {args.command}")
