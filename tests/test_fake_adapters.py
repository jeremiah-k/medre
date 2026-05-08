"""Tests for FakeTransportAdapter and FakePresentationAdapter: capabilities,
lifecycle (start/stop), inbound simulation, and event inspection lists.
"""

from __future__ import annotations

import pytest

from medre.adapters import (
    AdapterCapabilities,
    AdapterInfo,
    AdapterRole,
    FakePresentationAdapter,
    FakeTransportAdapter,
)
from medre.adapters.base import AdapterContext
from medre.core.events import CanonicalEvent, EventMetadata
from datetime import datetime, timezone


def _make_event(event_id: str = "evt-1") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_transport",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=[],
        relations=[],
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# FakeTransportAdapter
# ===================================================================


class TestFakeTransportAdapter:
    """FakeTransportAdapter capabilities, lifecycle, and inbound simulation."""

    def test_capabilities(self) -> None:
        """FakeTransportAdapter declares the expected capabilities."""
        adapter = FakeTransportAdapter("test_t")
        assert adapter.role == AdapterRole.TRANSPORT
        assert adapter.platform == "fake_transport"

    async def test_capabilities_from_health_check(
        self, make_adapter_context
    ) -> None:
        """Health check reports capabilities with correct feature flags."""
        adapter = FakeTransportAdapter("test_t")
        ctx = make_adapter_context("test_t")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.role == AdapterRole.TRANSPORT
        caps = info.capabilities
        assert caps.text is True
        assert caps.replies == "native"
        assert caps.reactions == "fallback"
        assert caps.edits == "unsupported"
        assert caps.deletes == "unsupported"
        assert caps.max_text_chars == 200

    async def test_start_sets_started_state(self, make_adapter_context) -> None:
        """After start(), is_started is True and ctx is stored."""
        adapter = FakeTransportAdapter("test_t")
        assert adapter.is_started is False
        ctx = make_adapter_context("test_t")
        await adapter.start(ctx)
        assert adapter.is_started is True
        assert adapter.ctx is ctx

    async def test_stop_clears_started_state(self, make_adapter_context) -> None:
        """After stop(), is_started is False."""
        adapter = FakeTransportAdapter("test_t")
        ctx = make_adapter_context("test_t")
        await adapter.start(ctx)
        await adapter.stop()
        assert adapter.is_started is False

    async def test_simulate_inbound_publishes_event(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """simulate_inbound calls publish_inbound and records the event."""
        adapter = FakeTransportAdapter("test_t")
        ctx = make_adapter_context("test_t")
        await adapter.start(ctx)
        event = _make_event()
        await adapter.simulate_inbound(event)
        assert event in inbound_collector.events
        assert event in adapter.delivered_events

    async def test_simulate_inbound_raises_before_start(self) -> None:
        """simulate_inbound raises RuntimeError if not started."""
        adapter = FakeTransportAdapter("test_t")
        event = _make_event()
        with pytest.raises(RuntimeError, match="has not been started"):
            await adapter.simulate_inbound(event)

    async def test_make_event(self) -> None:
        """make_event creates a valid CanonicalEvent."""
        adapter = FakeTransportAdapter("test_t", channel="ch-0")
        event = adapter.make_event(text="ping")
        assert event.source_adapter == "test_t"
        assert event.source_channel_id == "ch-0"
        assert event.payload["body"] == "ping"


# ===================================================================
# FakePresentationAdapter
# ===================================================================


class TestFakePresentationAdapter:
    """FakePresentationAdapter capabilities, lifecycle, and event receipt."""

    def test_capabilities(self) -> None:
        """FakePresentationAdapter has PRESENTATION role."""
        adapter = FakePresentationAdapter("test_p")
        assert adapter.role == AdapterRole.PRESENTATION
        assert adapter.platform == "fake_presentation"

    async def test_capabilities_from_health_check(
        self, make_adapter_context
    ) -> None:
        """Health check reports correct capabilities."""
        adapter = FakePresentationAdapter("test_p")
        ctx = make_adapter_context("test_p")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.role == AdapterRole.PRESENTATION
        caps = info.capabilities
        assert caps.text is True
        assert caps.replies == "native"
        assert caps.reactions == "native"
        assert caps.delivery_receipts is True

    async def test_start_and_stop(self, make_adapter_context) -> None:
        """Lifecycle transitions work."""
        adapter = FakePresentationAdapter("test_p")
        assert adapter.is_started is False
        ctx = make_adapter_context("test_p")
        await adapter.start(ctx)
        assert adapter.is_started is True
        await adapter.stop()
        assert adapter.is_started is False

    async def test_deliver_stores_received_event(self) -> None:
        """deliver() appends the event to received_events."""
        adapter = FakePresentationAdapter("test_p")
        event = _make_event()
        await adapter.deliver(event)
        assert event in adapter.received_events

    async def test_received_events_list_for_inspection(self) -> None:
        """Multiple delivered events accumulate in received_events."""
        adapter = FakePresentationAdapter("test_p")
        for i in range(3):
            await adapter.deliver(_make_event(event_id=f"evt-{i}"))
        assert len(adapter.received_events) == 3
        assert adapter.received_events[0].event_id == "evt-0"
        assert adapter.received_events[2].event_id == "evt-2"

    async def test_simulate_inbound_publishes_event(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """simulate_inbound calls publish_inbound and records in inbound_events."""
        adapter = FakePresentationAdapter("test_p")
        ctx = make_adapter_context("test_p")
        await adapter.start(ctx)
        event = _make_event()
        await adapter.simulate_inbound(event)
        assert event in inbound_collector.events
        assert event in adapter.inbound_events

    async def test_make_reply_event(self) -> None:
        """make_reply_event creates an event with a reply relation."""
        adapter = FakePresentationAdapter("test_p")
        target = _make_event(event_id="target-evt")
        reply = adapter.make_reply_event(target, text="reply text")
        assert len(reply.relations) == 1
        assert reply.relations[0].relation_type == "reply"
        assert reply.relations[0].target_event_id == "target-evt"
        assert reply.payload["body"] == "reply text"

    async def test_make_reaction_event(self) -> None:
        """make_reaction_event creates an event with a reaction relation."""
        adapter = FakePresentationAdapter("test_p")
        target = _make_event(event_id="target-evt")
        reaction = adapter.make_reaction_event(target, emoji="🔥")
        assert len(reaction.relations) == 1
        assert reaction.relations[0].relation_type == "reaction"
        assert reaction.relations[0].key == "🔥"
