# medre-test-tier: compatibility
"""Fake delivery planning must use the capabilities reported by its provider."""

import pytest

from medre.adapter_registry import iter_adapter_specs
from medre.adapter_spec import AdapterSpec
from medre.core.planning.capabilities import resolve_adapter_capabilities
from medre.core.routing.models import RouteTarget


@pytest.mark.parametrize(
    "spec",
    [spec for spec in iter_adapter_specs() if spec.fake_adapter is not None],
    ids=lambda spec: spec.transport,
)
async def test_fake_planning_capabilities_match_health_declaration(
    spec: AdapterSpec,
) -> None:
    assert spec.fake_adapter is not None
    adapter = spec.fake_adapter.load()(adapter_id="provider")
    health = await adapter.health_check()
    actual = resolve_adapter_capabilities(
        {"provider": adapter}, RouteTarget(adapter="provider")
    )
    assert actual == health.capabilities
