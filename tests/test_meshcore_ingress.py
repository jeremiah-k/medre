"""Tests for MeshCore inbound path: simulate_inbound, _on_message,
make_text_event, metadata namespacing for inbound, and session _on_sdk_event.
"""

from __future__ import annotations

import asyncio

import pytest

from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshcore.session import MeshCoreSession
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.events import CanonicalEvent


def _make_config(**overrides) -> MeshCoreConfig:
    defaults = dict(adapter_id="meshcore-1")
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


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
# Inbound simulation (FakeMeshCoreAdapter)
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

    async def test_simulate_inbound_ignores_non_text(
        self, make_adapter_context
    ) -> None:
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
# Real adapter inbound via simulate_inbound
# ===================================================================


class TestMeshCoreAdapterInbound:
    """Real adapter inbound via simulate_inbound in fake mode."""

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
# Task scheduling (_on_message)
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

    async def test_stop_cancels_background_tasks(self, make_adapter_context) -> None:
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
# Metadata namespacing and redaction (inbound)
# ===================================================================


class TestMetadataNamespacingAndRedaction:
    """Native metadata uses meshcore. namespace and diagnostics are safe."""

    async def test_inbound_metadata_uses_meshcore_namespace(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """Decoded events use meshcore. prefixed keys in native metadata."""
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        packet = _make_channel_packet(text="namespaced", channel_idx=2)
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        event = inbound_collector.events[0]
        data = event.metadata.native.data
        # All keys must be namespaced
        for key in data:
            assert key.startswith("meshcore."), f"Un-namespaced key: {key!r}"
        assert data["meshcore.channel"] == 2
        assert data["meshcore.is_direct_message"] is False

    async def test_dm_metadata_uses_meshcore_namespace(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """DM events also use meshcore. namespace."""
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("meshcore-1")
        await adapter.start(ctx)

        packet = _make_contact_packet(text="dm namespaced")
        await adapter.simulate_inbound(packet)

        event = inbound_collector.events[0]
        data = event.metadata.native.data
        for key in data:
            assert key.startswith("meshcore."), f"Un-namespaced key: {key!r}"
        assert data["meshcore.is_direct_message"] is True

    async def test_diagnostics_no_raw_metadata(self) -> None:
        """Diagnostics does not expose raw event metadata."""
        adapter = FakeMeshCoreAdapter()
        diag = adapter.diagnostics()
        diag_str = str(diag)
        # Should not contain any raw metadata field values
        assert "pubkey_prefix" not in diag_str
        assert "packet_id" not in diag_str

    async def test_real_adapter_diagnostics_no_secrets(self) -> None:
        """Real adapter diagnostics contain no secret values."""
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        diag = adapter.diagnostics()
        diag_str = str(diag)
        for secret in ("private_key", "secret", "password", "token"):
            assert secret not in diag_str, f"Secret {secret!r} found in diagnostics"


# ===================================================================
# _on_sdk_event boundary test
# ===================================================================


class TestMeshCoreSessionOnSdkEvent:
    """Boundary test: _on_sdk_event normalizes SDK Event objects to clean
    dicts and invokes _message_callback with the correct payload shape.
    """

    async def test_on_sdk_event_with_contact_msg_event(self) -> None:
        """_on_sdk_event extracts payload from SDK Event and forwards to callback."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config=config, adapter_id="mc-test")
        collected: list[dict] = []
        session._message_callback = AsyncMock(side_effect=lambda p: collected.append(p))

        sdk_event = SimpleNamespace(
            type="CONTACT_MSG_RECV",
            payload={
                "text": "hello from sdk",
                "pubkey_prefix": "abc123",
                "sender_timestamp": 99,
                "type": "CHAN",
                "txt_type": 0,
            },
        )

        await session._on_sdk_event(sdk_event)

        assert len(collected) == 1
        payload = collected[0]
        assert payload["text"] == "hello from sdk"
        assert payload["pubkey_prefix"] == "abc123"
        assert payload["sender_timestamp"] == 99
        assert payload["type"] == "CHAN"
        assert payload["txt_type"] == 0

    async def test_on_sdk_event_with_channel_msg_event(self) -> None:
        """_on_sdk_event handles channel messages with channel_idx."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config=config, adapter_id="mc-test")
        collected: list[dict] = []
        session._message_callback = AsyncMock(side_effect=lambda p: collected.append(p))

        sdk_event = SimpleNamespace(
            type="CHANNEL_MSG_RECV",
            payload={
                "text": "channel msg",
                "channel_idx": 2,
                "sender_timestamp": 200,
                "type": "CHAN",
                "txt_type": 0,
                "pubkey_prefix": "chan_peer",
            },
        )

        await session._on_sdk_event(sdk_event)

        assert len(collected) == 1
        assert collected[0]["channel_idx"] == 2
        assert collected[0]["pubkey_prefix"] == "chan_peer"

    async def test_on_sdk_event_no_callback_is_noop(self) -> None:
        """_on_sdk_event returns silently when no callback registered."""
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config=config, adapter_id="mc-test")
        # _message_callback is None by default
        assert session._message_callback is None

        from types import SimpleNamespace

        sdk_event = SimpleNamespace(
            type="CONTACT_MSG_RECV",
            payload={"text": "should not crash"},
        )
        # Must not raise
        await session._on_sdk_event(sdk_event)

    async def test_on_sdk_event_dict_passthrough(self) -> None:
        """_on_sdk_event passes dict events through without transformation."""
        from unittest.mock import AsyncMock

        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config=config, adapter_id="mc-test")
        collected: list[dict] = []
        session._message_callback = AsyncMock(side_effect=lambda p: collected.append(p))

        raw_dict = {"text": "raw dict", "pubkey_prefix": "xyz", "sender_timestamp": 1}
        await session._on_sdk_event(raw_dict)

        assert len(collected) == 1
        assert collected[0] is raw_dict

    async def test_on_sdk_event_updates_last_message_time(self) -> None:
        """_on_sdk_event updates session diagnostics last_message_time."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config=config, adapter_id="mc-test")
        session._message_callback = AsyncMock()

        assert session.last_message_time is None

        sdk_event = SimpleNamespace(
            type="CONTACT_MSG_RECV",
            payload={"text": "timecheck"},
        )
        await session._on_sdk_event(sdk_event)

        assert session.last_message_time is not None

    async def test_on_sdk_event_handles_missing_payload(self) -> None:
        """_on_sdk_event handles Event objects without .payload gracefully."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config=config, adapter_id="mc-test")
        collected: list[dict] = []
        session._message_callback = AsyncMock(side_effect=lambda p: collected.append(p))

        # Event with no .payload attribute
        sdk_event = SimpleNamespace(type="CONTACT_MSG_RECV")
        await session._on_sdk_event(sdk_event)

        assert len(collected) == 1
        assert collected[0] == {}
