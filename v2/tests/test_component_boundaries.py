"""Required package-boundary tests."""

from __future__ import annotations

import pytest
from packages import (
    contracts,
    delta,
    domain,
    legacy_bridge,
    publisher,
    renderers,
    storage,
    validation,
)

COMPONENTS: tuple[tuple[str, str], ...] = (
    (contracts.COMPONENT_NAME, contracts.COMPONENT_STATUS),
    (delta.COMPONENT_NAME, delta.COMPONENT_STATUS),
    (domain.COMPONENT_NAME, domain.COMPONENT_STATUS),
    (legacy_bridge.COMPONENT_NAME, legacy_bridge.COMPONENT_STATUS),
    (publisher.COMPONENT_NAME, publisher.COMPONENT_STATUS),
    (renderers.COMPONENT_NAME, renderers.COMPONENT_STATUS),
    (storage.COMPONENT_NAME, storage.COMPONENT_STATUS),
    (validation.COMPONENT_NAME, validation.COMPONENT_STATUS),
)


@pytest.mark.parametrize(("name", "status"), COMPONENTS)
def test_component_has_an_explicit_implementation_status(name: str, status: str) -> None:
    assert name
    assert status in {"stage-2-skeleton", "stage-3-implemented", "stage-4-implemented"}


def test_legacy_bridge_has_no_runtime_access() -> None:
    assert legacy_bridge.RUNTIME_ACCESS_ENABLED is False
