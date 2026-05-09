"""Tests for FakeLxmfAdapter and LxmfAdapter: capabilities,
lifecycle (start/stop), delivery contract, inbound simulation, rendering
boundary enforcement, and packet simulation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from medre.adapters import AdapterRole, FakeLxmfAdapter
from medre.adapters.base import AdapterContext, AdapterDeliveryResult
from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.lxmf.config import LxmfConfig
from medre.adapters.lxmf.compat import HAS_LXMF
from medre.adapters.lxmf.errors import LxmfConnectionError
from medre.core.events import CanonicalEvent, EventMetadata
from medre.adapters.lxmf.errors import LxmfSendError
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult


def _make_config(**overrides) -> LxmfConfig:
    defaults = dict(adapter_id="lxmf-1")
    defaults.update(overrides)
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
        payload=payload or {
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

    async def test_deliver_returns_none_in_tranche1(self) -> None:
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is None

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
        with pytest.raises(TypeError, match="RenderingResult only"):
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

    async def test_non_fake_reticulum_raises_not_implemented(
        self, make_adapter_context
    ) -> None:
        """Non-fake reticulum mode raises not-implemented error.

        Even when HAS_LXMF is True, start() must raise because no real
        LXMF client is created.  This test uses monkeypatch to control
        HAS_LXMF independently of the local installation.
        """
        import medre.adapters.lxmf.adapter as _adapter_mod

        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")

        original = _adapter_mod.HAS_LXMF
        try:
            # Even with HAS_LXMF=True, start must raise not-implemented.
            _adapter_mod.HAS_LXMF = True
            with pytest.raises(
                LxmfConnectionError,
                match="production LXMF/Reticulum connectivity is not implemented",
            ):
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
            with pytest.raises(
                LxmfConnectionError, match="lxmf/RNS not installed"
            ):
                await adapter.start(ctx)
        finally:
            _adapter_mod.HAS_LXMF = original

        assert adapter._started is False

    async def test_non_fake_never_reports_healthy(
        self, make_adapter_context
    ) -> None:
        """Non-fake reticulum mode never reaches _started=True."""
        import medre.adapters.lxmf.adapter as _adapter_mod

        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")

        for has_lxmf in (True, False):
            original = _adapter_mod.HAS_LXMF
            try:
                _adapter_mod.HAS_LXMF = has_lxmf
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

    async def test_unsubscribe_events_on_stop(
        self, make_adapter_context
    ) -> None:
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
        result = _make_rendering_result(payload={
            "content": "original", "title": "T", "fields": {}, "destination_hash": "",
        })
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
        with pytest.raises(TypeError, match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_deliver_failure_raises_send_error(self) -> None:
        adapter = FakeLxmfAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(LxmfSendError, match="simulated send failure"):
            await adapter.deliver(result)
        assert len(adapter.delivered_payloads) == 0

    async def test_deliver_failure_no_native_ref(self) -> None:
        adapter = FakeLxmfAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(LxmfSendError):
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

    async def test_simulate_inbound_ignores_empty(
        self, make_adapter_context
    ) -> None:
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

    async def test_stop_cancels_background_tasks(
        self, make_adapter_context
    ) -> None:
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
        """HAS_LXMF is True when lxmf imports successfully, False otherwise."""
        try:
            import lxmf  # noqa: F401
            assert HAS_LXMF is True
        except ImportError:
            assert HAS_LXMF is False
