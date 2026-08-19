"""Stage 7 full-seed, delta, state-machine and local publisher acceptance gates."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
    RefResolver,
)
from packages.delta import engine as delta_engine
from packages.delta.engine import (
    DeltaConflictError,
    DeltaValidationError,
    apply_delta_to_staging,
    build_delta,
    export_full_seed,
    finalize_release_database,
    import_full_seed,
    inspect_release_database,
    validate_delta,
    validate_full_seed_manifest,
)
from packages.publisher.local_simulation import (
    LocalPublisherError,
    LocalPublisherSimulator,
    PublisherLockBusyError,
    SimulatedPublisherCrashError,
    install_initial_release,
    read_active_pointer,
)
from packages.publisher.project_manager import build_project_manager_report
from packages.publisher.state_machine import (
    BLOCKING_STATE,
    INITIAL_STATE,
    TERMINAL_STATES,
    TRANSITIONS,
    PublisherStateError,
    PublisherStateMachine,
)
from packages.storage.hashing import database_digest, logical_state_hash, verify_database
from packages.storage.migrations import configure_staging_connection, create_database
from packages.storage.safe_files import SafeFilesystemError

V2_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = V2_ROOT.parent
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts/v1"
BASE_RELEASE_ID = "rel_content_stage7_base"
TARGET_RELEASE_ID = "rel_content_stage7_daily"
CORRECTION_RELEASE_ID = "rel_content_stage7_fix"
CANDIDATE_ID = "cand_stage7_daily_0001"
CORRECTION_CANDIDATE_ID = "cand_stage7_fix_0002"
APP_RELEASE_ID = "app_release_stage7_test"
NOW = "2026-08-20T05:10:00Z"
ACTIVATED = "2026-08-20T05:11:00Z"
FINISHED = "2026-08-20T05:12:00Z"


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _clone(source: Path, target: Path) -> None:
    shutil.copyfile(source, target)
    target.chmod(0o600)


def _base_database(path: Path) -> str:
    create_database(path, applied_at="2026-08-19T00:00:00Z")
    with sqlite3.connect(path) as connection:
        configure_staging_connection(connection)
        connection.execute(
            """
            INSERT INTO application_compatibility VALUES (
              ?, 1, '1.0.0', '1.0.0', '1.0.0', '1.0.0',
              '1.0.0', '1.0.0', '3.45.1', '2026-08-19T00:00:00Z'
            )
            """,
            (APP_RELEASE_ID,),
        )
        connection.execute("INSERT INTO rubrics VALUES ('governance', 'Governance', 1)")
        connection.commit()
        verify_database(connection)
        connection.commit()
        state_hash = logical_state_hash(connection)
        connection.execute(
            "INSERT INTO content_releases VALUES (?, 1, NULL, ?, 'daily', 1, ?, ?, ?, ?)",
            (
                BASE_RELEASE_ID,
                "cand_stage7_bootstrap",
                state_hash,
                state_hash,
                "2026-08-19T00:00:00Z",
                "2026-08-19T00:00:00Z",
            ),
        )
        connection.commit()
        verify_database(connection)
        connection.commit()
    return state_hash


def _daily_content(base: Path, content: Path) -> None:
    _clone(base, content)
    with sqlite3.connect(content) as connection:
        configure_staging_connection(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO source_snapshots VALUES (?, ?, ?, ?, ?)",
            ("snap_stage7_daily_0001", _hash("manifest"), _hash("payload"), NOW, 1),
        )
        connection.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)",
            (
                "source_stage7_synthetic",
                "Stage 7 Synthetic",
                "https://example.test/stage7",
                "synthetic",
                1,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "material_stage7_daily_0001",
                "Synthetic Stage 7 material",
                "https://example.test/stage7/material",
                "https://example.test/stage7/material",
                "Stage 7 Synthetic",
                "2026-08-20T04:00:00Z",
                "resolved",
                "Synthetic summary",
                "Synthetic AgPM takeaway",
                "Synthetic brief",
                _hash("material-stage7"),
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO material_sources VALUES (?, ?, ?, ?, ?, ?)",
            (
                "material_stage7_daily_0001",
                "source_stage7_synthetic",
                "https://example.test/stage7/material",
                "synthetic",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO editorial_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "queue_stage7_daily_0001",
                "material_stage7_daily_0001",
                "review",
                "2026-08-21",
                1,
                "Synthetic review queue",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_stage7_20260820",
                "2026-08-20",
                75,
                "Synthetic Stage 7 daily",
                "Synthetic daily brief",
                "published",
                NOW,
                "v2",
                None,
                _hash("issue-stage7"),
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO issue_materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_stage7_20260820",
                "material_stage7_daily_0001",
                0,
                "near",
                "core",
                "Synthetic summary",
                "Synthetic AgPM takeaway",
                "Synthetic brief",
                _json([{"lead": "Synthetic thesis", "rest": None}]),
                None,
                _json([]),
                1,
                95,
                "strong",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO issue_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_stage7_20260820",
                None,
                _json({"blocks": []}),
                _json([]),
                "Synthetic daily brief",
                "unavailable",
                "gpt-5.5",
                None,
                None,
                "stage7-rules-v1",
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO material_analysis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_stage7_20260820",
                "material_stage7_daily_0001",
                "Synthetic short text",
                "Synthetic angle",
                "unavailable",
                "gpt-5.5",
                None,
                None,
                "stage7-rules-v1",
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO llm_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "attempt_stage7_daily_0001",
                "issue",
                "issue_stage7_20260820",
                None,
                "gpt-5.5",
                "gpt-5.5",
                "openai",
                1,
                "error",
                "PROVIDER_UNAVAILABLE",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO material_quality VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "issue_stage7_20260820",
                "material_stage7_daily_0001",
                "resolved",
                0,
                "ok",
                "ok",
                None,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO material_rubrics VALUES (?, ?, ?, ?, ?)",
            (
                "issue_stage7_20260820",
                "material_stage7_daily_0001",
                "governance",
                0.95,
                "synthetic",
            ),
        )
        connection.execute(
            "INSERT INTO daily_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("issue_stage7_20260820", 1, 1, 0, 1, 0, 0, 1, 0, NOW),
        )
        connection.commit()
        verify_database(connection)
        connection.commit()


def _daily_release(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    base = tmp_path / "base.sqlite"
    before_state = _base_database(base)
    content = tmp_path / "daily-content.sqlite"
    _daily_content(base, content)
    target = tmp_path / "daily-release.sqlite"
    finalize_release_database(
        content,
        target,
        release_id=TARGET_RELEASE_ID,
        candidate_id=CANDIDATE_ID,
        operation="daily",
        created_at=NOW,
        activated_at=ACTIVATED,
        expected_base_release_id=BASE_RELEASE_ID,
        expected_base_sequence=1,
        expected_before_state_hash=before_state,
    )
    delta = build_delta(
        base,
        target,
        release_id=TARGET_RELEASE_ID,
        candidate_id=CANDIDATE_ID,
        operation="daily",
        application_release_id=APP_RELEASE_ID,
        created_at=NOW,
        assets=(
            {
                "bytes": 3,
                "mediaType": "application/json",
                "relativePath": "daily/2026-08-20/issue.json",
                "sha256": _hash("asset"),
            },
        ),
    )
    return base, target, cast(dict[str, object], delta)


def _no_llm_outcome() -> dict[str, object]:
    example = json.loads(
        (CONTRACT_ROOT / "examples/candidate-daily-no-llm.json").read_text(encoding="utf-8")
    )
    return cast(dict[str, object], example["llmOutcome"])


def _assert_delta_schema(delta: Mapping[str, object]) -> None:
    schema = json.loads((CONTRACT_ROOT / "delta.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert not list(validator.iter_errors(delta))


def _assert_publisher_result_schema(result: Mapping[str, object]) -> None:
    schema = json.loads(
        (CONTRACT_ROOT / "publisher-result.schema.json").read_text(encoding="utf-8")
    )
    llm_schema = json.loads((CONTRACT_ROOT / "llm-outcome.schema.json").read_text(encoding="utf-8"))
    resolver = RefResolver.from_schema(schema, store={llm_schema["$id"]: llm_schema})
    validator = Draft202012Validator(
        schema,
        resolver=resolver,
        format_checker=FormatChecker(),
    )
    assert not list(validator.iter_errors(result))


def _install_simulator(
    tmp_path: Path, base: Path, *, name: str
) -> tuple[LocalPublisherSimulator, Path, Path]:
    source_root = tmp_path / f"{name}-source"
    production_root = tmp_path / f"{name}-production"
    install_initial_release(source_root, base)
    install_initial_release(production_root, base)
    simulator = LocalPublisherSimulator(
        source_root=source_root,
        production_root=production_root,
        work_root=tmp_path / f"{name}-publisher",
    )
    return simulator, source_root, production_root


def test_full_seed_round_trip_and_reseed_restores_drift(tmp_path: Path) -> None:
    _base, target, _delta = _daily_release(tmp_path)
    export_dir = tmp_path / "seed-export"
    export_dir.mkdir(mode=0o700)
    seed = export_dir / "radar.sqlite"
    manifest_path = export_dir / "manifest.json"
    exported = export_full_seed(
        target,
        seed,
        manifest_path,
        created_at=FINISHED,
        application_release_id=APP_RELEASE_ID,
    )
    validate_full_seed_manifest(exported.manifest)
    assert stat.S_IMODE(seed.stat().st_mode) == 0o600
    assert json.loads(manifest_path.read_bytes()) == exported.manifest

    imported_dir = tmp_path / "seed-import"
    imported_dir.mkdir(mode=0o700)
    restored = import_full_seed(seed, manifest_path, imported_dir / "radar.sqlite")
    assert restored.file_sha256 == exported.file_sha256
    assert restored.digest == exported.digest

    drifted = tmp_path / "drifted.sqlite"
    _clone(target, drifted)
    with sqlite3.connect(drifted) as connection:
        connection.execute("UPDATE sources SET name = 'Undeclared drift'")
        connection.commit()
    with sqlite3.connect(drifted) as connection:
        drifted_digest = database_digest(connection)
    assert inspect_release_database(target).digest != drifted_digest
    reseed_dir = tmp_path / "reseed"
    reseed_dir.mkdir(mode=0o700)
    reseeded = import_full_seed(seed, manifest_path, reseed_dir / "radar.sqlite")
    assert reseeded.digest == inspect_release_database(target).digest


def test_delta_schema_transactional_apply_and_duplicate_apply(tmp_path: Path) -> None:
    base, target, delta = _daily_release(tmp_path)
    validate_delta(delta)
    _assert_delta_schema(delta)
    operations = cast(list[dict[str, object]], delta["operations"])
    assert operations[-1]["table"] == "content_releases"
    assert operations[-1]["action"] == "insert"
    assert {operation["table"] for operation in operations} >= {
        "source_snapshots",
        "editorial_queue",
        "issues",
        "issue_materials",
        "llm_attempts",
        "content_releases",
    }

    apply_dir = tmp_path / "apply"
    apply_dir.mkdir(mode=0o700)
    report = apply_delta_to_staging(base, apply_dir / "target.sqlite", delta)
    assert report.applied_operations == len(operations)
    assert report.idempotent_operations == 0
    assert report.state_hash == delta["afterStateHash"]
    target_digest = inspect_release_database(target).digest
    assert report.table_hashes == target_digest.table_hashes
    assert report.table_counts == target_digest.table_counts

    retry_dir = tmp_path / "retry"
    retry_dir.mkdir(mode=0o700)
    replay = apply_delta_to_staging(apply_dir / "target.sqlite", retry_dir / "target.sqlite", delta)
    assert replay.already_applied is True
    assert replay.applied_operations == 0
    assert replay.idempotent_operations == len(operations)
    assert replay.table_hashes == report.table_hashes
    assert replay.table_counts == report.table_counts


def test_correction_delta_contains_tombstones_and_full_table_evidence(tmp_path: Path) -> None:
    _base, daily, _delta = _daily_release(tmp_path)
    daily_report = inspect_release_database(daily)
    content = tmp_path / "correction-content.sqlite"
    _clone(daily, content)
    with sqlite3.connect(content) as connection:
        configure_staging_connection(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM editorial_queue")
        connection.execute("DELETE FROM material_rubrics")
        connection.execute(
            "UPDATE issues SET title = ?, content_hash = ?, updated_at = ?",
            ("Corrected synthetic issue", _hash("corrected-issue"), FINISHED),
        )
        connection.commit()
        verify_database(connection)
        connection.commit()
    corrected = tmp_path / "correction-release.sqlite"
    finalize_release_database(
        content,
        corrected,
        release_id=CORRECTION_RELEASE_ID,
        candidate_id=CORRECTION_CANDIDATE_ID,
        operation="correction",
        created_at=FINISHED,
        activated_at=FINISHED,
        expected_base_release_id=TARGET_RELEASE_ID,
        expected_base_sequence=2,
        expected_before_state_hash=daily_report.digest.state_hash,
    )
    delta = build_delta(
        daily,
        corrected,
        release_id=CORRECTION_RELEASE_ID,
        candidate_id=CORRECTION_CANDIDATE_ID,
        operation="correction",
        application_release_id=APP_RELEASE_ID,
        created_at=FINISHED,
    )
    _assert_delta_schema(delta)
    operations = cast(list[dict[str, object]], delta["operations"])
    assert {(item["table"], item["action"]) for item in operations} >= {
        ("editorial_queue", "delete"),
        ("material_rubrics", "delete"),
        ("issues", "upsert"),
        ("content_releases", "insert"),
    }
    assert len(cast(list[object], delta["expectedTables"])) == 23
    apply_dir = tmp_path / "correction-apply"
    apply_dir.mkdir(mode=0o700)
    applied = apply_delta_to_staging(daily, apply_dir / "target.sqlite", delta)
    assert applied.table_hashes == inspect_release_database(corrected).digest.table_hashes


def test_delta_rejects_out_of_order_unknown_and_optimistic_conflicts(tmp_path: Path) -> None:
    base, target, delta = _daily_release(tmp_path)
    out_of_order = copy.deepcopy(delta)
    out_of_order["targetSequence"] = cast(int, out_of_order["targetSequence"]) + 1
    with pytest.raises(DeltaValidationError, match="targetSequence"):
        validate_delta(out_of_order)

    unknown = copy.deepcopy(delta)
    cast(list[dict[str, object]], unknown["operations"])[0]["table"] = "schema_migrations"
    with pytest.raises(DeltaValidationError, match="unknown/non-content"):
        validate_delta(unknown)

    sql_payload = copy.deepcopy(delta)
    for operation in cast(list[dict[str, object]], sql_payload["operations"]):
        if operation["table"] == "issues":
            cast(dict[str, object], operation["values"])["title"] = "DROP TABLE issues"
            break
    with pytest.raises(DeltaValidationError, match="SQL/DDL"):
        validate_delta(sql_payload)

    correction_content = tmp_path / "conflict-content.sqlite"
    _clone(target, correction_content)
    with sqlite3.connect(correction_content) as connection:
        connection.execute(
            "UPDATE issues SET title = ?, content_hash = ?, updated_at = ?",
            ("Conflict target", _hash("conflict"), FINISHED),
        )
        connection.commit()
        verify_database(connection)
        connection.commit()
    target_report = inspect_release_database(target)
    correction_target = tmp_path / "conflict-target.sqlite"
    finalize_release_database(
        correction_content,
        correction_target,
        release_id=CORRECTION_RELEASE_ID,
        candidate_id=CORRECTION_CANDIDATE_ID,
        operation="correction",
        created_at=FINISHED,
        activated_at=FINISHED,
        expected_base_release_id=TARGET_RELEASE_ID,
        expected_base_sequence=2,
        expected_before_state_hash=target_report.digest.state_hash,
    )
    correction = build_delta(
        target,
        correction_target,
        release_id=CORRECTION_RELEASE_ID,
        candidate_id=CORRECTION_CANDIDATE_ID,
        operation="correction",
        application_release_id=APP_RELEASE_ID,
        created_at=FINISHED,
    )
    tampered = copy.deepcopy(correction)
    for operation in cast(list[dict[str, object]], tampered["operations"]):
        if operation["action"] == "upsert":
            operation["expectedBefore"] = "0" * 64
            break
    validate_delta(tampered)
    before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    conflict_dir = tmp_path / "conflict-apply"
    conflict_dir.mkdir(mode=0o700)
    with pytest.raises(DeltaConflictError, match="row hash"):
        apply_delta_to_staging(target, conflict_dir / "target.sqlite", tampered)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before_hash


def test_delta_consumes_one_pinned_base_inode_during_parent_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, target, delta = _daily_release(tmp_path)
    live_root = tmp_path / "live-base"
    live_root.mkdir(mode=0o700)
    live_database = live_root / "radar.sqlite"
    _clone(base, live_database)
    attacker_root = tmp_path / "attacker-base"
    attacker_root.mkdir(mode=0o700)
    _clone(target, attacker_root / "radar.sqlite")
    retained_root = tmp_path / "retained-original-base"
    original_reader = delta_engine._read_descriptor_bytes
    swapped = False

    def swap_parent_after_read(
        descriptor: int,
        path: Path,
        *,
        expected: tuple[int, int, int, int, int, int, int],
    ) -> bytes:
        nonlocal swapped
        content = original_reader(descriptor, path, expected=expected)
        if path == live_database and not swapped:
            os.rename(live_root, retained_root)
            os.rename(attacker_root, live_root)
            swapped = True
        return content

    monkeypatch.setattr(delta_engine, "_read_descriptor_bytes", swap_parent_after_read)
    apply_root = tmp_path / "path-swap-apply"
    apply_root.mkdir(mode=0o700)
    applied = apply_delta_to_staging(live_database, apply_root / "target.sqlite", delta)

    assert swapped is True
    assert applied.state_hash == delta["afterStateHash"]
    assert inspect_release_database(retained_root / "radar.sqlite").release.release_id == (
        BASE_RELEASE_ID
    )
    assert inspect_release_database(live_database).release.release_id == TARGET_RELEASE_ID


def test_database_inputs_reject_symlink_hardlink_and_broad_mode(tmp_path: Path) -> None:
    base = tmp_path / "base.sqlite"
    _base_database(base)

    symlink_path = tmp_path / "base-symlink.sqlite"
    symlink_path.symlink_to(base)
    with pytest.raises(SafeFilesystemError):
        inspect_release_database(symlink_path)

    broad_path = tmp_path / "base-broad.sqlite"
    _clone(base, broad_path)
    broad_path.chmod(0o640)
    with pytest.raises(SafeFilesystemError, match="broader than private"):
        inspect_release_database(broad_path)

    hardlink_path = tmp_path / "base-hardlink.sqlite"
    os.link(base, hardlink_path)
    with pytest.raises(SafeFilesystemError, match="single-link"):
        inspect_release_database(hardlink_path)


def test_state_machine_rejects_invalid_transition_and_blocks_after_reconciliation(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.jsonl"
    machine = PublisherStateMachine(journal)
    machine.receive(
        candidate_id=CANDIDATE_ID,
        release_id=TARGET_RELEASE_ID,
        occurred_at=NOW,
        before_state_hash="1" * 64,
        after_state_hash="2" * 64,
    )
    machine.transition(
        candidate_id=CANDIDATE_ID,
        release_id=TARGET_RELEASE_ID,
        event="validation_passed",
        occurred_at=NOW,
        before_state_hash="1" * 64,
        after_state_hash="2" * 64,
    )
    with pytest.raises(PublisherStateError, match="identity differs"):
        machine.receive(
            candidate_id=CANDIDATE_ID,
            release_id="rel_content_stage7_other",
            occurred_at=NOW,
            before_state_hash="1" * 64,
            after_state_hash="2" * 64,
        )
    with pytest.raises(PublisherStateError, match="invalid"):
        machine.transition(
            candidate_id=CANDIDATE_ID,
            release_id=TARGET_RELEASE_ID,
            event="result_persisted",
            occurred_at=NOW,
            before_state_hash="1" * 64,
            after_state_hash="2" * 64,
        )


def test_state_machine_matches_frozen_contract_yaml() -> None:
    contract = cast(
        dict[str, object],
        yaml.safe_load(
            (CONTRACT_ROOT / "publisher-state-machine.yaml").read_text(encoding="utf-8")
        ),
    )
    raw_transitions = cast(list[dict[object, object]], contract["transitions"])
    expected_transitions = {
        (
            cast(str, transition["from"]),
            cast(str, transition.get("on", transition[True])),
        ): cast(str, transition["to"])
        for transition in raw_transitions
    }
    raw_states = cast(dict[str, dict[str, object]], contract["states"])
    expected_terminal = frozenset(
        state for state, definition in raw_states.items() if definition.get("terminal") is True
    )
    expected_blocking = {
        state
        for state, definition in raw_states.items()
        if definition.get("blocksFuturePublishing") is True
    }

    assert contract["initialState"] == INITIAL_STATE
    assert expected_transitions == TRANSITIONS
    assert expected_terminal == TERMINAL_STATES
    assert expected_blocking == {BLOCKING_STATE}


def test_local_publisher_success_replay_and_fresh_process_import(tmp_path: Path) -> None:
    base, _target, delta = _daily_release(tmp_path)
    simulator, source_root, production_root = _install_simulator(tmp_path, base, name="success")
    result = simulator.publish(
        delta,
        llm_outcome=_no_llm_outcome(),
        started_at=NOW,
        finished_at=FINISHED,
        duration_ms=120_000,
        issue_date="2026-08-20",
    )
    _assert_publisher_result_schema(result)
    assert result["status"] == "published"
    assert result["publicationSucceeded"] is True
    assert read_active_pointer(source_root).release_id == TARGET_RELEASE_ID
    assert read_active_pointer(production_root).release_id == TARGET_RELEASE_ID
    report = build_project_manager_report(result)
    assert report["publicationSucceeded"] is True
    assert any("LLM" in warning for warning in cast(list[str], report["warnings"]))

    replay = simulator.publish(
        delta,
        llm_outcome=_no_llm_outcome(),
        started_at=FINISHED,
        finished_at=FINISHED,
        duration_ms=0,
        issue_date="2026-08-20",
    )
    _assert_publisher_result_schema(replay)
    assert replay["status"] == "already_succeeded"
    assert replay["idempotencyDisposition"] == "replayed"

    conflicting_delta = copy.deepcopy(delta)
    conflicting_delta["applicationReleaseId"] = "app_release_stage7_other"
    with pytest.raises(LocalPublisherError, match="preserved evidence differs"):
        simulator.publish(
            conflicting_delta,
            llm_outcome=_no_llm_outcome(),
            started_at=FINISHED,
            finished_at=FINISHED,
            duration_ms=0,
            issue_date="2026-08-20",
        )

    conflicting_llm = copy.deepcopy(_no_llm_outcome())
    fallback = cast(dict[str, object], conflicting_llm["deterministicFallback"])
    fallback["version"] = "2"
    with pytest.raises(LocalPublisherError, match="preserved evidence differs"):
        simulator.publish(
            delta,
            llm_outcome=conflicting_llm,
            started_at=FINISHED,
            finished_at=FINISHED,
            duration_ms=0,
            issue_date="2026-08-20",
        )

    process = subprocess.run(  # noqa: S603 - fixed interpreter/argv import regression
        [
            sys.executable,
            "-c",
            (
                "from packages.storage.replication_mutations import TABLE_SPECS; "
                "from packages.delta import build_delta; "
                "from packages.publisher import LocalPublisherSimulator; "
                "assert len(TABLE_SPECS) == 18"
            ),
        ],
        cwd=V2_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr


def test_crash_resume_preserves_valid_active_inodes(tmp_path: Path) -> None:
    base, _target, delta = _daily_release(tmp_path)
    simulator, source_root, production_root = _install_simulator(tmp_path, base, name="crash")
    with pytest.raises(SimulatedPublisherCrashError, match="REMOTE_ACTIVE"):
        simulator.publish(
            delta,
            llm_outcome=_no_llm_outcome(),
            started_at=NOW,
            finished_at=FINISHED,
            duration_ms=120_000,
            issue_date="2026-08-20",
            crash_after_state="REMOTE_ACTIVE",
        )
    assert read_active_pointer(source_root).release_id == BASE_RELEASE_ID
    assert read_active_pointer(production_root).release_id == TARGET_RELEASE_ID

    resumed = simulator.publish(
        delta,
        llm_outcome=_no_llm_outcome(),
        started_at=NOW,
        finished_at=FINISHED,
        duration_ms=120_000,
        issue_date="2026-08-20",
    )
    _assert_publisher_result_schema(resumed)
    assert resumed["status"] == "published"
    assert resumed["idempotencyDisposition"] == "resumed"
    assert read_active_pointer(source_root).release_id == TARGET_RELEASE_ID
    assert read_active_pointer(production_root).release_id == TARGET_RELEASE_ID


def test_crash_before_activation_keeps_both_active_pointers_on_base(tmp_path: Path) -> None:
    base, _target, delta = _daily_release(tmp_path)
    simulator, source_root, production_root = _install_simulator(
        tmp_path, base, name="crash-before-activation"
    )
    with pytest.raises(SimulatedPublisherCrashError, match="REMOTE_VERIFIED"):
        simulator.publish(
            delta,
            llm_outcome=_no_llm_outcome(),
            started_at=NOW,
            finished_at=FINISHED,
            duration_ms=120_000,
            issue_date="2026-08-20",
            crash_after_state="REMOTE_VERIFIED",
        )
    assert read_active_pointer(source_root).release_id == BASE_RELEASE_ID
    assert read_active_pointer(production_root).release_id == BASE_RELEASE_ID

    resumed = simulator.publish(
        delta,
        llm_outcome=_no_llm_outcome(),
        started_at=NOW,
        finished_at=FINISHED,
        duration_ms=120_000,
        issue_date="2026-08-20",
    )
    _assert_publisher_result_schema(resumed)
    assert resumed["status"] == "published"
    assert resumed["idempotencyDisposition"] == "resumed"
    assert read_active_pointer(source_root).release_id == TARGET_RELEASE_ID
    assert read_active_pointer(production_root).release_id == TARGET_RELEASE_ID


def test_crash_between_result_save_and_success_recovers_saved_result(tmp_path: Path) -> None:
    base, _target, delta = _daily_release(tmp_path)
    simulator, source_root, production_root = _install_simulator(
        tmp_path, base, name="crash-result-save"
    )
    with pytest.raises(SimulatedPublisherCrashError, match="result save"):
        simulator.publish(
            delta,
            llm_outcome=_no_llm_outcome(),
            started_at=NOW,
            finished_at=FINISHED,
            duration_ms=120_000,
            issue_date="2026-08-20",
            crash_after_state="RESULT_SAVED",
        )
    assert read_active_pointer(source_root).release_id == TARGET_RELEASE_ID
    assert read_active_pointer(production_root).release_id == TARGET_RELEASE_ID
    assert simulator.state_machine.state(CANDIDATE_ID).state == "SOURCE_COMMITTED"

    recovered = simulator.publish(
        delta,
        llm_outcome=_no_llm_outcome(),
        started_at=FINISHED,
        finished_at=FINISHED,
        duration_ms=0,
        issue_date="2026-08-20",
    )
    _assert_publisher_result_schema(recovered)
    assert recovered["status"] == "published"
    assert recovered["startedAt"] == NOW
    assert simulator.state_machine.state(CANDIDATE_ID).state == "SUCCEEDED"


def test_crash_after_rollback_transition_reconstructs_terminal_result(tmp_path: Path) -> None:
    base, _target, delta = _daily_release(tmp_path)
    simulator, source_root, production_root = _install_simulator(
        tmp_path, base, name="crash-rollback-result"
    )
    with pytest.raises(SimulatedPublisherCrashError, match="ROLLED_BACK"):
        simulator.publish(
            delta,
            llm_outcome=_no_llm_outcome(),
            started_at=NOW,
            finished_at=FINISHED,
            duration_ms=120_000,
            issue_date="2026-08-20",
            public_smoke_passes=False,
            prove_rollback=True,
            crash_after_state="ROLLED_BACK",
        )
    assert read_active_pointer(source_root).release_id == BASE_RELEASE_ID
    assert read_active_pointer(production_root).release_id == BASE_RELEASE_ID

    recovered = simulator.publish(
        delta,
        llm_outcome=_no_llm_outcome(),
        started_at=FINISHED,
        finished_at=FINISHED,
        duration_ms=0,
        issue_date="2026-08-20",
    )
    _assert_publisher_result_schema(recovered)
    assert recovered["status"] == "rolled_back"
    assert cast(dict[str, object], recovered["rollback"])["succeeded"] is True


def test_post_activation_failure_rolls_back_or_blocks_future_publication(tmp_path: Path) -> None:
    base, _target, delta = _daily_release(tmp_path)
    simulator, source_root, production_root = _install_simulator(tmp_path, base, name="rollback")
    rolled_back = simulator.publish(
        delta,
        llm_outcome=_no_llm_outcome(),
        started_at=NOW,
        finished_at=FINISHED,
        duration_ms=120_000,
        issue_date="2026-08-20",
        public_smoke_passes=False,
        prove_rollback=True,
    )
    _assert_publisher_result_schema(rolled_back)
    assert rolled_back["status"] == "rolled_back"
    assert cast(dict[str, object], rolled_back["rollback"])["succeeded"] is True
    assert read_active_pointer(source_root).release_id == BASE_RELEASE_ID
    assert read_active_pointer(production_root).release_id == BASE_RELEASE_ID

    blocked, _blocked_source, _blocked_production = _install_simulator(
        tmp_path, base, name="blocked"
    )
    reconciliation = blocked.publish(
        delta,
        llm_outcome=_no_llm_outcome(),
        started_at=NOW,
        finished_at=FINISHED,
        duration_ms=120_000,
        issue_date="2026-08-20",
        public_smoke_passes=False,
        prove_rollback=False,
    )
    _assert_publisher_result_schema(reconciliation)
    assert reconciliation["status"] == "needs_reconciliation"
    assert reconciliation["publishingBlocked"] is True
    replayed_reconciliation = blocked.publish(
        delta,
        llm_outcome=_no_llm_outcome(),
        started_at=FINISHED,
        finished_at=FINISHED,
        duration_ms=0,
        issue_date="2026-08-20",
    )
    _assert_publisher_result_schema(replayed_reconciliation)
    assert replayed_reconciliation == reconciliation
    with pytest.raises(PublisherStateError, match="blocked"):
        blocked.state_machine.receive(
            candidate_id="cand_stage7_future_0003",
            release_id="rel_content_stage7_future",
            occurred_at=FINISHED,
            before_state_hash=cast(str, delta["beforeStateHash"]),
            after_state_hash=cast(str, delta["afterStateHash"]),
        )


def test_publisher_lock_is_exclusive(tmp_path: Path) -> None:
    base, _target, delta = _daily_release(tmp_path)
    simulator, _source_root, _production_root = _install_simulator(tmp_path, base, name="lock")
    lock_path = simulator.work_root / "radar-mutation.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PublisherLockBusyError, match="busy"):
            simulator.publish(
                delta,
                llm_outcome=_no_llm_outcome(),
                started_at=NOW,
                finished_at=FINISHED,
                duration_ms=1,
                issue_date="2026-08-20",
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
