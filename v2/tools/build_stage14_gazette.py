"""Build the approved Stage 14 update of the existing August gazette."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

from packages.delta.engine import inspect_release_database
from packages.domain.candidate_package import build_candidate_package
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.safe_files import atomic_write_new


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    base = inspect_release_database(args.source_db)
    with sqlite3.connect(args.source_db) as connection:
        current = connection.execute(
            """
            SELECT gazette_id, period, content_hash
            FROM gazettes WHERE period = '2026-08' AND lifecycle_status = 'published'
            """
        ).fetchone()
    if current is None:
        raise ValueError("accepted August gazette is absent")
    gazette_id, period, content_hash = current
    content = args.html.read_bytes()
    relative = "gazettes/2026-08/index.html"
    title = "Новости Агентного управления — Понедельник, 3 августа 2026"
    descriptor: JsonObject = {
        "bytes": len(content),
        "mediaType": "text/html",
        "relativePath": relative,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    candidate: JsonObject = {
        "candidateId": args.candidate_id,
        "contractVersion": "1.0.0",
        "createdAt": args.created_at,
        "expectedBase": {
            "logicalStateHash": base.digest.state_hash,
            "releaseId": base.release.release_id,
            "sequence": base.release.sequence,
        },
        "expectedGazette": {"contentHash": content_hash, "state": "present"},
        "gazetteId": gazette_id,
        "htmlEntrypoint": relative,
        "idempotencyKey": "idem_" + args.candidate_id,
        "initiator": {
            "actorId": "project-manager",
            "kind": "project-manager",
            "requestId": None,
        },
        "inputAssets": [descriptor],
        "llmOutcome": {
            "attempts": [],
            "deterministicFallback": None,
            "effective": None,
            "effectiveAttemptOrder": None,
            "requested": None,
            "status": "not_requested",
        },
        "operation": "gazette",
        "ownerRequestDigest": hashlib.sha256(content).hexdigest(),
        "period": period,
        "reason": "Stage 14 manual update of the accepted August gazette asset",
        "schemaVersion": 1,
        "title": title,
    }
    args.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    staging = args.root / "staging"
    packages = args.root / "packages"
    staging.mkdir(mode=0o700)
    packages.mkdir(mode=0o700)
    atomic_write_new(args.root / "candidate.json", canonical_json_line(candidate), mode=0o600)
    result = build_candidate_package(
        source_database=args.source_db,
        staging_database=staging / "gazette.sqlite",
        package_store=packages,
        candidate=candidate,
        assets={relative: content},
    )
    print(
        canonical_json_line(
            {
                "package": str(result.package.path),
                "packageSha256": result.package.package_sha256,
                "staging": str(staging / "gazette.sqlite"),
            }
        ).decode(),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
