"""Full-seed, typed delta generation and transactional staging apply."""

from typing import Final

from packages.delta.engine import (
    CONTRACT_TABLE_ORDER,
    DELTA_CONTRACT_VERSION,
    FULL_SEED_FORMAT,
    TABLE_CONTRACT_VERSION,
    DeltaApplyError,
    DeltaApplyReport,
    DeltaConflictError,
    DeltaValidationError,
    FullSeedReport,
    ReleaseDatabaseReport,
    ReleaseIdentity,
    apply_delta_to_staging,
    build_delta,
    export_full_seed,
    finalize_release_database,
    import_full_seed,
    inspect_release_database,
    validate_delta,
    validate_full_seed_manifest,
)

COMPONENT_NAME: Final = "delta"
COMPONENT_STATUS: Final = "stage-7-implemented"

__all__ = [
    "COMPONENT_NAME",
    "COMPONENT_STATUS",
    "CONTRACT_TABLE_ORDER",
    "DELTA_CONTRACT_VERSION",
    "FULL_SEED_FORMAT",
    "TABLE_CONTRACT_VERSION",
    "DeltaApplyError",
    "DeltaApplyReport",
    "DeltaConflictError",
    "DeltaValidationError",
    "FullSeedReport",
    "ReleaseDatabaseReport",
    "ReleaseIdentity",
    "apply_delta_to_staging",
    "build_delta",
    "export_full_seed",
    "finalize_release_database",
    "import_full_seed",
    "inspect_release_database",
    "validate_delta",
    "validate_full_seed_manifest",
]
