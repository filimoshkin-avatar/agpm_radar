"""Runtime database, artifact and immutable gazette validation."""

from typing import Final

from packages.validation.artifacts import (
    ArtifactValidationError,
    DocxValidationReport,
    validate_daily_docx,
    validate_daily_json,
)
from packages.validation.gazette import (
    GazetteValidationError,
    GazetteValidationReport,
    validate_gazette_candidate,
)
from packages.validation.public_issue import (
    PublicIssueValidationError,
    build_public_issue,
    build_public_issue_from_views,
    validate_public_issue_document,
    validate_public_value,
    verify_public_database_connection,
)

COMPONENT_NAME: Final = "validation"
COMPONENT_STATUS: Final = "stage-6-implemented"

__all__ = [
    "COMPONENT_NAME",
    "COMPONENT_STATUS",
    "ArtifactValidationError",
    "DocxValidationReport",
    "GazetteValidationError",
    "GazetteValidationReport",
    "PublicIssueValidationError",
    "build_public_issue",
    "build_public_issue_from_views",
    "validate_daily_docx",
    "validate_daily_json",
    "validate_gazette_candidate",
    "validate_public_issue_document",
    "validate_public_value",
    "verify_public_database_connection",
]
