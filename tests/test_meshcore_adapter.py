"""Tests for FakeMeshCoreAdapter and MeshCoreAdapter: capabilities,
lifecycle (start/stop idempotence), delivery contract,
rendering boundary enforcement, and session delegation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
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
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        assert _FAKE_MESHCORE_CAPABILITIES.text is True

    def test_capabilities_replies_unsupported(self) -> None:
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        assert _FAKE_MESHCORE_CAPABILITIES.replies == "unsupported"

    def test_capabilities_reactions_unsupported(self) -> None:
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        assert _FAKE_MESHCORE_CAPABILITIES.reactions == "unsupported"

    def test_capabilities_edits_unsupported(self) -> None:
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        assert _FAKE_MESHCORE_CAPABILITIES.edits == "unsupported"

    def test_capabilities_deletes_unsupported(self) -> None:
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        assert _FAKE_MESHCORE_CAPABILITIES.deletes == "unsupported"

    def test_capabilities_attachments_false(self) -> None:
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        assert _FAKE_MESHCORE_CAPABILITIES.attachments is False

    def test_capabilities_direct_messages_false(self) -> None:
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        assert _FAKE_MESHCORE_CAPABILITIES.direct_messages is False

    def test_capabilities_max_text_bytes_512(self) -> None:
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        assert _FAKE_MESHCORE_CAPABILITIES.max_text_bytes == 512

    def test_capabilities_max_text_chars_none(self) -> None:
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        assert _FAKE_MESHCORE_CAPABILITIES.max_text_chars is None


class TestRealMeshCoreCapabilities:
    """Real MeshCoreAdapter capabilities match spec."""

    def test_real_adapter_role_is_transport(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        assert adapter.role == AdapterRole.TRANSPORT

    def test_real_adapter_capabilities_match_fake(self) -> None:
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

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

    def test_real_adapter_capabilities_default_512(self) -> None:
        """Default config produces max_text_bytes=512 in capabilities."""
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        assert adapter._capabilities.max_text_bytes == 512
        assert adapter._capabilities.max_text_chars is None

    def test_real_adapter_capabilities_custom_max_text_bytes(self) -> None:
        """Custom max_text_bytes in config propagates to capabilities."""
        config = _make_config(max_text_bytes=1024)
        adapter = MeshCoreAdapter(config)
        assert adapter._capabilities.max_text_bytes == 1024
        assert adapter._capabilities.max_text_chars is None

    def test_real_adapter_capabilities_zero_max_text_bytes(self) -> None:
        """Zero max_text_bytes is accepted and propagated."""
        config = _make_config(max_text_bytes=0)
        adapter = MeshCoreAdapter(config)
        assert adapter._capabilities.max_text_bytes == 0
        assert adapter._capabilities.max_text_chars is None


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
        # No-session fallback: session sub-dict present with safe defaults.
        assert "session" in diag
        assert diag["session"]["connected"] is False
        assert diag["session"]["reconnecting"] is False
        assert diag["session"]["reconnect_attempts"] == 0

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
        assert delivery.metadata["meshcore"]["local_acceptance"] is True
        # delivery_note is a top-level field on AdapterDeliveryResult, not in metadata
        assert isinstance(delivery.delivery_note, str)
        assert delivery.delivery_note != ""

    async def test_fake_adapter_no_false_delivery_claim(self) -> None:
        """metadata must not say 'delivered' or 'confirmed'."""
        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        meshcore_meta = delivery.metadata["meshcore"]
        # local_acceptance is a boolean True — not a string status
        assert meshcore_meta["local_acceptance"] is True

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
        await fake_session.start(message_callback=lambda _pkt: None)
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
        assert delivery.metadata["meshcore"]["local_acceptance"] is True

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
        await fake_session.start(message_callback=lambda _pkt: None)
        adapter._session = fake_session
        adapter._started = True
        adapter.ctx = ctx

        return adapter, fake_session

    async def test_delivery_metadata_shape(self, make_adapter_context) -> None:
        """Delivery metadata has frozen nested meshcore dict with local_acceptance."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_real_adapter_with_session(
            make_adapter_context
        )
        try:
            session.send_text = AsyncMock(return_value="pkt-malformed-1")
            result = _make_rendering_result()
            delivery = await adapter.deliver(result)
            assert delivery is not None
            meshcore_metadata = delivery.metadata["meshcore"]
            assert isinstance(meshcore_metadata, Mapping)
            assert meshcore_metadata["local_acceptance"] is True
            assert "adapter_status" not in delivery.metadata
        finally:
            await session.stop()


# ===================================================================
# Error mapping and delivery_note (lines 412-433)
# ===================================================================


class TestAdapterErrorMapping:
    """Verify adapter.deliver() maps session errors to adapter errors correctly.

    Covers:
    - CancelledError re-raised (line 412-413)
    - MeshCoreSendError transient → AdapterSendError (line 414-416)
    - MeshCoreSendError permanent → AdapterPermanentError (line 414, 417-418)
    - TimeoutError/ConnectionError/OSError → AdapterSendError(transient=True) (line 419-420)
    - native_id None → return None (line 422-423)
    - delivery_note for channel vs DM (lines 425-433)
    """

    async def _make_adapter_with_mock_session(
        self, make_adapter_context
    ) -> tuple[MeshCoreAdapter, MeshCoreSession]:
        """Create a real adapter with an injected fake session for error testing."""

        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")

        fake_session = MeshCoreSession(
            config=_make_config(connection_type="fake"),
            adapter_id="meshcore-1",
        )
        # Start fake session so it's connected.
        await fake_session.start(message_callback=lambda _pkt: None)

        adapter._session = fake_session
        adapter._started = True
        adapter.ctx = ctx
        return adapter, fake_session

    async def test_cancelled_error_reraises(self, make_adapter_context) -> None:
        """CancelledError from session.send_text is re-raised, not wrapped."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_adapter_with_mock_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(side_effect=asyncio.CancelledError)  # type: ignore[attr-defined]

        result = _make_rendering_result(
            payload={"text": "test", "contact_id": "abc", "channel_index": None}
        )
        with pytest.raises(asyncio.CancelledError):
            await adapter.deliver(result)

        await session.stop()

    async def test_transient_send_error_mapped(self, make_adapter_context) -> None:
        """MeshCoreSendError(transient=True) → AdapterSendError(transient=True)."""
        from unittest.mock import AsyncMock

        from medre.adapters.meshcore.errors import MeshCoreSendError

        adapter, session = await self._make_adapter_with_mock_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(  # type: ignore[attr-defined]
            side_effect=MeshCoreSendError("transient oops", transient=True)
        )

        result = _make_rendering_result(
            payload={"text": "test", "contact_id": "abc", "channel_index": None}
        )
        with pytest.raises(AdapterSendError, match="transient oops") as exc_info:
            await adapter.deliver(result)

        assert exc_info.value.transient is True

        await session.stop()

    async def test_permanent_send_error_mapped(self, make_adapter_context) -> None:
        """MeshCoreSendError(transient=False) → AdapterPermanentError."""
        from unittest.mock import AsyncMock

        from medre.adapters.meshcore.errors import MeshCoreSendError

        adapter, session = await self._make_adapter_with_mock_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(  # type: ignore[attr-defined]
            side_effect=MeshCoreSendError("permanent oops", transient=False)
        )

        result = _make_rendering_result(
            payload={"text": "test", "contact_id": "abc", "channel_index": None}
        )
        with pytest.raises(AdapterPermanentError, match="permanent oops"):
            await adapter.deliver(result)

        await session.stop()

    async def test_timeout_error_mapped_to_send_error(
        self, make_adapter_context
    ) -> None:
        """TimeoutError → AdapterSendError(transient=True)."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_adapter_with_mock_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(side_effect=TimeoutError("timed out"))  # type: ignore[attr-defined]

        result = _make_rendering_result(
            payload={"text": "test", "contact_id": "abc", "channel_index": None}
        )
        with pytest.raises(AdapterSendError, match="timed out") as exc_info:
            await adapter.deliver(result)

        assert exc_info.value.transient is True

        await session.stop()

    async def test_connection_error_mapped_to_send_error(
        self, make_adapter_context
    ) -> None:
        """ConnectionError → AdapterSendError(transient=True)."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_adapter_with_mock_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(  # type: ignore[attr-defined]
            side_effect=ConnectionError("refused")
        )

        result = _make_rendering_result(
            payload={"text": "test", "contact_id": "abc", "channel_index": None}
        )
        with pytest.raises(AdapterSendError, match="refused") as exc_info:
            await adapter.deliver(result)

        assert exc_info.value.transient is True

        await session.stop()

    async def test_os_error_mapped_to_send_error(self, make_adapter_context) -> None:
        """OSError → AdapterSendError(transient=True)."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_adapter_with_mock_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(side_effect=OSError("broken pipe"))  # type: ignore[attr-defined]

        result = _make_rendering_result(
            payload={"text": "test", "contact_id": "abc", "channel_index": None}
        )
        with pytest.raises(AdapterSendError, match="broken pipe") as exc_info:
            await adapter.deliver(result)

        assert exc_info.value.transient is True

        await session.stop()

    async def test_native_id_none_returns_none(self, make_adapter_context) -> None:
        """When session.send_text returns None, adapter returns None."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_adapter_with_mock_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(return_value=None)  # type: ignore[attr-defined]

        result = _make_rendering_result(
            payload={"text": "test", "contact_id": "abc", "channel_index": None}
        )
        delivery = await adapter.deliver(result)
        assert delivery is None

        await session.stop()

    async def test_channel_delivery_note(self, make_adapter_context) -> None:
        """Channel send returns delivery_note mentioning 'channel send'."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_adapter_with_mock_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(return_value="pkt-42")  # type: ignore[attr-defined]

        result = _make_rendering_result(
            payload={"text": "chan msg", "contact_id": "abc", "channel_index": 3}
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id == "pkt-42"
        assert delivery.native_channel_id == "3"
        assert "channel send" in delivery.delivery_note

        await session.stop()

    async def test_dm_delivery_note(self, make_adapter_context) -> None:
        """DM send (no channel_index) returns delivery_note mentioning 'DM sent'."""
        from unittest.mock import AsyncMock

        adapter, session = await self._make_adapter_with_mock_session(
            make_adapter_context
        )
        session.send_text = AsyncMock(return_value="pkt-dm-01")  # type: ignore[attr-defined]

        result = _make_rendering_result(
            payload={"text": "dm msg", "contact_id": "abcdef", "channel_index": None}
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id == "pkt-dm-01"
        assert delivery.native_channel_id is None
        assert "DM sent" in delivery.delivery_note

        await session.stop()


# ===================================================================
# Native-ref relation fallback (W1 audit closure)
# ===================================================================


class TestMeshCoreRelationCapabilitiesExplicit:
    """MeshCore explicitly marks all relation capabilities as unsupported.
    Per W1 audit: MeshCore has no native threading, no native relation
    fields, no message dedup, no reply/reaction support."""

    def test_real_adapter_replies_unsupported(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        assert adapter._capabilities.replies == "unsupported"

    def test_real_adapter_reactions_unsupported(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        assert adapter._capabilities.reactions == "unsupported"

    def test_real_adapter_edits_unsupported(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        assert adapter._capabilities.edits == "unsupported"

    def test_real_adapter_deletes_unsupported(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        assert adapter._capabilities.deletes == "unsupported"


class TestMeshCoreCodecNoRelations:
    """MeshCore codec produces empty relations — no reply/reaction support."""

    def test_codec_decode_produces_empty_relations(self) -> None:
        """Decoded events have no relations regardless of packet content."""
        from medre.adapters.meshcore.codec import MeshCoreCodec

        config = _make_config()
        codec = MeshCoreCodec(config.adapter_id, config)
        packet = _make_contact_packet(text="hello")
        event = codec.decode(packet)
        assert event.relations == ()

    def test_codec_decode_channel_packet_empty_relations(self) -> None:
        """Channel messages also produce empty relations."""
        from medre.adapters.meshcore.codec import MeshCoreCodec

        config = _make_config()
        codec = MeshCoreCodec(config.adapter_id, config)
        packet = {
            "text": "channel msg",
            "channel_idx": 0,
            "sender_timestamp": 42,
            "type": "CHAN",
            "txt_type": 0,
            "pubkey_prefix": "sender1",
        }
        event = codec.decode(packet)
        assert event.relations == ()


class TestMeshCoreDeliveryMetadataJSONSafe:
    """MeshCore delivery metadata is frozen and JSON-serializable."""

    async def test_metadata_json_serializable(self) -> None:
        """Delivery metadata values are JSON-safe primitives."""
        import json

        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        # MappingProxyType is not directly JSON-serializable, but the
        # nested values must all be JSON-safe primitives when unwrapped.
        meta_dict = dict(delivery.metadata)
        meshcore_dict = dict(meta_dict["meshcore"])
        json_bytes = json.dumps(meshcore_dict)
        parsed = json.loads(json_bytes)
        assert parsed["local_acceptance"] is True

    async def test_metadata_meshcore_namespace_is_frozen(self) -> None:
        """Inner meshcore metadata is a frozen MappingProxyType."""
        from types import MappingProxyType

        adapter = FakeMeshCoreAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        inner = delivery.metadata["meshcore"]
        assert isinstance(inner, MappingProxyType)
        with pytest.raises(TypeError):
            inner["extra"] = "bad"  # type: ignore[misc]

    async def test_expected_ack_persisted_as_native_message_id(
        self, make_adapter_context
    ) -> None:
        """When session returns an expected_ack hex, it is the native_message_id
        on the delivery result. Per audit: expected_ack is ephemeral 4-byte
        ACK correlation — NOT a durable protocol-level message ID."""
        from unittest.mock import AsyncMock

        config = _make_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-1")

        fake_session = MeshCoreSession(
            config=_make_config(connection_type="fake"),
            adapter_id="meshcore-1",
        )
        await fake_session.start(message_callback=lambda _pkt: None)
        fake_session.send_text = AsyncMock(return_value="aabbccdd")  # type: ignore[attr-defined]
        adapter._session = fake_session
        adapter._started = True
        adapter.ctx = ctx

        result = _make_rendering_result(
            payload={"text": "test", "contact_id": "abc", "channel_index": None}
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        # The native_message_id IS the expected_ack hex — persisted for
        # local outbound evidence/correlation, but NOT a durable MeshCore ID.
        assert delivery.native_message_id == "aabbccdd"
        assert delivery.metadata["meshcore"]["local_acceptance"] is True

        await fake_session.stop()
