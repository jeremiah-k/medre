"""Tests for FakeMatrixAdapter: capabilities, lifecycle, delivery contract,
rendering boundary enforcement, immutability, inbound simulation, event
factories, and relation helpers.
"""

from __future__ import annotations

import pytest

from medre.adapters import AdapterRole, FakeMatrixAdapter
from medre.adapters.base import AdapterContext, AdapterDeliveryResult
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult
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
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# Capabilities
# ===================================================================


class TestMatrixAdapterCapabilities:
    """FakeMatrixAdapter declares the correct role and platform."""

    def test_role_is_presentation(self) -> None:
        adapter = FakeMatrixAdapter("m")
        assert adapter.role == AdapterRole.PRESENTATION

    def test_platform_is_fake_matrix(self) -> None:
        adapter = FakeMatrixAdapter("m")
        assert adapter.platform == "fake_matrix"


# ===================================================================
# Lifecycle
# ===================================================================


class TestFakeMatrixAdapterLifecycle:
    """Start / stop / health-check transitions."""

    async def test_initial_started_state_is_false(self) -> None:
        adapter = FakeMatrixAdapter("m")
        assert adapter.is_started is False

    async def test_start_sets_started_state(self, make_adapter_context) -> None:
        adapter = FakeMatrixAdapter("m")
        ctx = make_adapter_context("m")
        await adapter.start(ctx)
        assert adapter.is_started is True
        assert adapter.ctx is ctx

    async def test_stop_clears_started_state(self, make_adapter_context) -> None:
        adapter = FakeMatrixAdapter("m")
        ctx = make_adapter_context("m")
        await adapter.start(ctx)
        await adapter.stop()
        assert adapter.is_started is False

    async def test_health_check_after_start(self, make_adapter_context) -> None:
        adapter = FakeMatrixAdapter("m")
        ctx = make_adapter_context("m")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"
        assert info.adapter_id == "m"
        assert info.role == AdapterRole.PRESENTATION


# ===================================================================
# Delivery contract
# ===================================================================


class TestFakeMatrixAdapterDeliver:
    """deliver() stores RenderingResult payloads correctly."""

    async def test_deliver_stores_rendering_result(self) -> None:
        adapter = FakeMatrixAdapter("m")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="m",
            target_channel="room-1",
            payload={"msgtype": "m.text", "body": "hello matrix"},
            metadata={"renderer": "matrix"},
        )
        delivery = await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert adapter.delivered_payloads[0].payload["body"] == "hello matrix"
        # Returns AdapterDeliveryResult with deterministic Matrix-like event ID.
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id == "$fake_evt-1"
        assert delivery.native_channel_id == "room-1"

    async def test_deliver_does_not_reformat(self) -> None:
        adapter = FakeMatrixAdapter("m")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="m",
            target_channel="room-1",
            payload={"msgtype": "m.text", "body": "original"},
            metadata={"renderer": "matrix"},
        )
        await adapter.deliver(result)
        assert adapter.delivered_payloads[0] is result

    async def test_deliver_preserves_payload_verbatim(self) -> None:
        adapter = FakeMatrixAdapter("m")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="m",
            target_channel="room-1",
            payload={"msgtype": "m.text", "body": "hello", "extra": [1, 2, 3]},
            metadata={"renderer": "matrix", "custom": True},
            truncated=True,
            fallback_applied="relation_reply",
        )
        await adapter.deliver(result)
        stored = adapter.delivered_payloads[0]
        assert stored is result
        assert stored.truncated is True
        assert stored.fallback_applied == "relation_reply"
        assert stored.payload["extra"] == [1, 2, 3]


# ===================================================================
# Rendering boundary
# ===================================================================


class TestFakeMatrixRenderingBoundary:
    """Adapter consumes RenderingResult, never performs its own formatting."""

    async def test_adapter_receives_rendering_result_not_raw_event(self) -> None:
        adapter = FakeMatrixAdapter("m")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="m",
            target_channel="room-1",
            payload={"msgtype": "m.text", "body": "hello"},
            metadata={"renderer": "matrix"},
        )
        await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert isinstance(adapter.delivered_payloads[0], RenderingResult)
        assert len(adapter.received_events) == 0

    async def test_adapter_does_not_perform_kind_specific_formatting(self) -> None:
        adapter = FakeMatrixAdapter("m")
        for kind in (EventKind.MESSAGE_TEXT, EventKind.MESSAGE_CREATED):
            result = RenderingResult(
                event_id=f"evt-{kind}",
                target_adapter="m",
                target_channel="room-1",
                payload={"msgtype": "m.text", "body": "test"},
                metadata={"renderer": "matrix"},
            )
            await adapter.deliver(result)

        assert len(adapter.delivered_payloads) == 2
        for stored in adapter.delivered_payloads:
            assert isinstance(stored, RenderingResult)
            assert stored.payload["body"] == "test"


# ===================================================================
# Immutability
# ===================================================================


class TestFakeMatrixAdapterImmutability:
    """Canonical events remain immutable through delivery."""

    async def test_adapter_does_not_mutate_canonical_event(self, make_adapter_context) -> None:
        adapter = FakeMatrixAdapter("m")
        ctx = make_adapter_context("m")
        await adapter.start(ctx)
        event = adapter.make_event(text="immutable test")
        result = RenderingResult(
            event_id=event.event_id,
            target_adapter="m",
            target_channel="room-1",
            payload={"msgtype": "m.text", "body": "immutable test"},
            metadata={"renderer": "matrix"},
        )
        await adapter.deliver(result)
        with pytest.raises(AttributeError):
            event.event_kind = "tampered"  # type: ignore[misc]


# ===================================================================
# Inbound simulation
# ===================================================================


class TestFakeMatrixAdapterSimulateInbound:
    """simulate_inbound publishes events through the adapter context."""

    async def test_simulate_inbound_publishes_to_ctx(
        self, make_adapter_context, inbound_collector
    ) -> None:
        adapter = FakeMatrixAdapter("m")
        ctx = make_adapter_context("m")
        await adapter.start(ctx)
        event = _make_event()
        await adapter.simulate_inbound(event)
        assert event in inbound_collector.events
        assert event in adapter.inbound_events

    async def test_simulate_inbound_without_start_raises(self) -> None:
        adapter = FakeMatrixAdapter("m")
        event = _make_event()
        with pytest.raises(RuntimeError, match="has not been started"):
            await adapter.simulate_inbound(event)


# ===================================================================
# make_event / make_reply_event / make_reaction_event
# ===================================================================


class TestFakeMatrixAdapterMakeEvent:
    """make_event creates valid canonical events."""

    def test_make_event_creates_canonical_event(self) -> None:
        adapter = FakeMatrixAdapter("m")
        event = adapter.make_event(text="ping")
        assert isinstance(event, CanonicalEvent)
        assert event.payload["body"] == "ping"

    def test_make_event_sets_correct_source_adapter(self) -> None:
        adapter = FakeMatrixAdapter("m")
        event = adapter.make_event(text="ping")
        assert event.source_adapter == "m"

    def test_make_event_with_payload_extra(self) -> None:
        adapter = FakeMatrixAdapter("m")
        event = adapter.make_event(text="hello", custom_field="value")
        assert event.payload["body"] == "hello"
        assert event.payload["custom_field"] == "value"


class TestFakeMatrixAdapterMakeReplyEvent:
    """make_reply_event creates events with reply relations."""

    def test_make_reply_event_creates_reply_relation(self) -> None:
        adapter = FakeMatrixAdapter("m")
        target = adapter.make_event(text="original")
        reply = adapter.make_reply_event(target, text="a reply")
        assert len(reply.relations) == 1

    def test_make_reply_event_creates_relation_with_type_reply(self) -> None:
        adapter = FakeMatrixAdapter("m")
        target = adapter.make_event(text="original")
        reply = adapter.make_reply_event(target, text="a reply")
        assert reply.relations[0].relation_type == "reply"

    def test_make_reply_event_sets_target_event_id(self) -> None:
        adapter = FakeMatrixAdapter("m")
        target = adapter.make_event(text="original")
        reply = adapter.make_reply_event(target, text="a reply")
        assert reply.relations[0].target_event_id == target.event_id


class TestFakeMatrixAdapterMakeReactionEvent:
    """make_reaction_event creates events with reaction relations."""

    def test_make_reaction_event_creates_reaction_relation(self) -> None:
        adapter = FakeMatrixAdapter("m")
        target = adapter.make_event(text="original")
        reaction = adapter.make_reaction_event(target, emoji="🔥")
        assert len(reaction.relations) == 1
        assert reaction.relations[0].relation_type == "reaction"

    def test_make_reaction_event_sets_emoji_key(self) -> None:
        adapter = FakeMatrixAdapter("m")
        target = adapter.make_event(text="original")
        reaction = adapter.make_reaction_event(target, emoji="🔥")
        assert reaction.relations[0].key == "🔥"
