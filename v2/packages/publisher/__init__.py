"""Publisher boundary with the Stage 5 Project Manager result adapter."""

from typing import Final

from packages.publisher.project_manager import (
    ProjectManagerReportError,
    build_project_manager_report,
    project_manager_report_bytes,
)

COMPONENT_NAME: Final = "publisher"
COMPONENT_STATUS: Final = "stage-5-adapter"

__all__ = [
    "COMPONENT_NAME",
    "COMPONENT_STATUS",
    "ProjectManagerReportError",
    "build_project_manager_report",
    "project_manager_report_bytes",
]
