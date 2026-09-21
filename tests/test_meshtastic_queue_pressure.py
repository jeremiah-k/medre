"""Meshtastic outbound queue pressure and watermark contracts."""

from __future__ import annotations

import pytest

from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue


def test_pressure_threshold_validation() -> None:
    with pytest.raises(ValueError, match="warning_threshold_pct"):
        MeshtasticOutboundQueue(warning_threshold_pct=0)
    with pytest.raises(ValueError, match="critical_threshold_pct"):
        MeshtasticOutboundQueue(critical_threshold_pct=100)
    with pytest.raises(ValueError, match="less than"):
        MeshtasticOutboundQueue(
            warning_threshold_pct=90,
            critical_threshold_pct=75,
        )


async def test_pressure_transitions_before_full_rejection() -> None:
    queue = MeshtasticOutboundQueue(
        max_queue_size=4,
        warning_threshold_pct=50,
        critical_threshold_pct=75,
    )
    assert queue.pressure_state == "normal"

    await queue.enqueue({"text": "1"}, 0)
    assert queue.pressure_state == "normal"
    await queue.enqueue({"text": "2"}, 0)
    assert queue.pressure_state == "warning"
    await queue.enqueue({"text": "3"}, 0)
    assert queue.pressure_state == "critical"
    await queue.enqueue({"text": "4"}, 0)
    assert queue.pressure_state == "full"

    health = queue.queue_health
    assert health["warning_threshold_pct"] == 50.0
    assert health["critical_threshold_pct"] == 75.0
    assert health["peak_depth"] == 4
    assert health["pressure_state"] == "full"


async def test_pressure_recovers_as_queue_drains() -> None:
    queue = MeshtasticOutboundQueue(
        max_queue_size=4,
        warning_threshold_pct=50,
        critical_threshold_pct=75,
    )
    for i in range(4):
        await queue.enqueue({"text": str(i)}, 0)
    assert queue.pressure_state == "full"

    await queue.dequeue()
    assert queue.pressure_state == "critical"
    await queue.dequeue()
    assert queue.pressure_state == "warning"
    await queue.dequeue()
    assert queue.pressure_state == "normal"
    assert queue.queue_health["peak_depth"] == 4


async def test_unbounded_queue_never_reports_pressure() -> None:
    queue = MeshtasticOutboundQueue(max_queue_size=None)
    for i in range(20):
        await queue.enqueue({"text": str(i)}, 0)
    assert queue.pressure_state == "normal"
    assert queue.queue_health["utilization_pct"] == 0.0


async def test_adapter_health_degrades_only_at_critical_pressure() -> None:
    from medre.adapters.meshtastic.adapter import MeshtasticAdapter
    from medre.adapters.meshtastic.session import MeshtasticSession
    from medre.config.adapters.meshtastic import MeshtasticConfig

    config = MeshtasticConfig(
        adapter_id="pressure-health",
        connection_type="fake",
        queue_max_size=4,
        queue_warning_threshold_pct=50,
        queue_critical_threshold_pct=75,
    ).validate()
    adapter = MeshtasticAdapter(config)
    session = MeshtasticSession(config, config.adapter_id, "meshtastic")
    session._started = True
    adapter._session = session
    adapter._started = True

    await adapter.queue.enqueue({"text": "1"}, 0)
    await adapter.queue.enqueue({"text": "2"}, 0)
    assert adapter.queue.pressure_state == "warning"
    assert (await adapter.health_check()).health == "healthy"

    await adapter.queue.enqueue({"text": "3"}, 0)
    assert adapter.queue.pressure_state == "critical"
    assert (await adapter.health_check()).health == "degraded"

    diagnostics = adapter.diagnostics()
    assert diagnostics["queue_pressure_state"] == "critical"
    assert diagnostics["queue_warning_threshold_pct"] == 50.0
    assert diagnostics["queue_critical_threshold_pct"] == 75.0


async def test_drain_all_resets_pressure_state() -> None:
    queue = MeshtasticOutboundQueue(
        max_queue_size=4,
        warning_threshold_pct=50,
        critical_threshold_pct=75,
    )
    for i in range(4):
        await queue.enqueue({"text": str(i)}, 0)
    assert queue.pressure_state == "full"

    drained = queue.drain_all()

    assert len(drained) == 4
    assert queue.pending_count == 0
    assert queue.pressure_state == "normal"
