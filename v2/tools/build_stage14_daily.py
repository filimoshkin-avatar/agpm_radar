"""Build a real manual daily candidate from a captured Legacy public response."""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import cast

from packages.contracts.analysis import clean_evidence_titles
from packages.delta.engine import inspect_release_database
from packages.domain.candidate_package import build_candidate_package
from packages.domain.dual_run import consume_snapshot_for_branch
from packages.domain.snapshot import JsonObject, canonical_json_line, create_snapshot
from packages.legacy_bridge.importer import deterministic_id
from packages.storage.safe_files import atomic_write_new

from tools.generate_v2_analysis import V2AnalysisError, generate_v2_analysis
from tools.v2_period_analysis import generate_period, period_blocks, strip_period_blocks


def _timestamp(value: object) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) == 10:
        return text + "T00:00:00Z"
    return text.replace(" ", "T").replace("+00:00", "Z")


def _material(raw: dict[str, object], position: int) -> JsonObject:
    summary = cast(dict[str, object] | None, raw.get("llm_summary"))
    llm_status = "unavailable"
    short_text = None
    agpm_angle = None
    if summary is not None and summary.get("status") == "success":
        llm_status = "success"
        short_text = summary.get("short_text")
        agpm_angle = summary.get("agpm_angle")
    flags = [
        name
        for name, field in (
            ("governance", "governance_flag"),
            ("security", "security_flag"),
            ("human_in_the_loop", "human_in_the_loop_flag"),
            ("pmo", "pmo_flag"),
            ("isup", "isup_flag"),
            ("mcp", "mcp_flag"),
        )
        if bool(raw.get(field))
    ]
    status = str(raw.get("publication_date_status") or "unresolved")
    published_at = _timestamp(raw.get("published_at")) if status != "unresolved" else None
    result: dict[str, object] = {
        "agpmTakeaway": raw.get("agpm_takeaway"),
        "brief": raw.get("brief"),
        "canonicalUrl": raw.get("canonical_url"),
        "flags": flags,
        "keyMaterial": bool(raw.get("key_material")),
        "llmAgpmAngle": agpm_angle,
        "llmShortText": short_text,
        "llmStatus": llm_status,
        "materialId": deterministic_id("material", str(raw["id"])),
        "perimeter": raw.get("perimeter")
        if raw.get("perimeter") in {"near", "mid", "far"}
        else "far",
        "position": position,
        "publicationDateStatus": status,
        "publishedAt": published_at,
        "rubrics": raw.get("rubrics") or [],
        "signalScore": raw.get("signal_score"),
        "signalStrength": raw.get("signal_strength")
        if raw.get("signal_strength") in {"strong", "context", "watch"}
        else "strong",
        "sourceName": raw.get("source_name"),
        "summary": raw.get("summary"),
        "theses": raw.get("theses") or [],
        "title": raw["title"],
        "trendNotes": raw.get("trend_notes"),
        "url": raw["url"],
        "verdict": raw.get("verdict") if raw.get("verdict") in {"core", "adjacent"} else "adjacent",
    }
    return cast(JsonObject, result)


def _material_count_word(count: int, *, prepositional: bool = False) -> str:
    if prepositional:
        return "материале" if count % 10 == 1 and count % 100 != 11 else "материалах"
    if count % 10 == 1 and count % 100 != 11:
        return "материал"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return "материала"
    return "материалов"


def _composition_sentence(stats: dict[str, int]) -> str:
    labels = (("near", "близкий"), ("mid", "средний"), ("far", "дальний"))
    parts = [f"{label} периметр — {stats[key]}" for key, label in labels if stats[key]]
    count = stats["included"]
    return f"В выпуске {count} {_material_count_word(count)}: {', '.join(parts)}."


def _reconcile_narrative(text: str, *, legacy_count: int, stats: dict[str, int]) -> str:
    """Remove Legacy count/perimeter claims invalidated by V2 eligibility filtering."""

    text = re.sub(r"В выпуске \d+ материал(?:ов|а)?: [^.]*\.", _composition_sentence(stats), text)
    count = stats["included"]
    text = re.sub(
        rf"\bВ {legacy_count} материалах\b",
        f"В {count} {_material_count_word(count, prepositional=True)}",
        text,
    )
    text = re.sub(
        rf"\b{legacy_count} материал(?:ов|а)?\b",
        f"{count} {_material_count_word(count)}",
        text,
    )
    return text


def _analysis(
    document: dict[str, object], *, legacy_count: int, stats: dict[str, int]
) -> JsonObject:
    daily = cast(dict[str, object], document["daily_analysis"])
    body = cast(dict[str, object], daily.get("analysis") or {})
    # The analysis is Legacy's finished LLM work, imported whole: the three
    # fields Legacy sends are the three blocks V2 shows. No field is searched
    # for where it never was - `risks` and `actions` were phantom keys, and
    # Legacy's "what next" field is `watch_next`, which becomes V2's `actions`
    # block (the block kind, not a Legacy field name).
    blocks: list[JsonObject] = []
    for kind, key, title in (
        ("overview", "signal", "Сигнал"),
        ("signals", "why_agpm", "Почему это важно для AgPM"),
        ("actions", "watch_next", "Что смотреть дальше"),
    ):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            blocks.append(
                {
                    "kind": kind,
                    "text": _reconcile_narrative(value, legacy_count=legacy_count, stats=stats),
                    "title": title,
                }
            )
    issue = cast(dict[str, object], document["issue"])
    llm_theses = cast(dict[str, object] | None, document.get("issue_llm_theses"))
    thesis_source = (
        cast(list[dict[str, object]], llm_theses.get("theses") or [])
        if llm_theses is not None and llm_theses.get("status") == "success"
        else cast(list[dict[str, object]], issue.get("theses") or [])
    )
    theses = [
        {
            "lead": _reconcile_narrative(
                str(item.get("lead") or ""), legacy_count=legacy_count, stats=stats
            ),
            "rest": _reconcile_narrative(
                str(item.get("rest") or ""), legacy_count=legacy_count, stats=stats
            ),
        }
        for item in thesis_source
    ]
    result: dict[str, object] = {
        "blocks": blocks,
        "brief": _reconcile_narrative(
            str(issue.get("brief") or ""), legacy_count=legacy_count, stats=stats
        ),
        "evidenceTitles": clean_evidence_titles(body.get("evidence_titles")),
        "headline": daily.get("headline"),
        "theses": theses,
    }
    return cast(JsonObject, result)


def _daily_analysis(
    document: dict[str, object],
    *,
    materials: list[JsonObject],
    issue_date: str,
    artifacts_root: Path,
    legacy_count: int,
    stats: dict[str, int],
    brief: str,
) -> tuple[JsonObject, str | None]:
    """The day's analysis, and the reason the V2-native one was not used.

    A rejected analysis is not a reason to leave the day without an issue. Legacy's
    imported analysis is what V2 published until 2026-08-28 and is still better than
    nothing; `_llm_outcome` records which of the two the issue was built from. This
    decision is a function rather than a branch inside `main` so that the fallback
    can be exercised: it had no caller at all for a day, and nothing noticed.
    """
    try:
        generated = generate_v2_analysis(
            issue_date=issue_date, materials=materials, artifacts_root=artifacts_root
        )
    except V2AnalysisError as error:
        return _analysis(document, legacy_count=legacy_count, stats=stats), str(error)
    legacy_daily = cast(dict[str, object], document["daily_analysis"])
    legacy_headline = str(legacy_daily.get("headline") or "").strip()
    if not legacy_headline:
        legacy_analysis = cast(dict[str, object], legacy_daily.get("analysis") or {})
        legacy_headline = str(legacy_analysis.get("headline") or "").strip()
    return (
        _native_analysis(
            cast(dict[str, object], generated),
            brief=brief,
            headline=legacy_headline,
        ),
        None,
    )


def _llm_outcome(*, native: bool) -> JsonObject:
    """The day's LLM record: what was asked for, and what the issue was actually built from.

    "unavailable" is this schema's word for "no model result, deterministic fallback
    used"; "fallback" means a second model answered, which is not what happens here.
    The record used to be a success literal regardless, so a fallback issue would have
    claimed a model wrote the analysis Legacy had.
    """
    requested = {"model": "gpt-5.5", "provider": "openai"}
    if native:
        return cast(
            JsonObject,
            {
                "attempts": [
                    {
                        "accepted": True,
                        "errorCode": None,
                        "model": "gpt-5.5",
                        "order": 1,
                        "provider": "openai",
                        "status": "success",
                    }
                ],
                "deterministicFallback": None,
                "effective": dict(requested),
                "effectiveAttemptOrder": 1,
                "requested": dict(requested),
                "status": "success",
            },
        )
    return cast(
        JsonObject,
        {
            "attempts": [
                {
                    "accepted": False,
                    "errorCode": "ANALYSIS_REJECTED",
                    "model": "gpt-5.5",
                    "order": 1,
                    "provider": "openai",
                    "status": "error",
                }
            ],
            "deterministicFallback": {"implementation": "legacy-analysis-import", "version": "1"},
            "effective": None,
            "effectiveAttemptOrder": None,
            "requested": dict(requested),
            "status": "unavailable",
        },
    )


def _native_analysis(
    generated: dict[str, object],
    *,
    brief: str,
    headline: str,
) -> JsonObject:
    """Map V2-native blocks while preserving Legacy's canonical issue headline."""
    return cast(
        JsonObject,
        {
            "blocks": [
                {"kind": "overview", "text": generated["signal"], "title": "Сигнал"},
                {
                    "kind": "signals",
                    "text": generated["why_agpm"],
                    "title": "Почему это важно для AgPM",
                },
                {
                    "kind": "actions",
                    "text": generated["watch_next"],
                    "title": "Что смотреть дальше",
                },
            ],
            "brief": brief,
            "evidenceMaterialIds": generated["evidence_material_ids"],
            "evidenceTitles": generated["evidence_titles"],
            "headline": headline,
            "inputContentHash": generated["input_content_hash"],
            "theses": generated["theses"],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-json", required=True, type=Path)
    parser.add_argument("--legacy-db", required=True, type=Path)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    document = cast(dict[str, object], json.loads(args.legacy_json.read_bytes()))
    issue = cast(dict[str, object], document["issue"])
    issue_date = str(issue["issue_date"])
    issue_day = date.fromisoformat(issue_date)
    earliest = issue_day - timedelta(days=30)
    eligible: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for item in cast(list[dict[str, object]], document["materials"]):
        status = str(item.get("publication_date_status") or "unresolved")
        published = _timestamp(item.get("published_at"))
        allowed = status == "unresolved" and published is None
        if status != "unresolved" and published is not None:
            published_day = date.fromisoformat(published[:10])
            allowed = earliest <= published_day <= issue_day
        if allowed:
            eligible.append(item)
        else:
            excluded.append(
                {
                    "id": item.get("id"),
                    "publicationDateStatus": status,
                    "publishedAt": published,
                    "reason": "outside_v2_30_day_window_or_inconsistent_date_status",
                    "title": item.get("title"),
                }
            )
    materials = [_material(item, index) for index, item in enumerate(eligible, 1)]
    with sqlite3.connect(f"file:{args.legacy_db}?mode=ro", uri=True) as legacy:
        legacy.row_factory = sqlite3.Row
        legacy.execute("PRAGMA query_only=ON")
        legacy_stats = dict(
            legacy.execute(
                """
                SELECT viewed, included, cut, near, mid, far, core, adjacent
                FROM daily_stats WHERE stat_date = ?
                """,
                (issue_date,),
            ).fetchone()
        )
    stats = {
        "adjacent": sum(1 for item in eligible if item.get("verdict") == "adjacent"),
        "core": sum(1 for item in eligible if item.get("verdict") == "core"),
        "cut": int(legacy_stats["viewed"]) - len(eligible),
        "far": sum(1 for item in eligible if item.get("perimeter") == "far"),
        "included": len(eligible),
        "mid": sum(1 for item in eligible if item.get("perimeter") == "mid"),
        "near": sum(1 for item in eligible if item.get("perimeter") == "near"),
        "viewed": int(legacy_stats["viewed"]),
    }
    legacy_count = len(cast(list[dict[str, object]], document["materials"]))
    reconciled_brief = _reconcile_narrative(
        str(issue.get("brief") or ""), legacy_count=legacy_count, stats=stats
    )
    # The source database is checked before the model is asked: a broken release
    # should not cost an inference first.
    base = inspect_release_database(args.source_db)
    args.root.mkdir(mode=0o700, parents=True, exist_ok=False)
    analysis, analysis_failure = _daily_analysis(
        document,
        materials=materials,
        issue_date=issue_date,
        artifacts_root=args.root / "llm-analysis",
        legacy_count=legacy_count,
        stats=stats,
        brief=reconciled_brief,
    )
    current_issue = cast(
        JsonObject,
        {
            "issueDate": issue_date,
            "materials": materials,
        },
    )
    period_root = args.root / "llm-period-analysis"
    period_7d = generate_period(
        database=args.source_db,
        anchor=issue_date,
        period="7d",
        artifacts_root=period_root,
        current_issue=current_issue,
    )
    period_30d = generate_period(
        database=args.source_db,
        anchor=issue_date,
        period="30d",
        artifacts_root=period_root,
        current_issue=current_issue,
        previous=cast(list[JsonObject], period_7d["theses"]),
    )
    analysis_blocks = cast(list[JsonObject], analysis["blocks"])
    cast(dict[str, object], analysis)["blocks"] = strip_period_blocks(
        analysis_blocks
    ) + period_blocks({"7d": period_7d, "30d": period_30d})
    atomic_write_new(
        args.root / "analysis-outcome.json",
        canonical_json_line(
            {
                "reason": analysis_failure,
                "source": "legacy-import" if analysis_failure else "v2-native",
                "periods": {
                    "7d": {
                        "attempts": period_7d["attempts"],
                        "model": period_7d["model"],
                        "status": period_7d["status"],
                    },
                    "30d": {
                        "attempts": period_30d["attempts"],
                        "model": period_30d["model"],
                        "status": period_30d["status"],
                    },
                },
            }
        ),
        mode=0o600,
    )
    snapshots = args.root / "snapshots"
    run_root = args.root / "run"
    staging = args.root / "staging"
    packages = args.root / "packages"
    staging.mkdir(mode=0o700)
    packages.mkdir(mode=0o700)
    snapshot = create_snapshot(
        snapshots,
        snapshot_id=f"snap_{issue_date.replace('-', '')}_stage14",
        collected_at=args.created_at,
        candidates=[{"issue": issue, "materials": document["materials"]}],
        safe_evidence_index={
            "capturedLegacyPublicResponseSha256": __import__("hashlib")
            .sha256(args.legacy_json.read_bytes())
            .hexdigest()
        },
    )
    workspace, _attestation = consume_snapshot_for_branch(
        run_root,
        branch="v2",
        snapshot_path=snapshots / snapshot.identity.snapshot_id,
        consumed_at=args.created_at,
        expected_identity=snapshot.identity,
    )
    llm = _llm_outcome(native=analysis_failure is None)
    raw_candidate: dict[str, object] = {
        "candidateId": args.candidate_id,
        "contractVersion": "1.0.0",
        "createdAt": args.created_at,
        "desiredIssue": {
            "analysis": analysis,
            "brief": reconciled_brief,
            "emptyReason": None,
            "issueDate": issue_date,
            "issueId": f"issue_{issue_date.replace('-', '')}",
            "issueNumber": issue.get("issue_number"),
            "lifecycleStatus": "published",
            "materials": materials,
            "publicationOrigin": "v2",
            "publishedAt": args.published_at,
            "stats": stats,
            "title": issue["title"],
        },
        "expectedBase": {
            "logicalStateHash": base.digest.state_hash,
            "releaseId": base.release.release_id,
            "sequence": base.release.sequence,
        },
        "expectedIssueAbsent": True,
        "idempotencyKey": "idem_" + args.candidate_id,
        "initiator": {"actorId": "project-manager", "kind": "project-manager", "requestId": None},
        "llmOutcome": llm,
        "operation": "daily",
        "queueChanges": [],
        "reason": "V2 daily issue with analysis generated from the final eligible composition",
        "schemaVersion": 1,
        "snapshot": {
            "itemCount": snapshot.identity.item_count,
            "manifestSha256": snapshot.identity.manifest_sha256,
            "payloadSha256": snapshot.identity.payload_sha256,
            "snapshotId": snapshot.identity.snapshot_id,
        },
    }
    candidate = cast(JsonObject, raw_candidate)
    candidate_path = args.root / "candidate.json"
    atomic_write_new(candidate_path, canonical_json_line(candidate), mode=0o600)
    atomic_write_new(
        args.root / "excluded-materials.json",
        canonical_json_line({"excluded": excluded}),
        mode=0o600,
    )
    result = build_candidate_package(
        source_database=args.source_db,
        staging_database=staging / "daily.sqlite",
        package_store=packages,
        candidate=candidate,
        v2_workspace=workspace,
    )
    print(
        canonical_json_line(
            {
                "analysisSource": "legacy-import" if analysis_failure else "v2-native",
                "periodAnalysis": {
                    "7d": {
                        "attempts": period_7d["attempts"],
                        "model": period_7d["model"],
                        "status": period_7d["status"],
                    },
                    "30d": {
                        "attempts": period_30d["attempts"],
                        "model": period_30d["model"],
                        "status": period_30d["status"],
                    },
                },
                "candidate": str(candidate_path),
                "excludedMaterials": len(excluded),
                "package": str(result.package.path),
                "packageSha256": result.package.package_sha256,
                "staging": str(staging / "daily.sqlite"),
            }
        ).decode(),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
