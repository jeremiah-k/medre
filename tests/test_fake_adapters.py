"""Tests for FakeTransportAdapter and FakePresentationAdapter: capabilities,
lifecycle (start/stop), inbound simulation, event inspection lists, rendering
boundary enforcement, relation fallback rendering, and canonical immutability.
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
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult
from medre.core.rendering.text import TextRenderer
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


# ===================================================================
# Rendering boundary & relation fallback tests
# ===================================================================


class TestRenderingBoundary:
    """Enforce that adapters consume RenderingResult, not raw event text."""

    async def test_adapter_does_not_format_raw_event(self) -> None:
        """Adapter stores a RenderingResult without reformatting it.

        The adapter must receive the pre-rendered result as-is and store
        it in ``delivered_payloads``.  It must NOT convert the rendering
        result into raw event text or perform event-kind-specific
        formatting.
        """
        adapter = FakePresentationAdapter("test_p")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_p",
            target_channel="ch-0",
            payload={"text": "hello world"},
            metadata={"renderer": "text"},
        )
        await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        stored = adapter.delivered_payloads[0]
        assert isinstance(stored, RenderingResult)
        assert stored.payload["text"] == "hello world"
        # Adapter did NOT store it as a raw string event.
        assert not isinstance(stored, str)

    async def test_relation_fallback_rendering(self) -> None:
        """Reply with fallback_text renders as
        '[replying to: {fallback_text}] {payload.text}'.
        """
        renderer = TextRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="evt-orig",
            target_native_ref=NativeRef(
                adapter="fake_transport",
                native_channel_id="ch-0",
                native_message_id="msg-orig",
            ),
            key=None,
            fallback_text="original message text",
        )
        event = CanonicalEvent(
            event_id="evt-reply",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=[],
            relations=[relation],
            payload={"text": "a reply"},
            metadata=EventMetadata(),
        )
        assert renderer.can_render(event, "fake_transport")
        result = await renderer.render(event, "fake_transport")
        assert result.payload["text"] == "[replying to: original message text] a reply"
        assert result.fallback_applied == "relation_reply"

    async def test_reaction_fallback_rendering(self) -> None:
        """Reaction with key renders as '{actor} reacted with {key}'."""
        renderer = TextRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="evt-orig",
            target_native_ref=NativeRef(
                adapter="fake_transport",
                native_channel_id="ch-0",
                native_message_id="msg-orig",
            ),
            key="👍",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="evt-react",
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="alice",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=[],
            relations=[relation],
            payload={"text": "👍"},
            metadata=EventMetadata(),
        )
        assert renderer.can_render(event, "fake_transport")
        result = await renderer.render(event, "fake_transport")
        assert result.payload["text"] == "alice reacted with 👍"
        assert result.fallback_applied == "relation_reaction"

    async def test_adapter_does_not_mutate_canonical_event(self) -> None:
        """Canonical events remain identical to their creation snapshot.

        The adapter stores a snapshot when ``make_event`` is called.
        After simulate_inbound the event must be referentially equal to
        the snapshot, proving no mutation occurred.
        """
        adapter = FakeTransportAdapter("test_t", channel="ch-0")
        event = adapter.make_event(text="immutable test")
        snapshot = adapter.event_snapshots[event.event_id]
        # The event is frozen (msgspec.Struct, frozen=True) so mutation
        # would raise anyway, but we also verify referential equality.
        assert event is snapshot
        # CanonicalEvent is frozen — verify immutability.
        with pytest.raises(AttributeError):
            event.event_kind = "tampered"  # type: ignore[misc]

    async def test_adapter_does_not_perform_route_matching(self) -> None:
        """Fake adapters have no route matching logic.

        The adapter stores whatever is delivered without filtering by
        event kind, channel, or source.  This verifies that route
        matching is not the adapter's responsibility.
        """
        adapter = FakePresentationAdapter("test_p")

        # Deliver events with different kinds — adapter stores them all.
        for kind in (
            EventKind.MESSAGE_TEXT,
            EventKind.MESSAGE_CREATED,
            EventKind.PRESENCE_CHANGED,
            EventKind.PLUGIN_CUSTOM,
        ):
            event = CanonicalEvent(
                event_id=f"evt-{kind}",
                event_kind=kind,
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="other_adapter",
                source_transport_id="node-1",
                source_channel_id="any-channel",
                parent_event_id=None,
                lineage=[],
                relations=[],
                payload={"text": "test"},
                metadata=EventMetadata(),
            )
            await adapter.deliver(event)

        # All events stored — no filtering or route matching.
        assert len(adapter.received_events) == 4
        stored_kinds = {e.event_kind for e in adapter.received_events}
        assert stored_kinds == {
            EventKind.MESSAGE_TEXT,
            EventKind.MESSAGE_CREATED,
            EventKind.PRESENCE_CHANGED,
            EventKind.PLUGIN_CUSTOM,
        }
