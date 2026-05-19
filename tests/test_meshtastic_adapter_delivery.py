"""Tests for MeshtasticAdapter send semantics, session boundary,
MeshtasticSession unit tests, adapter reply_id/emoji passthrough,
and queue metadata snapshot.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.errors import (
    MeshtasticSendError,
)
from medre.adapters.meshtastic.queue import QueueDeliveryResult
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterDeliveryResult,
    OutboundNativeRefRecord,
)
from medre.core.rendering.renderer import RenderingResult
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
        """process_one with send_fn returns QueueDeliveryResult."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test", "channel_index": 0}, 0)

        async def fake_send(item):
            return {"packet_id": 99}

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.delivery_result.native_message_id == "99"
        assert result.delivery_result.native_channel_id == "0"

    async def test_queue_process_one_extracts_id_from_object(self) -> None:
        """process_one captures packet id from objects with .id attribute."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 3)

        async def fake_send(item):
            return type("Packet", (), {"id": 123})()

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.delivery_result.native_message_id == "123"
        assert result.delivery_result.native_channel_id == "3"

    async def test_queue_process_one_handles_none_send_result(self) -> None:
        """process_one handles send_fn returning None gracefully."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send_none(item):
            return None

        result = await queue.process_one(send_fn=fake_send_none)
        assert result is not None
        assert result.delivery_result.native_message_id is None

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
            self.reply_id: int = 0

        def CopyFrom(self, other: "FakeData") -> None:
            self.portnum = other.portnum
            self.payload = other.payload
            self.emoji = other.emoji
            self.reply_id = other.reply_id

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
    monkeypatch.setitem(
        sys.modules, "meshtastic.protobuf.portnums_pb2", fake_portnums_pb2
    )

    return {
        "call_log": call_log,
        "FakeData": FakeData,
        "FakeMeshPacket": FakeMeshPacket,
    }


class TestSessionStructuredSend:
    """MeshtasticSession._send_structured via fake protobuf and _sendPacket."""

    async def test_send_structured_returns_mesh_packet_when_sendpacket_none(
        self, monkeypatch
    ) -> None:
        """_send_structured returns mesh_packet when _sendPacket returns None
        but mesh_packet.id was set via _generatePacketId."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        _install_fake_protobuf(monkeypatch)

        class FakeClient:
            def _generatePacketId(self):
                return 12345

            def _sendPacket(self, mesh_packet, wantAck=True):
                # SDK sends the packet but returns None
                return None

        session._client = FakeClient()

        result = await session.send(
            {"text": "hello", "channel_index": 0, "reply_id": 1}
        )
        assert result is not None
        assert getattr(result, "id", None) == 12345

    async def test_send_structured_returns_none_when_no_id_and_sendpacket_none(
        self, monkeypatch
    ) -> None:
        """_send_structured returns None when _sendPacket returns None and
        mesh_packet has no id (no _generatePacketId on client)."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        _install_fake_protobuf(monkeypatch)

        class FakeClient:
            # No _generatePacketId method
            def _sendPacket(self, mesh_packet, wantAck=True):
                return None

        session._client = FakeClient()

        result = await session.send(
            {"text": "hello", "channel_index": 0, "reply_id": 1}
        )
        assert result is None

    async def test_send_structured_sendpacket_none_queue_extracts_id(self) -> None:
        """Queue process_one extracts packet ID from mesh_packet returned
        by _send_structured when _sendPacket returns None."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test", "channel_index": 0}, 0)

        # Simulate _send_structured fallback: send_fn returns an object
        # with id set (the mesh_packet), mimicking _sendPacket returning None.
        async def fake_send(item):
            return type("MeshPacket", (), {"id": 12345, "channel": 0})()

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.delivery_result.native_message_id == "12345"

    async def test_send_with_reply_id_calls_send_structured(self, monkeypatch) -> None:
        """send() with reply_id routes to _send_structured path."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        _install_fake_protobuf(monkeypatch)

        send_packet_calls: list[Any] = []

        class FakeClient:
            def _sendPacket(self, mesh_packet, wantAck=True):
                send_packet_calls.append({"packet": mesh_packet, "wantAck": wantAck})
                return type("Result", (), {"id": 77})()

        session._client = FakeClient()

        result = await session.send(
            {"text": "hello", "channel_index": 2, "reply_id": 42}
        )
        assert result is not None
        assert len(send_packet_calls) == 1
        pkt = send_packet_calls[0]["packet"]
        assert pkt.decoded.reply_id == 42
        assert pkt.channel == 2
        assert send_packet_calls[0]["wantAck"] is False

    async def test_send_structured_sets_emoji_when_truthy(self, monkeypatch) -> None:
        """_send_structured sets emoji=1 on Data when emoji is truthy."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )

        _install_fake_protobuf(monkeypatch)

        captured_data: list[Any] = []
        orig_data_cls = sys.modules["meshtastic.protobuf.mesh_pb2"].Data

        class CapturingData(
            orig_data_cls.__bases__[0] if orig_data_cls.__bases__ else object
        ):
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

    async def test_send_structured_no_emoji_when_falsy(self, monkeypatch) -> None:
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

        await session.send({"text": "reply text", "channel_index": 0, "reply_id": 5})
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
            await session.send({"text": "hello", "channel_index": 0, "reply_id": 1})
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
            await session.send({"text": "hello", "channel_index": 0, "reply_id": 1})
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
                send_text_calls.append({"text": text, "channelIndex": channelIndex})
                return type("Packet", (), {"id": 33})()

        session._client = FakeClient()

        result = await session.send({"text": "plain msg", "channel_index": 1})
        assert result is not None
        assert len(send_text_calls) == 1
        assert send_text_calls[0]["text"] == "plain msg"
        assert send_text_calls[0]["channelIndex"] == 1

    async def test_emoji_none_skips_emoji(self, monkeypatch) -> None:
        """emoji=None does not set data.emoji."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        _install_fake_protobuf(monkeypatch)
        captured: list[Any] = []

        class FakeClient:
            def _sendPacket(self, pkt, wantAck=True):
                captured.append(pkt)
                return type("R", (), {"id": 1})()

        session._client = FakeClient()
        await session.send({"text": "hi", "channel_index": 0, "reply_id": 5})
        assert len(captured) == 1
        assert captured[0].decoded.emoji == 0

    async def test_emoji_zero_skips_emoji(self, monkeypatch) -> None:
        """emoji=0 does not set data.emoji."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        _install_fake_protobuf(monkeypatch)
        captured: list[Any] = []

        class FakeClient:
            def _sendPacket(self, pkt, wantAck=True):
                captured.append(pkt)
                return type("R", (), {"id": 1})()

        session._client = FakeClient()
        await session.send(
            {"text": "hi", "channel_index": 0, "reply_id": 5, "emoji": 0}
        )
        assert len(captured) == 1
        assert captured[0].decoded.emoji == 0

    async def test_emoji_one_sets_flag(self, monkeypatch) -> None:
        """emoji=1 sets data.emoji=1."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        _install_fake_protobuf(monkeypatch)
        captured: list[Any] = []

        class FakeClient:
            def _sendPacket(self, pkt, wantAck=True):
                captured.append(pkt)
                return type("R", (), {"id": 1})()

        session._client = FakeClient()
        await session.send(
            {"text": "👍", "channel_index": 0, "reply_id": 5, "emoji": 1}
        )
        assert len(captured) == 1
        assert captured[0].decoded.emoji == 1

    async def test_emoji_two_raises(self, monkeypatch) -> None:
        """emoji=2 raises MeshtasticSendError."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        _install_fake_protobuf(monkeypatch)

        class FakeClient:
            def _sendPacket(self, pkt, wantAck=True):
                return type("R", (), {"id": 1})()

        session._client = FakeClient()
        with pytest.raises(MeshtasticSendError):
            await session.send(
                {"text": "hi", "channel_index": 0, "reply_id": 5, "emoji": 2}
            )

    async def test_emoji_invalid_string_raises(self, monkeypatch) -> None:
        """emoji='yes' raises MeshtasticSendError."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        _install_fake_protobuf(monkeypatch)

        class FakeClient:
            def _sendPacket(self, pkt, wantAck=True):
                return type("R", (), {"id": 1})()

        session._client = FakeClient()
        with pytest.raises(MeshtasticSendError):
            await session.send(
                {"text": "hi", "channel_index": 0, "reply_id": 5, "emoji": "yes"}
            )

    async def test_emoji_string_one_sets_flag(self, monkeypatch) -> None:
        """emoji='1' sets data.emoji=1."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        _install_fake_protobuf(monkeypatch)
        captured: list[Any] = []

        class FakeClient:
            def _sendPacket(self, pkt, wantAck=True):
                captured.append(pkt)
                return type("R", (), {"id": 1})()

        session._client = FakeClient()
        await session.send(
            {"text": "👍", "channel_index": 0, "reply_id": 5, "emoji": "1"}
        )
        assert len(captured) == 1
        assert captured[0].decoded.emoji == 1

    async def test_emoji_string_zero_skips_emoji(self, monkeypatch) -> None:
        """emoji='0' does not set data.emoji."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        session = MeshtasticSession(
            config=config, adapter_id="mesh-1", platform="meshtastic"
        )
        _install_fake_protobuf(monkeypatch)
        captured: list[Any] = []

        class FakeClient:
            def _sendPacket(self, pkt, wantAck=True):
                captured.append(pkt)
                return type("R", (), {"id": 1})()

        session._client = FakeClient()
        await session.send(
            {"text": "hi", "channel_index": 0, "reply_id": 5, "emoji": "0"}
        )
        assert len(captured) == 1
        assert captured[0].decoded.emoji == 0


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
        assert result.delivery_result.metadata["packet_id"] == 42
        assert result.delivery_result.metadata["channel"] == 0
        assert result.delivery_result.metadata["reply_id"] == 7

    async def test_metadata_from_object_result(self) -> None:
        """process_one includes metadata snapshot from object send result."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 3)

        async def fake_send(item):
            return type("Packet", (), {"id": 123, "channel": 3, "reply_id": 99})()

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.delivery_result.metadata["id"] == 123
        assert result.delivery_result.metadata["channel"] == 3
        assert result.delivery_result.metadata["reply_id"] == 99

    async def test_metadata_empty_for_none_result(self) -> None:
        """process_one metadata is empty when send returns None."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send_none(item):
            return None

        result = await queue.process_one(send_fn=fake_send_none)
        assert result is not None
        assert len(result.delivery_result.metadata) == 0

    async def test_metadata_preserves_existing_send_result_id(self) -> None:
        """Metadata snapshot does not break existing native_message_id extraction."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send(item):
            return {"packet_id": 55}

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.delivery_result.native_message_id == "55"
        assert result.delivery_result.metadata["packet_id"] == 55

    async def test_bytes_metadata_json_safe_from_dict(self) -> None:
        """Dict send result with bytes in a captured key is JSON-safe."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "hi"}, 0)

        async def send_fn(item):
            return {"packet_id": 42, "to": b"\x00\xff"}

        result = await queue.process_one(send_fn=send_fn)
        assert result is not None
        assert result.delivery_result.metadata.get("to") == {
            "encoding": "base64",
            "data": "AP8=",
        }
        assert result.delivery_result.metadata.get("packet_id") == 42

    async def test_bytes_metadata_json_safe_from_object(self) -> None:
        """Object send result with bytes in a captured attr is JSON-safe."""
        from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue

        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "hi"}, 0)

        class FakeResult:
            id = 1
            to = b"\x00\xff"

        async def send_fn(item):
            return FakeResult()

        result = await queue.process_one(send_fn=send_fn)
        assert result is not None
        assert result.delivery_result.metadata.get("to") == {
            "encoding": "base64",
            "data": "AP8=",
        }
        assert result.delivery_result.metadata.get("id") == 1


# ===================================================================
# _packet_snapshot decoded subobject capture
# ===================================================================


class TestPacketSnapshotDecodedSubobject:
    """_packet_snapshot captures fields from decoded protobuf sub-object."""

    @staticmethod
    def _call(result: Any) -> dict[str, object]:
        from medre.adapters.meshtastic.queue import _packet_snapshot

        return _packet_snapshot(result)

    def test_dict_decoded_captures_reply_id_and_emoji(self) -> None:
        """Dict result with decoded sub-dict captures reply_id and emoji."""
        snap = self._call({"decoded": {"reply_id": 42, "emoji": 1}})
        assert snap["reply_id"] == 42
        assert snap["emoji"] == 1

    def test_dict_decoded_camel_case_replyId(self) -> None:
        """Dict decoded with replyId (camelCase) maps to reply_id."""
        snap = self._call({"decoded": {"replyId": 99}})
        assert snap["reply_id"] == 99

    def test_dict_decoded_captures_channel_and_to(self) -> None:
        """Dict decoded captures channel and to fields."""
        snap = self._call({"decoded": {"channel": 3, "to": 12345}})
        assert snap["channel"] == 3
        assert snap["to"] == 12345

    def test_dict_decoded_captures_reaction_key(self) -> None:
        """Dict decoded captures reaction_key when present."""
        snap = self._call({"decoded": {"reaction_key": "abc"}})
        assert snap["reaction_key"] == "abc"

    def test_object_decoded_captures_attributes(self) -> None:
        """Object result with decoded sub-object captures reply_id and emoji."""
        Decoded = type("Decoded", (), {"reply_id": 42, "emoji": 1})
        Packet = type("Packet", (), {"decoded": Decoded()})
        snap = self._call(Packet())
        assert snap["reply_id"] == 42
        assert snap["emoji"] == 1

    def test_object_decoded_camel_case_replyId(self) -> None:
        """Object decoded with replyId attribute maps to reply_id."""
        Decoded = type("Decoded", (), {"replyId": 77})
        Packet = type("Packet", (), {"decoded": Decoded()})
        snap = self._call(Packet())
        assert snap["reply_id"] == 77

    def test_top_level_not_overwritten_by_decoded(self) -> None:
        """Top-level reply_id is preserved; decoded reply_id does not overwrite."""
        snap = self._call({"reply_id": 10, "decoded": {"reply_id": 20, "emoji": 5}})
        assert snap["reply_id"] == 10
        assert snap["emoji"] == 5

    def test_object_top_level_not_overwritten_by_decoded(self) -> None:
        """Object top-level reply_id preserved; decoded does not overwrite."""
        Decoded = type("Decoded", (), {"reply_id": 20})
        Packet = type("Packet", (), {"reply_id": 10, "decoded": Decoded()})
        snap = self._call(Packet())
        assert snap["reply_id"] == 10

    def test_none_result_returns_empty(self) -> None:
        """result=None returns empty dict."""
        assert self._call(None) == {}

    def test_no_decoded_returns_top_level_only(self) -> None:
        """Result without decoded sub-object still captures top-level."""
        snap = self._call({"id": 5, "channel": 1})
        assert snap["id"] == 5
        assert snap["channel"] == 1
        assert "emoji" not in snap

    def test_decoded_values_pass_through_json_safe(self) -> None:
        """Decoded bytes values are JSON-safe'd via json_safe."""
        snap = self._call({"decoded": {"emoji": b"\x01\x02"}})
        assert snap["emoji"] == {"encoding": "base64", "data": "AQI="}

    def test_dict_decoded_none_values_skipped(self) -> None:
        """None values in decoded dict are not captured."""
        snap = self._call({"decoded": {"reply_id": None, "emoji": None}})
        assert "reply_id" not in snap
        assert "emoji" not in snap

    def test_object_decoded_missing_attrs_skipped(self) -> None:
        """Missing attributes on decoded object are silently skipped."""
        Decoded = type("Decoded", (), {})
        Packet = type("Packet", (), {"decoded": Decoded()})
        snap = self._call(Packet())
        assert "reply_id" not in snap
        assert "emoji" not in snap

    # --- New: packet_id / id / reaction_id / object decoded.to ---

    def test_dict_decoded_captures_packet_id(self) -> None:
        """Dict decoded with packet_id captures packet_id."""
        snap = self._call({"decoded": {"packet_id": 1234}})
        assert snap["packet_id"] == 1234

    def test_dict_decoded_id_captures_packet_id_when_missing(self) -> None:
        """Dict decoded with id maps to packet_id when packet_id otherwise
        absent from top level."""
        snap = self._call({"decoded": {"id": 5678}})
        assert snap["packet_id"] == 5678

    def test_dict_decoded_id_maps_to_packet_id_even_with_top_level_id(self) -> None:
        """Dict decoded id maps to packet_id even when top-level id exists
        (symmetric with object path)."""
        snap = self._call({"id": 10, "decoded": {"id": 20}})
        assert snap["id"] == 10
        assert snap["packet_id"] == 20

    def test_dict_decoded_reaction_id(self) -> None:
        """Dict decoded with reaction_id captures reaction_id."""
        snap = self._call({"decoded": {"reaction_id": "react-abc"}})
        assert snap["reaction_id"] == "react-abc"

    def test_object_decoded_captures_to(self) -> None:
        """Object decoded with to attribute captures to."""
        Decoded = type("Decoded", (), {"to": 12345})
        Packet = type("Packet", (), {"decoded": Decoded()})
        snap = self._call(Packet())
        assert snap["to"] == 12345

    def test_object_decoded_packet_id_and_id(self) -> None:
        """Object decoded with packet_id captures it; decoded id maps to
        packet_id only when packet_id is absent."""
        # packet_id present on decoded → captured
        DecodedA = type("Decoded", (), {"packet_id": 42})
        PacketA = type("Packet", (), {"decoded": DecodedA()})
        snap_a = self._call(PacketA())
        assert snap_a["packet_id"] == 42

        # id on decoded (no packet_id) → mapped to packet_id
        DecodedB = type("Decoded", (), {"id": 99})
        PacketB = type("Packet", (), {"decoded": DecodedB()})
        snap_b = self._call(PacketB())
        assert snap_b["packet_id"] == 99

    def test_top_level_packet_id_not_overwritten_by_decoded(self) -> None:
        """Top-level packet_id is preserved; decoded packet_id does not
        overwrite."""
        snap = self._call(
            {"packet_id": 100, "decoded": {"packet_id": 200, "emoji": 1}}
        )
        assert snap["packet_id"] == 100
        assert snap["emoji"] == 1

    def test_top_level_id_preserved_and_decoded_id_maps_to_packet_id(self) -> None:
        """Top-level id is preserved; decoded id maps to packet_id
        (symmetric with object path)."""
        snap = self._call({"id": 5, "decoded": {"id": 50}})
        assert snap["id"] == 5
        assert snap["packet_id"] == 50

    def test_object_top_level_packet_id_captured(self) -> None:
        """Object top-level packet_id attribute is captured."""
        Packet = type("Packet", (), {"packet_id": 777, "channel": 2})
        snap = self._call(Packet())
        assert snap["packet_id"] == 777
        assert snap["channel"] == 2


# ===================================================================
# Adapter deliver -> send_one passthrough
# ===================================================================


class TestAdapterDeliverPassthrough:
    """Adapter deliver path preserves structured fields through send_one."""

    async def test_reply_id_passthrough_deliver(self) -> None:
        """deliver -> send_one passes reply_id."""
        config = make_meshtastic_config()
        adapter = MeshtasticAdapter(config)

        # Wire a fake session that captures send calls
        send_calls: list[dict[str, Any]] = []

        class FakeSession:
            _started = True

            @property
            def client(self):
                return object()  # non-None so send_one proceeds

            async def send(self, d):
                send_calls.append(d)
                return {"id": 77}

        adapter._session = FakeSession()  # type: ignore[assignment]
        adapter._started = True

        await adapter.deliver(
            RenderingResult(
                event_id="evt-1",
                target_adapter="mesh-1",
                target_channel="0",
                payload={"text": "hi", "channel_index": 0, "reply_id": 99},
                metadata={},
            )
        )
        result = await adapter.send_one()
        assert result is not None
        assert len(send_calls) == 1
        assert send_calls[0].get("reply_id") == 99

    async def test_emoji_passthrough_deliver(self) -> None:
        """deliver -> send_one passes emoji."""
        config = make_meshtastic_config()
        adapter = MeshtasticAdapter(config)

        send_calls: list[dict[str, Any]] = []

        class FakeSession:
            _started = True

            @property
            def client(self):
                return object()

            async def send(self, d):
                send_calls.append(d)
                return {"id": 88}

        adapter._session = FakeSession()  # type: ignore[assignment]
        adapter._started = True

        await adapter.deliver(
            RenderingResult(
                event_id="evt-2",
                target_adapter="mesh-1",
                target_channel="0",
                payload={"text": "🔥", "channel_index": 0, "reply_id": 10, "emoji": 1},
                metadata={},
            )
        )
        result = await adapter.send_one()
        assert result is not None
        assert len(send_calls) == 1
        assert send_calls[0].get("reply_id") == 10
        assert send_calls[0].get("emoji") == 1


# ===================================================================
# Delayed outbound native ref recording (real adapter callback path)
# ===================================================================


class TestDelayedOutboundNativeRef:
    """Exercises the real MeshtasticAdapter _record_delayed_outbound_ref
    path: event_id → queue item → late record callback.

    This closes the gap left by FakeMeshtasticAdapter's immediate
    native_message_id path.  We call _record_delayed_outbound_ref
    directly with a QueueDeliveryResult to avoid the unbounded
    _process_queue loop.
    """

    async def test_event_id_flows_to_outbound_native_ref_record(self) -> None:
        """event_id enqueued via deliver flows through queue drain/send
        result to AdapterContext.record_outbound_native_ref as an
        OutboundNativeRefRecord with correct fields."""
        import logging
        from types import MappingProxyType

        from medre.core.events.canonical import CanonicalEvent

        config = make_meshtastic_config()
        adapter = MeshtasticAdapter(config)

        # Capture records from the callback.
        recorded: list[OutboundNativeRefRecord] = []

        async def on_outbound_ref(record: OutboundNativeRefRecord) -> None:
            recorded.append(record)

        async def noop_publish(event: CanonicalEvent) -> None:
            pass

        # Wire a minimal AdapterContext with the outbound ref callback.
        adapter.ctx = AdapterContext(
            adapter_id="mesh-1",
            event_bus=None,
            publish_inbound=noop_publish,
            logger=logging.getLogger("test.mesh-1"),
            clock=lambda: __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            shutdown_event=__import__("asyncio").Event(),
            record_outbound_native_ref=on_outbound_ref,
        )

        # Build a QueueDeliveryResult simulating what process_one returns
        # after a real send: the queued item carries the event_id and the
        # delivery has a real native_message_id.
        event_id = "$evt-delayed-001"
        payload = {
            "text": "hello mesh",
            "channel_index": 0,
        }
        item: dict[str, Any] = {
            "payload": payload,
            "channel_index": 0,
            "event_id": event_id,
        }
        delivery = AdapterDeliveryResult(
            native_message_id="987654321",
            native_channel_id="0",
            metadata=MappingProxyType({"packet_id": 987654321, "channel": 0}),
        )
        result = QueueDeliveryResult(item=item, delivery_result=delivery)

        # Call the real adapter's delayed ref recording method.
        await adapter._record_delayed_outbound_ref(result, event_id, delivery)

        # Verify the callback was invoked exactly once.
        assert len(recorded) == 1
        ref = recorded[0]

        # Core identity fields.
        assert ref.event_id == "$evt-delayed-001"
        assert ref.adapter == "mesh-1"
        assert ref.native_channel_id == "0"
        assert ref.native_message_id == "987654321"

        # Metadata includes the merged delivery snapshot + payload context.
        assert ref.metadata["packet_id"] == 987654321
        assert ref.metadata["channel"] == 0
        assert ref.metadata["text"] == "hello mesh"

    async def test_no_callback_means_no_error(self) -> None:
        """_record_delayed_outbound_ref is safe when ctx has no callback."""
        import logging
        from types import MappingProxyType

        from medre.core.events.canonical import CanonicalEvent

        config = make_meshtastic_config()
        adapter = MeshtasticAdapter(config)

        async def noop_publish(event: CanonicalEvent) -> None:
            pass

        # Context without record_outbound_native_ref (defaults to None).
        adapter.ctx = AdapterContext(
            adapter_id="mesh-1",
            event_bus=None,
            publish_inbound=noop_publish,
            logger=logging.getLogger("test.mesh-1"),
            clock=lambda: __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            shutdown_event=__import__("asyncio").Event(),
        )

        item: dict[str, Any] = {
            "payload": {"text": "test"},
            "channel_index": 0,
            "event_id": "$evt-no-cb",
        }
        delivery = AdapterDeliveryResult(
            native_message_id="111",
            native_channel_id="0",
            metadata=MappingProxyType({}),
        )
        result = QueueDeliveryResult(item=item, delivery_result=delivery)

        # Should not raise despite no callback.
        await adapter._record_delayed_outbound_ref(result, "$evt-no-cb", delivery)

    async def test_payload_fields_in_metadata(self) -> None:
        """_record_delayed_outbound_ref includes reply_id, emoji,
        meshnet_name, and channel_name from the queued payload."""
        import logging
        from types import MappingProxyType

        from medre.core.events.canonical import CanonicalEvent

        config = make_meshtastic_config()
        adapter = MeshtasticAdapter(config)

        recorded: list[OutboundNativeRefRecord] = []

        async def on_ref(record: OutboundNativeRefRecord) -> None:
            recorded.append(record)

        async def noop_publish(event: CanonicalEvent) -> None:
            pass

        adapter.ctx = AdapterContext(
            adapter_id="mesh-1",
            event_bus=None,
            publish_inbound=noop_publish,
            logger=logging.getLogger("test.mesh-1"),
            clock=lambda: __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            shutdown_event=__import__("asyncio").Event(),
            record_outbound_native_ref=on_ref,
        )

        item: dict[str, Any] = {
            "payload": {
                "text": "reaction text",
                "channel_index": 2,
                "reply_id": 42,
                "emoji": 1,
                "meshnet_name": "TestMesh",
                "channel_name": "ch2",
            },
            "channel_index": 2,
            "event_id": "$evt-full-meta",
        }
        delivery = AdapterDeliveryResult(
            native_message_id="555",
            native_channel_id="2",
            metadata=MappingProxyType({"packet_id": 555, "channel": 2, "reply_id": 42}),
        )
        result = QueueDeliveryResult(item=item, delivery_result=delivery)

        await adapter._record_delayed_outbound_ref(result, "$evt-full-meta", delivery)

        assert len(recorded) == 1
        ref = recorded[0]
        assert ref.metadata["text"] == "reaction text"
        assert ref.metadata["reply_id"] == 42
        assert ref.metadata["emoji"] == 1
        assert ref.metadata["meshnet_name"] == "TestMesh"
        assert ref.metadata["channel_name"] == "ch2"
        # Delivery snapshot keys are merged too.
        assert ref.metadata["packet_id"] == 555
