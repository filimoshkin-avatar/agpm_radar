"""Build a manual Stage 14 correction from one accepted V2 issue aggregate."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import cast

from packages.delta.engine import inspect_release_database
from packages.domain.candidate_mutations import issue_state_hash
from packages.domain.candidate_package import build_candidate_package
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.storage.replication_mutations import row_after_sha256
from packages.storage.safe_files import atomic_write_new
from packages.validation.public_issue import build_public_issue_from_views


def _json(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("expected JSON text in accepted source database")
    return json.loads(value)


def _desired_issue(
    connection: sqlite3.Connection,
    *,
    issue_date: str,
    remove_material_ids: frozenset[str],
    no_llm: bool,
) -> JsonObject:
    public = build_public_issue_from_views(connection, issue_date=issue_date)
    issue = connection.execute(
        """
        SELECT issue_id, lifecycle_status, publication_origin, empty_reason
        FROM issues WHERE issue_date = ?
        """,
        (issue_date,),
    ).fetchone()
    if issue is None:
        raise ValueError(f"accepted issue is absent: {issue_date}")
    issue_id, lifecycle_status, publication_origin, empty_reason = issue
    materials: list[JsonObject] = []
    for raw in cast(list[dict[str, object]], public["materials"]):
        material_id = cast(str, raw["id"])
        if material_id in remove_material_ids:
            continue
        private = connection.execute(
            """
            SELECT im.flags_json, ma.short_text, ma.agpm_angle, ma.llm_status
            FROM issue_materials AS im
            LEFT JOIN material_analysis AS ma
              ON ma.issue_id = im.issue_id AND ma.material_id = im.material_id
            WHERE im.issue_id = ? AND im.material_id = ?
            """,
            (issue_id, material_id),
        ).fetchone()
        if private is None:
            raise ValueError(f"accepted material aggregate is incomplete: {material_id}")
        flags_json, short_text, agpm_angle, _material_llm_status = private
        public_llm = cast(dict[str, object], raw["llm"])
        raw_flags = _json(flags_json)
        if not isinstance(raw_flags, dict):
            raise ValueError(f"accepted material flags are not an object: {material_id}")
        materials.append(
            cast(
                JsonObject,
                {
                    "agpmTakeaway": raw["agpmTakeaway"],
                    "brief": raw["brief"],
                    "canonicalUrl": raw["canonicalUrl"],
                    "flags": sorted(key for key, enabled in raw_flags.items() if enabled is True),
                    "keyMaterial": raw["keyMaterial"],
                    "llmAgpmAngle": None if no_llm else agpm_angle,
                    "llmShortText": None if no_llm else short_text,
                    "llmStatus": "unavailable" if no_llm else public_llm["status"],
                    "materialId": material_id,
                    "perimeter": raw["perimeter"],
                    "position": len(materials) + 1,
                    "publicationDateStatus": raw["publicationDateStatus"],
                    "publishedAt": raw["publishedAt"],
                    "rubrics": raw["rubrics"],
                    "signalScore": raw["signalScore"],
                    "signalStrength": raw["signalStrength"],
                    "sourceName": raw["sourceName"],
                    "summary": raw["summary"],
                    "theses": raw["theses"],
                    "title": raw["title"],
                    "trendNotes": raw["trendNotes"],
                    "url": raw["url"],
                    "verdict": raw["verdict"],
                },
            )
        )
    missing = remove_material_ids - {
        cast(str, item["id"]) for item in cast(list[dict[str, object]], public["materials"])
    }
    if missing:
        raise ValueError(f"requested removal is absent from issue: {sorted(missing)!r}")
    stats = cast(dict[str, int], dict(cast(dict[str, object], public["stats"])))
    stats.update(
        {
            "adjacent": sum(item["verdict"] == "adjacent" for item in materials),
            "core": sum(item["verdict"] == "core" for item in materials),
            "far": sum(item["perimeter"] == "far" for item in materials),
            "included": len(materials),
            "mid": sum(item["perimeter"] == "mid" for item in materials),
            "near": sum(item["perimeter"] == "near" for item in materials),
        }
    )
    stats["cut"] = stats["viewed"] - stats["included"]
    public_analysis = cast(dict[str, object], public["analysis"])
    analysis = cast(
        JsonObject,
        {
            "blocks": public_analysis["blocks"],
            "brief": public_analysis["brief"],
            "headline": public_analysis["headline"],
            "theses": public["theses"],
        },
    )
    brief = public["brief"]
    if remove_material_ids:
        brief = (
            f"Историческая коррекция: удалено подтверждённых дублей — "
            f"{len(remove_material_ids)}; материалов после коррекции — {len(materials)}."
        )
        analysis = {
            "blocks": [
                {
                    "kind": "overview",
                    "text": brief,
                    "title": "Историческая коррекция",
                }
            ],
            "brief": brief,
            "headline": "Подтверждённые дубли удалены из исторического выпуска",
            "theses": [],
        }
    return cast(
        JsonObject,
        {
            "analysis": analysis,
            "brief": brief,
            "emptyReason": empty_reason if materials else "no_qualifying_materials",
            "issueDate": issue_date,
            "issueId": issue_id,
            "issueNumber": public["issueNumber"],
            "lifecycleStatus": lifecycle_status,
            "materials": materials,
            "publicationOrigin": publication_origin,
            "publishedAt": public["publishedAt"],
            "stats": stats,
            "title": public["title"],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--projection-db", type=Path)
    parser.add_argument("--issue-date", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--remove-material-id", action="append", default=[])
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    base = inspect_release_database(args.source_db)
    args.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    staging = args.root / "staging"
    packages = args.root / "packages"
    staging.mkdir(mode=0o700)
    packages.mkdir(mode=0o700)
    projection_db = args.projection_db or args.source_db
    with sqlite3.connect(projection_db) as projection:
        desired = _desired_issue(
            projection,
            issue_date=args.issue_date,
            remove_material_ids=frozenset(args.remove_material_id),
            no_llm=args.no_llm,
        )
    with sqlite3.connect(args.source_db) as connection:
        issue_id = cast(str, desired["issueId"])
        expected_issue_hash = issue_state_hash(connection, issue_id)
        shared: list[JsonObject] = []
        for material in cast(list[dict[str, object]], desired["materials"]):
            material_id = cast(str, material["materialId"])
            links = connection.execute(
                "SELECT COUNT(*) FROM issue_materials WHERE material_id = ?", (material_id,)
            ).fetchone()[0]
            if links > 1:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(materials)")]
                row = connection.execute(
                    f"SELECT {', '.join(columns)} FROM materials WHERE material_id = ?",  # noqa: S608
                    (material_id,),
                ).fetchone()
                shared.append(
                    {
                        "materialId": material_id,
                        "rowSha256": row_after_sha256(dict(zip(columns, row, strict=True))),
                    }
                )
    if args.no_llm:
        llm: JsonObject = {
            "attempts": [
                {
                    "accepted": False,
                    "errorCode": "PROVIDER_UNAVAILABLE",
                    "model": "gpt-5.5",
                    "order": 1,
                    "provider": "openai",
                    "status": "error",
                }
            ],
            "deterministicFallback": {"implementation": "rules-daily", "version": "1"},
            "effective": None,
            "effectiveAttemptOrder": None,
            "requested": {"model": "gpt-5.5", "provider": "openai"},
            "status": "unavailable",
        }
    else:
        llm = {
            "attempts": [
                {
                    "accepted": False,
                    "errorCode": "PROVIDER_UNAVAILABLE",
                    "model": "gpt-5.5",
                    "order": 1,
                    "provider": "openai",
                    "status": "error",
                },
                {
                    "accepted": True,
                    "errorCode": None,
                    "model": "MiniMax-M3",
                    "order": 2,
                    "provider": "minimax",
                    "status": "success",
                },
            ],
            "deterministicFallback": None,
            "effective": {"model": "MiniMax-M3", "provider": "minimax"},
            "effectiveAttemptOrder": 2,
            "requested": {"model": "gpt-5.5", "provider": "openai"},
            "status": "fallback",
        }
    raw_candidate: dict[str, object] = {
        "candidateId": args.candidate_id,
        "contractVersion": "1.0.0",
        "createdAt": args.created_at,
        "desiredIssue": desired,
        "expectedBase": {
            "logicalStateHash": base.digest.state_hash,
            "releaseId": base.release.release_id,
            "sequence": base.release.sequence,
        },
        "expectedIssueStateHash": expected_issue_hash,
        "idempotencyKey": "idem_" + args.candidate_id,
        "initiator": {
            "actorId": "project-manager",
            "kind": "project-manager",
            "requestId": None,
        },
        "llmOutcome": llm,
        "operation": "correction",
        "reason": (
            "Stage 14 explicit no-LLM deterministic fallback dry run"
            if args.no_llm
            else "Stage 14 remove confirmed historical duplicate URLs"
        ),
        "schemaVersion": 1,
        "sharedMaterialPreconditions": shared,
        "targetIssueDate": args.issue_date,
    }
    candidate = cast(JsonObject, raw_candidate)
    candidate_path = args.root / "candidate.json"
    atomic_write_new(candidate_path, canonical_json_line(candidate), mode=0o600)
    result = build_candidate_package(
        source_database=args.source_db,
        staging_database=staging / "correction.sqlite",
        package_store=packages,
        candidate=candidate,
    )
    print(
        canonical_json_line(
            {
                "candidate": str(candidate_path),
                "package": str(result.package.path),
                "packageSha256": result.package.package_sha256,
                "staging": str(staging / "correction.sqlite"),
            }
        ).decode(),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
