"""Publisher adapter, durable state machine and disposable local simulation."""

from typing import Final

from packages.publisher.local_simulation import (
    ACTIVE_POINTER_NAME,
    ActivePointer,
    LocalPublisherError,
    LocalPublisherSimulator,
    PublisherLockBusyError,
    SimulatedPublisherCrashError,
    install_initial_release,
    read_active_pointer,
)
from packages.publisher.project_manager import (
    ProjectManagerReportError,
    build_project_manager_report,
    project_manager_report_bytes,
)
from packages.publisher.state_machine import (
    BLOCKING_STATE,
    INITIAL_STATE,
    TERMINAL_STATES,
    TRANSITIONS,
    CandidateState,
    PublisherStateError,
    PublisherStateMachine,
)

COMPONENT_NAME: Final = "publisher"
COMPONENT_STATUS: Final = "stage-7-implemented"

__all__ = [
    "ACTIVE_POINTER_NAME",
    "BLOCKING_STATE",
    "COMPONENT_NAME",
    "COMPONENT_STATUS",
    "INITIAL_STATE",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "ActivePointer",
    "CandidateState",
    "LocalPublisherError",
    "LocalPublisherSimulator",
    "ProjectManagerReportError",
    "PublisherLockBusyError",
    "PublisherStateError",
    "PublisherStateMachine",
    "SimulatedPublisherCrashError",
    "build_project_manager_report",
    "install_initial_release",
    "project_manager_report_bytes",
    "read_active_pointer",
]
