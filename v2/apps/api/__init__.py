"""Dependency-free published-only Radar V2 API."""

from __future__ import annotations

from typing import Final

from packages.contracts import CONTRACT_FAMILY, CONTRACT_VERSION

from apps.api.application import RadarApplication
from apps.api.database import ActiveDatabaseManager, DatabaseIdentity, PublicDatabaseError
from apps.api.service import ApiResponse, RadarApi, SearchRateLimiter

APPLICATION_NAME: Final = "radar-v2-api"
APPLICATION_STAGE: Final = "stage-8-implemented"


def status_payload() -> dict[str, str]:
    """Return deterministic build identity without opening runtime data."""
    return {
        "application": APPLICATION_NAME,
        "contractFamily": CONTRACT_FAMILY,
        "contractVersion": CONTRACT_VERSION,
        "stage": APPLICATION_STAGE,
        "status": "ready",
    }


__all__ = [
    "APPLICATION_NAME",
    "APPLICATION_STAGE",
    "ActiveDatabaseManager",
    "ApiResponse",
    "DatabaseIdentity",
    "PublicDatabaseError",
    "RadarApi",
    "RadarApplication",
    "SearchRateLimiter",
    "status_payload",
]
