"""Tests for FakeMeshCoreAdapter and MeshCoreAdapter: capabilities,
lifecycle (start/stop idempotence), delivery contract, inbound simulation,
rendering boundary enforcement, task scheduling, event subscription
scaffolding, and session delegation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from medre.adapters import AdapterRole, FakeMeshCoreAdapter
from medre.adapters.base import AdapterContext, AdapterDeliveryResult
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.meshcore.errors import MeshCoreConnectionError, MeshCoreSendError
from medre.adapters.meshcore.session import MeshCoreSession
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult


def _make_config(**overrides) -> MeshCoreConfig:
    defaults = dict(adapter_id="meshcore-1")
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


def _make_rendering_result(
    event_id: str = "evt-1",
    target_adapter: str = "meshcore-1",
    target_channel: str = "0",
    payload: dict | None = None,
) -> RenderingResult:
    return RenderingResult(
        event_id=event_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        payload=payload or {"text": "hello meshcore", "channel_index": 0, "meshnet_name": ""},
    )


def _make_contact_packet(
    text: str = "hello",
    sender: str = "abc123",
    timestamp: int = 42,
) -> dict:
    return {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": 0,
    }


def _make_channel_packet(
    text: str = "hello channel",
    channel_idx: int = 0,
    timestamp: int = 42,
) -> dict:
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": 0,
        "pubkey_prefix": "chan_sender",
    }


# ===================================================================
# Capabilities
# ===================================================================


class TestMeshCoreAdapterCapabilities:
    """FakeMeshCoreAdapter declares the correct role and platform."""

    def test_role_is_transport(self) -> None:
        adapter = FakeMeshCoreAdapter()
        assert adapter.role == AdapterRole.TRANSPORT

    def test_platform_is_meshcore(self) -> None:
        adapter = FakeMeshCoreAdapter()
        assert adapter.platform == "meshcore"

    def test_capabilities_text_true(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        assert _FAKE_MESHCORE_CAPABILITIES.text is True

    def test_capabilities_replies_unsupported(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        assert _FAKE_MESHCORE_CAPABILITIES.replies == "unsupported"

    def test_capabilities_reactions_unsupported(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        assert _FAKE_MESHCORE_CAPABILITIES.reactions == "unsupported"

    def test_capabilities_edits_unsupported(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        assert _FAKE_MESHCORE_CAPABILITIES.edits == "unsupported"

    def test_capabilities_deletes_unsupported(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        assert _FAKE_MESHCORE_CAPABILITIES.deletes == "unsupported"

    def test_capabilities_attachments_false(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        assert _FAKE_MESHCORE_CAPABILITIES.attachments is False

    def test_capabilities_direct_messages_false(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        assert _FAKE_MESHCORE_CAPABILITIES.direct_messages is False

    def test_capabilities_max_text_bytes_512(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        assert _FAKE_MESHCORE_CAPABILITIES.max_text_bytes == 512

    def test_capabilities_max_text_chars_512(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        assert _FAKE_MESHCORE_CAPABILITIES.max_text_chars == 512


class TestRealMeshCoreCapabilities:
    """Real MeshCoreAdapter capabilities match spec."""

    def test_real_adapter_role_is_transport(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        assert adapter.role == AdapterRole.TRANSPORT

    def test_real_adapter_capabilities_match_fake(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        real_caps = adapter._capabilities
        assert real_caps.text == _FAKE_MESHCORE_CAPABILITIES.text
        assert real_caps.replies == _FAKE_MESHCORE_CAPABILITIES.replies
        assert real_caps.reactions == _FAKE_MESHCORE_CAPABILITIES.reactions
        assert real_caps.edits == _FAKE_MESHCORE_CAPABILITIES.edits
        assert real_caps.deletes == _FAKE_MESHCORE_CAPABILITIES.deletes
        assert real_caps.attachments == _FAKE_MESHCORE_CAPABILITIES.attachments
        assert real_caps.direct_messages == _FAKE_MESHCORE_CAPABILITIES.direct_messages
        assert real_caps.max_text_bytes == _FAKE_MESHCORE_CAPABILITIES.max_text_bytes
        assert real_caps.max_text_chars == _FAKE_MESHCORE_CAPABILITIES.max_text_chars


# ===================================================================
# Lifecycle
# ===================================================================


class TestFakeMeshCoreAdapterLifecycle:
    """Start / stop / health-check transitions."""

    async def test_initial_started_state_is_false(self) -> None:
        adapter = FakeMeshCoreAdapter()
        assert adapter.is_started is False

    async def test_start_sets_started_state(self, make_adapter_context) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        assert adapter.is_started is True
        assert adapter.ctx is ctx

    async def test_stop_clears_started_state(self, make_adapter_context) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        await adapter.stop()
        assert adapter.is_started is False

    async def test_health_check_after_start(self, make_adapter_context) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"
        assert info.adapter_id == "fake_meshcore"
        assert info.role == AdapterRole.TRANSPORT


class TestMeshCoreAdapterLifecycle:
    """MeshCoreAdapter lifecycle: idempotent start/stop, health states."""

    async def test_start_fake_mode(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

    async def test_start_is_idempotent(self, make_adapter_context) -> None:
        """Calling start() twice is safe — second call is a no-op."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        await adapter.start(ctx)  # second call — no-op
        info = await adapter.health_check()
        assert info.health == "healthy"

    async def test_stop_is_idempotent(self) -> None:
        """Calling stop() on a never-started adapter is safe."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        await adapter.stop()  # never started — no-op
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_stop(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_health_unknown_before_start(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_non_fake_raises_connection_error(self, make_adapter_context) -> None:
        """Non-fake connection raises MeshCoreConnectionError."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        with pytest.raises(MeshCoreConnectionError):
            await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "unknown"


# ===================================================================
# Session delegation
# ===================================================================


class TestMeshCoreAdapterSessionDelegation:
    """Adapter delegates lifecycle to MeshCoreSession."""

    async def test_session_created_on_start(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        assert isinstance(adapter._session, MeshCoreSession)
        assert adapter._session.connected is True

    async def test_session_cleared_on_stop(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        await adapter.stop()
        assert adapter._session is None

    async def test_session_connected_affects_health(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        # In fake mode, session is connected → healthy
        assert adapter._session is not None
        assert adapter._session.connected is True
        info = await adapter.health_check()
        assert info.health == "healthy"


# ===================================================================
# Event subscription
# ===================================================================


class TestMeshCoreAdapterEventSubscription:
    """Event subscription scaffolding tests (legacy compat)."""

    async def test_subscribe_events_scaffolded(
        self, make_adapter_context
    ) -> None:
        """_subscribe_events() runs without error (delegated to session)."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        # Direct call should not raise
        adapter._subscribe_events()
        assert adapter._subscribed is True

    async def test_unsubscribe_events_without_subscribe(
        self, make_adapter_context
    ) -> None:
        """_unsubscribe_events() when not subscribed is a no-op."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        adapter._unsubscribe_events()  # no-op — no error

    async def test_unsubscribe_events_after_subscribe(
        self, make_adapter_context
    ) -> None:
        """_unsubscribe_events() clears the subscribed flag."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        adapter._subscribe_events()
        assert adapter._subscribed is True
        adapter._unsubscribe_events()
        assert adapter._subscribed is False


# ===================================================================
# Delivery contract
# ===================================================================


class TestFakeMeshCoreAdapterDeliver:
    """deliver() stores RenderingResult payloads correctly."""

    async def test_deliver_stores_rendering_result(self) -> None:
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert adapter.delivered_payloads[0] is result
        # Fake adapter returns AdapterDeliveryResult with deterministic ID
        assert delivery is not None
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id is not None
        assert delivery.native_channel_id == "0"

    async def test_deliver_returns_deterministic_packet_id(self) -> None:
        adapter = FakeMeshCoreAdapter()
        result1 = _make_rendering_result()
        result2 = _make_rendering_result()
        delivery1 = await adapter.deliver(result1)
        delivery2 = await adapter.deliver(result2)
        assert delivery1.native_message_id == "1"
        assert delivery2.native_message_id == "2"
        assert delivery1.native_channel_id == delivery2.native_channel_id

    async def test_deliver_does_not_reformat(self) -> None:
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result(payload={
            "text": "original", "channel_index": 0, "meshnet_name": "",
        })
        await adapter.deliver(result)
        assert adapter.delivered_payloads[0] is result

    async def test_deliver_rejects_canonical_event(self) -> None:
        adapter = FakeMeshCoreAdapter()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        with pytest.raises(TypeError, match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_deliver_failure_raises_send_error(self) -> None:
        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(MeshCoreSendError, match="simulated send failure"):
            await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 0

    async def test_deliver_failure_no_native_ref(self) -> None:
        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(MeshCoreSendError):
            await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 0

    async def test_fake_client_tracks_sent_packets(self) -> None:
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result()
        await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 1
        assert adapter.fake_client.sent_packets[0]["text"] == "hello meshcore"
        assert adapter.fake_client.sent_packets[0]["channel_index"] == 0


# ===================================================================
# Rendering boundary
# ===================================================================


class TestFakeMeshCoreRenderingBoundary:
    """Adapter consumes RenderingResult, never performs its own formatting."""

    async def test_adapter_receives_rendering_result_not_raw_event(self) -> None:
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result()
        await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert isinstance(adapter.delivered_payloads[0], RenderingResult)

    async def test_adapter_does_not_perform_kind_specific_formatting(self) -> None:
        adapter = FakeMeshCoreAdapter()
        for kind in (EventKind.MESSAGE_TEXT, EventKind.MESSAGE_CREATED):
            result = _make_rendering_result(event_id=f"evt-{kind}")
            await adapter.deliver(result)

        assert len(adapter.delivered_payloads) == 2
        for stored in adapter.delivered_payloads:
            assert isinstance(stored, RenderingResult)


# ===================================================================
# Inbound simulation
# ===================================================================


class TestFakeMeshCoreAdapterSimulateInbound:
    """simulate_inbound processes packets through classifier + codec."""

    async def test_simulate_inbound_contact_packet(
        self, make_adapter_context, inbound_collector
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        packet = _make_contact_packet(text="hello meshcore")
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert len(adapter.inbound_events) == 1
        event = inbound_collector.events[0]
        assert event.payload["body"] == "hello meshcore"

    async def test_simulate_inbound_channel_packet(
        self, make_adapter_context, inbound_collector
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        packet = _make_channel_packet(text="channel hello")
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "channel hello"

    async def test_simulate_inbound_without_start_raises(self) -> None:
        adapter = FakeMeshCoreAdapter()
        packet = _make_contact_packet()
        with pytest.raises(RuntimeError, match="has not been started"):
            await adapter.simulate_inbound(packet)

    async def test_simulate_inbound_ignores_non_text(self, make_adapter_context) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        packet = {"code": 0}
        await adapter.simulate_inbound(packet)
        assert len(adapter.inbound_events) == 0

    async def test_simulate_inbound_ignores_empty(self, make_adapter_context) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        packet = {}
        await adapter.simulate_inbound(packet)
        assert len(adapter.inbound_events) == 0


# ===================================================================
# make_text_event helper
# ===================================================================


class TestFakeMeshCoreAdapterMakeTextEvent:
    """make_text_event creates valid canonical events from packet data."""

    def test_make_text_event_creates_canonical_event(self) -> None:
        adapter = FakeMeshCoreAdapter()
        event = adapter.make_text_event(body="ping")
        assert isinstance(event, CanonicalEvent)
        assert event.payload["body"] == "ping"

    def test_make_text_event_sets_source_adapter(self) -> None:
        adapter = FakeMeshCoreAdapter()
        event = adapter.make_text_event()
        assert event.source_adapter == "fake_meshcore"

    def test_make_text_event_populates_native_ref(self) -> None:
        adapter = FakeMeshCoreAdapter()
        event = adapter.make_text_event(packet_id=999)
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "999"

    def test_make_text_event_with_sender(self) -> None:
        adapter = FakeMeshCoreAdapter()
        event = adapter.make_text_event(sender="custom_node")
        assert event.source_transport_id == "custom_node"

    def test_make_text_event_with_channel(self) -> None:
        adapter = FakeMeshCoreAdapter()
        event = adapter.make_text_event(channel=3)
        assert event.source_channel_id == "3"


# ===================================================================
# Real MeshCoreAdapter delivery + inbound
# ===================================================================


class TestMeshCoreAdapterDelivery:
    """Real adapter delivery and inbound via fake mode."""

    async def test_deliver_returns_none_in_fake(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context = None
        # Need to start first
        from unittest.mock import AsyncMock
        ctx = AdapterContext(
            adapter_id="meshcore-1",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=__import__("logging").getLogger("test"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is None

    async def test_deliver_rejects_canonical_event(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        with pytest.raises(TypeError, match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_simulate_inbound(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        packet = _make_contact_packet(text="via real adapter")
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "via real adapter"


# ===================================================================
# Task scheduling
# ===================================================================


class TestMeshCoreAdapterTaskScheduling:
    """Background tasks from _on_message are tracked and cleaned up."""

    async def test_on_message_creates_tracked_task(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        packet = _make_contact_packet(text="tracked")
        adapter._on_message(packet)

        # Allow the background task to complete
        await asyncio.sleep(0.05)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "tracked"
        # Task should have been discarded after completion
        assert len(adapter._background_tasks) == 0

    async def test_stop_cancels_background_tasks(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        # Inject a long-running task
        async def _slow():
            await asyncio.sleep(100)

        task = asyncio.create_task(_slow())
        adapter._background_tasks.add(task)

        await adapter.stop()
        assert task.cancelled() or task.done()
        assert len(adapter._background_tasks) == 0

    async def test_no_ensure_future(self) -> None:
        """Verify _on_message does not use asyncio.ensure_future."""
        import inspect
        source = inspect.getsource(MeshCoreAdapter._on_message)
        assert "ensure_future" not in source
        assert "create_task" in source


# ===================================================================
# Diagnostics
# ===================================================================


class TestMeshCoreAdapterDiagnostics:
    """Adapter diagnostics method exposes session state."""

    async def test_diagnostics_before_start(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        diag = adapter.diagnostics()
        assert diag["started"] is False
        assert "session" not in diag

    async def test_diagnostics_after_start(self, make_adapter_context) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        assert diag["started"] is True
        assert "session" in diag
        assert diag["session"]["connected"] is True

    async def test_diagnostics_no_secrets(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        diag = adapter.diagnostics()
        diag_str = str(diag)
        assert "private_key" not in diag_str
        assert "secret" not in diag_str
        assert "password" not in diag_str


# ===================================================================
# Compat guard
# ===================================================================


class TestMeshCoreCompat:
    """compat.py provides HAS_MESHCORE guard."""

    def test_compat_module_importable(self) -> None:
        from medre.adapters.meshcore.compat import HAS_MESHCORE
        assert isinstance(HAS_MESHCORE, bool)

    def test_has_meshcore_is_false_without_sdk(self) -> None:
        """In default test environment, meshcore SDK is not installed."""
        from medre.adapters.meshcore.compat import HAS_MESHCORE
        # The SDK is not installed in the test environment
        assert HAS_MESHCORE is False
