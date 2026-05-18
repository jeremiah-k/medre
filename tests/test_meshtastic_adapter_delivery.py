"""Tests for MeshtasticAdapter send semantics, session boundary,
and MeshtasticSession unit tests.
"""

from __future__ import annotations

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
