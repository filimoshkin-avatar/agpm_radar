"""Import-adapter boundary reserved for the Stage 3 synthetic-first importer."""

from typing import Final

COMPONENT_NAME: Final = "legacy_bridge"
COMPONENT_STATUS: Final = "stage-2-skeleton"
RUNTIME_ACCESS_ENABLED: Final = False

__all__ = ["COMPONENT_NAME", "COMPONENT_STATUS", "RUNTIME_ACCESS_ENABLED"]
