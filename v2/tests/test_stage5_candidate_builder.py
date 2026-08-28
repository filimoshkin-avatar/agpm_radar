"""Synthetic Stage 5 candidate, package, replay and Project Manager acceptance gates."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import cast

import pytest
from apps.candidate_builder.__main__ import main as candidate_cli
from packages.domain.candidate_mutations import (
    CandidateMutationError,
    build_candidate_mutations,
    issue_state_hash,
)
from packages.domain.candidate_package import (
    CandidateDuplicateError,
    CandidatePackageError,
    build_candidate_package,
    verify_candidate_package,
)
from packages.domain.candidates import (
    CandidateValidationError,
    build_correction_candidate,
    build_daily_candidate,
    build_gazette_candidate,
    load_candidate,
    validate_candidate,
)
from packages.domain.dual_run import (
    BranchWorkspace,
    ConsumptionAttestation,
    consume_snapshot_for_branch,
)
from packages.domain.snapshot import SnapshotIdentity, canonical_json_line, create_snapshot
from packages.publisher.project_manager import (
    ProjectManagerReportError,
    build_project_manager_report,
)
from packages.storage.hashing import logical_state_hash, rebuild_and_check_fts
from packages.storage.migrations import create_database
from packages.storage.replication_mutations import (
    MutationValidationError,
    StagingReplayError,
    replay_to_staging,
    row_after_sha256,
    validate_mutation_document,
)
from packages.storage.safe_files import read_tree_files

V2_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = V2_ROOT.parent
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts/v1"
EXAMPLES = CONTRACT_ROOT / "examples"
STAGE4_FIXTURE = V2_ROOT / "fixtures/synthetic/stage4-collected-input.json"
NOW = "2026-08-20T05:05:00Z"
BASE_RELEASE_ID = "rel_content_20260819_01"


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _seed_database(path: Path) -> str:
    create_database(path, applied_at="2026-08-19T00:00:00Z")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO application_compatibility VALUES (
              'app_release_synthetic01', 1, '1.0.0', '1.0.0', '1.0.0', '1.0.0',
              '1.0.0', '1.0.0', '3.45.1', '2026-08-19T00:00:00Z'
            )
            """
        )
        connection.executemany(
            "INSERT INTO rubrics VALUES (?, ?, ?)",
            (
                ("governance", "Governance", 1),
                ("security", "Security", 2),
            ),
        )
        connection.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)",
            (
                "source_synthetic_seed01",
                "Synthetic Seed",
                "https://example.test/seed",
                "synthetic",
                1,
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "material_draft_synthetic01",
                "Synthetic draft material",
                "https://example.test/draft-material",
                "https://example.test/draft-material",
                "Synthetic Seed",
                "2026-08-19T01:00:00Z",
                "resolved",
                "Draft summary",
                "Draft takeaway",
                "Draft brief",
                _hash("draft-material"),
                "2026-08-19T01:00:00Z",
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO material_sources VALUES (?, ?, ?, ?, ?, ?)",
            (
                "material_draft_synthetic01",
                "source_synthetic_seed01",
                "https://example.test/draft-material",
                "synthetic",
                "2026-08-19T01:00:00Z",
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_draft_20260821",
                "2026-08-21",
                76,
                "Synthetic draft issue",
                "Draft brief",
                "draft",
                None,
                None,
                None,
                _hash("draft-issue"),
                "2026-08-19T01:00:00Z",
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO issue_materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_draft_20260821",
                "material_draft_synthetic01",
                0,
                "near",
                "core",
                "Draft summary",
                "Draft takeaway",
                "Draft brief",
                _json([]),
                None,
                _json(["governance"]),
                1,
                90,
                "strong",
                "2026-08-19T01:00:00Z",
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO issue_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_draft_20260821",
                None,
                _json({"blocks": []}),
                _json([]),
                "Draft analysis",
                "unavailable",
                "gpt-5.5",
                None,
                None,
                "synthetic-v1",
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO material_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_draft_20260821",
                "material_draft_synthetic01",
                "Draft text",
                "Draft angle",
                "unavailable",
                "gpt-5.5",
                None,
                None,
                "synthetic-v1",
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO material_quality VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_draft_20260821",
                "material_draft_synthetic01",
                "resolved",
                -2,
                "medium",
                "queued",
                "Synthetic review",
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO material_rubrics VALUES (?, ?, ?, ?, ?)",
            (
                "issue_draft_20260821",
                "material_draft_synthetic01",
                "governance",
                0.9,
                "synthetic",
            ),
        )
        connection.execute(
            "INSERT INTO daily_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_draft_20260821",
                1,
                1,
                0,
                1,
                0,
                0,
                1,
                0,
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO editorial_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "queue_draft_synthetic01",
                "material_draft_synthetic01",
                "deferred",
                "2026-08-22",
                1,
                "Synthetic queue",
                "2026-08-19T01:00:00Z",
                "2026-08-19T01:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_20260819",
                "2026-08-19",
                74,
                "Synthetic historical issue",
                "Historical brief",
                "published",
                None,
                "legacy_inferred",
                None,
                _hash("historical-issue"),
                "2026-08-19T02:00:00Z",
                "2026-08-19T02:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO issue_materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_20260819",
                "material_draft_synthetic01",
                0,
                "near",
                "core",
                "Historical summary",
                "Historical takeaway",
                "Historical brief",
                _json([]),
                None,
                _json(["governance"]),
                1,
                90,
                "strong",
                "2026-08-19T02:00:00Z",
                "2026-08-19T02:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO material_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_20260819",
                "material_draft_synthetic01",
                None,
                None,
                "unavailable",
                "gpt-5.5",
                None,
                None,
                "synthetic-v1",
                "2026-08-19T02:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO material_quality VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_20260819",
                "material_draft_synthetic01",
                "resolved",
                0,
                "ok",
                "ok",
                None,
                "2026-08-19T02:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO material_rubrics VALUES (?, ?, ?, ?, ?)",
            (
                "issue_20260819",
                "material_draft_synthetic01",
                "governance",
                0.9,
                "synthetic",
            ),
        )
        connection.execute(
            "INSERT INTO issue_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_20260819",
                None,
                _json({"blocks": [{"kind": "overview", "text": "None", "title": "Result"}]}),
                _json([]),
                "Historical brief",
                "unavailable",
                "gpt-5.5",
                None,
                None,
                "synthetic-v1",
                "2026-08-19T02:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO daily_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_20260819",
                42,
                1,
                41,
                1,
                0,
                0,
                1,
                0,
                "2026-08-19T02:00:00Z",
            ),
        )
        rebuild_and_check_fts(connection)
        connection.commit()
        state_hash = logical_state_hash(connection)
        connection.execute(
            "INSERT INTO content_releases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                BASE_RELEASE_ID,
                1,
                None,
                "candidate_bootstrap_synthetic01",
                "daily",
                1,
                state_hash,
                state_hash,
                "2026-08-19T03:00:00Z",
                "2026-08-19T03:00:00Z",
            ),
        )
        connection.commit()
        return state_hash


def _load_example(name: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads((EXAMPLES / name).read_text(encoding="utf-8")))


def _base(candidate: dict[str, object], state_hash: str) -> None:
    candidate["expectedBase"] = {
        "logicalStateHash": state_hash,
        "releaseId": BASE_RELEASE_ID,
        "sequence": 1,
    }


def _snapshot_workspace(
    tmp_path: Path,
) -> tuple[BranchWorkspace, ConsumptionAttestation]:
    fixture = cast(dict[str, object], json.loads(STAGE4_FIXTURE.read_text(encoding="utf-8")))
    verified = create_snapshot(
        tmp_path / "snapshots",
        snapshot_id=cast(str, fixture["snapshotId"]),
        collected_at=cast(str, fixture["collectedAt"]),
        candidates=cast(list[dict[str, object]], fixture["candidates"]),
        safe_evidence_index=cast(dict[str, object], fixture["safeEvidenceIndex"]),
    )
    workspace, attestation = consume_snapshot_for_branch(
        tmp_path / "run",
        branch="v2",
        snapshot_path=tmp_path / "snapshots" / verified.identity.snapshot_id,
        consumed_at="2026-08-20T05:01:00Z",
        expected_identity=verified.identity,
    )
    return workspace, attestation


def _daily_candidate(state_hash: str, identity: SnapshotIdentity) -> dict[str, object]:
    candidate = _load_example("candidate-daily-no-llm.json")
    _base(candidate, state_hash)
    candidate["snapshot"] = {
        "itemCount": identity.item_count,
        "manifestSha256": identity.manifest_sha256,
        "payloadSha256": identity.payload_sha256,
        "snapshotId": identity.snapshot_id,
    }
    candidate["queueChanges"] = [
        {
            "action": "upsert",
            "materialId": "material_draft_synthetic01",
            "priority": 2,
            "queueId": "queue_draft_synthetic01",
            "reason": "Synthetic queue retained",
            "state": "deferred",
            "targetIssueDate": "2026-08-22",
        }
    ]
    return candidate


def test_daily_candidate_accepts_30_day_or_unresolved_new_materials_and_rejects_repeats(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    state_hash = _seed_database(source)
    workspace, attestation = _snapshot_workspace(tmp_path)
    candidate = _daily_candidate(state_hash, attestation.identity)
    desired = cast(dict[str, object], candidate["desiredIssue"])
    desired["emptyReason"] = None
    desired["stats"] = {
        "adjacent": 0,
        "core": 1,
        "cut": 41,
        "far": 1,
        "included": 1,
        "mid": 0,
        "near": 0,
        "viewed": 42,
    }
    desired["materials"] = [
        {
            "agpmTakeaway": "Synthetic takeaway",
            "brief": "Synthetic brief",
            "canonicalUrl": "https://example.test/window-material",
            "flags": [],
            "keyMaterial": False,
            "llmAgpmAngle": None,
            "llmShortText": None,
            "llmStatus": "unavailable",
            "materialId": "material_window_synthetic01",
            "perimeter": "far",
            "position": 1,
            "publicationDateStatus": "resolved",
            "publishedAt": "2026-07-21T00:00:00Z",
            "rubrics": [],
            "signalScore": None,
            "signalStrength": "watch",
            "sourceName": "Synthetic Source",
            "summary": "Synthetic summary",
            "theses": [],
            "title": "Synthetic window material",
            "trendNotes": None,
            "url": "https://example.test/window-material",
            "verdict": "core",
        }
    ]
    materials = cast(list[dict[str, object]], desired["materials"])

    def bind(value: dict[str, object]) -> None:
        with sqlite3.connect(source) as connection:
            build_candidate_mutations(
                connection,
                value,
                snapshot_identity=attestation.identity,
                snapshot_collected_at="2026-08-20T05:00:00Z",
            )

    materials[0]["publishedAt"] = "2026-07-21T00:00:00Z"
    materials[0]["publicationDateStatus"] = "resolved"
    bind(candidate)

    too_old = copy.deepcopy(candidate)
    cast(list[dict[str, object]], cast(dict[str, object], too_old["desiredIssue"])["materials"])[0][
        "publishedAt"
    ] = "2026-07-20T23:59:59Z"
    too_old["candidateId"] = "candidate_daily_too_old_0001"
    with pytest.raises(CandidateMutationError, match="outside the 30-day publication window"):
        bind(too_old)

    future = copy.deepcopy(candidate)
    cast(list[dict[str, object]], cast(dict[str, object], future["desiredIssue"])["materials"])[0][
        "publishedAt"
    ] = "2026-08-21T00:00:00Z"
    future["candidateId"] = "candidate_daily_future_0001"
    with pytest.raises(CandidateMutationError, match="outside the 30-day publication window"):
        bind(future)

    unresolved = copy.deepcopy(candidate)
    unresolved_material = cast(
        list[dict[str, object]], cast(dict[str, object], unresolved["desiredIssue"])["materials"]
    )[0]
    unresolved_material["publishedAt"] = None
    unresolved_material["publicationDateStatus"] = "unresolved"
    unresolved["candidateId"] = "candidate_daily_unresolved_0001"
    bind(unresolved)

    repeated = copy.deepcopy(candidate)
    repeated_material = cast(
        list[dict[str, object]], cast(dict[str, object], repeated["desiredIssue"])["materials"]
    )[0]
    repeated_material["materialId"] = "material_draft_synthetic01"
    repeated["candidateId"] = "candidate_daily_repeated_0001"
    with pytest.raises(CandidateMutationError, match="already included in an earlier issue"):
        bind(repeated)


def _correction_candidate(source: Path, state_hash: str) -> dict[str, object]:
    candidate = _load_example("candidate-correction.json")
    _base(candidate, state_hash)
    with sqlite3.connect(source) as connection:
        candidate["expectedIssueStateHash"] = issue_state_hash(connection, "issue_20260819")
    desired = cast(dict[str, object], candidate["desiredIssue"])
    desired["title"] = "Corrected synthetic historical issue"
    desired["brief"] = "Corrected historical brief"
    desired["emptyReason"] = "corrected_no_qualifying_materials"
    return candidate


def _gazette_candidate(state_hash: str, content: bytes) -> dict[str, object]:
    candidate = _load_example("candidate-gazette.json")
    _base(candidate, state_hash)
    candidate["inputAssets"] = [
        {
            "bytes": len(content),
            "mediaType": "text/html",
            "relativePath": "gazettes/2026-08/index.html",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]
    return candidate


def test_runtime_accepts_every_frozen_candidate_example() -> None:
    for path in sorted(EXAMPLES.glob("candidate-*.json")):
        load_candidate(path)


def test_candidate_builders_and_runtime_reject_malformed_or_unsafe_input() -> None:
    daily = _load_example("candidate-daily-no-llm.json")
    rebuilt = build_daily_candidate(
        common={
            key: daily[key]
            for key in daily
            if key
            not in {"operation", "snapshot", "desiredIssue", "expectedIssueAbsent", "queueChanges"}
        },
        snapshot=cast(dict[str, object], daily["snapshot"]),
        desired_issue=cast(dict[str, object], daily["desiredIssue"]),
        queue_changes=cast(list[dict[str, object]], daily["queueChanges"]),
    )
    assert rebuilt == daily

    correction = _load_example("candidate-correction.json")
    rebuilt_correction = build_correction_candidate(
        common={
            key: correction[key]
            for key in correction
            if key
            not in {
                "operation",
                "targetIssueDate",
                "expectedIssueStateHash",
                "sharedMaterialPreconditions",
                "desiredIssue",
            }
        },
        target_issue_date=cast(str, correction["targetIssueDate"]),
        expected_issue_state_hash=cast(str, correction["expectedIssueStateHash"]),
        shared_material_preconditions=cast(
            list[dict[str, object]], correction["sharedMaterialPreconditions"]
        ),
        desired_issue=cast(dict[str, object], correction["desiredIssue"]),
    )
    assert rebuilt_correction == correction

    gazette = _load_example("candidate-gazette.json")
    rebuilt_gazette = build_gazette_candidate(
        common={
            key: gazette[key]
            for key in gazette
            if key
            not in {
                "operation",
                "gazetteId",
                "expectedGazette",
                "period",
                "title",
                "ownerRequestDigest",
                "htmlEntrypoint",
                "inputAssets",
            }
        },
        gazette={
            key: gazette[key]
            for key in (
                "gazetteId",
                "expectedGazette",
                "period",
                "title",
                "ownerRequestDigest",
                "htmlEntrypoint",
                "inputAssets",
            )
        },
    )
    assert rebuilt_gazette == gazette

    bad = copy.deepcopy(daily)
    bad["unknown"] = True
    with pytest.raises(CandidateValidationError, match="unknown or missing"):
        validate_candidate(bad)
    bad = copy.deepcopy(daily)
    bad["reason"] = "DROP TABLE issues"
    with pytest.raises(CandidateValidationError, match="SQL/DDL"):
        validate_candidate(bad)
    bad = copy.deepcopy(daily)
    bad["schemaVersion"] = 2
    with pytest.raises(CandidateValidationError, match="version mismatch"):
        validate_candidate(bad)
    bad = copy.deepcopy(daily)
    bad["reason"] = "/root/private/candidate.json"
    with pytest.raises(CandidateValidationError, match="host-local path"):
        validate_candidate(bad)
    bad = copy.deepcopy(daily)
    bad["reason"] = "sk-" + "x" * 24
    with pytest.raises(CandidateValidationError, match="secret-shaped"):
        validate_candidate(bad)
    bad = copy.deepcopy(gazette)
    bad["htmlEntrypoint"] = "../index.html"
    with pytest.raises(CandidateValidationError, match="unsafe relative path"):
        validate_candidate(bad)
    bad = copy.deepcopy(daily)
    cast(dict[str, object], bad["llmOutcome"])["status"] = "success"
    with pytest.raises(CandidateValidationError, match="inconsistent"):
        validate_candidate(bad)


def test_daily_package_replays_snapshot_drafts_and_queue_deterministically(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    state_hash = _seed_database(source)
    workspace, attestation = _snapshot_workspace(tmp_path)
    candidate = _daily_candidate(state_hash, attestation.identity)

    first = build_candidate_package(
        source_database=source,
        staging_database=tmp_path / "daily-staging-a.sqlite",
        package_store=tmp_path / "packages-a",
        candidate=candidate,
        v2_workspace=workspace,
    )
    second = build_candidate_package(
        source_database=source,
        staging_database=tmp_path / "daily-staging-b.sqlite",
        package_store=tmp_path / "packages-b",
        candidate=candidate,
        v2_workspace=workspace,
    )
    assert first.package.package_sha256 == second.package.package_sha256
    assert read_tree_files(first.package.path) == read_tree_files(second.package.path)
    assert "deterministic fallback" in first.package.preview
    assert stat.S_IMODE(first.package.path.stat().st_mode) == 0o500
    assert all(
        stat.S_IMODE(path.stat().st_mode) in {0o400, 0o500}
        for path in first.package.path.rglob("*")
    )
    packaged_bytes = read_tree_files(first.package.path).values()
    assert all(str(tmp_path).encode() not in content for content in packaged_bytes)
    assert all(b"/mnt/" not in content and b"/root/" not in content for content in packaged_bytes)

    mutations = cast(list[dict[str, object]], first.package.mutations["mutations"])
    tables = {cast(str, mutation["table"]) for mutation in mutations}
    assert {"source_snapshots", "issues", "editorial_queue", "issue_materials"} <= tables
    assert "content_releases" not in tables
    completeness = cast(dict[str, object], first.package.mutations["completeness"])
    assert completeness["totalMutationCount"] == len(mutations)

    with sqlite3.connect(first.replay.staging_path) as staging:
        assert (
            staging.execute(
                "SELECT COUNT(*) FROM source_snapshots WHERE snapshot_id = ?",
                (attestation.identity.snapshot_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            staging.execute(
                "SELECT lifecycle_status FROM issues WHERE issue_id = 'issue_draft_20260821'"
            ).fetchone()[0]
            == "draft"
        )
        assert (
            staging.execute(
                "SELECT priority FROM editorial_queue WHERE queue_id = 'queue_draft_synthetic01'"
            ).fetchone()[0]
            == 2
        )
        assert (
            staging.execute(
                "SELECT COUNT(*) FROM pub_issues_v1 WHERE issue_id = 'issue_draft_20260821'"
            ).fetchone()[0]
            == 0
        )
        assert (
            staging.execute(
                "SELECT COUNT(*) FROM pub_issues_v1 WHERE issue_id = 'issue_20260820'"
            ).fetchone()[0]
            == 1
        )

    idempotent = replay_to_staging(
        first.replay.staging_path,
        tmp_path / "daily-idempotent.sqlite",
        first.package.mutations,
        first.package.candidate,
        expected_source_state_hash=first.replay.after_state_hash,
    )
    assert idempotent.applied == 0
    assert idempotent.idempotent_skips == len(mutations)
    assert idempotent.after_state_hash == first.replay.after_state_hash


def test_duplicate_candidate_and_idempotency_keys_are_rejected_before_replay(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    state_hash = _seed_database(source)
    workspace, attestation = _snapshot_workspace(tmp_path)
    candidate = _daily_candidate(state_hash, attestation.identity)
    store = tmp_path / "packages"
    build_candidate_package(
        source_database=source,
        staging_database=tmp_path / "first.sqlite",
        package_store=store,
        candidate=candidate,
        v2_workspace=workspace,
    )
    with pytest.raises(CandidateDuplicateError, match="candidate id"):
        build_candidate_package(
            source_database=source,
            staging_database=tmp_path / "duplicate.sqlite",
            package_store=store,
            candidate=candidate,
            v2_workspace=workspace,
        )
    assert not (tmp_path / "duplicate.sqlite").exists()

    duplicate_key = copy.deepcopy(candidate)
    duplicate_key["candidateId"] = "cand_daily_20260820_02"
    with pytest.raises(CandidateDuplicateError, match="idempotency key"):
        build_candidate_package(
            source_database=source,
            staging_database=tmp_path / "duplicate-key.sqlite",
            package_store=store,
            candidate=duplicate_key,
            v2_workspace=workspace,
        )
    assert not (tmp_path / "duplicate-key.sqlite").exists()


def test_correction_and_gazette_packages_replay_without_snapshot_capability(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    state_hash = _seed_database(source)
    correction = _correction_candidate(source, state_hash)
    corrected = build_candidate_package(
        source_database=source,
        staging_database=tmp_path / "correction.sqlite",
        package_store=tmp_path / "correction-packages",
        candidate=correction,
    )
    assert "payload/snapshot-attestation.json" not in read_tree_files(corrected.package.path)
    assert "fallback" in corrected.package.preview
    with sqlite3.connect(corrected.replay.staging_path) as connection:
        assert (
            connection.execute(
                "SELECT title FROM pub_issues_v1 WHERE issue_id = 'issue_20260819'"
            ).fetchone()[0]
            == "Corrected synthetic historical issue"
        )

    unsafe_html = b"<!doctype html><p>sk-" + b"x" * 24 + b"</p>"
    unsafe_candidate = _gazette_candidate(state_hash, unsafe_html)
    unsafe_candidate["candidateId"] = "cand_gazette_unsafe_01"
    unsafe_candidate["idempotencyKey"] = "idem_gazette_unsafe_01"
    with pytest.raises(CandidatePackageError, match="secret-shaped"):
        build_candidate_package(
            source_database=source,
            staging_database=tmp_path / "unsafe-gazette.sqlite",
            package_store=tmp_path / "unsafe-gazette-packages",
            candidate=unsafe_candidate,
            assets={"gazettes/2026-08/index.html": unsafe_html},
        )
    assert not (tmp_path / "unsafe-gazette.sqlite").exists()

    html = b"<!doctype html><html><body>Synthetic gazette</body></html>\n"
    gazette = _gazette_candidate(state_hash, html)
    published = build_candidate_package(
        source_database=source,
        staging_database=tmp_path / "gazette.sqlite",
        package_store=tmp_path / "gazette-packages",
        candidate=gazette,
        assets={"gazettes/2026-08/index.html": html},
    )
    package_files = read_tree_files(published.package.path)
    assert package_files["assets/gazettes/2026-08/index.html"] == html
    assert "payload/snapshot-attestation.json" not in package_files
    with sqlite3.connect(published.replay.staging_path) as connection:
        assert (
            connection.execute(
                "SELECT lifecycle_status FROM gazettes WHERE gazette_id = 'gazette_2026_08'"
            ).fetchone()[0]
            == "published"
        )


def test_mutation_and_package_tampering_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    state_hash = _seed_database(source)
    workspace, attestation = _snapshot_workspace(tmp_path)
    candidate = _daily_candidate(state_hash, attestation.identity)
    result = build_candidate_package(
        source_database=source,
        staging_database=tmp_path / "staging.sqlite",
        package_store=tmp_path / "packages",
        candidate=candidate,
        v2_workspace=workspace,
    )
    document = copy.deepcopy(result.package.mutations)
    first = cast(list[dict[str, object]], document["mutations"])[0]
    values = cast(dict[str, object], first.get("values", {}))
    if values:
        first_column = next(iter(values))
        values[first_column] = "DROP TABLE issues"
        with pytest.raises(MutationValidationError, match="SQL/DDL"):
            validate_mutation_document(document, candidate)

    forbidden_ledger = copy.deepcopy(result.package.mutations)
    cast(list[dict[str, object]], forbidden_ledger["mutations"])[0]["table"] = "content_releases"
    with pytest.raises(MutationValidationError, match="cannot author content_releases"):
        validate_mutation_document(forbidden_ledger, candidate)

    non_finite = copy.deepcopy(result.package.mutations)
    analysis_mutation = next(
        mutation
        for mutation in cast(list[dict[str, object]], non_finite["mutations"])
        if mutation["table"] == "issue_analysis"
    )
    analysis_values = cast(dict[str, object], analysis_mutation["values"])
    analysis_values["analysis_json"] = "NaN"
    analysis_mutation["rowAfterSha256"] = row_after_sha256(analysis_values)
    with pytest.raises(MutationValidationError, match="non-finite JSON"):
        validate_mutation_document(non_finite, candidate)

    negative_count = copy.deepcopy(result.package.mutations)
    stats_mutation = next(
        mutation
        for mutation in cast(list[dict[str, object]], negative_count["mutations"])
        if mutation["table"] == "daily_stats"
    )
    stats_values = cast(dict[str, object], stats_mutation["values"])
    stats_values["viewed"] = -1
    stats_mutation["rowAfterSha256"] = row_after_sha256(stats_values)
    with pytest.raises(MutationValidationError, match="must be at least 0"):
        validate_mutation_document(negative_count, candidate)

    leaked_path = copy.deepcopy(result.package.mutations)
    issue_mutation = next(
        mutation
        for mutation in cast(list[dict[str, object]], leaked_path["mutations"])
        if mutation["table"] == "issues"
    )
    issue_values = cast(dict[str, object], issue_mutation["values"])
    issue_values["title"] = "/root/private/report"
    issue_mutation["rowAfterSha256"] = row_after_sha256(issue_values)
    with pytest.raises(MutationValidationError, match="host-local path"):
        validate_mutation_document(leaked_path, candidate)

    manifest = result.package.path / "manifest.json"
    manifest.chmod(0o600)
    with pytest.raises(CandidatePackageError, match="mode"):
        verify_candidate_package(result.package.path)
    manifest.chmod(0o400)

    attestation_file = workspace.area("attestations") / "snapshot-consumption.json"
    attestation_file.chmod(0o600)
    with pytest.raises(CandidatePackageError, match="mode"):
        build_candidate_package(
            source_database=source,
            staging_database=tmp_path / "blocked.sqlite",
            package_store=tmp_path / "blocked-packages",
            candidate=candidate,
            v2_workspace=workspace,
        )
    assert not (tmp_path / "blocked.sqlite").exists()
    attestation_file.chmod(0o400)

    parent_alias = tmp_path / "source-parent-alias"
    os.symlink(tmp_path, parent_alias)
    with pytest.raises(CandidatePackageError, match="failed closed"):
        build_candidate_package(
            source_database=parent_alias / source.name,
            staging_database=tmp_path / "symlink-source.sqlite",
            package_store=tmp_path / "symlink-source-packages",
            candidate=candidate,
            v2_workspace=workspace,
        )
    assert not (tmp_path / "symlink-source.sqlite").exists()

    staging_parent_alias = tmp_path / "staging-parent-alias"
    os.symlink(tmp_path, staging_parent_alias)
    with pytest.raises(StagingReplayError, match="unsafe SQLite path boundary"):
        replay_to_staging(
            source,
            staging_parent_alias / "symlink-staging.sqlite",
            result.package.mutations,
            candidate,
            expected_source_state_hash=cast(
                str, cast(dict[str, object], candidate["expectedBase"])["logicalStateHash"]
            ),
        )
    assert not (tmp_path / "symlink-staging.sqlite").exists()

    source_hardlink = tmp_path / "source-hardlink.sqlite"
    os.link(source, source_hardlink)
    with pytest.raises(CandidatePackageError, match="single-link"):
        build_candidate_package(
            source_database=source,
            staging_database=tmp_path / "hardlink-source.sqlite",
            package_store=tmp_path / "hardlink-source-packages",
            candidate=candidate,
            v2_workspace=workspace,
        )
    assert not (tmp_path / "hardlink-source.sqlite").exists()

    hardlink = tmp_path / "manifest-hardlink.json"
    os.link(manifest, hardlink)
    with pytest.raises(CandidatePackageError, match="exactly one link"):
        verify_candidate_package(result.package.path)


def test_project_manager_report_preserves_no_llm_fallback_and_failure_semantics() -> None:
    published = _load_example("publisher-result-published-no-llm.json")
    report = build_project_manager_report(published)
    assert set(report) == set(_load_example("project-manager-report-published-no-llm.json"))
    assert report["publicationSucceeded"] is True
    assert report["llmOutcome"] == published["llmOutcome"]
    assert any("LLM" in warning for warning in cast(list[str], report["warnings"]))

    rolled_back = _load_example("publisher-result-rolled-back.json")
    failure = build_project_manager_report(rolled_back)
    assert set(failure) == set(report)
    assert failure["publicationStatus"] == "rolled_back"
    assert failure["publicationSucceeded"] is False
    assert failure["errorCode"] == "PUBLIC_SMOKE_FAILED_ROLLED_BACK"
    assert failure["llmOutcome"] == rolled_back["llmOutcome"]

    contradictory = copy.deepcopy(published)
    contradictory["publicationSucceeded"] = False
    with pytest.raises(ProjectManagerReportError, match="contradictory"):
        build_project_manager_report(contradictory)

    malformed_check = copy.deepcopy(published)
    cast(list[dict[str, object]], malformed_check["checks"])[0]["status"] = "unknown"
    with pytest.raises(ProjectManagerReportError, match="check status"):
        build_project_manager_report(malformed_check)

    leaked_warning = copy.deepcopy(published)
    cast(list[dict[str, object]], leaked_warning["warnings"])[0]["message"] = (
        "/root/private/provider.log"
    )
    with pytest.raises(ProjectManagerReportError, match="host-local path"):
        build_project_manager_report(leaked_warning)

    invalid_rollback = copy.deepcopy(rolled_back)
    cast(dict[str, object], invalid_rollback["rollback"])["succeeded"] = False
    with pytest.raises(ProjectManagerReportError, match="rollback evidence"):
        build_project_manager_report(invalid_rollback)


def test_cli_daily_status_retry_and_report_are_machine_readable(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.sqlite"
    state_hash = _seed_database(source)
    workspace, attestation = _snapshot_workspace(tmp_path)
    candidate = _daily_candidate(state_hash, attestation.identity)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_bytes(canonical_json_line(candidate))
    store = tmp_path / "packages"
    assert (
        candidate_cli(
            [
                "daily",
                "--candidate",
                str(candidate_path),
                "--source-db",
                str(source),
                "--staging-db",
                str(tmp_path / "staging.sqlite"),
                "--package-store",
                str(store),
                "--v2-workspace",
                str(workspace.root),
            ]
        )
        == 0
    )
    output = json.loads(capfd.readouterr().out)
    assert output["status"] == "candidate_ready"
    package_path = store / cast(str, candidate["candidateId"])

    assert candidate_cli(["status", "--package", str(package_path)]) == 0
    assert json.loads(capfd.readouterr().out)["disposition"] == "immutable_candidate_verified"
    assert candidate_cli(["retry", "--package", str(package_path)]) == 0
    assert json.loads(capfd.readouterr().out)["disposition"] == "ready_for_publisher_retry"

    publisher_path = tmp_path / "publisher-result.json"
    publisher_path.write_text(
        json.dumps(_load_example("publisher-result-published-no-llm.json")),
        encoding="utf-8",
    )
    assert candidate_cli(["report", "--publisher-result", str(publisher_path)]) == 0
    assert json.loads(capfd.readouterr().out)["deliveryRequired"] is True


def test_cli_correction_and_gazette_playbooks_use_closed_inputs(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.sqlite"
    state_hash = _seed_database(source)
    package_store = tmp_path / "packages"

    correction = _correction_candidate(source, state_hash)
    correction_path = tmp_path / "correction.json"
    correction_path.write_bytes(canonical_json_line(correction))
    assert (
        candidate_cli(
            [
                "correction",
                "--candidate",
                str(correction_path),
                "--source-db",
                str(source),
                "--staging-db",
                str(tmp_path / "correction.sqlite"),
                "--package-store",
                str(package_store),
            ]
        )
        == 0
    )
    assert json.loads(capfd.readouterr().out)["operation"] == "correction"

    html = b"<!doctype html><html><body>CLI gazette</body></html>\n"
    gazette = _gazette_candidate(state_hash, html)
    gazette_path = tmp_path / "gazette.json"
    gazette_path.write_bytes(canonical_json_line(gazette))
    asset_root = tmp_path / "gazette-assets"
    asset_root.mkdir(mode=0o700)
    gazettes = asset_root / "gazettes"
    gazettes.mkdir(mode=0o700)
    period = gazettes / "2026-08"
    period.mkdir(mode=0o700)
    asset = period / "index.html"
    asset.write_bytes(html)
    asset.chmod(0o644)
    unsafe_staging = tmp_path / "gazette-unsafe.sqlite"
    assert (
        candidate_cli(
            [
                "gazette",
                "--candidate",
                str(gazette_path),
                "--asset-root",
                str(asset_root),
                "--source-db",
                str(source),
                "--staging-db",
                str(unsafe_staging),
                "--package-store",
                str(package_store),
            ]
        )
        == 2
    )
    assert json.loads(capfd.readouterr().err)["status"] == "rejected"
    assert not unsafe_staging.exists()

    asset.chmod(0o600)
    assert (
        candidate_cli(
            [
                "gazette",
                "--candidate",
                str(gazette_path),
                "--asset-root",
                str(asset_root),
                "--source-db",
                str(source),
                "--staging-db",
                str(tmp_path / "gazette.sqlite"),
                "--package-store",
                str(package_store),
            ]
        )
        == 0
    )
    assert json.loads(capfd.readouterr().out)["operation"] == "gazette"


def test_analysis_evidence_titles_are_part_of_the_candidate_contract() -> None:
    """The LLM's ordered source list travels with the analysis, bounded not sorted."""
    from packages.domain.candidates import CandidateValidationError, _validate_analysis

    _validate_analysis(
        {
            "headline": "Сигнал",
            "brief": "Кратко.",
            "evidenceTitles": ["Первый", "Второй", "Первый"],
            "blocks": [{"kind": "overview", "title": "Сигнал", "text": "Текст."}],
            "theses": [],
        },
        [],
    )
    for wrong in ("не список", ["Первый", ""], [["вложенный"]]):
        with pytest.raises(CandidateValidationError, match="evidenceTitles"):
            _validate_analysis(
                {
                    "headline": "Сигнал",
                    "brief": "Кратко.",
                    "evidenceTitles": wrong,
                    "blocks": [{"kind": "overview", "title": "Сигнал", "text": "Текст."}],
                    "theses": [],
                },
                [],
            )


def _grounded_analysis(materials: list[object], **overrides: object) -> dict[str, object]:
    from packages.contracts.analysis import issue_content_hash

    analysis: dict[str, object] = {
        "headline": "Сигнал",
        "brief": "Кратко.",
        "blocks": [{"kind": "overview", "title": "Сигнал", "text": "Текст."}],
        "theses": [],
        "evidenceMaterialIds": [cast(dict[str, object], item)["materialId"] for item in materials],
        "inputContentHash": issue_content_hash(
            [cast(dict[str, object], item) for item in materials]
        ),
    }
    analysis.update(overrides)
    return analysis


def _grounded_materials() -> list[object]:
    return [
        {
            "materialId": f"mat_{index}",
            "title": f"Материал {index}",
            "summary": f"Содержание {index}",
            "rubrics": ["governance_control"],
            "perimeter": "mid",
        }
        for index in (1, 2)
    ]


def test_grounded_analysis_is_checked_against_the_composition_it_lands_on() -> None:
    """The hash is re-derived at the door, not taken on the analysis's word.

    Generation checks the binding once. Every later path - a correction, an analysis
    lifted out of another candidate with --analysis-candidate - arrives here, and the
    door used to ask only whether the hash looked like a hash.
    """
    from packages.domain.candidates import CandidateValidationError, _validate_analysis

    materials = _grounded_materials()
    _validate_analysis(_grounded_analysis(materials), materials)

    changed = [
        *materials,
        {
            "materialId": "mat_3",
            "title": "Материал 3",
            "summary": "Содержание 3",
            "rubrics": ["governance_control"],
            "perimeter": "far",
        },
    ]
    with pytest.raises(CandidateValidationError, match="does not describe this issue"):
        _validate_analysis(_grounded_analysis(materials), changed)


def test_grounded_analysis_cannot_cite_a_material_the_issue_does_not_carry() -> None:
    from packages.domain.candidates import CandidateValidationError, _validate_analysis

    materials = _grounded_materials()
    analysis = _grounded_analysis(materials, evidenceMaterialIds=["mat_1", "mat_removed"])
    with pytest.raises(CandidateValidationError, match="outside the issue"):
        _validate_analysis(analysis, materials)
