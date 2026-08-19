"""Immutable snapshot, dual-run and Stage 5 candidate domain boundary."""

from typing import Final

from packages.domain.candidate_mutations import (
    CandidateMutationError,
    CandidateMutationPlan,
    build_candidate_mutations,
    issue_state_hash,
)
from packages.domain.candidate_package import (
    CandidateBuildResult,
    CandidateDuplicateError,
    CandidatePackage,
    CandidatePackageError,
    build_candidate_package,
    verify_candidate_package,
)
from packages.domain.candidates import (
    CandidateValidationError,
    build_correction_candidate,
    build_daily_candidate,
    build_gazette_candidate,
    candidate_bytes,
    load_candidate,
    validate_candidate,
    validate_llm_outcome,
)
from packages.domain.dual_run import (
    BranchExecution,
    BranchResult,
    BranchWorkspace,
    DualRunExecution,
    ForkResult,
    LegacyBaseline,
    MaterialDecision,
    build_daily_comparison,
    execute_dual_run,
    fork_snapshot,
    write_daily_comparison,
)
from packages.domain.snapshot import (
    SnapshotIdentity,
    SnapshotIntegrityError,
    VerifiedSnapshot,
    create_snapshot,
    verify_snapshot,
)

COMPONENT_NAME: Final = "domain"
COMPONENT_STATUS: Final = "stage-5-implemented"

__all__ = [
    "COMPONENT_NAME",
    "COMPONENT_STATUS",
    "BranchExecution",
    "BranchResult",
    "BranchWorkspace",
    "CandidateBuildResult",
    "CandidateDuplicateError",
    "CandidateMutationError",
    "CandidateMutationPlan",
    "CandidatePackage",
    "CandidatePackageError",
    "CandidateValidationError",
    "DualRunExecution",
    "ForkResult",
    "LegacyBaseline",
    "MaterialDecision",
    "SnapshotIdentity",
    "SnapshotIntegrityError",
    "VerifiedSnapshot",
    "build_candidate_mutations",
    "build_candidate_package",
    "build_correction_candidate",
    "build_daily_candidate",
    "build_daily_comparison",
    "build_gazette_candidate",
    "candidate_bytes",
    "create_snapshot",
    "execute_dual_run",
    "fork_snapshot",
    "issue_state_hash",
    "load_candidate",
    "validate_candidate",
    "validate_llm_outcome",
    "verify_candidate_package",
    "verify_snapshot",
    "write_daily_comparison",
]
