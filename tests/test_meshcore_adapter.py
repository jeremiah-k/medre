"""Tests for FakeMeshCoreAdapter and MeshCoreAdapter: capabilities,
lifecycle (start/stop idempotence), delivery contract,
rendering boundary enforcement, and session delegation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from medre.adapters import FakeMeshCoreAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshcore.errors import MeshCoreConnectionError
from medre.adapters.meshcore.session import MeshCoreSession
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterDeliveryResult,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)
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
        payload=payload
        or {"text": "hello meshcore", "channel_index": 0, "meshnet_name": ""},
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

    async def test_session_connected_affects_health(self, make_adapter_context) -> None:
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
        result = _make_rendering_result(
            payload={
                "text": "original",
                "channel_index": 0,
                "meshnet_name": "",
            }
        )
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
        with pytest.raises(
            (TypeError, AdapterPermanentError), match="RenderingResult only"
        ):
            await adapter.deliver(event)

    async def test_deliver_failure_raises_send_error(self) -> None:
        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError, match="simulated send failure"):
            await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 0

    async def test_deliver_failure_no_native_ref(self) -> None:
        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError):
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
# Real MeshCoreAdapter delivery
# ===================================================================


class TestMeshCoreAdapterDelivery:
    """Real adapter delivery and inbound via fake mode."""

    async def test_deliver_returns_none_in_fake(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = None
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
        with pytest.raises(
            (TypeError, AdapterPermanentError), match="RenderingResult only"
        ):
            await adapter.deliver(event)


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


# ===================================================================
# Honest delivery semantics
# ===================================================================


class TestHonestDeliverySemantics:
    """Delivery results report honest 'local_accepted' status."""

    async def test_fake_adapter_delivery_status_is_local_accepted(self) -> None:
        """Fake adapter must NOT claim end-to-end delivery."""
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.metadata["delivery_status"] == "local_accepted"
        # delivery_note is a top-level field on AdapterDeliveryResult, not in metadata
        assert isinstance(delivery.delivery_note, str)
        assert delivery.delivery_note != ""

    async def test_fake_adapter_no_false_delivery_claim(self) -> None:
        """delivery_status must not say 'delivered' or 'confirmed'."""
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        status = delivery.metadata["delivery_status"]
        assert status not in ("delivered", "confirmed", "acknowledged")

    async def test_real_adapter_fake_mode_delivery_is_none(self) -> None:
        """Real adapter in fake mode returns None — no false delivery claim."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
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

    async def test_real_adapter_real_mode_honest_delivery_status(
        self, make_adapter_context
    ) -> None:
        """Real adapter with mocked session returns local_accepted metadata."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")

        # Manually construct a fake session to avoid real SDK connection.
        from unittest.mock import AsyncMock

        from medre.adapters.meshcore.session import MeshCoreSession

        fake_session = MeshCoreSession(
            config=_make_config(connection_type="fake"),
            adapter_id="meshcore-1",
        )
        # Start the fake session so it's connected.
        await fake_session.start(message_callback=lambda pkt: None)
        # Patch send_text to simulate a real SDK returning an ID.
        fake_session.send_text = AsyncMock(return_value="pkt-42")  # type: ignore[attr-defined]
        # Bypass real start — inject the fake session.
        adapter._session = fake_session
        adapter._started = True
        adapter.ctx = ctx

        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id == "pkt-42"
        assert delivery.metadata["delivery_status"] == "local_accepted"

        await fake_session.stop()


# ===================================================================
# Channel send vs DM send
# ===================================================================


class TestChannelVsDMSend:
    """Channel and DM delivery paths work correctly."""

    async def test_channel_send_sets_native_channel_id(self) -> None:
        """Channel delivery populates native_channel_id."""
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result(
            payload={"text": "channel msg", "channel_index": 3, "meshnet_name": ""},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_channel_id == "3"

    async def test_channel_send_tracks_in_fake_client(self) -> None:
        """Channel send is recorded in the fake client."""
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result(
            payload={"text": "chan hello", "channel_index": 5, "meshnet_name": ""},
        )
        await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 1
        assert adapter.fake_client.sent_packets[0]["channel_index"] == 5
        assert adapter.fake_client.sent_packets[0]["text"] == "chan hello"

    async def test_dm_send_passes_dest_id(self) -> None:
        """DM delivery passes dest_id to the fake client."""
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result(
            payload={
                "text": "dm hello",
                "channel_index": 0,
                "meshnet_name": "",
                "dest_id": "abcdef12",
            },
        )
        await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 1
        sent = adapter.fake_client.sent_packets[0]
        assert sent["dest_id"] == "abcdef12"
        assert sent["text"] == "dm hello"

    async def test_dm_send_without_dest_id_passes_none(self) -> None:
        """Delivery without dest_id passes None for dest_id."""
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result(
            payload={"text": "broadcast", "channel_index": 0, "meshnet_name": ""},
        )
        await adapter.deliver(result)
        sent = adapter.fake_client.sent_packets[0]
        assert sent["dest_id"] is None


# ===================================================================
# Send failure
# ===================================================================


class TestSendFailure:
    """Send failures propagate AdapterSendError cleanly."""

    async def test_send_failure_no_delivery_result(self) -> None:
        """Failed sends do not produce partial delivery results."""
        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError, match="simulated send failure"):
            await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 0

    async def test_send_failure_does_not_store_payload(self) -> None:
        """Failed sends do not store payloads."""
        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError):
            await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 0

    async def test_send_failure_recoverable(self) -> None:
        """Adapter recovers after a failure — next send succeeds."""
        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError):
            await adapter.deliver(result)

        adapter.set_deliver_failure(False)
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id == "1"


# ===================================================================
# Malformed SDK response
# ===================================================================


class TestMalformedSDKResponse:
    """Adapter handles unexpected SDK responses gracefully."""

    async def _make_real_adapter_with_session(
        self, make_adapter_context
    ) -> tuple["MeshCoreAdapter", "MeshCoreSession"]:
        """Helper: create a real adapter with an injected fake session."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")

        from medre.adapters.meshcore.session import MeshCoreSession

        fake_session = MeshCoreSession(
            config=_make_config(connection_type="fake"),
            adapter_id="meshcore-1",
        )
        await fake_session.start(message_callback=lambda pkt: None)
        adapter._session = fake_session
        adapter._started = True
        adapter.ctx = ctx
        return adapter, fake_session

    async def test_real_adapter_handles_none_from_session(
        self, make_adapter_context
    ) -> None:
        """When session.send_text returns None, adapter returns None."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_real_adapter_with_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(return_value=None)  # type: ignore[attr-defined]

        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is None
        await session.stop()

    async def test_real_adapter_handles_non_dict_payload(
        self, make_adapter_context
    ) -> None:
        """When payload is not a dict, adapter returns None gracefully."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_real_adapter_with_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(return_value="pkt-1")  # type: ignore[attr-defined]

        result = _make_rendering_result(payload="not a dict")  # type: ignore[arg-type]
        delivery = await adapter.deliver(result)
        assert delivery is None
        await session.stop()

    async def test_real_adapter_handles_session_not_initialised(self) -> None:
        """When session is None, adapter raises AdapterPermanentError."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshCoreAdapter(config)
        # Don't start — session remains None.
        result = _make_rendering_result()
        with pytest.raises(AdapterPermanentError, match="Session not initialised"):
            await adapter.deliver(result)


# ===================================================================
# Diagnostics shape
# ===================================================================


class TestDiagnosticsShape:
    """Diagnostics output has correct structure for both adapters."""

    def test_fake_adapter_diagnostics_before_start(self) -> None:
        """Fake adapter diagnostics before start show not started."""
        adapter = FakeMeshCoreAdapter()
        diag = adapter.diagnostics()
        assert diag["started"] is False
        assert diag["mode"] == "fake"
        assert diag["adapter_id"] == "fake_meshcore"
        assert diag["platform"] == "meshcore"
        assert diag["delivered_count"] == 0
        assert diag["inbound_count"] == 0

    async def test_fake_adapter_diagnostics_after_start(
        self, make_adapter_context
    ) -> None:
        """Fake adapter diagnostics after start show started."""
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        assert diag["started"] is True

    async def test_fake_adapter_diagnostics_tracks_counts(
        self, make_adapter_context
    ) -> None:
        """Fake adapter diagnostics track delivered/inbound counts."""
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        result = _make_rendering_result()
        await adapter.deliver(result)
        await adapter.deliver(result)

        packet = _make_contact_packet(text="diag test")
        await adapter.simulate_inbound(packet)

        diag = adapter.diagnostics()
        assert diag["delivered_count"] == 2
        assert diag["inbound_count"] == 1

    def test_real_adapter_diagnostics_before_start(self) -> None:
        """Real adapter diagnostics before start have expected shape."""
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        diag = adapter.diagnostics()
        assert "adapter_id" in diag
        assert "platform" in diag
        assert "started" in diag
        assert "mode" in diag
        assert diag["started"] is False
        assert "session" not in diag

    async def test_real_adapter_diagnostics_after_start(
        self, make_adapter_context
    ) -> None:
        """Real adapter diagnostics after start include session state."""
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        assert diag["started"] is True
        assert "session" in diag
        assert diag["session"]["connected"] is True
        # All values must be JSON-safe primitives
        _assert_json_safe(diag)

    async def test_real_adapter_diagnostics_sanitizes_non_primitives(
        self, make_adapter_context
    ) -> None:
        """Diagnostics sanitizes non-primitive values."""
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        # All values at every nesting level must be primitives
        _assert_json_safe(diag)


def _assert_json_safe(obj: object, path: str = "root") -> None:
    """Assert that *obj* contains only JSON-serializable primitives."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert isinstance(k, str), f"Non-string key at {path}: {k!r}"
            _assert_json_safe(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _assert_json_safe(v, f"{path}[{i}]")
    else:
        assert (
            isinstance(obj, (str, int, float, bool)) or obj is None
        ), f"Non-primitive at {path}: {type(obj).__name__} = {obj!r}"


# ===================================================================
# Repeated start / stop
# ===================================================================


class TestRepeatedStartStop:
    """Repeated start/stop cycles are safe and idempotent."""

    async def test_fake_adapter_repeated_start_stop(self, make_adapter_context) -> None:
        """Fake adapter survives multiple start/stop cycles."""
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        for _ in range(5):
            await adapter.start(ctx)
            assert adapter.is_started is True
            await adapter.stop()
            assert adapter.is_started is False

    async def test_fake_adapter_double_start(self, make_adapter_context) -> None:
        """Double start is a no-op."""
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        await adapter.start(ctx)  # no-op
        assert adapter.is_started is True
        await adapter.stop()

    async def test_fake_adapter_double_stop(self) -> None:
        """Double stop is a no-op."""
        adapter = FakeMeshCoreAdapter()
        await adapter.stop()  # never started
        await adapter.stop()  # still no-op
        assert adapter.is_started is False

    async def test_real_adapter_repeated_start_stop(self, make_adapter_context) -> None:
        """Real adapter survives multiple start/stop cycles in fake mode."""
        config = _make_config(connection_type="fake")
        for _ in range(3):
            adapter = MeshCoreAdapter(config)
            ctx = make_adapter_context("meshcore-1")
            await adapter.start(ctx)
            info = await adapter.health_check()
            assert info.health == "healthy"
            await adapter.stop()
            info = await adapter.health_check()
            assert info.health == "unknown"

    async def test_real_adapter_start_stop_start(self, make_adapter_context) -> None:
        """Real adapter can restart after stop."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)
        await adapter.stop()
        # Restart with same adapter
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"
        await adapter.stop()

    async def test_fake_adapter_deliver_works_after_restart(
        self, make_adapter_context
    ) -> None:
        """Fake adapter delivery works correctly after a restart cycle."""
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")

        # First cycle
        await adapter.start(ctx)
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id == "1"
        await adapter.stop()

        # Second cycle — counter continues
        await adapter.start(ctx)
        result2 = _make_rendering_result()
        delivery2 = await adapter.deliver(result2)
        assert delivery2 is not None
        assert delivery2.native_message_id == "2"
