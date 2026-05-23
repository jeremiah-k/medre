"""Focused tests for Meshtastic startup backlog suppression.

Verifies that relay-classified inbound packets with valid ``rxTime`` older
than ``adapter_start_time - startup_backlog_suppress_seconds`` are suppressed
before codec decode / event creation / publish.  Tests counters, diagnostics,
safe logging, boundary semantics, and conservative handling of missing or
malformed timestamps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.core.contracts.adapter import AdapterContext
from tests.helpers.meshtastic import (
    make_meshtastic_config,
    make_meshtastic_text_packet,
)

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


def _make_context(adapter_id: str = "mesh-1") -> AdapterContext:
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


class TestStalePacketsSuppressed:
    """Relay-classified packets with rxTime older than cutoff are suppressed."""

    async def test_stale_packet_suppressed_simulate_inbound(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        # Packet rxTime is 20 seconds before adapter start → stale
        stale_rx = adapter._adapter_start_epoch - 20.0
        packet = _make_stale_packet(rx_time=stale_rx)

        await adapter.simulate_inbound(packet)

        # No canonical event should have been published
        ctx.publish_inbound.assert_not_called()
        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 1
        assert diag["startup_backlog_packets_suppressed"] == 1
        assert diag["inbound_published"] == 0

        await adapter.stop()

    async def test_stale_packet_suppressed_on_packet_callback(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        stale_rx = adapter._adapter_start_epoch - 20.0
        packet = _make_stale_packet(rx_time=stale_rx)
        adapter._on_packet(packet)

        await asyncio.sleep(0.05)

        ctx.publish_inbound.assert_not_called()
        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_suppressed"] == 1
        assert diag["inbound_published"] == 0

        await adapter.stop()


# ---------------------------------------------------------------------------
# Fresh packets relayed
# ---------------------------------------------------------------------------


class TestFreshPacketsRelayed:
    """Recent packets with rxTime inside the window are relayed normally."""

    async def test_recent_packet_relayed(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        # Packet rxTime is 2 seconds before adapter start → within window
        fresh_rx = adapter._adapter_start_epoch - 2.0
        packet = _make_stale_packet(rx_time=fresh_rx, text="fresh")

        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 1
        assert diag["startup_backlog_packets_suppressed"] == 0
        assert diag["inbound_published"] == 1

        await adapter.stop()

    async def test_future_packet_relayed(self) -> None:
        """Packet with rxTime after start is always relayed."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
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


class TestDisabledSuppression:
    """startup_backlog_suppress_seconds == 0 disables suppression entirely."""

    async def test_zero_window_no_suppression(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        # Very old packet, but window is 0 → no suppression
        ancient_rx = adapter._adapter_start_epoch - 10000.0
        packet = _make_stale_packet(rx_time=ancient_rx, text="ancient")

        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_suppressed"] == 0
        assert diag["inbound_published"] == 1

        await adapter.stop()

    async def test_zero_window_seen_counter_still_increments(self) -> None:
        """With window=0, seen counter increments but suppressed stays 0."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = _make_stale_packet(rx_time=adapter._adapter_start_epoch - 50.0)
        await adapter.simulate_inbound(packet)

        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 1
        assert diag["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()


# ---------------------------------------------------------------------------
# Counter semantics
# ---------------------------------------------------------------------------


class TestCounterSemantics:
    """Counters track seen and suppressed packets correctly."""

    async def test_seen_only_increments_for_relay_packets(self) -> None:
        """startup_backlog_packets_seen only counts relay-classified packets."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        # Non-relay packet (telemetry) — should NOT be seen by backlog gate
        await adapter.simulate_inbound(
            {
                "fromId": "!node1",
                "id": 1,
                "decoded": {"portnum": "telemetry"},
            }
        )

        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 0
        assert diag["classifier_packets_seen"] == 1

        await adapter.stop()

    async def test_counters_multiple_packets(self) -> None:
        """Counters accumulate correctly across multiple packets."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        # Stale packet
        await adapter.simulate_inbound(
            _make_stale_packet(
                rx_time=adapter._adapter_start_epoch - 20.0,
                packet_id=1,
            )
        )
        # Fresh packet
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
        assert diag["inbound_published"] == 1

        await adapter.stop()

    async def test_classifier_counters_still_increment_for_suppressed_relay(
        self,
    ) -> None:
        """Classifier counters increment for suppressed relay packets."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        stale_rx = adapter._adapter_start_epoch - 20.0
        packet = _make_stale_packet(rx_time=stale_rx)
        await adapter.simulate_inbound(packet)

        diag = adapter.diagnostics()
        # Classifier should still see and relay-count the packet
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_relayed"] == 1
        # But startup backlog suppression prevents publish
        assert diag["startup_backlog_packets_suppressed"] == 1
        assert diag["inbound_published"] == 0

        await adapter.stop()


# ---------------------------------------------------------------------------
# No canonical event / receipt for suppressed packet
# ---------------------------------------------------------------------------


class TestNoCanonicalEventForSuppressed:
    """Suppressed packets produce no canonical event or delivery side effects."""

    async def test_no_publish_inbound_call(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        stale_rx = adapter._adapter_start_epoch - 20.0
        await adapter.simulate_inbound(_make_stale_packet(rx_time=stale_rx))

        ctx.publish_inbound.assert_not_called()

        await adapter.stop()

    async def test_inbound_published_counter_stays_zero(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        stale_rx = adapter._adapter_start_epoch - 20.0
        await adapter.simulate_inbound(_make_stale_packet(rx_time=stale_rx))

        assert adapter.diagnostics()["inbound_published"] == 0

        await adapter.stop()

    async def test_suppressed_then_fresh_only_publishes_once(self) -> None:
        """After a suppressed stale packet, a fresh one publishes correctly."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
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

        assert ctx.publish_inbound.call_count == 1
        assert adapter.diagnostics()["inbound_published"] == 1

        await adapter.stop()


# ---------------------------------------------------------------------------
# Malformed / missing rxTime — conservative pass-through
# ---------------------------------------------------------------------------


class TestMalformedTimestamps:
    """Missing or malformed rxTime never suppresses."""

    async def test_missing_rx_time_passes_through(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="no rxTime")
        # No rxTime key at all
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_none_rx_time_passes_through(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="none rxTime")
        packet["rxTime"] = None
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_string_rx_time_passes_through(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="string rxTime")
        packet["rxTime"] = "1700000000"
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_bool_rx_time_passes_through(self) -> None:
        """Boolean rxTime is not treated as numeric."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="bool rxTime")
        packet["rxTime"] = True
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_list_rx_time_passes_through(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="list rxTime")
        packet["rxTime"] = [1700000000]
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()


# ---------------------------------------------------------------------------
# Boundary: exactly at cutoff
# ---------------------------------------------------------------------------


class TestBoundaryEquality:
    """Packets with rxTime exactly equal to cutoff are NOT suppressed.

    The cutoff is ``adapter_start_epoch - suppress_seconds``.  A packet at
    exactly the cutoff boundary is treated as within the window (>=).
    """

    async def test_exactly_at_cutoff_not_suppressed(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        # rxTime == cutoff exactly → NOT suppressed (conservative)
        cutoff = adapter._adapter_start_epoch - 10.0
        packet = _make_stale_packet(rx_time=cutoff, text="boundary")
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_just_before_cutoff_suppressed(self) -> None:
        """rxTime slightly below cutoff → suppressed."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        cutoff = adapter._adapter_start_epoch - 10.0
        just_before = cutoff - 0.001
        packet = _make_stale_packet(rx_time=just_before)
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_not_called()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 1

        await adapter.stop()

    async def test_just_after_cutoff_not_suppressed(self) -> None:
        """rxTime slightly above cutoff → not suppressed."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        cutoff = adapter._adapter_start_epoch - 10.0
        just_after = cutoff + 0.001
        packet = _make_stale_packet(rx_time=just_after, text="just after")
        await adapter.simulate_inbound(packet)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()


# ---------------------------------------------------------------------------
# Diagnostics exposure
# ---------------------------------------------------------------------------


class TestStartupBacklogDiagnostics:
    """Diagnostics expose startup backlog counters and window info."""

    async def test_diagnostics_keys_present(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        assert "startup_backlog_packets_seen" in diag
        assert "startup_backlog_packets_suppressed" in diag
        assert "startup_backlog_suppress_seconds" in diag
        assert "adapter_start_epoch" in diag

        await adapter.stop()

    async def test_diagnostics_primitive_types(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        assert isinstance(diag["startup_backlog_packets_seen"], int)
        assert isinstance(diag["startup_backlog_packets_suppressed"], int)
        assert isinstance(diag["startup_backlog_suppress_seconds"], float)
        assert isinstance(diag["adapter_start_epoch"], float)

        await adapter.stop()

    async def test_diagnostics_initial_values(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
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
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        before = time.time()
        await adapter.start(ctx)
        after = time.time()

        epoch = adapter.diagnostics()["adapter_start_epoch"]
        assert before <= epoch <= after

        await adapter.stop()


# ---------------------------------------------------------------------------
# _on_packet callback path
# ---------------------------------------------------------------------------


class TestOnPacketCallbackPath:
    """Startup backlog suppression works via _on_packet (non-async callback)."""

    async def test_on_packet_suppresses_stale(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        stale_rx = adapter._adapter_start_epoch - 20.0
        packet = _make_stale_packet(rx_time=stale_rx)
        adapter._on_packet(packet)

        await asyncio.sleep(0.05)

        ctx.publish_inbound.assert_not_called()
        assert adapter.diagnostics()["startup_backlog_packets_suppressed"] == 1
        assert adapter.diagnostics()["inbound_published"] == 0

        await adapter.stop()

    async def test_on_packet_relays_fresh(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        fresh_rx = adapter._adapter_start_epoch - 2.0
        packet = _make_stale_packet(rx_time=fresh_rx, text="fresh callback")
        adapter._on_packet(packet)

        await asyncio.sleep(0.05)

        ctx.publish_inbound.assert_called_once()
        assert adapter.diagnostics()["inbound_published"] == 1

        await adapter.stop()

    async def test_on_packet_classifier_counters_increments_for_suppressed(
        self,
    ) -> None:
        """Classifier counters increment even for startup-backlog-suppressed packets."""
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        stale_rx = adapter._adapter_start_epoch - 20.0
        adapter._on_packet(_make_stale_packet(rx_time=stale_rx))

        await asyncio.sleep(0.05)

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_relayed"] == 1
        assert diag["startup_backlog_packets_suppressed"] == 1

        await adapter.stop()


# ---------------------------------------------------------------------------
# Non-relay packets unaffected
# ---------------------------------------------------------------------------


class TestNonRelayPacketsUnaffected:
    """Startup backlog suppression does not affect non-relay classified packets."""

    async def test_telemetry_packet_unaffected(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        await adapter.simulate_inbound(
            {
                "fromId": "!node1",
                "id": 1,
                "rxTime": adapter._adapter_start_epoch - 100.0,
                "decoded": {"portnum": "telemetry"},
            }
        )

        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 0
        assert diag["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_encrypted_packet_unaffected(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        await adapter.simulate_inbound(
            {
                "fromId": "!node1",
                "id": 1,
                "rxTime": adapter._adapter_start_epoch - 100.0,
                "encrypted": True,
                "decoded": {"portnum": "text_message", "text": "secret"},
            }
        )

        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 0
        assert diag["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()

    async def test_malformed_packet_unaffected(self) -> None:
        config = make_meshtastic_config(startup_backlog_suppress_seconds=10.0)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)

        await adapter.simulate_inbound(
            {
                "fromId": "!node1",
                "id": 1,
                "rxTime": adapter._adapter_start_epoch - 100.0,
            }
        )

        diag = adapter.diagnostics()
        assert diag["startup_backlog_packets_seen"] == 0
        assert diag["startup_backlog_packets_suppressed"] == 0

        await adapter.stop()
