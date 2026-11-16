# medre-test-tier: compatibility
"""The selected provider retains its production startup policy in core."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from tests.helpers.meshtastic import make_meshtastic_text_packet


async def test_default_drain_blocks_fresh_ingress_until_expiry() -> None:
    """Immediate-ingress fixture opt-outs must not replace the provider default."""
    config = MeshtasticConfig(adapter_id="default-drain")
    assert config.startup_packet_drain_seconds == 15.0
    publish = AsyncMock()
    context = AdapterContext(
        adapter_id=config.adapter_id,
        publish_inbound=publish,
        logger=logging.getLogger(__name__),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )
    adapter = FakeMeshtasticAdapter(config)
    await adapter.start(context)
    try:
        await adapter.simulate_inbound(
            make_meshtastic_text_packet(text="fresh during startup", packet_id=1)
        )
        publish.assert_not_awaited()
        assert adapter.diagnostics()["startup_drain_packets_dropped"] == 1
        assert adapter.diagnostics()["startup_backlog_packets_seen"] == 0

        # Advance this policy's deadline without sleeping or changing the loop clock.
        adapter._startup_drain_deadline_monotonic = time.monotonic() - 1
        await adapter.simulate_inbound(
            make_meshtastic_text_packet(text="after startup", packet_id=2)
        )
        publish.assert_awaited_once()
        assert publish.await_args.args[0].payload["body"] == "after startup"
        assert adapter.diagnostics()["startup_drain_packets_dropped"] == 1
        assert adapter.diagnostics()["startup_backlog_packets_seen"] == 1
    finally:
        await adapter.stop()
