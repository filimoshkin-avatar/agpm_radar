"""Immutable snapshot and isolated dual-run domain boundary."""

from typing import Final

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
COMPONENT_STATUS: Final = "stage-4-implemented"

__all__ = [
    "COMPONENT_NAME",
    "COMPONENT_STATUS",
    "BranchExecution",
    "BranchResult",
    "BranchWorkspace",
    "DualRunExecution",
    "ForkResult",
    "LegacyBaseline",
    "MaterialDecision",
    "SnapshotIdentity",
    "SnapshotIntegrityError",
    "VerifiedSnapshot",
    "build_daily_comparison",
    "create_snapshot",
    "execute_dual_run",
    "fork_snapshot",
    "verify_snapshot",
    "write_daily_comparison",
]
