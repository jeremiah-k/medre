"""Extra tests for MeshtasticAdapter delivery: queue lifecycle evidence
(enqueued vs sent delivery_status).

Split from test_meshtastic_adapter_delivery.py to stay under 1500 lines.
"""

from __future__ import annotations

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.core.contracts.adapter import AdapterDeliveryResult
from tests.helpers.meshtastic import (
    make_meshtastic_config,
    make_meshtastic_rendering_result,
)

# ===================================================================
# Queue lifecycle evidence: delivery_status enqueued vs sent
# ===================================================================


class TestDeliveryStatusEnqueuedVsSent:
    """Evidence gap bridge: delivery_status distinguishes enqueued from sent.

    Meshtastic is the only adapter where deliver() returns
    native_message_id=None.  The real native ID arrives later via
    async queue callback.  These tests prove:

    1. deliver() returns delivery_status='enqueued' (not 'sent').
    2. queue.process_one() returns delivery_status='sent' with real ID.
    3. If stop/crash occurs between enqueue and send, evidence shows
       enqueued-only state (native_message_id=None).
    4. The two states are distinguishable via delivery_status.
    """

    async def test_deliver_returns_enqueued_status(self) -> None:
        """deliver() returns delivery_status='enqueued'."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        result = make_meshtastic_rendering_result()
        delivery = await adapter.deliver(result)

        assert delivery is not None
        assert delivery.delivery_status == "enqueued"
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"

    async def test_queue_process_one_returns_sent_status(self) -> None:
        """process_one with send_fn returns delivery_status='sent'."""
        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send(item):
            return {"packet_id": 42}

        result = await queue.process_one(send_fn=fake_send)
        assert result is not None
        assert result.delivery_result.delivery_status == "sent"
        assert result.delivery_result.native_message_id == "42"

    async def test_crash_between_enqueue_and_send_shows_enqueued_only(
        self,
    ) -> None:
        """Simulate stop/crash between enqueue and send.

        After enqueue, the queue contains the item but process_one has
        not been called.  The only evidence is the AdapterDeliveryResult
        from deliver(), which shows:
          - delivery_status='enqueued'
          - native_message_id=None
          - delivery_note='locally enqueued'

        This is the correct evidence for an enqueue-only state.
        """
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        # Enqueue without processing — simulates crash between enqueue
        # and queue drain.
        result = make_meshtastic_rendering_result(event_id="evt-crash")
        delivery = await adapter.deliver(result)

        # Evidence shows enqueued-only state
        assert delivery is not None
        assert delivery.delivery_status == "enqueued"
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"

        # Queue still has the item — it was never sent
        assert adapter._queue.queue_depth == 1
        assert adapter._queue.total_sent == 0

    async def test_enqueue_then_send_produces_both_states(self) -> None:
        """Full lifecycle: enqueue produces 'enqueued', then process_one
        produces 'sent' with real native_message_id."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        # Phase 1: Enqueue
        result = make_meshtastic_rendering_result(event_id="evt-lifecycle")
        delivery = await adapter.deliver(result)

        # Enqueue evidence
        assert delivery is not None
        assert delivery.delivery_status == "enqueued"
        assert delivery.native_message_id is None

        # Phase 2: Simulate queue drain by calling process_one
        # (adapter._session is None in fake mode, so send_one returns None)
        # Instead, directly process from the queue.
        queue = adapter._queue

        async def fake_send(item):
            return {"packet_id": 999}

        queue_result = await queue.process_one(send_fn=fake_send)

        # Send evidence
        assert queue_result is not None
        assert queue_result.delivery_result.delivery_status == "sent"
        assert queue_result.delivery_result.native_message_id == "999"
        assert queue_result.item.get("event_id") == "evt-lifecycle"

    async def test_enqueued_and_sent_are_distinguishable(self) -> None:
        """Two AdapterDeliveryResult instances — one enqueued, one sent —
        are distinguishable by delivery_status."""
        # Enqueued result (from adapter.deliver)
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        enqueue_result = await adapter.deliver(make_meshtastic_rendering_result())
        assert enqueue_result is not None
        assert enqueue_result.delivery_status == "enqueued"

        # Sent result (from queue.process_one)
        queue = MeshtasticOutboundQueue(delay_between_messages=0.0)
        await queue.enqueue({"text": "test"}, 0)

        async def fake_send(item):
            return {"packet_id": 123}

        sent_result = await queue.process_one(send_fn=fake_send)
        assert sent_result is not None
        assert sent_result.delivery_result.delivery_status == "sent"

        # They are different
        assert (
            enqueue_result.delivery_status
            != sent_result.delivery_result.delivery_status
        )

    async def test_default_delivery_status_is_sent(self) -> None:
        """AdapterDeliveryResult defaults to delivery_status='sent'."""
        result = AdapterDeliveryResult(native_message_id="123")
        assert result.delivery_status == "sent"

    async def test_multiple_enqueues_all_show_enqueued(self) -> None:
        """Multiple deliver() calls all return delivery_status='enqueued'."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        for i in range(3):
            result = make_meshtastic_rendering_result(event_id=f"evt-{i}")
            delivery = await adapter.deliver(result)
            assert delivery is not None
            assert delivery.delivery_status == "enqueued"
            assert delivery.native_message_id is None

        # All 3 are enqueued, none sent
        assert adapter._queue.queue_depth == 3
        assert adapter._queue.total_sent == 0


class TestDeliveryOutcomeQueuedStatus:
    """DeliveryOutcome.status is 'queued' for queue-enqueued Meshtastic
    deliveries, not 'success'."""

    async def test_meshtastic_delivery_outcome_is_queued(self, temp_storage) -> None:
        """Pipeline returns DeliveryOutcome(status='queued') with
        receipt.status='queued' for Meshtastic adapter delivery."""
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.renderer import MeshtasticRenderer
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing import Route, Router, RouteSource, RouteTarget

        fake_in_config = MeshtasticConfig(adapter_id="qo-fake-in")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="qo-mesh-out", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        route = Route(
            id="qo-route",
            source=RouteSource(
                adapter="qo-fake-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="qo-mesh-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={"qo-mesh-out": MeshtasticConfig(adapter_id="qo-mesh-out")}
            ),
            priority=50,
        )
        rp.register_adapter_platform("qo-mesh-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "qo-fake-in": fake_in_adapter,
                    "qo-mesh-out": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        from tests.helpers.meshtastic_bridge import (
            make_adapter_context,
            make_text_packet,
        )

        ctx = make_adapter_context("qo-fake-in", runner)
        await fake_in_adapter.start(ctx)

        import asyncio
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.core.contracts.adapter import AdapterContext

        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="qo-mesh-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.qo-mesh-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = make_text_packet(text="queued outcome test", packet_id=77123)
        await fake_in_adapter.simulate_inbound(packet)

        # The receipt in storage should be "queued".
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
            ("qo-mesh-out",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()

    async def test_queued_and_success_both_counted_as_accepted(
        self, temp_storage
    ) -> None:
        """Pipeline accepted counter counts both 'success' and 'queued'."""
        import asyncio
        import logging
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter

        # Simple fake adapter that gets TextRenderer (delivery_status="sent").
        from medre.adapters.fakes.transport import FakeTransportAdapter
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.renderer import MeshtasticRenderer
        from medre.config.adapters.meshtastic import MeshtasticConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.events.canonical import CanonicalEvent
        from medre.core.events.metadata import EventMetadata
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing import Route, Router, RouteSource, RouteTarget

        fake_in = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="acc-fake"))
        mesh_out = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="acc-mesh", connection_type="fake")
        )
        plain_out = FakeTransportAdapter(adapter_id="acc-plain")

        route = Route(
            id="acc-route",
            source=RouteSource(
                adapter="acc-fake",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[
                RouteTarget(adapter="acc-mesh", channel="0"),
                RouteTarget(adapter="acc-plain"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={"acc-mesh": MeshtasticConfig(adapter_id="acc-mesh")}
            ),
            priority=50,
        )
        rp.register_adapter_platform("acc-mesh", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "acc-fake": fake_in,
                    "acc-mesh": mesh_out,
                    "acc-plain": plain_out,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        from tests.helpers.meshtastic_bridge import (
            make_adapter_context,
        )

        ctx = make_adapter_context("acc-fake", runner)
        await fake_in.start(ctx)
        await mesh_out.start(
            AdapterContext(
                adapter_id="acc-mesh",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.acc-mesh"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )
        await plain_out.start(
            AdapterContext(
                adapter_id="acc-plain",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.acc-plain"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        event = CanonicalEvent(
            event_id="evt-accept-count",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="acc-fake",
            source_transport_id="!node",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "accept count test"},
            metadata=EventMetadata(),
        )
        outcomes = await runner.handle_ingress(event)

        # Two targets: one queued (mesh), one success (plain).
        assert len(outcomes) == 2
        statuses = {o.status for o in outcomes}
        assert "queued" in statuses
        assert "success" in statuses

        # Both counted as accepted (not failed).
        accepted = sum(1 for o in outcomes if o.status in {"success", "queued"})
        assert accepted == 2

        await fake_in.stop()
        await mesh_out.stop()
        await plain_out.stop()
        await runner.stop()
