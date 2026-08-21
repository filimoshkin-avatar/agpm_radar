"""Synthetic Stage 4 immutable-snapshot, fork, isolation and comparison gates."""

from __future__ import annotations

import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
from packages.domain import snapshot as snapshot_module
from packages.domain.dual_run import (
    BranchContractError,
    BranchResult,
    ForkResult,
    LegacyBaseline,
    MaterialDecision,
    build_daily_comparison,
    consume_snapshot_for_branch,
    execute_dual_run,
    fork_snapshot,
    verify_consumption_attestation,
    write_daily_comparison,
)
from packages.domain.snapshot import (
    CANDIDATES_NAME,
    CHECKSUMS_NAME,
    EVIDENCE_NAME,
    MANIFEST_NAME,
    SNAPSHOT_NAMES,
    JsonObject,
    SnapshotError,
    SnapshotIdentity,
    SnapshotIntegrityError,
    VerifiedSnapshot,
    canonical_json_line,
    create_snapshot,
    verify_snapshot,
)
from packages.storage.safe_files import (
    ArtifactExistsError,
    PathEscapeError,
    SafeFilesystemError,
    ensure_private_directory,
    read_regular_file_at,
)

V2_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = V2_ROOT / "fixtures/synthetic/stage4-collected-input.json"
CONSUMED_AT = "2026-08-19T05:01:00Z"
GENERATED_AT = "2026-08-19T06:00:00Z"


def _fixture() -> tuple[str, str, list[dict[str, object]], dict[str, object]]:
    raw = cast(dict[str, object], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
    return (
        cast(str, raw["snapshotId"]),
        cast(str, raw["collectedAt"]),
        cast(list[dict[str, object]], raw["candidates"]),
        cast(dict[str, object], raw["safeEvidenceIndex"]),
    )


def _create_snapshot(
    tmp_path: Path, store_name: str = "snapshots"
) -> tuple[Path, VerifiedSnapshot]:
    snapshot_id, collected_at, candidates, evidence = _fixture()
    store = tmp_path / store_name
    verified = create_snapshot(
        store,
        snapshot_id=snapshot_id,
        collected_at=collected_at,
        candidates=candidates,
        safe_evidence_index=evidence,
    )
    return store / snapshot_id, verified


def _legacy_result(
    output: bytes = b"synthetic legacy output\n",
    exit_code: int = 0,
) -> BranchResult:
    return BranchResult(
        branch="legacy",
        output=output,
        exit_code=exit_code,
        decisions=(
            MaterialDecision(
                "synthetic-material-001",
                "included",
                ("governance",),
                "2026-08-19",
            ),
            MaterialDecision(
                "synthetic-material-002",
                "included",
                ("delivery",),
                "2026-08-19",
            ),
        ),
        statistics=(("included", 2), ("viewed", 2)),
        llm_status="fallback",
        llm_provider="synthetic-provider",
        llm_model="synthetic-legacy-model",
        publication_status="published",
        health_status="passed",
    )


def _v2_result() -> BranchResult:
    return BranchResult(
        branch="v2",
        output=b"synthetic v2 shadow output\n",
        exit_code=0,
        decisions=(
            MaterialDecision(
                "synthetic-material-001",
                "included",
                ("risk",),
                "2026-08-18",
                "synthetic-duplicate",
            ),
            MaterialDecision("synthetic-material-002", "rejected"),
            MaterialDecision(
                "synthetic-material-003",
                "included",
                ("delivery",),
                "2026-08-19",
            ),
        ),
        statistics=(("included", 2), ("viewed", 3)),
        llm_status="unavailable",
        llm_provider=None,
        llm_model=None,
        publication_status="not_published",
        health_status="shadow-only",
    )


def _valid_fork(tmp_path: Path) -> tuple[Path, ForkResult]:
    snapshot_path, verified = _create_snapshot(tmp_path)
    run_root = tmp_path / "run"
    result = fork_snapshot(
        snapshot_path,
        run_root,
        expected_identity=verified.identity,
        legacy_consumed_at=CONSUMED_AT,
        v2_consumed_at=CONSUMED_AT,
    )
    assert result.both_attest_same_input
    return run_root, result


def test_snapshot_is_canonical_deterministic_and_private(tmp_path: Path) -> None:
    first_path, first = _create_snapshot(tmp_path, "first")
    second_path, second = _create_snapshot(tmp_path, "second")

    assert first.identity == second.identity
    assert first.files == second.files
    assert set(path.name for path in first_path.iterdir()) == SNAPSHOT_NAMES
    for name in SNAPSHOT_NAMES:
        assert (first_path / name).read_bytes() == (second_path / name).read_bytes()
        assert stat.S_IMODE((first_path / name).stat().st_mode) == 0o400
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o500

    manifest = (first_path / MANIFEST_NAME).read_bytes()
    assert manifest.endswith(b"\n")
    assert b" " not in manifest
    checksums = (first_path / CHECKSUMS_NAME).read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in checksums] == [
        CANDIDATES_NAME,
        MANIFEST_NAME,
        EVIDENCE_NAME,
    ]
    assert verify_snapshot(first_path).identity == first.identity


def test_snapshot_identity_has_all_stage4_hash_dimensions(tmp_path: Path) -> None:
    _snapshot_path, verified = _create_snapshot(tmp_path)
    identity = verified.identity
    assert identity == SnapshotIdentity(
        snapshot_id="snap_20260819_synthetic01",
        manifest_sha256="0cb1bb4fbc8e7f7185bda198207d8c433a44d10c883e62edc5d8a905759e14ff",
        checksums_sha256="fffe0340a68336d22614a3e3f453369d2d386bae5e3891154b225760647875c4",
        payload_sha256="f4e468e29acb5cd49bac3b3165be6f1b7fcf539ce8523260a1ff6c874dd7063e",
        item_count=2,
    )
    assert len({identity.manifest_sha256, identity.checksums_sha256, identity.payload_sha256}) == 3
    assert all(
        len(digest) == 64
        for digest in (
            identity.manifest_sha256,
            identity.checksums_sha256,
            identity.payload_sha256,
        )
    )


@pytest.mark.parametrize(
    "name",
    [MANIFEST_NAME, CANDIDATES_NAME, EVIDENCE_NAME, CHECKSUMS_NAME],
)
def test_any_snapshot_file_byte_mutation_is_detected(tmp_path: Path, name: str) -> None:
    snapshot_path, _verified = _create_snapshot(tmp_path)
    target = snapshot_path / name
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b"x")
    target.chmod(0o400)

    with pytest.raises(SnapshotIntegrityError):
        verify_snapshot(snapshot_path)


def test_snapshot_extra_member_and_broad_permissions_are_rejected(tmp_path: Path) -> None:
    snapshot_path, _verified = _create_snapshot(tmp_path)
    snapshot_path.chmod(0o700)
    extra = snapshot_path / "unexpected.json"
    extra.write_text("{}\n", encoding="utf-8")
    extra.chmod(0o400)
    snapshot_path.chmod(0o500)
    with pytest.raises(SnapshotIntegrityError, match="membership"):
        verify_snapshot(snapshot_path)

    snapshot_path.chmod(0o700)
    extra.unlink()
    (snapshot_path / MANIFEST_NAME).chmod(0o444)
    snapshot_path.chmod(0o500)
    with pytest.raises(SnapshotIntegrityError, match="mode differs"):
        verify_snapshot(snapshot_path)


def test_snapshot_owner_write_mode_is_rejected_without_byte_change(tmp_path: Path) -> None:
    snapshot_path, _verified = _create_snapshot(tmp_path)
    target = snapshot_path / MANIFEST_NAME
    original = target.read_bytes()
    target.chmod(0o600)

    with pytest.raises(SnapshotIntegrityError, match="mode differs"):
        verify_snapshot(snapshot_path)
    assert target.read_bytes() == original


def test_snapshot_directory_requires_exact_immutable_mode(tmp_path: Path) -> None:
    snapshot_path, _verified = _create_snapshot(tmp_path)
    snapshot_path.chmod(0o700)

    with pytest.raises(SnapshotIntegrityError, match="exactly 0500"):
        verify_snapshot(snapshot_path)


def test_same_snapshot_creation_race_never_overwrites(tmp_path: Path) -> None:
    store = tmp_path / "snapshots"
    ensure_private_directory(store)
    snapshot_id, collected_at, candidates, evidence = _fixture()
    barrier = threading.Barrier(2)

    def create_concurrently() -> str:
        barrier.wait()
        try:
            create_snapshot(
                store,
                snapshot_id=snapshot_id,
                collected_at=collected_at,
                candidates=candidates,
                safe_evidence_index=evidence,
            )
        except SnapshotError:
            return "exists"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _index: create_concurrently(), range(2)))

    assert outcomes == ["created", "exists"]
    assert verify_snapshot(store / snapshot_id).identity.item_count == 2
    assert not [path for path in store.iterdir() if path.name.startswith(".")]


def test_snapshot_store_symlink_is_never_followed(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    snapshot_id, collected_at, candidates, evidence = _fixture()

    with pytest.raises(OSError):
        create_snapshot(
            linked,
            snapshot_id=snapshot_id,
            collected_at=collected_at,
            candidates=candidates,
            safe_evidence_index=evidence,
        )
    assert list(actual.iterdir()) == []


def test_fully_rewritten_snapshot_with_same_id_is_rejected_by_creation_identity(
    tmp_path: Path,
) -> None:
    original_path, original = _create_snapshot(tmp_path, "original")
    del original_path
    snapshot_id, collected_at, candidates, evidence = _fixture()
    changed_candidates: list[dict[str, object]] = [
        *candidates,
        {"candidateKey": "synthetic-material-rewrite"},
    ]
    changed = create_snapshot(
        tmp_path / "changed",
        snapshot_id=snapshot_id,
        collected_at=collected_at,
        candidates=changed_candidates,
        safe_evidence_index=evidence,
    )

    with pytest.raises(SnapshotIntegrityError, match="changed after its creation"):
        fork_snapshot(
            tmp_path / "changed" / snapshot_id,
            tmp_path / "run",
            expected_identity=original.identity,
            legacy_consumed_at=CONSUMED_AT,
            v2_consumed_at=CONSUMED_AT,
        )
    assert changed.identity.snapshot_id == original.identity.snapshot_id
    assert changed.identity != original.identity
    assert not (tmp_path / "run").exists()


def test_snapshot_reads_are_bound_to_the_pinned_directory_during_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path, original = _create_snapshot(tmp_path)
    snapshot_id, collected_at, candidates, evidence = _fixture()
    replacement_store = tmp_path / "replacement"
    replacement = create_snapshot(
        replacement_store,
        snapshot_id=snapshot_id,
        collected_at=collected_at,
        candidates=[*candidates, {"candidateKey": "replacement-material"}],
        safe_evidence_index=evidence,
    )
    moved_store = tmp_path / "original-store-moved"
    swapped = False

    def swap_parent_then_read(
        directory_descriptor: int,
        name: str,
        *,
        label: str,
        expected_mode: int | None = None,
    ) -> bytes:
        nonlocal swapped
        if not swapped:
            os.rename(snapshot_path.parent, moved_store)
            os.rename(replacement_store, snapshot_path.parent)
            swapped = True
        return read_regular_file_at(
            directory_descriptor,
            name,
            label=label,
            expected_mode=expected_mode,
        )

    monkeypatch.setattr(snapshot_module, "read_regular_file_at", swap_parent_then_read)

    assert verify_snapshot(snapshot_path).identity == original.identity
    assert verify_snapshot(snapshot_path).identity == replacement.identity


def test_fork_creates_separate_verified_copies_and_attestations(tmp_path: Path) -> None:
    run_root, result = _valid_fork(tmp_path)
    assert result.legacy.workspace is not None
    assert result.v2.workspace is not None
    legacy = result.legacy.workspace
    v2 = result.v2.workspace

    assert legacy.root != v2.root
    assert os.stat(legacy.root).st_ino != os.stat(v2.root).st_ino
    for area in ("queues", "corpus", "database", "logs"):
        assert legacy.area(area) != v2.area(area)
        assert legacy.area(area).is_dir()
        assert v2.area(area).is_dir()
        assert stat.S_IMODE(legacy.area(area).stat().st_mode) == 0o700
    for name in SNAPSHOT_NAMES:
        legacy_file = legacy.area("input") / legacy.snapshot_id / name
        v2_file = v2.area("input") / v2.snapshot_id / name
        assert legacy_file.read_bytes() == v2_file.read_bytes()
        assert legacy_file.stat().st_ino != v2_file.stat().st_ino

    legacy_attestation = verify_consumption_attestation(legacy)
    v2_attestation = verify_consumption_attestation(v2)
    assert legacy_attestation.identity == v2_attestation.identity == result.source_identity
    assert legacy_attestation.attestation_sha256 != v2_attestation.attestation_sha256
    assert (
        stat.S_IMODE((legacy.area("attestations") / "snapshot-consumption.json").stat().st_mode)
        == 0o400
    )
    assert not (run_root / "publication").exists()


def test_attestation_owner_write_mode_is_rejected_without_byte_change(tmp_path: Path) -> None:
    _run_root, result = _valid_fork(tmp_path)
    assert result.v2.workspace is not None
    attestation = result.v2.workspace.area("attestations") / "snapshot-consumption.json"
    original = attestation.read_bytes()
    attestation.chmod(0o600)

    with pytest.raises(SnapshotIntegrityError, match="mode differs"):
        verify_consumption_attestation(result.v2.workspace)
    assert attestation.read_bytes() == original


def test_semantically_valid_attestation_rewrite_blocks_runner(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    assert fork.v2.workspace is not None
    attestation = fork.v2.workspace.area("attestations") / "snapshot-consumption.json"
    payload = cast(dict[str, object], json.loads(attestation.read_bytes()))
    payload["consumedAt"] = "2026-08-19T05:02:00Z"
    attestation.chmod(0o600)
    attestation.write_bytes(canonical_json_line(payload))
    attestation.chmod(0o400)
    invoked = False

    def v2_runner(workspace: object) -> BranchResult:
        nonlocal invoked
        del workspace
        invoked = True
        return _v2_result()

    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(b"synthetic legacy output\n"),
        legacy_runner=lambda _workspace: _legacy_result(),
        v2_runner=v2_runner,
    )

    assert execution.legacy.status == "success"
    assert execution.v2.status == "blocked"
    assert execution.v2.error_type == "SnapshotIntegrityError"
    assert execution.v2.error_message == (
        "consumption attestation changed before runner invocation"
    )
    assert invoked is False


def test_mutation_between_branch_consumptions_preserves_legacy_and_blocks_v2(
    tmp_path: Path,
) -> None:
    snapshot_path, source = _create_snapshot(tmp_path)
    run_root = tmp_path / "run"
    legacy_workspace, legacy_attestation = consume_snapshot_for_branch(
        run_root,
        branch="legacy",
        snapshot_path=snapshot_path,
        consumed_at=CONSUMED_AT,
        expected_identity=source.identity,
    )
    target = snapshot_path / CANDIDATES_NAME
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b"x")
    target.chmod(0o400)

    with pytest.raises(SnapshotIntegrityError):
        consume_snapshot_for_branch(
            run_root,
            branch="v2",
            snapshot_path=snapshot_path,
            consumed_at=CONSUMED_AT,
            expected_identity=source.identity,
        )
    assert verify_consumption_attestation(legacy_workspace) == legacy_attestation
    assert not (run_root / "v2").exists()


def test_v2_setup_failure_does_not_remove_successful_legacy_copy(tmp_path: Path) -> None:
    snapshot_path, source = _create_snapshot(tmp_path)
    run_root = tmp_path / "run"
    ensure_private_directory(run_root)
    existing_v2_root = run_root / "v2"
    existing_v2_root.mkdir(mode=0o700)

    result = fork_snapshot(
        snapshot_path,
        run_root,
        expected_identity=source.identity,
        legacy_consumed_at=CONSUMED_AT,
        v2_consumed_at=CONSUMED_AT,
    )

    assert result.legacy.succeeded
    assert not result.v2.succeeded
    assert result.v2.error_type in {"ArtifactExistsError", "FileExistsError"}
    assert result.legacy.workspace is not None
    assert (
        verify_consumption_attestation(result.legacy.workspace).identity == result.source_identity
    )


def test_branch_write_capability_rejects_escape_symlink_and_publication(
    tmp_path: Path,
) -> None:
    _run_root, result = _valid_fork(tmp_path)
    assert result.legacy.workspace is not None
    assert result.v2.workspace is not None
    legacy = result.legacy.workspace
    v2 = result.v2.workspace
    protected = legacy.write_new("logs", "protected.log", b"legacy\n")

    with pytest.raises(PathEscapeError):
        v2.write_new("logs", "../legacy/logs/protected.log", b"v2\n")
    with pytest.raises(PathEscapeError):
        v2.write_new("logs", PurePosixPath("/absolute.log"), b"v2\n")
    with pytest.raises(Exception, match="forbidden"):
        v2.write_new("publication", "release.json", b"{}\n")

    escape = v2.area("queues") / "escape"
    escape.symlink_to(legacy.area("logs"), target_is_directory=True)
    with pytest.raises(OSError):
        v2.write_new("queues", "escape/overwrite.log", b"v2\n")
    assert protected.read_bytes() == b"legacy\n"
    assert not (legacy.area("logs") / "overwrite.log").exists()


def test_branch_atomic_write_is_private_and_never_overwrites(tmp_path: Path) -> None:
    _run_root, result = _valid_fork(tmp_path)
    assert result.v2.workspace is not None
    target = result.v2.workspace.write_new("logs", "result.json", b"{}\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(ArtifactExistsError):
        result.v2.workspace.write_new("logs", "result.json", b'{"changed":true}\n')
    assert target.read_bytes() == b"{}\n"


def test_v2_runner_failure_never_blocks_or_changes_legacy_success(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    legacy_output = b"synthetic legacy output\n"

    class SyntheticV2Error(RuntimeError):
        pass

    def legacy_runner(workspace: object) -> BranchResult:
        del workspace
        return _legacy_result(legacy_output)

    def v2_runner(workspace: object) -> BranchResult:
        del workspace
        raise SyntheticV2Error("synthetic isolated V2 failure")

    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(legacy_output),
        legacy_runner=legacy_runner,
        v2_runner=v2_runner,
    )

    assert execution.legacy.status == "success"
    assert execution.legacy.result is not None
    assert execution.legacy.result.output == legacy_output
    assert execution.legacy.legacy_baseline_match is True
    assert execution.v2.status == "failed"
    assert execution.v2.error_type == "SyntheticV2Error"
    assert execution.v2.error_message == "synthetic isolated V2 failure"
    assert fork.legacy.workspace is not None
    assert stat.S_IMODE(fork.legacy.workspace.root.stat().st_mode) == 0o500


def test_legacy_output_mismatch_fails_closed_while_v2_still_runs(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    actual_output = b"changed synthetic legacy output\n"

    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(b"accepted synthetic legacy output\n"),
        legacy_runner=lambda _workspace: _legacy_result(actual_output),
        v2_runner=lambda _workspace: _v2_result(),
    )

    assert execution.legacy.status == "failed"
    assert execution.legacy.error_type == "LegacyBaselineMismatch"
    assert execution.legacy.legacy_baseline_match is False
    assert execution.legacy.result is not None
    assert execution.legacy.result.output == actual_output
    assert execution.legacy.output_sha256 is not None
    assert execution.v2.status == "success"


def test_legacy_exit_code_mismatch_fails_closed(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    output = b"synthetic legacy output\n"

    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(output, exit_code=0),
        legacy_runner=lambda _workspace: _legacy_result(output, exit_code=7),
        v2_runner=lambda _workspace: _v2_result(),
    )

    assert execution.legacy.status == "failed"
    assert execution.legacy.error_type == "LegacyBaselineMismatch"
    assert execution.legacy.legacy_baseline_match is False
    assert execution.legacy.result is not None
    assert execution.legacy.result.exit_code == 7
    assert execution.v2.status == "success"


def test_expected_legacy_failure_but_successful_runner_fails_closed(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)

    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.failed("ExpectedLegacyError", "expected failure"),
        legacy_runner=lambda _workspace: _legacy_result(),
        v2_runner=lambda _workspace: _v2_result(),
    )

    assert execution.legacy.status == "failed"
    assert execution.legacy.error_type == "LegacyBaselineMismatch"
    assert execution.legacy.legacy_baseline_match is False
    assert execution.v2.status == "success"


def test_legacy_failure_type_and_message_are_preserved_while_v2_runs(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)

    class SyntheticLegacyError(RuntimeError):
        pass

    def legacy_runner(workspace: object) -> BranchResult:
        del workspace
        raise SyntheticLegacyError("unchanged synthetic legacy failure")

    def v2_runner(workspace: object) -> BranchResult:
        del workspace
        return _v2_result()

    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.failed(
            "SyntheticLegacyError",
            "unchanged synthetic legacy failure",
        ),
        legacy_runner=legacy_runner,
        v2_runner=v2_runner,
    )

    assert execution.legacy.status == "failed"
    assert execution.legacy.error_type == "SyntheticLegacyError"
    assert execution.legacy.error_message == "unchanged synthetic legacy failure"
    assert execution.legacy.legacy_baseline_match is True
    assert execution.v2.status == "success"


def test_branch_result_rejects_runtime_type_and_llm_shape_errors() -> None:
    valid = _legacy_result()

    with pytest.raises(BranchContractError, match="non-negative integer"):
        replace(valid, statistics=(("included", cast(int, "2")),))
    with pytest.raises(BranchContractError, match="exit code"):
        replace(valid, exit_code=256)
    with pytest.raises(BranchContractError, match="cannot name provider"):
        replace(valid, llm_status="unavailable")
    with pytest.raises(BranchContractError, match="real ISO date"):
        MaterialDecision("synthetic-material-invalid", "included", publication_date="2026-02-30")


def test_legacy_baseline_rejects_ambiguous_runtime_shape() -> None:
    with pytest.raises(BranchContractError, match="exactly one valid"):
        LegacyBaseline(
            output_sha256="0" * 64,
            exit_code=0,
            error_type="UnexpectedError",
            error_message="both success and failure",
        )


def test_mutated_branch_copy_blocks_v2_before_runner_invocation(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    assert fork.v2.workspace is not None
    target = fork.v2.workspace.area("input") / fork.v2.workspace.snapshot_id / CANDIDATES_NAME
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b"x")
    target.chmod(0o400)
    invoked = False

    def v2_runner(workspace: object) -> BranchResult:
        nonlocal invoked
        del workspace
        invoked = True
        return _v2_result()

    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(b"synthetic legacy output\n"),
        legacy_runner=lambda _workspace: _legacy_result(),
        v2_runner=v2_runner,
    )

    assert execution.legacy.status == "success"
    assert execution.v2.status == "blocked"
    assert execution.v2.error_type == "SnapshotIntegrityError"
    assert invoked is False


def test_stage4_rejects_any_v2_publication_claim(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    invalid = _v2_result()
    invalid = BranchResult(
        branch=invalid.branch,
        output=invalid.output,
        exit_code=invalid.exit_code,
        decisions=invalid.decisions,
        statistics=invalid.statistics,
        llm_status=invalid.llm_status,
        llm_provider=invalid.llm_provider,
        llm_model=invalid.llm_model,
        publication_status="published",
        health_status=invalid.health_status,
    )
    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(b"synthetic legacy output\n"),
        legacy_runner=lambda _workspace: _legacy_result(),
        v2_runner=lambda _workspace: invalid,
    )
    assert execution.legacy.status == "success"
    assert execution.v2.status == "failed"
    assert execution.v2.error_type == "BranchContractError"
    assert execution.v2.error_message == "V2 publication is forbidden during Stage 4"


def test_daily_comparison_is_canonical_complete_deterministic_and_private(
    tmp_path: Path,
) -> None:
    run_root, fork = _valid_fork(tmp_path)
    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(b"synthetic legacy output\n"),
        legacy_runner=lambda _workspace: _legacy_result(),
        v2_runner=lambda _workspace: _v2_result(),
    )

    first = build_daily_comparison(fork, execution, generated_at=GENERATED_AT)
    second = build_daily_comparison(fork, execution, generated_at=GENERATED_AT)
    assert first == second
    assert first.endswith(b"\n")
    parsed = cast(JsonObject, json.loads(first))
    snapshot = cast(JsonObject, parsed["snapshot"])
    assert snapshot["snapshotId"] == fork.source_identity.snapshot_id
    assert snapshot["manifestSha256"] == fork.source_identity.manifest_sha256
    assert snapshot["checksumsSha256"] == fork.source_identity.checksums_sha256
    assert snapshot["payloadSha256"] == fork.source_identity.payload_sha256
    assert parsed["inputItemCount"] == 2
    differences = cast(JsonObject, parsed["differences"])
    assert differences["onlyLegacy"] == ["synthetic-material-002"]
    assert differences["onlyV2"] == ["synthetic-material-003"]
    assert len(cast(list[object], differences["rubrics"])) == 1
    assert len(cast(list[object], differences["dates"])) == 1
    assert len(cast(list[object], differences["duplicates"])) == 1
    assert parsed["v2PublicationAllowed"] is False
    branches = cast(JsonObject, parsed["branches"])
    v2 = cast(JsonObject, branches["v2"])
    v2_outcome = cast(JsonObject, v2["outcome"])
    assert v2_outcome["publicationStatus"] == "not_published"
    assert v2_outcome["healthStatus"] == "shadow-only"

    report_path, report_sha256 = write_daily_comparison(
        run_root,
        fork,
        execution,
        generated_at=GENERATED_AT,
    )
    assert report_path.read_bytes() == first
    assert len(report_sha256) == 64
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    with pytest.raises(ArtifactExistsError):
        write_daily_comparison(
            run_root,
            fork,
            execution,
            generated_at=GENERATED_AT,
        )


def test_daily_comparison_records_isolated_v2_failure_without_hiding_legacy(
    tmp_path: Path,
) -> None:
    _run_root, fork = _valid_fork(tmp_path)

    def failed_v2(_workspace: object) -> BranchResult:
        raise RuntimeError("synthetic V2 comparison failure")

    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(b"synthetic legacy output\n"),
        legacy_runner=lambda _workspace: _legacy_result(),
        v2_runner=failed_v2,
    )
    report = cast(
        JsonObject,
        json.loads(build_daily_comparison(fork, execution, generated_at=GENERATED_AT)),
    )
    branches = cast(JsonObject, report["branches"])
    legacy_outcome = cast(JsonObject, cast(JsonObject, branches["legacy"])["outcome"])
    v2_outcome = cast(JsonObject, cast(JsonObject, branches["v2"])["outcome"])
    assert legacy_outcome["status"] == "success"
    assert legacy_outcome["baselineMatch"] is True
    assert v2_outcome["status"] == "failed"
    assert v2_outcome["publicationStatus"] is None
    assert cast(JsonObject, v2_outcome["error"])["message"] == ("synthetic V2 comparison failure")


def test_comparison_reverifies_attestations_instead_of_trusting_execution(
    tmp_path: Path,
) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(b"synthetic legacy output\n"),
        legacy_runner=lambda _workspace: _legacy_result(),
        v2_runner=lambda _workspace: _v2_result(),
    )
    assert fork.v2.workspace is not None
    attestation = fork.v2.workspace.area("attestations") / "snapshot-consumption.json"
    attestation.chmod(0o600)
    attestation.write_bytes(attestation.read_bytes() + b"x")
    attestation.chmod(0o400)

    with pytest.raises(SnapshotIntegrityError):
        build_daily_comparison(fork, execution, generated_at=GENERATED_AT)


def test_comparison_rejects_valid_but_rewritten_attestation(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    execution = execute_dual_run(
        fork,
        legacy_baseline=LegacyBaseline.successful(b"synthetic legacy output\n"),
        legacy_runner=lambda _workspace: _legacy_result(),
        v2_runner=lambda _workspace: _v2_result(),
    )
    assert fork.v2.workspace is not None
    attestation = fork.v2.workspace.area("attestations") / "snapshot-consumption.json"
    payload = cast(dict[str, object], json.loads(attestation.read_bytes()))
    payload["consumedAt"] = "2026-08-19T05:02:00Z"
    attestation.chmod(0o600)
    attestation.write_bytes(canonical_json_line(payload))
    attestation.chmod(0o400)

    with pytest.raises(SnapshotIntegrityError, match="changed since the branch fork"):
        build_daily_comparison(fork, execution, generated_at=GENERATED_AT)


def test_v2_capability_cannot_name_a_legacy_path_even_when_it_exists(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    assert fork.legacy.workspace is not None
    assert fork.v2.workspace is not None
    legacy_target = fork.legacy.workspace.write_new("database", "legacy.sqlite", b"legacy")
    relative_escape = PurePosixPath("..") / "legacy" / "database" / "legacy.sqlite"

    with pytest.raises(PathEscapeError):
        fork.v2.workspace.write_new("database", relative_escape, b"v2")
    assert legacy_target.read_bytes() == b"legacy"


def test_branch_copy_hardlink_is_rejected_at_consumption(tmp_path: Path) -> None:
    _run_root, fork = _valid_fork(tmp_path)
    assert fork.v2.workspace is not None
    snapshot_root = fork.v2.workspace.area("input") / fork.v2.workspace.snapshot_id
    target = snapshot_root / EVIDENCE_NAME
    target.chmod(0o600)
    hardlink = tmp_path / "linked-evidence"
    os.link(target, hardlink)
    target.chmod(0o400)

    with pytest.raises(SnapshotIntegrityError, match="exactly one link"):
        verify_consumption_attestation(fork.v2.workspace)


def test_non_private_run_root_is_rejected(tmp_path: Path) -> None:
    snapshot_path, _verified = _create_snapshot(tmp_path)
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o755)

    with pytest.raises(SafeFilesystemError, match="permissions"):
        consume_snapshot_for_branch(
            run_root,
            branch="legacy",
            snapshot_path=snapshot_path,
            consumed_at=CONSUMED_AT,
            expected_identity=_verified.identity,
        )
