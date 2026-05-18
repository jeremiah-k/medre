"""Tests for FakeLxmfAdapter and LxmfAdapter: capabilities,
lifecycle (start/stop), delivery contract, inbound simulation, rendering
boundary enforcement, and packet simulation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from medre.adapters import FakeLxmfAdapter
from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.lxmf.compat import HAS_LXMF
from medre.adapters.lxmf.errors import LxmfConnectionError
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.contracts.adapter import (
    AdapterDeliveryResult,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult


def _make_config(**overrides) -> LxmfConfig:
    defaults = dict(adapter_id="lxmf-1")
    defaults.update(overrides)
    # storage_path is required when connection_type is reticulum.
    if (
        defaults.get("connection_type") == "reticulum"
        and "storage_path" not in defaults
    ):
        defaults["storage_path"] = "/tmp/medre-test-lxmf-router"
    return LxmfConfig(**defaults)


def _make_rendering_result(
    event_id: str = "evt-1",
    target_adapter: str = "lxmf-1",
    target_channel: str = None,
    payload: dict | None = None,
) -> RenderingResult:
    return RenderingResult(
        event_id=event_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        payload=payload
        or {
            "content": "hello lxmf",
            "title": "",
            "fields": {},
            "destination_hash": "",
        },
    )


def _make_text_packet(
    content: str = "hello",
    source_hash: str = "ab" * 16,
    msg_id: str = "cd" * 32,
) -> dict:
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id,
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


# ===================================================================
# Capabilities
# ===================================================================


class TestLxmfAdapterCapabilities:
    """FakeLxmfAdapter declares the correct role and platform."""

    def test_role_is_transport(self) -> None:
        adapter = FakeLxmfAdapter()
        assert adapter.role == AdapterRole.TRANSPORT

    def test_platform_is_lxmf(self) -> None:
        adapter = FakeLxmfAdapter()
        assert adapter.platform == "lxmf"

    def test_capabilities_text_true(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.text is True

    def test_capabilities_title_true(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.title is True

    def test_capabilities_metadata_fields_true(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.metadata_fields is True

    def test_capabilities_direct_messages_true(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.direct_messages is True

    def test_capabilities_replies_unsupported(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.replies == "unsupported"

    def test_capabilities_reactions_unsupported(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.reactions == "unsupported"

    def test_capabilities_edits_unsupported(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.edits == "unsupported"

    def test_capabilities_deletes_unsupported(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.deletes == "unsupported"

    def test_capabilities_attachments_false(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.attachments is False

    def test_capabilities_max_text_chars_16384(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.max_text_chars == 16384

    def test_capabilities_max_text_bytes_none(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.max_text_bytes is None

    def test_capabilities_store_and_forward_true(self) -> None:
        """LXMF supports store-and-forward via propagation nodes."""
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        assert _FAKE_LXMF_CAPABILITIES.store_and_forward is True


class TestRealLxmfCapabilities:
    """Real LxmfAdapter capabilities match spec."""

    def test_real_adapter_role_is_transport(self) -> None:
        config = _make_config()
        adapter = LxmfAdapter(config)
        assert adapter.role == AdapterRole.TRANSPORT

    def test_real_adapter_capabilities_match_fake(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        config = _make_config()
        adapter = LxmfAdapter(config)
        real_caps = adapter._capabilities
        assert real_caps.text == _FAKE_LXMF_CAPABILITIES.text
        assert real_caps.title == _FAKE_LXMF_CAPABILITIES.title
        assert real_caps.metadata_fields == _FAKE_LXMF_CAPABILITIES.metadata_fields
        assert real_caps.direct_messages == _FAKE_LXMF_CAPABILITIES.direct_messages
        assert real_caps.replies == _FAKE_LXMF_CAPABILITIES.replies
        assert real_caps.max_text_chars == _FAKE_LXMF_CAPABILITIES.max_text_chars

    def test_real_adapter_store_and_forward_true(self) -> None:
        """Real LXMF adapter must report store_and_forward=True."""
        config = _make_config()
        adapter = LxmfAdapter(config)
        assert adapter._capabilities.store_and_forward is True


# ===================================================================
# Lifecycle
# ===================================================================


class TestFakeLxmfAdapterLifecycle:
    """Start / stop / health-check transitions."""

    async def test_initial_started_state_is_false(self) -> None:
        adapter = FakeLxmfAdapter()
        assert adapter.is_started is False

    async def test_start_sets_started_state(self, make_adapter_context) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        assert adapter.is_started is True
        assert adapter.ctx is ctx

    async def test_stop_clears_started_state(self, make_adapter_context) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        await adapter.stop()
        assert adapter.is_started is False

    async def test_health_check_after_start(self, make_adapter_context) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"
        assert info.adapter_id == "fake_lxmf"
        assert info.role == AdapterRole.TRANSPORT


# ===================================================================
# Real LxmfAdapter lifecycle
# ===================================================================


class TestLxmfAdapterLifecycle:
    """LxmfAdapter lifecycle with fake config."""

    async def test_start_fake_mode(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

    async def test_start_is_idempotent(self, make_adapter_context) -> None:
        """Calling start() twice is a no-op."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

    async def test_stop_is_idempotent(self, make_adapter_context) -> None:
        """Calling stop() twice is a no-op."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        await adapter.stop()
        await adapter.stop()  # second stop is no-op
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_stop(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_health_unknown_before_start(self) -> None:
        config = _make_config()
        adapter = LxmfAdapter(config)
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_deliver_returns_none_in_tranche1(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        # In fake mode via session, deliver now returns an AdapterDeliveryResult
        # with pending state (not None).
        assert delivery is not None
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id is not None
        await adapter.stop()

    async def test_deliver_rejects_canonical_event(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
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

    async def test_simulate_inbound(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        packet = _make_text_packet(content="via real adapter")
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "via real adapter"


# ===================================================================
# Non-fake connection mode
# ===================================================================


class TestLxmfAdapterNonFakeMode:
    """Non-fake connection_type behaviour — always raises, never healthy."""

    async def test_non_fake_reticulum_raises_without_sdk(
        self, make_adapter_context
    ) -> None:
        """Non-fake reticulum mode raises when SDK is not available.

        When HAS_LXMF is False, start() must raise a clear
        LxmfConnectionError.
        """
        import medre.adapters.lxmf.adapter as _adapter_mod

        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")

        original = _adapter_mod.HAS_LXMF
        try:
            _adapter_mod.HAS_LXMF = False
            with pytest.raises(LxmfConnectionError):
                await adapter.start(ctx)
        finally:
            _adapter_mod.HAS_LXMF = original

        assert adapter._started is False

    async def test_non_fake_without_sdk_raises_dependency_error(
        self, make_adapter_context
    ) -> None:
        """Non-fake mode with HAS_LXMF=False raises missing-SDK error."""
        import medre.adapters.lxmf.adapter as _adapter_mod

        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")

        original = _adapter_mod.HAS_LXMF
        try:
            _adapter_mod.HAS_LXMF = False
            with pytest.raises(LxmfConnectionError, match="lxmf/RNS not installed"):
                await adapter.start(ctx)
        finally:
            _adapter_mod.HAS_LXMF = original

        assert adapter._started is False

    async def test_non_fake_never_reports_healthy(self, make_adapter_context) -> None:
        """Non-fake reticulum mode never reaches _started=True when
        SDK is unavailable.

        When HAS_LXMF is False, start() must raise.  When HAS_LXMF is
        True, the outcome depends on whether the packages are actually
        importable and Reticulum can be initialised — so we only assert
        the False case.
        """
        import medre.adapters.lxmf.adapter as _adapter_mod

        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")

        # Case 1: HAS_LXMF=False → must raise.
        original = _adapter_mod.HAS_LXMF
        try:
            _adapter_mod.HAS_LXMF = False
            with pytest.raises(LxmfConnectionError):
                await adapter.start(ctx)
        finally:
            _adapter_mod.HAS_LXMF = original

        assert adapter._started is False
        info = await adapter.health_check()
        assert info.health != "healthy"


# ===================================================================
# Event subscription scaffold
# ===================================================================


class TestLxmfAdapterEventSubscription:
    """_subscribe_events / _unsubscribe_events scaffold."""

    async def test_subscribe_events_logs_without_error(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        # _subscribe_events was called during start for fake mode
        # but it should not raise
        assert adapter._started is True

    async def test_unsubscribe_events_on_stop(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        await adapter.stop()
        assert adapter._started is False


# ===================================================================
# Delivery contract
# ===================================================================


class TestFakeLxmfAdapterDeliver:
    """deliver() stores RenderingResult payloads correctly."""

    async def test_deliver_stores_rendering_result(self) -> None:
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert adapter.delivered_payloads[0] is result
        assert delivery is not None
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id is not None

    async def test_deliver_returns_deterministic_message_id(self) -> None:
        adapter = FakeLxmfAdapter()
        result1 = _make_rendering_result()
        result2 = _make_rendering_result()
        delivery1 = await adapter.deliver(result1)
        delivery2 = await adapter.deliver(result2)
        assert delivery1.native_message_id != delivery2.native_message_id
        # Both should be 64-char hex strings (SHA-256)
        assert len(delivery1.native_message_id) == 64
        assert len(delivery2.native_message_id) == 64

    async def test_deliver_does_not_reformat(self) -> None:
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result(
            payload={
                "content": "original",
                "title": "T",
                "fields": {},
                "destination_hash": "",
            }
        )
        await adapter.deliver(result)
        assert adapter.delivered_payloads[0] is result

    async def test_deliver_rejects_canonical_event(self) -> None:
        adapter = FakeLxmfAdapter()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
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
        adapter = FakeLxmfAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError, match="simulated send failure"):
            await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 0

    async def test_deliver_failure_no_native_ref(self) -> None:
        adapter = FakeLxmfAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError):
            await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 0

    async def test_fake_client_tracks_sent_messages(self) -> None:
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result()
        await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 1
        assert adapter.fake_client.sent_messages[0]["text"] == "hello lxmf"


# ===================================================================
# Rendering boundary
# ===================================================================


class TestFakeLxmfRenderingBoundary:
    """Adapter consumes RenderingResult, never performs its own formatting."""

    async def test_adapter_receives_rendering_result_not_raw_event(self) -> None:
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result()
        await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 1
        assert isinstance(adapter.delivered_payloads[0], RenderingResult)

    async def test_adapter_does_not_perform_kind_specific_formatting(self) -> None:
        adapter = FakeLxmfAdapter()
        for kind in (EventKind.MESSAGE_TEXT, EventKind.MESSAGE_CREATED):
            result = _make_rendering_result(event_id=f"evt-{kind}")
            await adapter.deliver(result)

        assert len(adapter.delivered_payloads) == 2
        for stored in adapter.delivered_payloads:
            assert isinstance(stored, RenderingResult)


# ===================================================================
# Inbound simulation
# ===================================================================


class TestFakeLxmfAdapterSimulateInbound:
    """simulate_inbound processes packets through classifier + codec."""

    async def test_simulate_inbound_text_packet(
        self, make_adapter_context, inbound_collector
    ) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        packet = _make_text_packet(content="hello lxmf")
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert len(adapter.inbound_events) == 1
        event = inbound_collector.events[0]
        assert event.payload["body"] == "hello lxmf"

    async def test_simulate_inbound_without_start_raises(self) -> None:
        adapter = FakeLxmfAdapter()
        packet = _make_text_packet()
        with pytest.raises(RuntimeError, match="has not been started"):
            await adapter.simulate_inbound(packet)

    async def test_simulate_inbound_ignores_unsupported(
        self, make_adapter_context
    ) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        packet = {
            "source_hash": "ab" * 16,
            "fields": {0x05: [{"name": "file.txt"}]},
        }
        await adapter.simulate_inbound(packet)
        assert len(adapter.inbound_events) == 0

    async def test_simulate_inbound_ignores_empty(self, make_adapter_context) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        packet = {}
        await adapter.simulate_inbound(packet)
        assert len(adapter.inbound_events) == 0


# ===================================================================
# make_text_event helper
# ===================================================================


class TestFakeLxmfAdapterMakeTextEvent:
    """make_text_event creates valid canonical events from packet data."""

    def test_make_text_event_creates_canonical_event(self) -> None:
        adapter = FakeLxmfAdapter()
        event = adapter.make_text_event(body="ping")
        assert isinstance(event, CanonicalEvent)
        assert event.payload["body"] == "ping"

    def test_make_text_event_sets_source_adapter(self) -> None:
        adapter = FakeLxmfAdapter()
        event = adapter.make_text_event()
        assert event.source_adapter == "fake_lxmf"

    def test_make_text_event_populates_native_ref(self) -> None:
        adapter = FakeLxmfAdapter()
        event = adapter.make_text_event(msg_id="aa" * 32)
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "aa" * 32

    def test_make_text_event_with_source_hash(self) -> None:
        adapter = FakeLxmfAdapter()
        event = adapter.make_text_event(source_hash="ef" * 16)
        assert event.source_transport_id == "ef" * 16

    def test_make_text_event_with_title(self) -> None:
        adapter = FakeLxmfAdapter()
        event = adapter.make_text_event(title="Subject")
        assert event.payload["title"] == "Subject"


# ===================================================================
# Task scheduling
# ===================================================================


class TestLxmfAdapterTaskScheduling:
    """Background tasks from _on_packet are tracked and cleaned up."""

    async def test_on_packet_creates_tracked_task(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        packet = _make_text_packet(content="tracked")
        adapter._on_packet(packet)

        await asyncio.sleep(0.05)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "tracked"
        assert len(adapter._background_tasks) == 0

    async def test_stop_cancels_background_tasks(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        async def _slow():
            await asyncio.sleep(100)

        task = asyncio.create_task(_slow())
        adapter._background_tasks.add(task)

        await adapter.stop()
        assert task.cancelled() or task.done()
        assert len(adapter._background_tasks) == 0

    async def test_no_ensure_future(self) -> None:
        """Verify _on_packet does not use asyncio.ensure_future."""
        import inspect

        source = inspect.getsource(LxmfAdapter._on_packet)
        assert "ensure_future" not in source
        assert "create_task" in source


# ===================================================================
# HAS_LXMF export
# ===================================================================


class TestLxmfCompat:
    """HAS_LXMF is importable and is a bool."""

    def test_has_lxmf_is_bool(self) -> None:
        assert isinstance(HAS_LXMF, bool)

    def test_has_lxmf_value_consistent_with_import(self) -> None:
        """HAS_LXMF is True when LXMF imports successfully, False otherwise."""
        try:
            import LXMF  # noqa: F401

            assert HAS_LXMF is True
        except ImportError:
            assert HAS_LXMF is False


# ===================================================================
# Session integration
# ===================================================================


class TestLxmfAdapterSessionIntegration:
    """Adapter delegates to session for lifecycle and delivery."""

    async def test_adapter_exposes_session(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        from medre.adapters.lxmf.session import LxmfSession

        assert isinstance(adapter.session, LxmfSession)

    async def test_start_stop_delegates_to_session(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        assert adapter.session.connected is True
        await adapter.stop()
        assert adapter.session.connected is False

    async def test_repeated_start_stop_via_adapter(self, make_adapter_context) -> None:
        """Repeated start/stop through the adapter is safe."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        for _ in range(3):
            await adapter.start(ctx)
            assert adapter._started is True
            assert adapter.session.connected is True
            await adapter.stop()
            assert adapter._started is False
            assert adapter.session.connected is False

    async def test_deliver_returns_delivery_state_metadata(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert "lxmf" in delivery.metadata
        assert "delivery_state" in delivery.metadata["lxmf"]
        assert delivery.metadata["lxmf"]["delivery_state"] == "outbound"
        await adapter.stop()

    async def test_inbound_via_session_callback(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        packet = _make_text_packet(content="via session")
        adapter._on_packet(packet)

        await asyncio.sleep(0.05)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "via session"
        await adapter.stop()

    async def test_session_diagnostics_accessible(self, make_adapter_context) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        diag = adapter.session.diagnostics()
        assert diag.connected is True
        assert diag.mode == "fake"
        await adapter.stop()

    async def test_adapter_diagnostics_returns_dict(self, make_adapter_context) -> None:
        """Track 5: adapter.diagnostics() returns a structured dict."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-diag")
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["adapter_id"] == config.adapter_id
        assert diag["platform"] == "lxmf"
        assert diag["started"] is True
        assert diag["mode"] == "fake"
        assert "session" in diag
        assert diag["session"]["connected"] is True
        assert diag["session"]["router_running"] is True
        assert diag["session"]["reconnecting"] is False
        assert diag["session"]["reconnect_attempts"] == 0
        await adapter.stop()

    async def test_adapter_diagnostics_before_start(self) -> None:
        """Track 5: adapter.diagnostics() works before start()."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)

        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["started"] is False
        assert "session" in diag
        assert diag["session"]["connected"] is False


# ===================================================================
# Fake adapter outbound pending semantics
# ===================================================================


class TestFakeLxmfAdapterOutboundPending:
    """FakeLxmfAdapter.deliver() returns honest pending/outbound state."""

    async def test_deliver_metadata_has_outbound_state(self) -> None:
        """deliver() metadata reports delivery_state='outbound' (pending)."""
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert "lxmf" in delivery.metadata
        assert delivery.metadata["lxmf"]["delivery_state"] == "outbound"

    async def test_deliver_metadata_has_delivery_method(self) -> None:
        """deliver() metadata includes delivery_method from config."""
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result(
            payload={
                "content": "hello",
                "title": "",
                "fields": {},
                "destination_hash": "",
                "delivery_method": "direct",
            }
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.metadata["lxmf"]["delivery_method"] == "direct"

    async def test_deliver_metadata_default_delivery_method(self) -> None:
        """deliver() metadata uses config default when no method in payload."""
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        # Default delivery method from LxmfConfig
        assert delivery.metadata["lxmf"]["delivery_method"] is not None

    async def test_deliver_no_instant_delivery_claim(self) -> None:
        """deliver() must NOT claim instant/delivered state."""
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        state = delivery.metadata["lxmf"]["delivery_state"]
        assert state != "delivered"
        assert state != "sent"
        assert state == "outbound"


# ===================================================================
# Fake adapter metadata fields preserved
# ===================================================================


class TestFakeLxmfAdapterMetadataPreserved:
    """Fields from the rendering payload are passed to FakeLxmfClient."""

    async def test_fields_passed_to_fake_client(self) -> None:
        adapter = FakeLxmfAdapter()
        fields = {0x01: "test_value", 0x02: {"nested": True}}
        result = _make_rendering_result(
            payload={
                "content": "hello",
                "title": "T",
                "fields": fields,
                "destination_hash": "ab" * 16,
            }
        )
        await adapter.deliver(result)
        assert adapter.fake_client.sent_messages[-1]["fields"] == fields

    async def test_title_preserved(self) -> None:
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result(
            payload={
                "content": "body",
                "title": "Important",
                "fields": {},
                "destination_hash": "",
            }
        )
        await adapter.deliver(result)
        assert adapter.fake_client.sent_messages[-1]["title"] == "Important"

    async def test_destination_hash_preserved(self) -> None:
        adapter = FakeLxmfAdapter()
        result = _make_rendering_result(
            payload={
                "content": "body",
                "title": "",
                "fields": {},
                "destination_hash": "cd" * 16,
            }
        )
        await adapter.deliver(result)
        assert adapter.fake_client.sent_messages[-1]["destination_hash"] == "cd" * 16


# ===================================================================
# Fake adapter diagnostics parity
# ===================================================================


class TestFakeLxmfAdapterDiagnostics:
    """FakeLxmfAdapter.diagnostics() mirrors real adapter structure."""

    async def test_diagnostics_returns_dict(self, make_adapter_context) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["adapter_id"] == "fake_lxmf"
        assert diag["platform"] == "lxmf"
        assert diag["started"] is True
        assert diag["mode"] == "fake"

    async def test_diagnostics_before_start(self) -> None:
        adapter = FakeLxmfAdapter()
        diag = adapter.diagnostics()
        assert diag["started"] is False

    async def test_diagnostics_tracks_sent_count(self, make_adapter_context) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        await adapter.deliver(_make_rendering_result())
        await adapter.deliver(_make_rendering_result())
        diag = adapter.diagnostics()
        assert diag["sent_count"] == 2
        assert diag["delivered_count"] == 2

    async def test_diagnostics_after_stop(self, make_adapter_context) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        await adapter.stop()
        diag = adapter.diagnostics()
        assert diag["started"] is False

    async def test_diagnostics_parity_with_real_adapter(
        self, make_adapter_context
    ) -> None:
        """Fake adapter diagnostics has same top-level keys as real adapter."""
        fake = FakeLxmfAdapter()
        real_config = _make_config(connection_type="fake")
        real = LxmfAdapter(real_config)
        ctx = make_adapter_context("lxmf-1")
        await fake.start(ctx)
        await real.start(ctx)

        fake_diag = fake.diagnostics()
        real_diag = real.diagnostics()

        # Both must share these keys
        shared_keys = {"adapter_id", "platform", "started", "mode"}
        for key in shared_keys:
            assert key in fake_diag, f"Fake missing key: {key}"
            assert key in real_diag, f"Real missing key: {key}"

        await fake.stop()
        await real.stop()


# ===================================================================
# Delivery callback marks delivered
# ===================================================================


class TestDeliveryCallbackMarksDelivered:
    """Session delivery callback transitions state to DELIVERED."""

    async def test_delivery_state_update_to_delivered(
        self, make_adapter_context
    ) -> None:
        """_on_delivery_state_update transitions outbound → delivered."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        native_id = delivery.native_message_id
        assert native_id is not None

        # Verify it's tracked as outbound
        session = adapter.session
        tracked = session._outbound_deliveries.get(native_id)
        assert tracked is not None
        from medre.adapters.lxmf.session import LxmfDeliveryState

        assert tracked.state == LxmfDeliveryState.OUTBOUND

        # Simulate delivery state callback with a mock message
        class MockDeliveredMessage:
            hash = native_id  # str, matches the fake_id
            state = LxmfDeliveryState.DELIVERED

        session._on_delivery_state_update(MockDeliveredMessage())

        # After delivery callback, entry should be untracked (terminal state)
        assert native_id not in session._outbound_deliveries
        await adapter.stop()

    async def test_delivery_state_update_logs_transition(
        self, make_adapter_context
    ) -> None:
        """Delivery callback processes state without error."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        native_id = delivery.native_message_id

        from medre.adapters.lxmf.session import LxmfDeliveryState

        class MockDeliveredMessage:
            hash = native_id
            state = LxmfDeliveryState.DELIVERED

        # Should not raise
        adapter.session._on_delivery_state_update(MockDeliveredMessage())
        await adapter.stop()


# ===================================================================
# Delivery callback marks failed
# ===================================================================


class TestDeliveryCallbackMarksFailed:
    """Session delivery callback transitions state to FAILED."""

    async def test_delivery_state_update_to_failed(self, make_adapter_context) -> None:
        """_on_delivery_state_update transitions outbound → failed."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        native_id = delivery.native_message_id

        from medre.adapters.lxmf.session import LxmfDeliveryState

        # Record initial failure count
        initial_failures = adapter.session.permanent_delivery_failures

        class MockFailedMessage:
            hash = native_id
            state = LxmfDeliveryState.FAILED

        adapter.session._on_delivery_state_update(MockFailedMessage())

        # Failed is terminal — entry should be untracked
        assert native_id not in adapter.session._outbound_deliveries
        # Failure counter should have incremented
        assert adapter.session.permanent_delivery_failures == initial_failures + 1
        await adapter.stop()

    async def test_rejected_increments_permanent_failures(
        self, make_adapter_context
    ) -> None:
        """REJECTED state also increments permanent_delivery_failures."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        native_id = delivery.native_message_id

        from medre.adapters.lxmf.session import LxmfDeliveryState

        initial_failures = adapter.session.permanent_delivery_failures

        class MockRejectedMessage:
            hash = native_id
            state = LxmfDeliveryState.REJECTED

        adapter.session._on_delivery_state_update(MockRejectedMessage())

        assert native_id not in adapter.session._outbound_deliveries
        assert adapter.session.permanent_delivery_failures == initial_failures + 1
        await adapter.stop()


# ===================================================================
# Delivery-state eviction boundedness
# ===================================================================


class TestDeliveryStateEvictionBounded:
    """Outbound delivery tracking is bounded by _MAX_OUTBOUND_DELIVERIES."""

    async def test_eviction_enforces_cap(self, make_adapter_context) -> None:
        """Oldest entries evicted when tracking exceeds cap."""
        from medre.adapters.lxmf.session import _MAX_OUTBOUND_DELIVERIES

        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        session = adapter.session
        total = _MAX_OUTBOUND_DELIVERIES + 50

        for i in range(total):
            result = _make_rendering_result(
                event_id=f"evt-{i}",
                payload={
                    "content": f"msg-{i}",
                    "title": "",
                    "fields": {},
                    "destination_hash": "ab" * 16,
                },
            )
            await adapter.deliver(result)

        # Tracking dict should not exceed cap
        assert len(session._outbound_deliveries) <= _MAX_OUTBOUND_DELIVERIES
        await adapter.stop()

    async def test_eviction_removes_oldest(self, make_adapter_context) -> None:
        """First inserted IDs are evicted first."""
        from medre.adapters.lxmf.session import _MAX_OUTBOUND_DELIVERIES

        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        session = adapter.session
        first_ids: list[str] = []

        for i in range(_MAX_OUTBOUND_DELIVERIES + 10):
            result = _make_rendering_result(
                event_id=f"evt-{i}",
                payload={
                    "content": f"msg-{i}",
                    "title": "",
                    "fields": {},
                    "destination_hash": "ab" * 16,
                },
            )
            delivery = await adapter.deliver(result)
            if i < 10:
                first_ids.append(delivery.native_message_id)

        # Early IDs should have been evicted
        for fid in first_ids:
            assert fid not in session._outbound_deliveries
        await adapter.stop()

    async def test_delivery_state_counts_accurate_under_eviction(
        self, make_adapter_context
    ) -> None:
        """delivery_state_counts reflects only tracked entries."""
        from medre.adapters.lxmf.session import _MAX_OUTBOUND_DELIVERIES

        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        for i in range(_MAX_OUTBOUND_DELIVERIES + 20):
            result = _make_rendering_result(
                event_id=f"evt-{i}",
                payload={
                    "content": f"msg-{i}",
                    "title": "",
                    "fields": {},
                    "destination_hash": "ab" * 16,
                },
            )
            await adapter.deliver(result)

        counts = adapter.session.delivery_state_counts()
        total_tracked = sum(counts.values())
        assert total_tracked <= _MAX_OUTBOUND_DELIVERIES
        await adapter.stop()


# ===================================================================
# Adapter diagnostics secret safety
# ===================================================================


class TestAdapterDiagnosticsSecretSafety:
    """diagnostics() exposes no secrets, identity, or RNS objects."""

    async def test_real_adapter_diagnostics_no_raw_objects(
        self, make_adapter_context
    ) -> None:
        """adapter.diagnostics() dict contains only JSON-safe primitives."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        await adapter.stop()

        # Recursively verify all values are JSON-safe types
        def _check_safe(obj, path="root"):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    _check_safe(v, f"{path}.{k}")
            elif isinstance(obj, (list, tuple)):
                for i, v in enumerate(obj):
                    _check_safe(v, f"{path}[{i}]")
            elif isinstance(obj, (bool, int, float, str, type(None))):
                pass
            else:
                raise AssertionError(
                    f"Non-safe type at {path}: {type(obj).__name__} = {obj!r}"
                )

        _check_safe(diag)

    async def test_real_adapter_diagnostics_no_forbidden_keys(
        self, make_adapter_context
    ) -> None:
        """adapter.diagnostics() has no secret/identity keys."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        await adapter.stop()

        forbidden = {
            "identity",
            "private_key",
            "secret",
            "password",
            "token",
            "reticulum",
            "router",
            "raw",
            "_identity",
            "_reticulum",
            "_router",
        }

        def _check_keys(obj, path="root"):
            if isinstance(obj, dict):
                for k in obj:
                    assert k not in forbidden, f"Forbidden key {k!r} at {path}"
                    _check_keys(obj[k], f"{path}.{k}")

        _check_keys(diag)

    async def test_fake_adapter_diagnostics_no_raw_objects(
        self, make_adapter_context
    ) -> None:
        """FakeLxmfAdapter.diagnostics() contains only JSON-safe primitives."""
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        await adapter.stop()

        def _check_safe(obj, path="root"):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    _check_safe(v, f"{path}.{k}")
            elif isinstance(obj, (bool, int, float, str, type(None))):
                pass
            else:
                raise AssertionError(f"Non-safe type at {path}: {type(obj).__name__}")

        _check_safe(diag)

    async def test_fake_adapter_diagnostics_no_forbidden_keys(
        self, make_adapter_context
    ) -> None:
        """FakeLxmfAdapter.diagnostics() has no secret/identity keys."""
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        await adapter.stop()

        forbidden = {
            "identity",
            "private_key",
            "secret",
            "password",
            "token",
            "_identity",
            "_reticulum",
            "_router",
        }

        for key in diag:
            assert key not in forbidden, f"Forbidden key {key!r}"


# ===================================================================
# Fake adapter repeated stop
# ===================================================================


class TestFakeLxmfAdapterRepeatedStop:
    """Repeated stop() on FakeLxmfAdapter is safe."""

    async def test_repeated_stop_is_noop(self, make_adapter_context) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        await adapter.stop()
        await adapter.stop()
        await adapter.stop()
        assert adapter.is_started is False
