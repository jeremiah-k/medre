"""Focused tests for FakeMeshtasticAdapter startup backlog suppression.

Verifies that the fake adapter mirrors the real adapter's startup backlog
suppression behavior: relay-classified packets with stale rxTime are
suppressed, fresh packets pass through, missing/malformed timestamps
are conservative (not suppressed), window 0 disables suppression,
and diagnostics counters track correctly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from tests.helpers.meshtastic import make_meshtastic_text_packet

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stale_packet(
    *,
    text: str = "old message",
    rx_time: float,
    packet_id: int = 99,
) -> dict:
    """Build a relay-classifiable text packet with an explicit rxTime."""
    pkt = make_meshtastic_text_packet(text=text, packet_id=packet_id)
    pkt["rxTime"] = rx_time
    return pkt


def _make_context(adapter_id: str = "fake-mesh-1") -> AdapterContext:
    """Build a minimal AdapterContext for testing."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# Stale packets suppressed
# ---------------------------------------------------------------------------


class TestFakeStalePacketsSuppressed:
    """Relay-classified packets with rxTime older than cutoff are suppressed."""

    async def test_stale_packet_suppressed(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        stale_rx = adapter._adapter_start_epoch - 20.0
        packet = _make_stale_packet(rx_time=stale_rx)

        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_not_called()
        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 1
        assert diag["startup_backlog_packets_suppressed"] == 1
        assert len(adapter.inbound_events) == 0

        await adapter.stop()

    async def test_very_old_packet_suppressed(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=5.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        ancient_rx = adapter._adapter_start_epoch - 86400.0
        packet = _make_stale_packet(rx_time=ancient_rx, text="ancient")

        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_not_called()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 1

        await adapter.stop()


# ---------------------------------------------------------------------------
# Fresh packets relayed
# ---------------------------------------------------------------------------


class TestFakeFreshPacketsRelayed:
    """Recent packets with rxTime inside the window are relayed normally."""

    async def test_recent_packet_relayed(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        fresh_rx = adapter._adapter_start_epoch - 2.0
        packet = _make_stale_packet(rx_time=fresh_rx, text="fresh")

        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 1
        assert diag["startup_backlog_packets_suppressed"] == 0
        assert len(adapter.inbound_events) == 1

        await adapter.stop()

    async def test_future_packet_relayed(self) -> None:
        """Packet with rxTime after start is always relayed."""
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        future_rx = adapter._adapter_start_epoch + 5.0
        packet = _make_stale_packet(rx_time=future_rx, text="future")

        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()


# ---------------------------------------------------------------------------
# Disabled suppression (window == 0)
# ---------------------------------------------------------------------------


class TestFakeDisabledSuppression:
    """startup_backlog_suppress_seconds == 0 disables suppression entirely."""

    async def test_zero_window_no_suppression(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        ancient_rx = adapter._adapter_start_epoch - 10000.0
        packet = _make_stale_packet(rx_time=ancient_rx, text="ancient")

        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_suppressed"] == 0
        assert diag["startup_backlog_packets_seen"] == 1

        await adapter.stop()


# ---------------------------------------------------------------------------
# Missing / malformed rxTime — conservative pass-through
# ---------------------------------------------------------------------------


class TestFakeMalformedTimestamps:
    """Missing or malformed rxTime never suppresses."""

    async def test_missing_rx_time_passes_through(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="no rxTime")
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_none_rx_time_passes_through(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="none rxTime")
        packet["rxTime"] = None
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_string_rx_time_passes_through(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="string rxTime")
        packet["rxTime"] = "1700000000"
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_bool_rx_time_passes_through(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="bool rxTime")
        packet["rxTime"] = True
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()


# ---------------------------------------------------------------------------
# Boundary: exactly at cutoff
# ---------------------------------------------------------------------------


class TestFakeBoundaryEquality:
    """Packets with rxTime exactly at cutoff are NOT suppressed."""

    async def test_exactly_at_cutoff_not_suppressed(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        cutoff = adapter._adapter_start_epoch - 10.0
        packet = _make_stale_packet(rx_time=cutoff, text="boundary")
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_just_before_cutoff_suppressed(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        cutoff = adapter._adapter_start_epoch - 10.0
        just_before = cutoff - 0.001
        packet = _make_stale_packet(rx_time=just_before)
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_not_called()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 1

        await adapter.stop()


# ---------------------------------------------------------------------------
# Counter semantics
# ---------------------------------------------------------------------------


class TestFakeCounterSemantics:
    """Counters track seen and suppressed packets correctly."""

    async def test_seen_only_increments_for_relay_packets(self) -> None:
        """startup_backlog_packets_seen only counts relay-classified packets."""
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        # Non-relay packet (telemetry)
        await adapter.simulate_inbound(
            {
                "fromId": "!node1",
                "id": 1,
                "decoded": {"portnum": "telemetry"},
            }
        )

        assert adapter.diagnostics()["startup_backlog_packets_seen"] == 0

        await adapter.stop()

    async def test_counters_multiple_packets(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        # Stale
        await adapter.simulate_inbound(
            _make_stale_packet(
                rx_time=adapter._adapter_start_epoch - 20.0,
                packet_id=1,
            )
        )
        # Fresh
        await adapter.simulate_inbound(
            _make_stale_packet(
                rx_time=adapter._adapter_start_epoch - 1.0,
                text="fresh",
                packet_id=2,
            )
        )
        # Another stale
        await adapter.simulate_inbound(
            _make_stale_packet(
                rx_time=adapter._adapter_start_epoch - 100.0,
                packet_id=3,
            )
        )

        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 3
        assert diag["startup_backlog_packets_suppressed"] == 2
        assert len(adapter.inbound_events) == 1

        await adapter.stop()


# ---------------------------------------------------------------------------
# Diagnostics exposure
# ---------------------------------------------------------------------------


class TestFakeStartupBacklogDiagnostics:
    """Diagnostics expose startup backlog counters and window info."""

    async def test_diagnostics_keys_present(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        assert "startup_backlog_packets_seen" in diag
        assert "startup_backlog_packets_suppressed" in diag
        assert "startup_backlog_suppress_seconds" in diag
        assert "adapter_start_epoch" in diag

        await adapter.stop()

    async def test_diagnostics_initial_values(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 0
        assert diag["startup_backlog_packets_suppressed"] == 0
        assert diag["startup_backlog_suppress_seconds"] == 10.0
        assert diag["adapter_start_epoch"] is not None
        assert diag["adapter_start_epoch"] > 0

        await adapter.stop()

    async def test_adapter_start_epoch_is_reasonable(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-mesh-1",
            startup_backlog_suppress_seconds=10.0,
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_context()
        before = time.time()
        await adapter.start(ctx)
        after = time.time()

        epoch = adapter.diagnostics()["adapter_start_epoch"]
        assert before <= epoch <= after

        await adapter.stop()
