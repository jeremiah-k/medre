"""Tests for MeshtasticAdapter: real adapter lifecycle (start/stop),
connection modes, pubsub subscription, task scheduling, and queue ownership.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.errors import (
    MeshtasticConnectionError,
)
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterPermanentError,
)
from medre.core.events import CanonicalEvent, EventMetadata
from tests.helpers.meshtastic import (
    make_meshtastic_config,
    make_meshtastic_rendering_result,
    make_meshtastic_text_packet,
)

# ===================================================================
# Real MeshtasticAdapter tests
# ===================================================================


class TestMeshtasticAdapterLifecycle:
    """MeshtasticAdapter lifecycle with fake config."""

    async def test_start_fake_mode(self, make_adapter_context) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

    async def test_stop(self, make_adapter_context) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_deliver_returns_none_scaffold(self) -> None:
        """Real adapter deliver() enqueues and returns AdapterDeliveryResult with
        delivery_note='locally enqueued' and native_message_id=None."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = make_meshtastic_rendering_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"

    async def test_deliver_enqueues_to_queue(self) -> None:
        """deliver() puts the payload into the adapter-owned queue."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = make_meshtastic_rendering_result()
        await adapter.deliver(result)
        assert adapter.queue.pending_count == 1

    async def test_deliver_rejects_canonical_event(self) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="mesh-1",
            source_transport_id="!node1",
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

    async def test_simulate_inbound(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="via real adapter")
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "via real adapter"

    async def test_simulate_inbound_symbolic_text_message_app(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="symbolic real adapter")
        packet["decoded"]["portnum"] = "TEXT_MESSAGE_APP"
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "symbolic real adapter"


# ===================================================================
# Idempotent lifecycle
# ===================================================================


class TestMeshtasticAdapterIdempotentLifecycle:
    """start/stop are idempotent — calling multiple times is safe."""

    async def test_double_start_is_no_op(self, make_adapter_context) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        # Second start should not raise or change state
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

    async def test_double_stop_is_no_op(self, make_adapter_context) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        await adapter.stop()
        # Second stop should not raise
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_stop_without_start_is_no_op(self) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        # Should not raise
        await adapter.stop()

    async def test_start_stop_start_cycle(self, make_adapter_context) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        await adapter.stop()
        # Restart should work
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"


# ===================================================================
# Connection modes with monkeypatched fake clients
# ===================================================================


class TestMeshtasticAdapterConnectionModes:
    """Non-fake connection modes work with monkeypatched fake modules."""

    @staticmethod
    def _make_fake_interface_class(name: str):
        """Create a fake interface class that records its constructor args."""

        class FakeInterface:
            _instances = []

            def __init__(self, **kwargs):
                self._kwargs = kwargs
                self._closed = False
                FakeInterface._instances.append(self)

            def close(self):
                self._closed = True

            def sendText(self, text, channelIndex=0):
                """Sync sendText returning a packet with id."""
                return type("Packet", (), {"id": 42})()

        FakeInterface._instances = []
        FakeInterface.__name__ = name
        FakeInterface.__qualname__ = name
        return FakeInterface

    def _patch_session_create_client(self, adapter, FakeClass, monkeypatch):
        """Patch MeshtasticSession._create_client to return a FakeClass instance."""

        def fake_create_client(session_self):
            return FakeClass()

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
        # Skip pubsub subscription — these tests use fake clients without pubsub.
        monkeypatch.setattr(
            MeshtasticSession, "_subscribe_callbacks", lambda self: None
        )

    async def test_tcp_mode_with_monkeypatched_client(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """TCP mode creates TCPInterface(hostname, portNumber) via session."""
        FakeTCP = self._make_fake_interface_class("FakeTCPInterface")
        config = make_meshtastic_config(
            connection_type="tcp",
            host="192.168.1.100",
            port=4403,
        )
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeTCP(
                hostname=session_self._config.host,
                portNumber=session_self._config.port,
            )

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
        monkeypatch.setattr(
            MeshtasticSession, "_subscribe_callbacks", lambda self: None
        )

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        assert adapter._session.client is not None
        assert adapter._session.client._kwargs["hostname"] == "192.168.1.100"
        assert adapter._session.client._kwargs["portNumber"] == 4403

        await adapter.stop()
        assert adapter._session is None

    async def test_serial_mode_with_monkeypatched_client(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """Serial mode creates SerialInterface(devPath) via session."""
        FakeSerial = self._make_fake_interface_class("FakeSerialInterface")
        config = make_meshtastic_config(
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
        )
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeSerial(devPath=session_self._config.serial_port)

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
        monkeypatch.setattr(
            MeshtasticSession, "_subscribe_callbacks", lambda self: None
        )

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        assert adapter._session.client is not None
        assert adapter._session.client._kwargs["devPath"] == "/dev/ttyUSB0"

        await adapter.stop()

    async def test_ble_mode_with_monkeypatched_client(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """BLE mode creates BLEInterface(address) via session."""
        FakeBLE = self._make_fake_interface_class("FakeBLEInterface")
        config = make_meshtastic_config(
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeBLE(address=session_self._config.ble_address)

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
        monkeypatch.setattr(
            MeshtasticSession, "_subscribe_callbacks", lambda self: None
        )

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None
        assert adapter._session.client is not None
        assert adapter._session.client._kwargs["address"] == "AA:BB:CC:DD:EE:FF"

        await adapter.stop()

    async def test_non_fake_without_mtjk_raises(self, monkeypatch) -> None:
        """Non-fake mode raises MeshtasticConnectionError when mtjk missing."""
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", False)
        config = make_meshtastic_config(
            connection_type="tcp",
            host="192.168.1.100",
        )
        adapter = MeshtasticAdapter(config)
        with pytest.raises(MeshtasticConnectionError, match="mtjk not installed"):
            await adapter.start(
                AdapterContext(
                    adapter_id="mesh-1",
                    event_bus=None,
                    publish_inbound=AsyncMock(),
                    logger=__import__("logging").getLogger("test"),
                    clock=lambda: datetime.now(timezone.utc),
                    shutdown_event=asyncio.Event(),
                )
            )

    async def test_stop_closes_client(self, make_adapter_context, monkeypatch) -> None:
        """stop() calls client.close() on the real client via session."""
        FakeTCP = self._make_fake_interface_class("FakeTCPInterface")
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)
        self._patch_session_create_client(adapter, FakeTCP, monkeypatch)

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        session = adapter._session
        client = session.client
        assert not client._closed
        await adapter.stop()
        assert client._closed


# ===================================================================
# Pubsub subscription
# ===================================================================


def _patch_pubsub(monkeypatch, subscribe_fn=None, unsubscribe_fn=None):
    """Patch pubsub module for session tests."""
    fake_pubsub = types.ModuleType("pubsub")
    fake_pub = types.ModuleType("pubsub.pub")
    fake_pub.subscribe = subscribe_fn or (lambda cb, topic: None)
    fake_pub.unsubscribe = unsubscribe_fn or (lambda cb, topic: None)
    fake_pubsub.pub = fake_pub
    monkeypatch.setitem(sys.modules, "pubsub", fake_pubsub)
    monkeypatch.setitem(sys.modules, "pubsub.pub", fake_pub)


class TestMeshtasticAdapterPubsubSubscription:
    """Subscription failures are raised, not swallowed."""

    async def test_successful_subscription_calls_pub_subscribe(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """start() calls pub.subscribe on non-fake connection."""
        subscribed = []

        def fake_subscribe(callback, topic):
            subscribed.append(("_on_receive", topic))

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(monkeypatch, subscribe_fn=fake_subscribe)

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert len(subscribed) == 1
        assert subscribed[0] == ("_on_receive", "meshtastic.receive")
        await adapter.stop()

    async def test_subscription_failure_during_start_raises(self, monkeypatch) -> None:
        """start() raises MeshtasticConnectionError when subscription fails."""

        class FakeClient:
            def close(self):
                self.closed = True

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("nope")),
        )

        with pytest.raises(MeshtasticConnectionError, match="meshtastic.receive"):
            await adapter.start(
                AdapterContext(
                    adapter_id="mesh-1",
                    event_bus=None,
                    publish_inbound=AsyncMock(),
                    logger=__import__("logging").getLogger("test"),
                    clock=lambda: datetime.now(timezone.utc),
                    shutdown_event=asyncio.Event(),
                )
            )

    async def test_start_failure_closes_client(self, monkeypatch) -> None:
        """When subscription fails, start() closes the client before re-raising."""
        closed_flag = {"closed": False}

        class FakeClient:
            def close(self):
                closed_flag["closed"] = True

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("fail")),
        )

        with pytest.raises(MeshtasticConnectionError):
            await adapter.start(
                AdapterContext(
                    adapter_id="mesh-1",
                    event_bus=None,
                    publish_inbound=AsyncMock(),
                    logger=__import__("logging").getLogger("test"),
                    clock=lambda: datetime.now(timezone.utc),
                    shutdown_event=asyncio.Event(),
                )
            )

        assert closed_flag[
            "closed"
        ], "Client should be closed after subscription failure"

    async def test_start_failure_no_orphaned_state(self, monkeypatch) -> None:
        """After subscription failure, adapter is not started and session is None."""

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("fail")),
        )

        with pytest.raises(MeshtasticConnectionError):
            await adapter.start(
                AdapterContext(
                    adapter_id="mesh-1",
                    event_bus=None,
                    publish_inbound=AsyncMock(),
                    logger=__import__("logging").getLogger("test"),
                    clock=lambda: datetime.now(timezone.utc),
                    shutdown_event=asyncio.Event(),
                )
            )

        assert adapter._started is False
        assert adapter._client is None
        assert adapter._session is None

    async def test_health_check_unknown_after_subscription_failure(
        self, monkeypatch
    ) -> None:
        """health_check() returns 'unknown' after subscription failure and cleanup."""

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("fail")),
        )

        with pytest.raises(MeshtasticConnectionError):
            await adapter.start(
                AdapterContext(
                    adapter_id="mesh-1",
                    event_bus=None,
                    publish_inbound=AsyncMock(),
                    logger=__import__("logging").getLogger("test"),
                    clock=lambda: datetime.now(timezone.utc),
                    shutdown_event=asyncio.Event(),
                )
            )

        # After failed start, client is cleaned up, health should be "unknown"
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_unsubscribe_only_when_subscribed(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """stop() does not call pub.unsubscribe if subscription never succeeded."""
        unsubscribe_calls = []

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        _patch_pubsub(
            monkeypatch,
            subscribe_fn=lambda cb, topic: (_ for _ in ()).throw(RuntimeError("fail")),
            unsubscribe_fn=lambda cb, topic: unsubscribe_calls.append((cb, topic)),
        )

        with pytest.raises(MeshtasticConnectionError):
            await adapter.start(
                AdapterContext(
                    adapter_id="mesh-1",
                    event_bus=None,
                    publish_inbound=AsyncMock(),
                    logger=__import__("logging").getLogger("test"),
                    clock=lambda: datetime.now(timezone.utc),
                    shutdown_event=asyncio.Event(),
                )
            )

        # stop should not try to unsubscribe since subscription never succeeded
        await adapter.stop()
        assert len(unsubscribe_calls) == 0


# ===================================================================
# Task scheduling
# ===================================================================


class TestMeshtasticAdapterTaskScheduling:
    """Background tasks from _on_packet are tracked and cleaned up."""

    async def test_on_packet_creates_tracked_task(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        packet = make_meshtastic_text_packet(text="tracked")
        adapter._on_packet(packet)

        # Allow the background task to complete
        await asyncio.sleep(0.05)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "tracked"
        # Task should have been discarded after completion
        assert len(adapter._background_tasks) == 0

    async def test_stop_cancels_background_tasks(self, make_adapter_context) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        # Inject a long-running task
        async def _slow():
            await asyncio.sleep(100)

        task = asyncio.create_task(_slow())
        adapter._background_tasks.add(task)

        await adapter.stop()
        assert task.cancelled() or task.done()
        assert len(adapter._background_tasks) == 0

    async def test_drain_background_tasks_with_timeout(
        self, make_adapter_context
    ) -> None:
        """_drain_background_tasks cancels and awaits all tracked tasks."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        async def _block_forever():
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                # Swallow to test drain behavior
                pass

        t1 = asyncio.create_task(_block_forever())
        t2 = asyncio.create_task(_block_forever())
        adapter._background_tasks.add(t1)
        adapter._background_tasks.add(t2)

        await adapter._drain_background_tasks(timeout=1.0)
        assert len(adapter._background_tasks) == 0
        assert t1.done()
        assert t2.done()

    async def test_no_ensure_future(self) -> None:
        """Verify _on_packet does not use asyncio.ensure_future."""
        import inspect

        source = inspect.getsource(MeshtasticAdapter._on_packet)
        assert "ensure_future" not in source
        assert "create_task" in source


# ===================================================================
# Queue ownership and pacing
# ===================================================================


class TestMeshtasticAdapterQueueOwnership:
    """Adapter owns queue/pacing; runtime pipeline and renderer do not sleep."""

    async def test_adapter_owns_queue(self) -> None:
        config = make_meshtastic_config(
            connection_type="fake", message_delay_seconds=0.25
        )
        adapter = MeshtasticAdapter(config)
        assert adapter.queue is adapter._queue
        assert adapter.queue.delay_between_messages == 0.25

    async def test_queue_health_accessible(self) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        health = adapter.queue_health
        assert "pending_count" in health
        assert "total_sent" in health
        assert "total_failed" in health
        assert health["pending_count"] == 0

    async def test_deliver_enqueues_and_queue_pending_grows(self) -> None:
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = make_meshtastic_rendering_result()
        await adapter.deliver(result)
        assert adapter.queue.pending_count == 1

        result2 = make_meshtastic_rendering_result(event_id="evt-2")
        await adapter.deliver(result2)
        assert adapter.queue.pending_count == 2

    async def test_send_one_returns_none_when_no_client(self) -> None:
        """send_one() returns None in fake mode (no real client)."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = await adapter.send_one()
        assert result is None

    async def test_send_one_dequeues_and_sends_with_fake_client(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """send_one() with a monkeypatched client sends via the queue."""
        config = make_meshtastic_config(connection_type="tcp", host="1.2.3.4")
        adapter = MeshtasticAdapter(config)

        class FakeClient:
            def __init__(self):
                self.sent = []

            def sendText(self, text, channelIndex=0):
                self.sent.append({"text": text, "channel_index": channelIndex})
                return type("Packet", (), {"id": 77})()

        fake_client = FakeClient()

        # Patch session to use our fake client
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
        monkeypatch.setattr(
            MeshtasticSession,
            "_create_client",
            lambda self: fake_client,
        )
        monkeypatch.setattr(
            MeshtasticSession, "_subscribe_callbacks", lambda self: None
        )

        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        # Enqueue a payload
        await adapter._queue.enqueue({"text": "hello"}, 0)
        assert adapter.queue.pending_count == 1

        # send_one processes the queue item
        result = await adapter.send_one()
        assert result is not None
        assert result.delivery_result.native_message_id == "77"
        assert result.delivery_result.native_channel_id == "0"
        assert adapter.queue.pending_count == 0
        assert len(fake_client.sent) == 1

        await adapter.stop()
