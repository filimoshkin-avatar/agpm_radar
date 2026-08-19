"""Inert Radar V2 API skeleton."""

from __future__ import annotations

from typing import Final

from packages.contracts import CONTRACT_FAMILY, CONTRACT_VERSION

APPLICATION_NAME: Final = "radar-v2-api"
APPLICATION_STAGE: Final = "stage-2-skeleton"


def status_payload() -> dict[str, str]:
    """Return deterministic build identity without opening runtime data."""
    return {
        "application": APPLICATION_NAME,
        "contractFamily": CONTRACT_FAMILY,
        "contractVersion": CONTRACT_VERSION,
        "stage": APPLICATION_STAGE,
        "status": "skeleton",
    }


__all__ = ["APPLICATION_NAME", "APPLICATION_STAGE", "status_payload"]
