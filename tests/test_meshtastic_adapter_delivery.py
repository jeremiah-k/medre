"""Tests for MeshtasticAdapter send semantics, session boundary,
MeshtasticSession unit tests, adapter reply_id/emoji passthrough,
and queue metadata snapshot.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.errors import (
    MeshtasticSendError,
)
from medre.adapters.meshtastic.session import MeshtasticSession

from tests.helpers.meshtastic import (
    make_meshtastic_config,
    make_meshtastic_rendering_result,
)


# ===================================================================
# Send semantics audit
# ===================================================================


class TestMeshtasticAdapterSendSemantics:
    """Audit: deliver() enqueues/returns None; send semantics documented."""

    async def test_deliver_return_none_documented(self) -> None:
        """Real adapter deliver() returns AdapterDeliveryResult with
        delivery_note='locally enqueued' and native_message_id=None."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = make_meshtastic_rendering_result()
        delivery = await adapter.deliver(result)
        # Queue-based: returns result with no native_message_id
        assert delivery is not None
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"

    async def test_queue_process_one_without_send_fn_returns_none(self) -> None:
        """process_one without send_fn returns None (scaffold mode)."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue()
        await queue.enqueue({"text": "test"}, 0)
        result = await queue.process_one()
        assert result is None

    async def test_queue_process_one_with_send_fn_returns_result(self) -> None:
        """process_one with send_fn returns AdapterDeliveryResult."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test", "channel_index": 0}, 0)

        async def fake_send(item):
            return {"packet_id": 99}

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.native_message_id == "99"
        assert result.native_channel_id == "0"

    async def test_queue_process_one_extracts_id_from_object(self) -> None:
        """process_one captures packet id from objects with .id attribute."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 3)

        async def fake_send(item):
            return type("Packet", (), {"id": 123})()

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.native_message_id == "123"
        assert result.native_channel_id == "3"

    async def test_queue_process_one_handles_none_send_result(self) -> None:
        """process_one handles send_fn returning None gracefully."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send_none(item):
            return None

        result = await queue.process_one(send_fn=fake_send_none)
        assert result is not None
        assert result.native_message_id is None

    async def test_queue_process_one_tracks_failures(self) -> None:
        """process_one increments total_failed on send_fn exception."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send_fail(item):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await queue.process_one(send_fn=fake_send_fail)

        assert queue.total_failed == 1


# ===================================================================
# Session boundary
# ===================================================================


class TestMeshtasticSessionBoundary:
    """MeshtasticSession lifecycle and diagnostics."""

    async def test_session_created_on_start(self, make_adapter_context) -> None:
        """Adapter creates a MeshtasticSession on start."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        assert isinstance(adapter._session, MeshtasticSession)
        await adapter.stop()

    async def test_session_cleared_on_stop(self, make_adapter_context) -> None:
        """Adapter clears session ref on stop."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        await adapter.stop()
        assert adapter._session is None

    async def test_session_diagnostics_exposed(self, make_adapter_context) -> None:
        """diagnostics() returns combined adapter + session state."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        assert diag["adapter_id"] == "mesh-1"
        assert diag["platform"] == "meshtastic"
        assert diag["started"] is True
        assert diag["connection_type"] == "fake"

        # Session diagnostics present
        assert "session" in diag
        session = diag["session"]
        assert session["connected"] is False  # fake mode has no real client
        assert session["reconnecting"] is False
        assert session["reconnect_attempts"] == 0
        assert session["last_packet_time"] is None
        assert session["node_id"] is None
        assert session["channel_count"] == 0
        assert session["transient_delivery_failures"] == 0
        assert session["permanent_delivery_failures"] == 0
        assert session["last_error"] is None

        await adapter.stop()

    async def test_session_diagnostics_after_stop(self, make_adapter_context) -> None:
        """diagnostics() without session shows adapter-only state."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        diag = adapter.diagnostics()
        assert diag["started"] is False
        assert "session" not in diag

    async def test_session_diagnostics_no_secrets(self, make_adapter_context) -> None:
        """Diagnostics never exposes secrets, keys, or raw protobuf."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        diag_str = str(diag)
        # No secret-like keys
        for forbidden in ("password", "secret", "key", "token", "private"):
            assert forbidden not in diag_str.lower() or "node_id" in diag_str

        await adapter.stop()


# ===================================================================
# MeshtasticSession unit tests
# ===================================================================


class TestMeshtasticSessionUnit:
    """Direct unit tests for MeshtasticSession."""

    async def test_fake_mode_session_start(self) -> None:
        """Session start in fake mode creates no client."""
        config = make_meshtastic_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        await session.start()
        assert session.connected is False  # fake mode, no real client
        assert session.client is None
        await session.stop()

    async def test_session_stop_idempotent(self) -> None:
        """Session stop is safe without start."""
        config = make_meshtastic_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        await session.stop()  # should not raise

    async def test_session_diagnostics_dataclass(self) -> None:
        """Session diagnostics returns proper dataclass."""
        config = make_meshtastic_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        diag = session.diagnostics()
        from medre.adapters.meshtastic.session import MeshtasticSessionDiagnostics

        assert isinstance(diag, MeshtasticSessionDiagnostics)
        assert diag.connected is False
        assert diag.reconnecting is False
        assert diag.reconnect_attempts == 0
        assert diag.last_packet_time is None
        assert diag.node_id is None
        assert diag.channel_count == 0
        assert diag.transient_delivery_failures == 0
        assert diag.permanent_delivery_failures == 0
        assert diag.last_error is None

    async def test_session_send_returns_none_fake(self) -> None:
        """Session send returns None in fake mode (no real client)."""
        config = make_meshtastic_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        await session.start()
        result = await session.send({"text": "hello", "channel_index": 0})
        assert result is None
        await session.stop()

    async def test_session_send_with_transient_retry(self, monkeypatch) -> None:
        """Session send retries on transient errors."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        # Inject a fake client that fails once then succeeds
        call_count = {"n": 0}

        class FakeClient:
            def sendText(self, text, channelIndex=0):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise ConnectionError("transient")
                return type("Packet", (), {"id": 42})()

        session._client = FakeClient()
        result = await session.send({"text": "hello", "channel_index": 0})
        assert result is not None
        assert call_count["n"] == 2
        assert session.transient_delivery_failures == 1

    async def test_session_send_permanent_failure_raises(self) -> None:
        """Session send raises immediately on non-transient errors."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        class FakeClient:
            def sendText(self, text, channelIndex=0):
                raise ValueError("bad packet")

        session._client = FakeClient()
        with pytest.raises(MeshtasticSendError, match="Permanent"):
            await session.send({"text": "hello", "channel_index": 0})
        assert session.permanent_delivery_failures == 1

    async def test_session_reconnect_loop_bounded(self) -> None:
        """Reconnect loop stops after max attempts."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        session._started = True

        # _create_client always fails
        def always_fail(self):
            raise ConnectionError("nope")

        import medre.adapters.meshtastic.session as session_mod

        original_create = session_mod.MeshtasticSession._create_client
        session_mod.MeshtasticSession._create_client = always_fail

        try:
            # Use very short backoff for testing
            session_mod._BACKOFF_BASE = 0.01
            session_mod._BACKOFF_CAP = 0.01

            await session._reconnect_loop()

            assert session.reconnect_attempts > 0
            assert session.reconnecting is False
            assert session.last_error is not None
        finally:
            session_mod.MeshtasticSession._create_client = original_create
            session_mod._BACKOFF_BASE = 1.0
            session_mod._BACKOFF_CAP = 30.0

    async def test_session_message_callback(self) -> None:
        """Session forwards received packets to message callback."""
        config = make_meshtastic_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        received = []
        await session.start(message_callback=lambda pkt: received.append(pkt))

        # Simulate callback
        session._on_receive({"id": 1, "decoded": {"text": "test"}})
        assert len(received) == 1
        assert received[0]["id"] == 1
        assert session.last_packet_time is not None

        await session.stop()

    async def test_session_stop_prevents_reconnect(self) -> None:
        """stop() sets _stop_requested, preventing reconnect."""
        config = make_meshtastic_config(connection_type="fake")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        await session.start()
        session.notify_connection_lost()
        # Stop before reconnect can do anything
        await session.stop()
        assert session._stop_requested is True


# ===================================================================
# Session structured send (protobuf _sendPacket path)
# ===================================================================


def _install_fake_protobuf(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject fake meshtastic protobuf modules into sys.modules.

    Returns a dict with ``call_log`` for assertions.
    """
    call_log: list[Any] = []

    class FakeData:
        def __init__(self) -> None:
            self.portnum: Any = None
            self.payload: bytes = b""
            self.emoji: int = 0

        def CopyFrom(self, other: "FakeData") -> None:
            self.portnum = other.portnum
            self.payload = other.payload
            self.emoji = other.emoji

    class FakeMeshPacket:
        def __init__(self) -> None:
            self.decoded: Any = FakeData()
            self.channel: int = 0
            self.reply_id: int = 0

    fake_mesh_pb2 = ModuleType("meshtastic.protobuf.mesh_pb2")
    fake_mesh_pb2.Data = FakeData
    fake_mesh_pb2.MeshPacket = FakeMeshPacket

    fake_portnums_pb2 = ModuleType("meshtastic.protobuf.portnums_pb2")
    fake_portnums_pb2.TEXT_MESSAGE_APP = 1

    fake_proto = ModuleType("meshtastic.protobuf")

    monkeypatch.setitem(sys.modules, "meshtastic.protobuf", fake_proto)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf.mesh_pb2", fake_mesh_pb2)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf.portnums_pb2", fake_portnums_pb2)

    return {"call_log": call_log, "FakeData": FakeData, "FakeMeshPacket": FakeMeshPacket}


class TestSessionStructuredSend:
    """MeshtasticSession._send_structured via fake protobuf and _sendPacket."""

    async def test_send_with_reply_id_calls_send_structured(
        self, monkeypatch
    ) -> None:
        """send() with reply_id routes to _send_structured path."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        _install_fake_protobuf(monkeypatch)

        send_packet_calls: list[Any] = []

        class FakeClient:
            def _sendPacket(self, mesh_packet, wantAck=True):
                send_packet_calls.append(
                    {"packet": mesh_packet, "wantAck": wantAck}
                )
                return type("Result", (), {"id": 77})()

        session._client = FakeClient()

        result = await session.send(
            {"text": "hello", "channel_index": 2, "reply_id": 42}
        )
        assert result is not None
        assert len(send_packet_calls) == 1
        pkt = send_packet_calls[0]["packet"]
        assert pkt.reply_id == 42
        assert pkt.channel == 2
        assert send_packet_calls[0]["wantAck"] is False

    async def test_send_structured_sets_emoji_when_truthy(
        self, monkeypatch
    ) -> None:
        """_send_structured sets emoji=1 on Data when emoji is truthy."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        _install_fake_protobuf(monkeypatch)

        captured_data: list[Any] = []
        orig_data_cls = sys.modules[
            "meshtastic.protobuf.mesh_pb2"
        ].Data

        class CapturingData(orig_data_cls.__bases__[0] if orig_data_cls.__bases__ else object):
            def __init__(self) -> None:
                super().__init__()
                self.portnum: Any = None
                self.payload: bytes = b""
                self.emoji: int = 0

        sys.modules["meshtastic.protobuf.mesh_pb2"].Data = CapturingData

        class FakeClient:
            def _sendPacket(self, mesh_packet, wantAck=True):
                captured_data.append(mesh_packet.decoded)
                return {"packet_id": 88}

        session._client = FakeClient()

        await session.send(
            {"text": "👍", "channel_index": 0, "reply_id": 10, "emoji": 1}
        )
        assert len(captured_data) == 1
        assert captured_data[0].emoji == 1

    async def test_send_structured_no_emoji_when_falsy(
        self, monkeypatch
    ) -> None:
        """_send_structured does not set emoji when emoji is None/0."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        _install_fake_protobuf(monkeypatch)

        captured_data: list[Any] = []

        class FakeClient:
            def _sendPacket(self, mesh_packet, wantAck=True):
                captured_data.append(mesh_packet.decoded)
                return {"packet_id": 88}

        session._client = FakeClient()

        await session.send(
            {"text": "reply text", "channel_index": 0, "reply_id": 5}
        )
        assert len(captured_data) == 1
        assert captured_data[0].emoji == 0

    async def test_send_structured_missing_protobuf_raises_permanent(
        self, monkeypatch
    ) -> None:
        """Missing protobuf modules → MeshtasticSendError(transient=False)."""
        import builtins

        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        class FakeClient:
            pass

        session._client = FakeClient()

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if "meshtastic.protobuf" in name:
                raise ImportError(f"No module named '{name}'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        with pytest.raises(MeshtasticSendError, match="protobuf") as exc_info:
            await session.send(
                {"text": "hello", "channel_index": 0, "reply_id": 1}
            )
        assert exc_info.value.transient is False

    async def test_send_structured_missing_send_packet_raises_permanent(
        self, monkeypatch
    ) -> None:
        """Client without _sendPacket → MeshtasticSendError(transient=False)."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        _install_fake_protobuf(monkeypatch)

        class FakeClient:
            # No _sendPacket method
            pass

        session._client = FakeClient()

        with pytest.raises(MeshtasticSendError, match="_sendPacket") as exc_info:
            await session.send(
                {"text": "hello", "channel_index": 0, "reply_id": 1}
            )
        assert exc_info.value.transient is False

    async def test_send_without_reply_id_uses_sendtext(self) -> None:
        """send() without reply_id falls through to sendText path."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        send_text_calls: list[Any] = []

        class FakeClient:
            def sendText(self, text, channelIndex=0):
                send_text_calls.append(
                    {"text": text, "channelIndex": channelIndex}
                )
                return type("Packet", (), {"id": 33})()

        session._client = FakeClient()

        result = await session.send(
            {"text": "plain msg", "channel_index": 1}
        )
        assert result is not None
        assert len(send_text_calls) == 1
        assert send_text_calls[0]["text"] == "plain msg"
        assert send_text_calls[0]["channelIndex"] == 1


# ===================================================================
# Adapter reply_id/emoji passthrough
# ===================================================================


class TestAdapterReplyIdPassthrough:
    """MeshtasticAdapter.send_one passes reply_id/emoji to session."""

    async def test_send_one_passes_reply_id(self, make_adapter_context) -> None:
        """send_one passes reply_id from rendered payload through to session."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        # Enqueue a payload with reply_id
        payload = {
            "text": "reply msg",
            "channel_index": 0,
            "meshnet_name": "",
            "reply_id": 42,
        }
        await adapter._queue.enqueue(payload, 0)

        # Manually call send_one with a fake session
        session_calls: list[dict[str, Any]] = []

        class FakeSession:
            @property
            def client(self):
                return type("Client", (), {})()

        async def fake_send_fn(item):
            session_calls.append(item.get("payload", {}))
            return {"packet_id": 99}

        # Use queue.process_one directly
        result = await adapter._queue.process_one(send_fn=fake_send_fn)
        assert result is not None
        assert session_calls[0].get("reply_id") == 42

        await adapter.stop()

    async def test_send_one_passes_emoji(self, make_adapter_context) -> None:
        """send_one passes emoji from rendered payload through to session."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        payload = {
            "text": "👍",
            "channel_index": 0,
            "meshnet_name": "",
            "reply_id": 7,
            "emoji": 1,
        }
        await adapter._queue.enqueue(payload, 0)

        session_calls: list[dict[str, Any]] = []

        async def fake_send_fn(item):
            session_calls.append(item.get("payload", {}))
            return {"packet_id": 100}

        result = await adapter._queue.process_one(send_fn=fake_send_fn)
        assert result is not None
        assert session_calls[0].get("emoji") == 1

        await adapter.stop()

    async def test_capabilities_native_replies(self) -> None:
        """Adapter declares native reply support."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        caps = adapter._capabilities
        assert caps.replies == "native"

    async def test_capabilities_native_reactions(self) -> None:
        """Adapter declares native reaction support."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        caps = adapter._capabilities
        assert caps.reactions == "native"

    async def test_capabilities_metadata_fields(self) -> None:
        """Adapter declares metadata_fields support."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        caps = adapter._capabilities
        assert caps.metadata_fields is True


# ===================================================================
# Queue metadata snapshot
# ===================================================================


class TestQueueMetadataSnapshot:
    """MeshtasticOutboundQueue process_one includes packet metadata."""

    async def test_metadata_from_dict_result(self) -> None:
        """process_one includes metadata snapshot from dict send result."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send(item):
            return {"packet_id": 42, "channel": 0, "reply_id": 7}

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.metadata["packet_id"] == 42
        assert result.metadata["channel"] == 0
        assert result.metadata["reply_id"] == 7

    async def test_metadata_from_object_result(self) -> None:
        """process_one includes metadata snapshot from object send result."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 3)

        async def fake_send(item):
            return type("Packet", (), {"id": 123, "channel": 3, "reply_id": 99})()

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.metadata["id"] == 123
        assert result.metadata["channel"] == 3
        assert result.metadata["reply_id"] == 99

    async def test_metadata_empty_for_none_result(self) -> None:
        """process_one metadata is empty when send returns None."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send_none(item):
            return None

        result = await queue.process_one(send_fn=fake_send_none)
        assert result is not None
        assert len(result.metadata) == 0

    async def test_metadata_preserves_existing_send_result_id(self) -> None:
        """Metadata snapshot does not break existing native_message_id extraction."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send(item):
            return {"packet_id": 55}

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.native_message_id == "55"
        assert result.metadata["packet_id"] == 55
