"""Session callback and send_one bridge tests for Meshtastic adapter.

4. **Session callback bridge**: Exercises the sync _on_packet -> asyncio
   task -> publish_inbound path with the real adapter.

5. **send_one bridge**: Exercises queue.process_one with a monkeypatched
   session client to prove the full outbound send path.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.meshtastic_bridge import make_adapter_context, make_text_packet


# ===================================================================
# 4. Session callback bridge
# ===================================================================


class TestMeshtasticSessionCallbackBridge:
    """Exercises the sync _on_packet -> asyncio task -> publish_inbound
    path with the real MeshtasticAdapter connected to PipelineRunner.

    Tests verify that packets arriving through the session callback
    (matching real meshtastic-python pubsub behavior) are correctly
    converted to canonical events and flow through the pipeline.
    """

    async def test_on_packet_creates_background_task_for_inbound(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """_on_packet creates a tracked asyncio task that publishes inbound
        through the pipeline."""
        mesh_config = MeshtasticConfig(adapter_id="cb-mesh-in", connection_type="fake")
        mesh_adapter = MeshtasticAdapter(mesh_config)

        fake_config = MeshtasticConfig(adapter_id="cb-fake-out")
        fake_adapter = FakeMeshtasticAdapter(fake_config)

        route = Route(
            id="cb-route",
            source=RouteSource(
                adapter="cb-mesh-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="cb-fake-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("cb-fake-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"cb-mesh-in": mesh_adapter, "cb-fake-out": fake_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = make_adapter_context("cb-mesh-in", runner)
        await mesh_adapter.start(ctx)

        # Use _on_packet (sync callback path) instead of simulate_inbound.
        packet = make_text_packet(text="callback path test", packet_id=55555)
        mesh_adapter._on_packet(packet)

        # Allow background task to complete.
        _YIELD_MS = 0.1
        await asyncio.sleep(_YIELD_MS)

        # Background task should have completed and been discarded.
        assert len(mesh_adapter._background_tasks) == 0

        # Fake adapter received the rendered payload.
        assert len(fake_adapter.delivered_payloads) == 1
        assert (
            fake_adapter.delivered_payloads[0].payload["text"] == "callback path test"
        )

        await mesh_adapter.stop()
        await runner.stop()

    async def test_on_packet_ignores_non_text_packets(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """_on_packet silently drops non-text packets without creating
        background tasks."""
        mesh_config = MeshtasticConfig(
            adapter_id="cb-drop-mesh", connection_type="fake"
        )
        mesh_adapter = MeshtasticAdapter(mesh_config)

        route = Route(
            id="cb-drop-route",
            source=RouteSource(
                adapter="cb-drop-mesh",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"cb-drop-mesh": mesh_adapter},
                event_bus=EventBus(),
                rendering_pipeline=RenderingPipeline(),
            )
        )
        await runner.start()

        ctx = make_adapter_context("cb-drop-mesh", runner)
        await mesh_adapter.start(ctx)

        # Non-text packet: should be silently dropped.
        telemetry_packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "telemetry"},
        }
        mesh_adapter._on_packet(telemetry_packet)
        # Yield to the event loop so any stray callback can run.
        await asyncio.sleep(0)

        # No background tasks created for non-text packets.
        assert len(mesh_adapter._background_tasks) == 0

        await mesh_adapter.stop()
        await runner.stop()


# ===================================================================
# 5. send_one bridge (monkeypatched session)
# ===================================================================


class TestMeshtasticSendOneBridge:
    """Exercises send_one() with monkeypatched session client to prove
    the full outbound send path through the queue.

    These tests use MeshtasticAdapter(connection_type="tcp") with a
    monkeypatched session._create_client to inject a fake client that
    tracks sendText calls.  This exercises the queue.process_one ->
    session.send path with pacing.
    """

    async def test_send_one_with_monkeypatched_client_sends(
        self, make_adapter_context, monkeypatch
    ) -> None:
        """send_one() dequeues and sends via the monkeypatched client."""
        config = MeshtasticConfig(
            adapter_id="sendone-mesh", connection_type="tcp", host="1.2.3.4"
        )
        adapter = MeshtasticAdapter(config)

        class FakeClient:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            def sendText(self, text: str, channelIndex: int = 0) -> Any:
                self.sent.append({"text": text, "channel_index": channelIndex})
                return type("Packet", (), {"id": 42})()

        fake_client = FakeClient()

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        def fake_create_client(session_self: MeshtasticSession) -> FakeClient:
            return fake_client

        monkeypatch.setattr(MeshtasticSession, "_create_client", fake_create_client)

        # Patch pubsub to no-op.
        import sys
        import types

        fake_pubsub = types.ModuleType("pubsub")
        fake_pub = types.ModuleType("pubsub.pub")
        fake_pub.subscribe = lambda cb, topic: None
        fake_pub.unsubscribe = lambda cb, topic: None
        fake_pubsub.pub = fake_pub
        monkeypatch.setitem(sys.modules, "pubsub", fake_pubsub)
        monkeypatch.setitem(sys.modules, "pubsub.pub", fake_pub)

        ctx = make_adapter_context("sendone-mesh")
        await adapter.start(ctx)

        # Enqueue via deliver.
        result = RenderingResult(
            event_id="evt-sendone",
            target_adapter="sendone-mesh",
            target_channel="0",
            payload={"text": "send one test", "channel_index": 0, "meshnet_name": ""},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is None  # queue-based
        assert adapter.queue.pending_count == 1

        # send_one processes the queue item via the monkeypatched client.
        send_result = await adapter.send_one()
        assert send_result is not None
        assert send_result.delivery_result.native_message_id == "42"
        assert send_result.delivery_result.native_channel_id == "0"
        assert adapter.queue.pending_count == 0
        assert len(fake_client.sent) == 1
        assert fake_client.sent[0]["text"] == "send one test"
        assert fake_client.sent[0]["channel_index"] == 0

        await adapter.stop()

    async def test_send_one_returns_none_when_no_client(
        self, make_adapter_context
    ) -> None:
        """send_one() returns None in fake mode (no real client)."""
        config = MeshtasticConfig(adapter_id="sendone-noclient", connection_type="fake")
        adapter = MeshtasticAdapter(config)

        result = RenderingResult(
            event_id="evt-no-client",
            target_adapter="sendone-noclient",
            target_channel="0",
            payload={"text": "no client", "channel_index": 0, "meshnet_name": ""},
        )
        await adapter.deliver(result)
        assert adapter.queue.pending_count == 1

        send_result = await adapter.send_one()
        assert send_result is None
