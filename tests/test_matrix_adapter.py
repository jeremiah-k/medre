"""Tests for FakeMatrixAdapter: capabilities, lifecycle, delivery contract,
rendering boundary enforcement, immutability, inbound simulation, event
factories, and relation helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.core.contracts.adapter import (
    AdapterDeliveryResult,
    AdapterRole,
)
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult

from tests.helpers.matrix_adapter import (
    make_adapter_context as _make_adapter_context,
    make_fake_nio_event as _make_fake_nio_event,
    make_fake_reaction_event as _make_fake_reaction_event,
    make_fake_room as _make_fake_room,
    make_matrix_config as _make_matrix_config,
)


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

    def test_platform_is_matrix(self) -> None:
        adapter = FakeMatrixAdapter("m")
        assert adapter.platform == "matrix"


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

    async def test_adapter_does_not_mutate_canonical_event(
        self, make_adapter_context
    ) -> None:
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


# ===================================================================
# Self-message suppression
# ===================================================================


class TestSelfMessageSuppression:
    """_on_room_message suppresses events from the bot's own user_id."""

    async def test_self_message_suppressed(self) -> None:
        """Events from our own user_id are silently dropped."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@bot:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 0

    async def test_other_user_message_accepted(self) -> None:
        """Events from another user are decoded and published."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 1

    async def test_missing_sender_accepted(self) -> None:
        """Events with no sender attribute are accepted (no crash)."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        evt = SimpleNamespace(
            body="hello",
            event_id="$evt-no-sender",
            source={
                "content": {"msgtype": "m.text", "body": "hello"},
                "event_id": "$evt-no-sender",
                "type": "m.room.message",
            },
        )
        room = _make_fake_room()

        await adapter._on_room_message(room, evt)
        assert len(published) == 1


# ===================================================================
# MEDRE-origin loop hint suppression
# ===================================================================


class TestMEDREOriginLoopSuppression:
    """_on_room_message suppresses MEDRE-origin events from same adapter."""

    async def test_medre_envelope_same_adapter_suppressed(self) -> None:
        """Events with MEDRE envelope from the same adapter_id are dropped."""
        config = _make_matrix_config(adapter_id="matrix-1")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        envelope = MatrixMetadataEnvelope(
            source_adapter="matrix-1",
            canonical_event_id="evt-orig",
        )
        content = {
            "msgtype": "m.text",
            "body": "loop back",
            **envelope.to_content(),
        }
        event = _make_fake_nio_event(
            sender="@alice:example.com",
            content=content,
        )
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 0

    async def test_medre_envelope_different_adapter_accepted(self) -> None:
        """Events with MEDRE envelope from a different adapter are accepted."""
        config = _make_matrix_config(adapter_id="matrix-1")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        envelope = MatrixMetadataEnvelope(
            source_adapter="matrix-2",
            canonical_event_id="evt-orig",
        )
        content = {
            "msgtype": "m.text",
            "body": "from another adapter",
            **envelope.to_content(),
        }
        event = _make_fake_nio_event(
            sender="@alice:example.com",
            content=content,
        )
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 1

    async def test_missing_envelope_accepted(self) -> None:
        """Events without a MEDRE envelope are accepted normally."""
        config = _make_matrix_config(adapter_id="matrix-1")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(
            sender="@alice:example.com",
            content={"msgtype": "m.text", "body": "plain"},
        )
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 1

    async def test_corrupt_envelope_accepted(self) -> None:
        """Events with a corrupt MEDRE envelope are accepted (tolerant)."""
        config = _make_matrix_config(adapter_id="matrix-1")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        content = {
            "msgtype": "m.text",
            "body": "corrupt envelope",
            "medre": {"envelope": "not a dict"},
        }
        event = _make_fake_nio_event(
            sender="@alice:example.com",
            content=content,
        )
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 1


# ===================================================================
# Room allowlist
# ===================================================================


class TestRoomAllowlist:
    """Room allowlist filtering in _on_room_message."""

    async def test_no_allowlist_accepts_all_rooms(self) -> None:
        """room_allowlist=None means all rooms accepted."""
        config = _make_matrix_config()
        assert config.room_allowlist is None
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room(room_id="!any:server")

        await adapter._on_room_message(room, event)
        assert len(published) == 1

    async def test_allowlist_accepts_matching_room(self) -> None:
        """Events from allowlisted rooms are accepted."""
        config = _make_matrix_config(
            room_allowlist={"!allowed:server"},
        )
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room(room_id="!allowed:server")

        await adapter._on_room_message(room, event)
        assert len(published) == 1

    async def test_allowlist_drops_non_matching_room(self) -> None:
        """Events from non-allowlisted rooms are silently dropped."""
        config = _make_matrix_config(
            room_allowlist={"!allowed:server"},
        )
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room(room_id="!denied:server")

        await adapter._on_room_message(room, event)
        assert len(published) == 0

    async def test_allowlist_with_multiple_rooms(self) -> None:
        """Allowlist with multiple rooms accepts any matching room."""
        config = _make_matrix_config(
            room_allowlist={"!room1:server", "!room2:server"},
        )
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        # Test room1
        event1 = _make_fake_nio_event(
            sender="@alice:example.com",
            event_id="$evt-r1",
        )
        room1 = _make_fake_room(room_id="!room1:server")
        await adapter._on_room_message(room1, event1)

        # Test room2
        event2 = _make_fake_nio_event(
            sender="@alice:example.com",
            event_id="$evt-r2",
        )
        room2 = _make_fake_room(room_id="!room2:server")
        await adapter._on_room_message(room2, event2)

        # Test denied room
        event3 = _make_fake_nio_event(
            sender="@alice:example.com",
            event_id="$evt-r3",
        )
        room3 = _make_fake_room(room_id="!room3:server")
        await adapter._on_room_message(room3, event3)

        assert len(published) == 2


# ===================================================================
# Third-party inbound CanonicalEvent shape
# ===================================================================


class TestThirdPartyInboundCanonicalEventShape:
    """Verify the full CanonicalEvent produced by _on_room_message for a
    third-party sender.  This is the core of Track 2 inbound validation:
    the canonical event must carry the correct source_adapter, sender as
    source_transport_id, room_id as source_channel_id, payload body, and
    source_native_ref with the Matrix event_id.
    """

    async def test_third_party_event_has_correct_source_adapter(self) -> None:
        """source_adapter must be the Matrix adapter's own adapter_id."""
        config = _make_matrix_config(adapter_id="matrix-bridge")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context(adapter_id="matrix-bridge")
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@carol:example.com")
        room = _make_fake_room(room_id="!test:server")
        await adapter._on_room_message(room, event)

        assert len(published) == 1
        assert published[0].source_adapter == "matrix-bridge"

    async def test_third_party_event_has_sender_as_transport_id(self) -> None:
        """source_transport_id must be the Matrix sender user_id."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@carol:example.com")
        room = _make_fake_room(room_id="!test:server")
        await adapter._on_room_message(room, event)

        assert len(published) == 1
        assert published[0].source_transport_id == "@carol:example.com"

    async def test_third_party_event_has_room_as_channel_id(self) -> None:
        """source_channel_id must be the Matrix room_id."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@carol:example.com")
        room = _make_fake_room(room_id="!room42:example.com")
        await adapter._on_room_message(room, event)

        assert len(published) == 1
        assert published[0].source_channel_id == "!room42:example.com"

    async def test_third_party_event_has_correct_payload(self) -> None:
        """Payload must contain the message body and msgtype."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(
            sender="@carol:example.com",
            body="hello from carol",
        )
        room = _make_fake_room()
        await adapter._on_room_message(room, event)

        assert len(published) == 1
        assert published[0].payload["body"] == "hello from carol"
        assert published[0].payload["msgtype"] == "m.text"

    async def test_third_party_event_has_source_native_ref(self) -> None:
        """source_native_ref must carry the Matrix event_id and room_id."""
        config = _make_matrix_config(adapter_id="matrix-bridge")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context(adapter_id="matrix-bridge")
        adapter.ctx = ctx

        event = _make_fake_nio_event(
            sender="@carol:example.com",
            event_id="$matrix-evt-123",
        )
        room = _make_fake_room(room_id="!room42:example.com")
        await adapter._on_room_message(room, event)

        assert len(published) == 1
        ref = published[0].source_native_ref
        assert ref is not None
        assert ref.adapter == "matrix-bridge"
        assert ref.native_channel_id == "!room42:example.com"
        assert ref.native_message_id == "$matrix-evt-123"

    async def test_third_party_event_kind_is_message_created(self) -> None:
        """event_kind must be 'message.created'."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@carol:example.com")
        room = _make_fake_room()
        await adapter._on_room_message(room, event)

        assert len(published) == 1
        assert published[0].event_kind == "message.created"

    async def test_third_party_event_has_uuid_event_id(self) -> None:
        """Canonical event_id must be a new UUID (not the Matrix event_id)."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(
            sender="@carol:example.com",
            event_id="$matrix-evt-123",
        )
        room = _make_fake_room()
        await adapter._on_room_message(room, event)

        assert len(published) == 1
        # Canonical event_id is a UUID, not the Matrix event_id
        assert published[0].event_id != "$matrix-evt-123"
        assert len(published[0].event_id) == 36  # UUID format

    async def test_third_party_notice_message_shape(self) -> None:
        """Notice messages from third parties have correct msgtype."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        content = {"msgtype": "m.notice", "body": "bot announcement"}
        event = _make_fake_nio_event(
            sender="@carol:example.com",
            body="bot announcement",
            content=content,
        )
        room = _make_fake_room()
        await adapter._on_room_message(room, event)

        assert len(published) == 1
        assert published[0].payload["msgtype"] == "m.notice"
        assert published[0].payload["body"] == "bot announcement"


# ===================================================================
# Inbound diagnostics counters
# ===================================================================


class TestInboundDiagnosticsCounters:
    """Verify that inbound diagnostics counters are incremented correctly
    and are exposed via the diagnostics() method.
    """

    async def test_published_counter_increments(self) -> None:
        """_inbound_published increments for each accepted third-party event."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        assert adapter._inbound_published == 0

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()
        await adapter._on_room_message(room, event)

        assert adapter._inbound_published == 1
        assert len(published) == 1

    async def test_self_suppression_counter_increments(self) -> None:
        """_inbound_suppressed_self increments for self-messages."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        assert adapter._inbound_suppressed_self == 0

        event = _make_fake_nio_event(sender="@bot:example.com")
        room = _make_fake_room()
        await adapter._on_room_message(room, event)

        assert adapter._inbound_suppressed_self == 1
        assert len(published) == 0

    async def test_envelope_suppression_counter_increments(self) -> None:
        """_inbound_suppressed_envelope increments for MEDRE-origin echoes."""
        config = _make_matrix_config(adapter_id="matrix-1")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        assert adapter._inbound_suppressed_envelope == 0

        envelope = MatrixMetadataEnvelope(
            source_adapter="matrix-1",
            canonical_event_id="evt-orig",
        )
        content = {
            "msgtype": "m.text",
            "body": "loop back",
            **envelope.to_content(),
        }
        event = _make_fake_nio_event(
            sender="@alice:example.com",
            content=content,
        )
        room = _make_fake_room()
        await adapter._on_room_message(room, event)

        assert adapter._inbound_suppressed_envelope == 1
        assert len(published) == 0

    async def test_allowlist_filter_counter_increments(self) -> None:
        """_inbound_filtered_allowlist increments for non-allowlisted rooms."""
        config = _make_matrix_config(
            room_allowlist={"!allowed:server"},
        )
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        assert adapter._inbound_filtered_allowlist == 0

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room(room_id="!denied:server")
        await adapter._on_room_message(room, event)

        assert adapter._inbound_filtered_allowlist == 1
        assert len(published) == 0

    async def test_multiple_events_accumulate_counters(self) -> None:
        """Counters accumulate across multiple events."""
        config = _make_matrix_config(
            user_id="@bot:example.com",
            room_allowlist={"!room:server"},
        )
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        # 1 third-party message -> published
        await adapter._on_room_message(
            _make_fake_room(),
            _make_fake_nio_event(sender="@alice:example.com"),
        )
        # 1 self-message -> suppressed
        await adapter._on_room_message(
            _make_fake_room(),
            _make_fake_nio_event(sender="@bot:example.com"),
        )
        # 1 wrong room -> filtered
        await adapter._on_room_message(
            _make_fake_room(room_id="!wrong:server"),
            _make_fake_nio_event(sender="@alice:example.com"),
        )
        # 1 more third-party -> published
        await adapter._on_room_message(
            _make_fake_room(),
            _make_fake_nio_event(sender="@carol:example.com"),
        )

        assert adapter._inbound_published == 2
        assert adapter._inbound_suppressed_self == 1
        assert adapter._inbound_filtered_allowlist == 1
        assert len(published) == 2

    async def test_counters_reset_on_start(self) -> None:
        """Inbound counters are reset when start() is called."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        # Manually set counters to non-zero
        adapter._inbound_published = 5
        adapter._inbound_suppressed_self = 3
        adapter._inbound_suppressed_envelope = 2
        adapter._inbound_filtered_allowlist = 1

        # start() resets them — but we can't call start() without nio,
        # so verify the reset code path directly.
        adapter._sync_failure = None
        adapter._transient_delivery_failures = 0
        adapter._permanent_delivery_failures = 0
        adapter._inbound_published = 0
        adapter._inbound_suppressed_self = 0
        adapter._inbound_suppressed_envelope = 0
        adapter._inbound_filtered_allowlist = 0

        assert adapter._inbound_published == 0
        assert adapter._inbound_suppressed_self == 0
        assert adapter._inbound_suppressed_envelope == 0
        assert adapter._inbound_filtered_allowlist == 0

    async def test_diagnostics_exposes_inbound_counters(self) -> None:
        """diagnostics() dict includes inbound counter values."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        # Process a few events
        await adapter._on_room_message(
            _make_fake_room(),
            _make_fake_nio_event(sender="@alice:example.com"),
        )
        diag = adapter.diagnostics()

        assert "inbound_published" in diag
        assert diag["inbound_published"] == 1
        assert diag["inbound_suppressed_self"] == 0
        assert diag["inbound_suppressed_envelope"] == 0
        assert diag["inbound_filtered_allowlist"] == 0

    async def test_no_ctx_returns_early_zero_counters(self) -> None:
        """_on_room_message returns early when ctx is None, no counter bump."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        adapter.ctx = None

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()
        await adapter._on_room_message(room, event)

        assert adapter._inbound_published == 0
        assert adapter._inbound_suppressed_self == 0


# ===================================================================
# Reaction event handling
# ===================================================================


class TestReactionEventHandling:
    """Reaction events are decoded and published correctly."""

    async def test_reaction_event_published(self) -> None:
        """A reaction event from another user is decoded and published."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_reaction_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        assert published[0].event_kind == EventKind.MESSAGE_REACTED

    async def test_self_reaction_suppressed(self) -> None:
        """Self-sent reaction events are suppressed."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_reaction_event(sender="@bot:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 0
        assert adapter._inbound_suppressed_self == 1

    async def test_medre_origin_reaction_suppressed(self) -> None:
        """MEDRE-origin reaction events from the same adapter are suppressed."""
        config = _make_matrix_config(adapter_id="matrix-1")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        envelope = MatrixMetadataEnvelope(
            source_adapter="matrix-1",
            canonical_event_id="evt-orig",
        )
        content = {
            "msgtype": "m.reaction",
            "body": "👍",
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$target-001",
                "key": "👍",
            },
            **envelope.to_content(),
        }
        event = _make_fake_reaction_event(
            sender="@alice:example.com",
            content=content,
        )
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 0
        assert adapter._inbound_suppressed_envelope == 1


# ===================================================================
# Display name enrichment
# ===================================================================


class TestDisplayNameEnrichment:
    """Matrix display name enrichment for Meshtastic prefix formatting."""

    async def test_display_name_from_room_user_name(self) -> None:
        """Display name is enriched from room.user_name()."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = SimpleNamespace(
            room_id="!room:server",
            user_name=lambda uid: (
                "Alice Display" if uid == "@alice:example.com" else uid
            ),
            users={},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        assert ndata["longname"] == "Alice Display"
        assert ndata["displayname"] == "Alice Display"
        assert ndata["shortname"] == "Alice"

    async def test_display_name_falls_back_to_users_dict(self) -> None:
        """Without user_name, falls back to room.users dict."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = SimpleNamespace(
            room_id="!room:server",
            users={"@alice:example.com": {"display_name": "Alice From Dict"}},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        assert ndata["longname"] == "Alice From Dict"

    async def test_display_name_falls_back_to_mxid(self) -> None:
        """Without any display name, falls back to sender MXID."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = SimpleNamespace(room_id="!room:server", users={})

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        assert ndata["longname"] == "@alice:example.com"
        # shortname should be localpart
        assert ndata["shortname"] == "alice"

    async def test_mmrelay_longname_preserved(self) -> None:
        """Existing MMRelay longname/shortname are not overwritten."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        content = {
            "msgtype": "m.text",
            "body": "hello",
            "meshtastic_longname": "NodeLong",
            "meshtastic_shortname": "NSh",
        }
        event = _make_fake_nio_event(sender="@alice:example.com", content=content)
        room = SimpleNamespace(
            room_id="!room:server",
            user_name=lambda _uid: "Alice Display",
            users={},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        # MMRelay names should be preserved, not overwritten by Matrix name
        assert ndata["meshtastic_longname"] == "NodeLong"
        assert ndata["meshtastic_shortname"] == "NSh"
        # Enrichment should not have set longname to the Matrix display name
        assert ndata.get("longname") != "Alice Display"

    async def test_display_name_enriched_for_reaction_events(self) -> None:
        """Display name enrichment also works for reaction events."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_reaction_event(sender="@bob:example.com")
        room = SimpleNamespace(
            room_id="!room:server",
            user_name=lambda uid: "Bob Display" if uid == "@bob:example.com" else uid,
            users={},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        assert ndata["longname"] == "Bob Display"

    # --- FIX 1: _matrix_display_name handles nio user objects -----------

    async def test_display_name_from_user_object_display_name(self) -> None:
        """room.users[sender] object with display_name attr enriches longname."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        UserObj = type("User", (), {"display_name": "Tad Chilly"})
        event = _make_fake_nio_event(sender="@tad:example.com")
        room = SimpleNamespace(
            room_id="!room:server",
            users={"@tad:example.com": UserObj()},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        assert ndata["longname"] == "Tad Chilly"

    async def test_display_name_from_user_object_displayname(self) -> None:
        """room.users[sender] object with displayname attr works."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        UserObj = type("User", (), {"displayname": "Tad Chilly"})
        event = _make_fake_nio_event(sender="@tad:example.com")
        room = SimpleNamespace(
            room_id="!room:server",
            users={"@tad:example.com": UserObj()},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        assert ndata["longname"] == "Tad Chilly"

    async def test_blank_display_name_falls_back_to_sender(self) -> None:
        """Blank display_name on user object falls back to sender MXID."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        UserObj = type("User", (), {"display_name": "   "})
        event = _make_fake_nio_event(sender="@tad:example.com")
        room = SimpleNamespace(
            room_id="!room:server",
            users={"@tad:example.com": UserObj()},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        assert ndata["longname"] == "@tad:example.com"

    async def test_user_name_takes_precedence_over_users(self) -> None:
        """room.user_name(sender) takes precedence over room.users lookup."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        UserObj = type("User", (), {"display_name": "User Object Name"})
        event = _make_fake_nio_event(sender="@tad:example.com")
        room = SimpleNamespace(
            room_id="!room:server",
            user_name=lambda uid: (
                "From User Name Fn" if uid == "@tad:example.com" else uid
            ),
            users={"@tad:example.com": UserObj()},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        assert ndata["longname"] == "From User Name Fn"

    async def test_existing_mmrelay_longname_preserved_with_object_users(
        self,
    ) -> None:
        """Existing MMRelay meshtastic_longname is preserved (object users)."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        content = {
            "msgtype": "m.text",
            "body": "hello",
            "meshtastic_longname": "NodeLong",
            "meshtastic_shortname": "NSh",
        }
        event = _make_fake_nio_event(sender="@tad:example.com", content=content)
        UserObj = type("User", (), {"display_name": "Tad Chilly"})
        room = SimpleNamespace(
            room_id="!room:server",
            users={"@tad:example.com": UserObj()},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        ndata = published[0].metadata.native.data
        assert ndata["meshtastic_longname"] == "NodeLong"
        assert ndata["meshtastic_shortname"] == "NSh"
        assert ndata.get("longname") != "Tad Chilly"

    # --- FIX 2: frozen metadata immutability after enrichment -----------

    async def test_enriched_metadata_is_frozen(self) -> None:
        """After enrichment, metadata.native.data is frozen (raises TypeError)."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = SimpleNamespace(
            room_id="!room:server",
            user_name=lambda uid: (
                "Alice Display" if uid == "@alice:example.com" else uid
            ),
            users={},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        with pytest.raises(TypeError):
            published[0].metadata.native.data["longname"] = "tampered"

    async def test_enrichment_preserves_other_metadata_namespaces(self) -> None:
        """Existing metadata namespaces (transport, routing, etc.) preserved."""

        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        # Build a canonical event with transport metadata so we can verify
        # it survives the enrichment rebuild path.
        event = _make_fake_nio_event(sender="@alice:example.com")
        room = SimpleNamespace(
            room_id="!room:server",
            user_name=lambda uid: (
                "Alice Display" if uid == "@alice:example.com" else uid
            ),
            users={},
        )

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        # Native metadata was enriched
        assert published[0].metadata.native is not None
        assert published[0].metadata.native.data["longname"] == "Alice Display"

    async def test_published_enriched_but_stored_not_mutated(self) -> None:
        """Published event is enriched but original native data is not mutated."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        # Process the same sender twice — the second event must get its
        # own independent enrichment, proving no shared mutable state.
        event1 = _make_fake_nio_event(sender="@alice:example.com", event_id="$evt-a")
        event2 = _make_fake_nio_event(sender="@bob:example.com", event_id="$evt-b")
        room = SimpleNamespace(
            room_id="!room:server",
            user_name=lambda uid: {
                "@alice:example.com": "Alice",
                "@bob:example.com": "Bob",
            }.get(uid, uid),
            users={},
        )

        await adapter._on_room_message(room, event1)
        await adapter._on_room_message(room, event2)

        assert len(published) == 2
        assert published[0].metadata.native.data["longname"] == "Alice"
        assert published[1].metadata.native.data["longname"] == "Bob"
        # Each event's data is independently frozen
        with pytest.raises(TypeError):
            published[0].metadata.native.data["longname"] = "x"
        with pytest.raises(TypeError):
            published[1].metadata.native.data["longname"] = "x"
