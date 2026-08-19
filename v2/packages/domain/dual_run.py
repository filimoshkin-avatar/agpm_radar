"""Independent Legacy/V2 consumption, execution isolation and daily comparison."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from packages.domain.snapshot import (
    SnapshotIdentity,
    SnapshotIntegrityError,
    canonical_json_line,
    copy_verified_snapshot,
    parse_canonical_json_object,
    sha256_bytes,
    verify_snapshot,
)
from packages.storage.safe_files import (
    SafeFilesystemError,
    atomic_write_new,
    create_private_directory,
    ensure_private_directory,
    private_tree_sha256,
    read_regular_file,
    seal_private_tree,
    write_new_relative,
)

type BranchName = Literal["legacy", "v2"]
type ExecutionStatus = Literal["success", "failed", "blocked"]
type Disposition = Literal["included", "rejected", "deferred"]
type PublicationStatus = Literal["published", "failed", "not_published"]

ATTESTATION_FORMAT: Final = "radar-snapshot-consumption-attestation/v1"
COMPARISON_FORMAT: Final = "radar-daily-comparison/v1"
ATTESTATION_NAME: Final = "snapshot-consumption.json"
COMPARISON_NAME: Final = "daily-comparison.json"
BRANCH_AREAS: Final = (
    "input",
    "queues",
    "corpus",
    "database",
    "logs",
    "attestations",
)
MUTABLE_BRANCH_AREAS: Final = frozenset({"queues", "corpus", "database", "logs"})
_UTC_TIMESTAMP_PATTERN: Final = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$"
)
_DATE_PATTERN: Final = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])$")
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_LLM_STATUSES: Final = frozenset({"success", "fallback", "unavailable", "not_requested"})
_LEGACY_BASELINE_MISMATCH_MESSAGE: Final = (
    "Legacy output or failure semantics differ from the accepted baseline"
)


class DualRunError(RuntimeError):
    """A Stage 4 fork, attestation, separation or comparison gate failed."""


class BranchContractError(DualRunError):
    """A branch returned data that violates the Stage 4 boundary."""


class BranchIsolationError(DualRunError):
    """The V2 execution changed the sealed Legacy branch tree."""


def _valid_exit_code(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True, slots=True)
class BranchWorkspace:
    """A capability scoped to one branch's disjoint private state roots."""

    root: Path
    branch: BranchName
    snapshot_id: str

    def area(self, name: str) -> Path:
        """Return one declared branch-local area; publication is deliberately absent."""
        if name not in BRANCH_AREAS:
            raise BranchIsolationError(f"undeclared branch area: {name}")
        return self.root / name

    def write_new(
        self,
        area: str,
        relative: str | PurePosixPath,
        content: bytes,
    ) -> Path:
        """Write a new private file only through an allowed mutable branch capability."""
        if area not in MUTABLE_BRANCH_AREAS:
            raise BranchIsolationError(f"branch area is immutable or forbidden: {area}")
        return write_new_relative(self.area(area), relative, content)


@dataclass(frozen=True, slots=True)
class ConsumptionAttestation:
    """Verified canonical evidence that one branch consumed its own snapshot copy."""

    branch: BranchName
    consumed_at: str
    identity: SnapshotIdentity
    snapshot_relative_path: str
    attestation_sha256: str


@dataclass(frozen=True, slots=True)
class BranchForkResult:
    """Success or failure of one branch copy, without coupling the other branch."""

    branch: BranchName
    workspace: BranchWorkspace | None
    consumption: ConsumptionAttestation | None
    error_type: str | None
    error_message: str | None

    @property
    def succeeded(self) -> bool:
        """Whether the branch has a verified copy and attestation."""
        return self.workspace is not None and self.consumption is not None


@dataclass(frozen=True, slots=True)
class ForkResult:
    """The common boundary identity and two independently attempted branch copies."""

    source_identity: SnapshotIdentity
    legacy: BranchForkResult
    v2: BranchForkResult

    @property
    def both_attest_same_input(self) -> bool:
        """Require all four identity dimensions, not snapshotId alone."""
        if self.legacy.consumption is None or self.v2.consumption is None:
            return False
        return (
            self.legacy.consumption.identity == self.v2.consumption.identity == self.source_identity
        )


@dataclass(frozen=True, slots=True)
class MaterialDecision:
    """One branch's deterministic daily disposition for comparison."""

    material_id: str
    disposition: Disposition
    rubrics: tuple[str, ...] = ()
    publication_date: str | None = None
    duplicate_of: str | None = None

    def __post_init__(self) -> None:
        if not _nonempty_text(self.material_id):
            raise BranchContractError("material_id must not be empty")
        if self.disposition not in {"included", "rejected", "deferred"}:
            raise BranchContractError(f"invalid disposition: {self.disposition}")
        if not isinstance(self.rubrics, tuple) or any(
            not _nonempty_text(rubric) for rubric in self.rubrics
        ):
            raise BranchContractError("rubrics must be a tuple of non-empty strings")
        if len(set(self.rubrics)) != len(self.rubrics):
            raise BranchContractError(f"duplicate rubric for material: {self.material_id}")
        if self.publication_date is not None:
            if not isinstance(self.publication_date, str) or not _DATE_PATTERN.fullmatch(
                self.publication_date
            ):
                raise BranchContractError("publication_date must be a real ISO date")
            try:
                datetime.strptime(self.publication_date, "%Y-%m-%d")
            except ValueError as error:
                raise BranchContractError("publication_date must be a real ISO date") from error
        if self.duplicate_of is not None:
            if not _nonempty_text(self.duplicate_of):
                raise BranchContractError("duplicate_of must be a non-empty material id")
            if self.duplicate_of == self.material_id:
                raise BranchContractError("a material cannot be a duplicate of itself")


@dataclass(frozen=True, slots=True)
class BranchResult:
    """Opaque branch output plus the minimum structured comparison surface."""

    branch: BranchName
    output: bytes
    exit_code: int
    decisions: tuple[MaterialDecision, ...]
    statistics: tuple[tuple[str, int], ...]
    llm_status: str
    llm_provider: str | None
    llm_model: str | None
    publication_status: PublicationStatus
    health_status: str

    def __post_init__(self) -> None:
        if self.branch not in {"legacy", "v2"}:
            raise BranchContractError("invalid branch identity")
        if not isinstance(self.output, bytes):
            raise BranchContractError("branch output must be exact bytes")
        if not _valid_exit_code(self.exit_code):
            raise BranchContractError("branch exit code must be an integer from 0 to 255")
        if not isinstance(self.decisions, tuple) or any(
            not isinstance(decision, MaterialDecision) for decision in self.decisions
        ):
            raise BranchContractError("branch decisions must be MaterialDecision values")
        material_ids = [decision.material_id for decision in self.decisions]
        if len(set(material_ids)) != len(material_ids):
            raise BranchContractError("branch result repeats a material decision")
        if not isinstance(self.statistics, tuple) or any(
            not isinstance(statistic, tuple) or len(statistic) != 2 for statistic in self.statistics
        ):
            raise BranchContractError("branch statistics must be name/value tuples")
        statistic_names = [statistic[0] for statistic in self.statistics]
        if len(set(statistic_names)) != len(statistic_names):
            raise BranchContractError("branch result repeats a statistic")
        if any(
            not _nonempty_text(name)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for name, value in self.statistics
        ):
            raise BranchContractError(
                "branch statistics must have names and non-negative integer values"
            )
        if self.llm_status not in _LLM_STATUSES:
            raise BranchContractError("invalid LLM status")
        if self.llm_status in {"success", "fallback"}:
            if not _nonempty_text(self.llm_provider) or not _nonempty_text(self.llm_model):
                raise BranchContractError("accepted LLM output requires provider and model")
        elif self.llm_provider is not None or self.llm_model is not None:
            raise BranchContractError(
                "unavailable/not-requested LLM outcome cannot name provider or model"
            )
        if self.publication_status not in {"published", "failed", "not_published"}:
            raise BranchContractError("invalid publication status")
        if not _nonempty_text(self.health_status):
            raise BranchContractError("health status must be a non-empty string")


@dataclass(frozen=True, slots=True)
class LegacyBaseline:
    """Exact normal-output or exact failure semantics expected from Legacy."""

    output_sha256: str | None
    exit_code: int | None
    error_type: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        successful = (
            isinstance(self.output_sha256, str)
            and _SHA256_PATTERN.fullmatch(self.output_sha256) is not None
            and _valid_exit_code(self.exit_code)
            and self.error_type is None
            and self.error_message is None
        )
        failed = (
            self.output_sha256 is None
            and self.exit_code is None
            and _nonempty_text(self.error_type)
            and _nonempty_text(self.error_message)
        )
        if not (successful or failed):
            raise BranchContractError(
                "Legacy baseline must describe exactly one valid success or failure shape"
            )

    @classmethod
    def successful(cls, output: bytes, exit_code: int = 0) -> LegacyBaseline:
        """Create an exact byte/exit-code baseline."""
        return cls(
            output_sha256=sha256_bytes(output),
            exit_code=exit_code,
            error_type=None,
            error_message=None,
        )

    @classmethod
    def failed(cls, error_type: str, error_message: str) -> LegacyBaseline:
        """Create an exact exception type/message baseline."""
        return cls(
            output_sha256=None,
            exit_code=None,
            error_type=error_type,
            error_message=error_message,
        )


@dataclass(frozen=True, slots=True)
class BranchExecution:
    """One attempt; a post-run gate failure may retain the actual result as evidence."""

    branch: BranchName
    snapshot_identity: SnapshotIdentity | None
    consumption_attestation_sha256: str | None
    status: ExecutionStatus
    result: BranchResult | None
    output_sha256: str | None
    error_type: str | None
    error_message: str | None
    legacy_baseline_match: bool | None


@dataclass(frozen=True, slots=True)
class DualRunExecution:
    """Legacy and V2 outcomes; neither outcome is inferred from the other."""

    legacy: BranchExecution
    v2: BranchExecution


def _validate_timestamp(value: str) -> None:
    if not _UTC_TIMESTAMP_PATTERN.fullmatch(value):
        raise DualRunError(f"timestamp must be second-precision UTC: {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise DualRunError(f"timestamp is not a real UTC calendar instant: {value!r}") from error


def _identity_json(identity: SnapshotIdentity) -> dict[str, object]:
    return {
        "checksumsSha256": identity.checksums_sha256,
        "itemCount": identity.item_count,
        "manifestSha256": identity.manifest_sha256,
        "payloadSha256": identity.payload_sha256,
        "snapshotId": identity.snapshot_id,
    }


def _attestation_bytes(
    branch: BranchName,
    consumed_at: str,
    identity: SnapshotIdentity,
) -> bytes:
    return canonical_json_line(
        {
            "attestationFormat": ATTESTATION_FORMAT,
            "branch": branch,
            "consumedAt": consumed_at,
            "snapshot": _identity_json(identity),
            "snapshotRelativePath": f"input/{identity.snapshot_id}",
        }
    )


def _branch_name(value: object) -> BranchName:
    if value == "legacy":
        return "legacy"
    if value == "v2":
        return "v2"
    raise SnapshotIntegrityError("consumption attestation branch is invalid")


def verify_consumption_attestation(workspace: BranchWorkspace) -> ConsumptionAttestation:
    """Re-verify the branch-local snapshot and canonical attestation at consumption time."""
    path = workspace.area("attestations") / ATTESTATION_NAME
    try:
        content = read_regular_file(path, expected_mode=0o400)
        parsed = parse_canonical_json_object(content, ATTESTATION_NAME)
        if set(parsed) != {
            "attestationFormat",
            "branch",
            "consumedAt",
            "snapshot",
            "snapshotRelativePath",
        }:
            raise SnapshotIntegrityError("consumption attestation has unknown/missing properties")
        branch = _branch_name(parsed.get("branch"))
        if branch != workspace.branch:
            raise SnapshotIntegrityError("consumption attestation is assigned to another branch")
        consumed_at = parsed.get("consumedAt")
        if not isinstance(consumed_at, str):
            raise SnapshotIntegrityError("consumption attestation timestamp is invalid")
        try:
            _validate_timestamp(consumed_at)
        except DualRunError as error:
            raise SnapshotIntegrityError("consumption attestation timestamp is invalid") from error
        expected_relative = f"input/{workspace.snapshot_id}"
        if parsed.get("snapshotRelativePath") != expected_relative:
            raise SnapshotIntegrityError("consumption attestation snapshot path is invalid")
        if parsed.get("attestationFormat") != ATTESTATION_FORMAT:
            raise SnapshotIntegrityError("unsupported consumption attestation format")
        snapshot_copy = verify_snapshot(workspace.area("input") / workspace.snapshot_id)
        snapshot_value = parsed.get("snapshot")
        if not isinstance(snapshot_value, dict) or snapshot_value != _identity_json(
            snapshot_copy.identity
        ):
            raise SnapshotIntegrityError("attestation identity differs from branch snapshot bytes")
        return ConsumptionAttestation(
            branch=branch,
            consumed_at=consumed_at,
            identity=snapshot_copy.identity,
            snapshot_relative_path=expected_relative,
            attestation_sha256=sha256_bytes(content),
        )
    except (OSError, SafeFilesystemError, SnapshotIntegrityError) as error:
        if isinstance(error, SnapshotIntegrityError):
            raise
        raise SnapshotIntegrityError(f"consumption attestation failed closed: {error}") from error


def consume_snapshot_for_branch(
    run_root: Path,
    *,
    branch: BranchName,
    snapshot_path: Path,
    consumed_at: str,
    expected_identity: SnapshotIdentity,
) -> tuple[BranchWorkspace, ConsumptionAttestation]:
    """Verify source bytes, create a separate copy, and attest that exact branch copy."""
    _validate_timestamp(consumed_at)
    verified_source = verify_snapshot(snapshot_path)
    if verified_source.identity != expected_identity:
        raise SnapshotIntegrityError(
            f"{branch} source identity changed after the common collection boundary"
        )
    ensure_private_directory(run_root)
    branch_root = run_root / branch
    create_private_directory(branch_root)
    for area in BRANCH_AREAS:
        create_private_directory(branch_root / area)
    copy_verified_snapshot(verified_source, branch_root / "input")
    attestation_content = _attestation_bytes(branch, consumed_at, verified_source.identity)
    atomic_write_new(
        branch_root / "attestations" / ATTESTATION_NAME,
        attestation_content,
        mode=0o400,
    )
    workspace = BranchWorkspace(
        root=branch_root,
        branch=branch,
        snapshot_id=verified_source.identity.snapshot_id,
    )
    attestation = verify_consumption_attestation(workspace)
    if attestation.identity != verified_source.identity:
        raise SnapshotIntegrityError("post-copy attestation identity mismatch")
    return workspace, attestation


def _failed_fork(branch: BranchName, error: Exception) -> BranchForkResult:
    return BranchForkResult(
        branch=branch,
        workspace=None,
        consumption=None,
        error_type=type(error).__name__,
        error_message=str(error),
    )


def fork_snapshot(
    snapshot_path: Path,
    run_root: Path,
    *,
    expected_identity: SnapshotIdentity,
    legacy_consumed_at: str,
    v2_consumed_at: str,
) -> ForkResult:
    """Attempt Legacy first and V2 second so V2 setup failure cannot erase Legacy success."""
    source = verify_snapshot(snapshot_path)
    if source.identity != expected_identity:
        raise SnapshotIntegrityError("snapshot identity changed after its creation boundary")
    try:
        legacy_workspace, legacy_attestation = consume_snapshot_for_branch(
            run_root,
            branch="legacy",
            snapshot_path=snapshot_path,
            consumed_at=legacy_consumed_at,
            expected_identity=source.identity,
        )
        legacy = BranchForkResult(
            branch="legacy",
            workspace=legacy_workspace,
            consumption=legacy_attestation,
            error_type=None,
            error_message=None,
        )
    except Exception as error:
        legacy = _failed_fork("legacy", error)

    try:
        v2_workspace, v2_attestation = consume_snapshot_for_branch(
            run_root,
            branch="v2",
            snapshot_path=snapshot_path,
            consumed_at=v2_consumed_at,
            expected_identity=source.identity,
        )
        v2 = BranchForkResult(
            branch="v2",
            workspace=v2_workspace,
            consumption=v2_attestation,
            error_type=None,
            error_message=None,
        )
    except Exception as error:
        v2 = _failed_fork("v2", error)
    return ForkResult(source_identity=source.identity, legacy=legacy, v2=v2)


def _blocked_execution(branch: BranchName, error_type: str, message: str) -> BranchExecution:
    return BranchExecution(
        branch=branch,
        snapshot_identity=None,
        consumption_attestation_sha256=None,
        status="blocked",
        result=None,
        output_sha256=None,
        error_type=error_type,
        error_message=message,
        legacy_baseline_match=None,
    )


def _execute_branch(
    fork: BranchForkResult,
    runner: Callable[[BranchWorkspace], BranchResult],
) -> BranchExecution:
    if fork.workspace is None or fork.consumption is None:
        return _blocked_execution(
            fork.branch,
            fork.error_type or "BranchForkFailed",
            fork.error_message or "branch workspace was not created",
        )
    try:
        consumption = verify_consumption_attestation(fork.workspace)
        if consumption != fork.consumption:
            raise SnapshotIntegrityError("consumption attestation changed before runner invocation")
    except Exception as error:
        return _blocked_execution(fork.branch, type(error).__name__, str(error))
    try:
        result = runner(fork.workspace)
        if result.branch != fork.branch:
            raise BranchContractError("runner returned another branch identity")
        if fork.branch == "v2" and result.publication_status != "not_published":
            raise BranchContractError("V2 publication is forbidden during Stage 4")
        return BranchExecution(
            branch=fork.branch,
            snapshot_identity=consumption.identity,
            consumption_attestation_sha256=consumption.attestation_sha256,
            status="success",
            result=result,
            output_sha256=sha256_bytes(result.output),
            error_type=None,
            error_message=None,
            legacy_baseline_match=None,
        )
    except Exception as error:
        return BranchExecution(
            branch=fork.branch,
            snapshot_identity=consumption.identity,
            consumption_attestation_sha256=consumption.attestation_sha256,
            status="failed",
            result=None,
            output_sha256=None,
            error_type=type(error).__name__,
            error_message=str(error),
            legacy_baseline_match=None,
        )


def _matches_legacy_baseline(
    execution: BranchExecution,
    baseline: LegacyBaseline,
) -> bool:
    if baseline.error_type is not None:
        return (
            execution.status == "failed"
            and execution.error_type == baseline.error_type
            and execution.error_message == baseline.error_message
        )
    return (
        execution.status == "success"
        and execution.result is not None
        and execution.output_sha256 == baseline.output_sha256
        and execution.result.exit_code == baseline.exit_code
    )


def _with_baseline_match(execution: BranchExecution, matched: bool) -> BranchExecution:
    if not matched and execution.status == "success":
        return BranchExecution(
            branch=execution.branch,
            snapshot_identity=execution.snapshot_identity,
            consumption_attestation_sha256=execution.consumption_attestation_sha256,
            status="failed",
            result=execution.result,
            output_sha256=execution.output_sha256,
            error_type="LegacyBaselineMismatch",
            error_message=_LEGACY_BASELINE_MISMATCH_MESSAGE,
            legacy_baseline_match=False,
        )
    return BranchExecution(
        branch=execution.branch,
        snapshot_identity=execution.snapshot_identity,
        consumption_attestation_sha256=execution.consumption_attestation_sha256,
        status=execution.status,
        result=execution.result,
        output_sha256=execution.output_sha256,
        error_type=execution.error_type,
        error_message=execution.error_message,
        legacy_baseline_match=matched,
    )


def execute_dual_run(
    fork: ForkResult,
    *,
    legacy_baseline: LegacyBaseline,
    legacy_runner: Callable[[BranchWorkspace], BranchResult],
    v2_runner: Callable[[BranchWorkspace], BranchResult],
) -> DualRunExecution:
    """Run independently, seal Legacy, and prove V2 did not mutate the Legacy tree."""
    legacy = _execute_branch(fork.legacy, legacy_runner)
    legacy = _with_baseline_match(legacy, _matches_legacy_baseline(legacy, legacy_baseline))

    legacy_tree_before_v2: str | None = None
    isolation_error: Exception | None = None
    if fork.legacy.workspace is not None:
        try:
            seal_private_tree(fork.legacy.workspace.root)
            legacy_tree_before_v2 = private_tree_sha256(fork.legacy.workspace.root)
        except Exception as error:
            isolation_error = error

    if isolation_error is not None:
        v2 = _blocked_execution(
            "v2",
            "LegacyIsolationSealFailed",
            str(isolation_error),
        )
    else:
        v2 = _execute_branch(fork.v2, v2_runner)

    if fork.legacy.workspace is not None and legacy_tree_before_v2 is not None:
        try:
            legacy_tree_after_v2 = private_tree_sha256(fork.legacy.workspace.root)
            if legacy_tree_after_v2 != legacy_tree_before_v2:
                raise BranchIsolationError("V2 changed the sealed Legacy workspace")
        except Exception as error:
            v2 = BranchExecution(
                branch="v2",
                snapshot_identity=v2.snapshot_identity,
                consumption_attestation_sha256=v2.consumption_attestation_sha256,
                status="failed",
                result=None,
                output_sha256=None,
                error_type=type(error).__name__,
                error_message=str(error),
                legacy_baseline_match=None,
            )
    return DualRunExecution(legacy=legacy, v2=v2)


def _decision_map(execution: BranchExecution) -> dict[str, MaterialDecision]:
    if execution.result is None:
        return {}
    return {decision.material_id: decision for decision in execution.result.decisions}


def _statistics(execution: BranchExecution) -> dict[str, int]:
    if execution.result is None:
        return {}
    return dict(execution.result.statistics)


def _execution_json(execution: BranchExecution) -> dict[str, object]:
    decisions = _decision_map(execution)
    dispositions = {
        disposition: sorted(
            material_id
            for material_id, decision in decisions.items()
            if decision.disposition == disposition
        )
        for disposition in ("included", "rejected", "deferred")
    }
    result = execution.result
    return {
        "baselineMatch": execution.legacy_baseline_match,
        "counts": {name: len(values) for name, values in dispositions.items()},
        "error": (
            None
            if execution.error_type is None
            else {
                "message": execution.error_message,
                "type": execution.error_type,
            }
        ),
        "exitCode": None if result is None else result.exit_code,
        "healthStatus": None if result is None else result.health_status,
        "llm": (
            None
            if result is None
            else {
                "model": result.llm_model,
                "provider": result.llm_provider,
                "status": result.llm_status,
            }
        ),
        "materials": dispositions,
        "outputSha256": execution.output_sha256,
        "publicationStatus": None if result is None else result.publication_status,
        "statistics": _statistics(execution),
        "status": execution.status,
    }


def _consumption_json(consumption: ConsumptionAttestation) -> dict[str, object]:
    return {
        "attestationSha256": consumption.attestation_sha256,
        "branch": consumption.branch,
        "consumedAt": consumption.consumed_at,
        "snapshot": _identity_json(consumption.identity),
    }


def build_daily_comparison(
    fork: ForkResult,
    execution: DualRunExecution,
    *,
    generated_at: str,
) -> bytes:
    """Build a canonical report only after both branch copies re-attest identical bytes."""
    _validate_timestamp(generated_at)
    if fork.legacy.workspace is None or fork.v2.workspace is None:
        raise DualRunError("daily comparison requires both branch workspaces")
    legacy_consumption = verify_consumption_attestation(fork.legacy.workspace)
    v2_consumption = verify_consumption_attestation(fork.v2.workspace)
    if not (legacy_consumption.identity == v2_consumption.identity == fork.source_identity):
        raise SnapshotIntegrityError(
            "daily comparison branch attestations do not identify the exact same snapshot bytes"
        )
    if (
        fork.legacy.consumption is None
        or fork.v2.consumption is None
        or legacy_consumption != fork.legacy.consumption
        or v2_consumption != fork.v2.consumption
    ):
        raise SnapshotIntegrityError("consumption attestation changed since the branch fork")
    if execution.legacy.branch != "legacy" or execution.v2.branch != "v2":
        raise BranchContractError("daily comparison execution branches are swapped")
    if (
        execution.legacy.snapshot_identity != fork.source_identity
        or execution.v2.snapshot_identity != fork.source_identity
        or execution.legacy.consumption_attestation_sha256 != legacy_consumption.attestation_sha256
        or execution.v2.consumption_attestation_sha256 != v2_consumption.attestation_sha256
    ):
        raise BranchContractError("daily comparison execution is not bound to this snapshot fork")

    legacy_decisions = _decision_map(execution.legacy)
    v2_decisions = _decision_map(execution.v2)
    legacy_included = {
        material_id
        for material_id, decision in legacy_decisions.items()
        if decision.disposition == "included"
    }
    v2_included = {
        material_id
        for material_id, decision in v2_decisions.items()
        if decision.disposition == "included"
    }
    common_included = sorted(legacy_included & v2_included)
    rubric_differences = [
        {
            "legacy": sorted(legacy_decisions[material_id].rubrics),
            "materialId": material_id,
            "v2": sorted(v2_decisions[material_id].rubrics),
        }
        for material_id in common_included
        if set(legacy_decisions[material_id].rubrics) != set(v2_decisions[material_id].rubrics)
    ]
    date_differences = [
        {
            "legacy": legacy_decisions[material_id].publication_date,
            "materialId": material_id,
            "v2": v2_decisions[material_id].publication_date,
        }
        for material_id in common_included
        if legacy_decisions[material_id].publication_date
        != v2_decisions[material_id].publication_date
    ]
    duplicate_differences = [
        {
            "legacy": legacy_decisions[material_id].duplicate_of,
            "materialId": material_id,
            "v2": v2_decisions[material_id].duplicate_of,
        }
        for material_id in common_included
        if legacy_decisions[material_id].duplicate_of != v2_decisions[material_id].duplicate_of
    ]
    legacy_statistics = _statistics(execution.legacy)
    v2_statistics = _statistics(execution.v2)
    statistics = {
        name: {
            "delta": v2_statistics.get(name, 0) - legacy_statistics.get(name, 0),
            "legacy": legacy_statistics.get(name),
            "v2": v2_statistics.get(name),
        }
        for name in sorted(set(legacy_statistics) | set(v2_statistics))
    }
    report = {
        "branches": {
            "legacy": {
                "consumption": _consumption_json(legacy_consumption),
                "outcome": _execution_json(execution.legacy),
            },
            "v2": {
                "consumption": _consumption_json(v2_consumption),
                "outcome": _execution_json(execution.v2),
            },
        },
        "comparisonFormat": COMPARISON_FORMAT,
        "differences": {
            "dates": date_differences,
            "duplicates": duplicate_differences,
            "onlyLegacy": sorted(legacy_included - v2_included),
            "onlyV2": sorted(v2_included - legacy_included),
            "rubrics": rubric_differences,
            "statistics": statistics,
        },
        "generatedAt": generated_at,
        "inputItemCount": fork.source_identity.item_count,
        "snapshot": _identity_json(fork.source_identity),
        "v2PublicationAllowed": False,
    }
    return canonical_json_line(report)


def write_daily_comparison(
    run_root: Path,
    fork: ForkResult,
    execution: DualRunExecution,
    *,
    generated_at: str,
) -> tuple[Path, str]:
    """Atomically write one private canonical daily report and return its SHA-256."""
    content = build_daily_comparison(fork, execution, generated_at=generated_at)
    comparison_root = run_root / "comparison"
    ensure_private_directory(comparison_root)
    report_path = comparison_root / COMPARISON_NAME
    atomic_write_new(report_path, content, mode=0o600)
    return report_path, hashlib.sha256(content).hexdigest()


__all__ = [
    "ATTESTATION_FORMAT",
    "ATTESTATION_NAME",
    "BRANCH_AREAS",
    "COMPARISON_FORMAT",
    "COMPARISON_NAME",
    "MUTABLE_BRANCH_AREAS",
    "BranchContractError",
    "BranchExecution",
    "BranchForkResult",
    "BranchIsolationError",
    "BranchResult",
    "BranchWorkspace",
    "ConsumptionAttestation",
    "DualRunError",
    "DualRunExecution",
    "ForkResult",
    "LegacyBaseline",
    "MaterialDecision",
    "build_daily_comparison",
    "consume_snapshot_for_branch",
    "execute_dual_run",
    "fork_snapshot",
    "verify_consumption_attestation",
    "write_daily_comparison",
]
