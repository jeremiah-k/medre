"""Error mapping bridge tests: Meshtastic adapter error propagation.

Verifies that Meshtastic-specific errors are mapped to the framework
AdapterSendError / AdapterPermanentError taxonomy through the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.errors import (
    MeshtasticSendError,
)
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import (
    AdapterContext,
)
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
# 3. Error mapping bridge
# ===================================================================


class TestMeshtasticBridgeErrorMapping:
    """Error propagation through the bridge when Meshtastic adapter's
    queue or session fails.

    Verifies that Meshtastic-specific errors are mapped to the framework
    AdapterSendError / AdapterPermanentError taxonomy through the pipeline.
    """

    async def test_transient_queue_error_produces_failed_receipt(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When MeshtasticAdapter's queue raises a transient error, the
        pipeline records a 'failed' receipt."""
        fake_in_config = MeshtasticConfig(adapter_id="err-fake-in")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="err-mesh-out", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        # Patch queue.enqueue to raise transient error.
        mesh_out_adapter._queue.enqueue = AsyncMock(
            side_effect=MeshtasticSendError("radio busy", transient=True)
        )

        route = Route(
            id="err-transient-route",
            source=RouteSource(
                adapter="err-fake-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="err-mesh-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={"err-mesh-out": MeshtasticConfig(adapter_id="err-mesh-out")}
            ),
            priority=50,
        )
        rp.register_adapter_platform("err-mesh-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "err-fake-in": fake_in_adapter,
                    "err-mesh-out": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = make_adapter_context("err-fake-in", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="err-mesh-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.err-mesh-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = make_text_packet(text="transient error test")
        await fake_in_adapter.simulate_inbound(packet)

        # Delivery receipt should be 'failed'.
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
            ("err-mesh-out",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"

        # No outbound native ref for failed delivery.
        outbound_refs = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE adapter = ? AND direction = 'outbound'",
            ("err-mesh-out",),
        )
        assert len(outbound_refs) == 0

        # Inbound native ref should still exist for the source.
        inbound_refs = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE adapter = ? AND direction = 'inbound'",
            ("err-fake-in",),
        )
        assert len(inbound_refs) >= 1

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()

    async def test_permanent_error_produces_failed_receipt(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When MeshtasticAdapter's queue raises a permanent error, the
        pipeline records a 'failed' receipt."""
        fake_in_config = MeshtasticConfig(adapter_id="perm-fake-in")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="perm-mesh-out", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)

        # Patch queue.enqueue to raise permanent error.
        mesh_out_adapter._queue.enqueue = AsyncMock(
            side_effect=MeshtasticSendError("encoding failure", transient=False)
        )

        route = Route(
            id="perm-err-route",
            source=RouteSource(
                adapter="perm-fake-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="perm-mesh-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={"perm-mesh-out": MeshtasticConfig(adapter_id="perm-mesh-out")}
            ),
            priority=50,
        )
        rp.register_adapter_platform("perm-mesh-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "perm-fake-in": fake_in_adapter,
                    "perm-mesh-out": mesh_out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = make_adapter_context("perm-fake-in", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="perm-mesh-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.perm-mesh-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = make_text_packet(text="permanent error test")
        await fake_in_adapter.simulate_inbound(packet)

        # Delivery receipt should be 'failed'.
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
            ("perm-mesh-out",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await runner.stop()

    async def test_cancelled_error_propagates_through_deliver(self) -> None:
        """CancelledError propagates through MeshtasticAdapter.deliver()
        without being swallowed."""
        mesh_config = MeshtasticConfig(adapter_id="cancel-mesh", connection_type="fake")
        mesh_adapter = MeshtasticAdapter(mesh_config)

        # Patch queue.enqueue to raise CancelledError.
        mesh_adapter._queue.enqueue = AsyncMock(side_effect=asyncio.CancelledError())

        result = RenderingResult(
            event_id="evt-cancel",
            target_adapter="cancel-mesh",
            target_channel="0",
            payload={"text": "cancel test", "channel_index": 0},
        )

        with pytest.raises(asyncio.CancelledError):
            await mesh_adapter.deliver(result)

    async def test_error_in_one_target_does_not_affect_other(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A Meshtastic delivery failure does not prevent delivery to a
        second target in the same route."""
        fake_in_config = MeshtasticConfig(adapter_id="iso-fake-in")
        fake_in_adapter = FakeMeshtasticAdapter(fake_in_config)

        mesh_out_config = MeshtasticConfig(
            adapter_id="iso-mesh-out", connection_type="fake"
        )
        mesh_out_adapter = MeshtasticAdapter(mesh_out_config)
        # Inject failure into the Meshtastic adapter.
        mesh_out_adapter._queue.enqueue = AsyncMock(
            side_effect=MeshtasticSendError("radio busy", transient=True)
        )

        good_config = MeshtasticConfig(adapter_id="iso-good-out")
        good_adapter = FakeMeshtasticAdapter(good_config)

        route = Route(
            id="iso-route",
            source=RouteSource(
                adapter="iso-fake-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[
                RouteTarget(adapter="iso-mesh-out", channel="0"),
                RouteTarget(adapter="iso-good-out", channel="0"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={
                    "iso-mesh-out": MeshtasticConfig(adapter_id="iso-mesh-out"),
                    "iso-good-out": MeshtasticConfig(adapter_id="iso-good-out"),
                }
            ),
            priority=50,
        )
        rp.register_adapter_platform("iso-mesh-out", "meshtastic")
        rp.register_adapter_platform("iso-good-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "iso-fake-in": fake_in_adapter,
                    "iso-mesh-out": mesh_out_adapter,
                    "iso-good-out": good_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = make_adapter_context("iso-fake-in", runner)
        await fake_in_adapter.start(ctx)
        await mesh_out_adapter.start(
            AdapterContext(
                adapter_id="iso-mesh-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.iso-mesh-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )
        await good_adapter.start(
            AdapterContext(
                adapter_id="iso-good-out",
                event_bus=None,
                publish_inbound=AsyncMock(),
                logger=logging.getLogger("test.bridge.iso-good-out"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
        )

        packet = make_text_packet(text="isolation test")
        await fake_in_adapter.simulate_inbound(packet)

        # Good adapter received its payload despite the other target failing.
        assert len(good_adapter.delivered_payloads) == 1

        # Two receipts: one sent, one failed.
        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE event_id = ?",
            (fake_in_adapter.inbound_events[0].event_id,),
        )
        assert len(rows) == 2
        by_status = {r["target_adapter"]: r["status"] for r in rows}
        assert by_status["iso-mesh-out"] == "failed"
        assert by_status["iso-good-out"] == "sent"

        await fake_in_adapter.stop()
        await mesh_out_adapter.stop()
        await good_adapter.stop()
        await runner.stop()
