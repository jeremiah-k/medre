"""Tests for MeshCore adapter pipeline integration: ingress through the
pipeline with MeshCore adapters, renderer registration, and end-to-end
event flow.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from typing import Any

from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage


def _make_renderer(*adapter_ids: str) -> MeshCoreRenderer:
    """Create MeshCoreRenderer with configs for given adapter IDs."""
    configs = {aid: MeshCoreConfig(adapter_id=aid) for aid in adapter_ids}
    return MeshCoreRenderer(configs=configs)


def _make_contact_packet(
    text: str = "hello pipeline",
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


async def _make_pipeline(
    mesh_adapter: FakeMeshCoreAdapter,
    rendering_pipeline: RenderingPipeline | None = None,
) -> PipelineRunner:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()

    route = Route(
        id="meshcore-route",
        source=RouteSource(
            adapter="fake_meshcore",
            event_kinds=("message.created",),
            channel="0",
        ),
        targets=[RouteTarget(adapter="meshcore-out", channel="0")],
    )
    router = Router(routes=[route])

    # Create an outbound fake adapter to receive deliveries.
    out_adapter_id = "meshcore-out"
    out_config = MeshCoreConfig(adapter_id=out_adapter_id)
    out_adapter = FakeMeshCoreAdapter(out_config)

    rp = rendering_pipeline or RenderingPipeline()
    rp.register(_make_renderer(out_adapter_id, "fake_meshcore"), priority=50)
    rp.register_adapter_platform(out_adapter_id, "meshcore")
    rp.register(TextRenderer(), priority=100)

    config = PipelineConfig(
        storage=storage,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={
            "fake_meshcore": mesh_adapter,
            out_adapter_id: out_adapter,
        },
        event_bus=EventBus(),
        rendering_pipeline=rp,
    )

    runner = PipelineRunner(config)
    return runner


def _make_adapter_context_for_pipeline(adapter_id: str, runner: PipelineRunner) -> Any:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""
    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=runner.ingress_handler,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


class TestMeshCorePipelineIntegration:
    """Pipeline integration with MeshCore adapters."""

    async def test_meshcore_renderer_registered(self) -> None:
        """MeshCoreRenderer can be registered in the rendering pipeline."""
        rp = RenderingPipeline()
        rp.register(_make_renderer("meshcore_node"), priority=50)
        rp.register_adapter_platform("meshcore_node", "meshcore")
        rp.register(TextRenderer(), priority=100)

        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )

        result = await rp.render(event, "meshcore_node")
        assert result.payload["text"] == "hello"
        assert result.metadata["renderer"] == "meshcore"

    async def test_text_renderer_fallback_for_non_meshcore(self) -> None:
        """TextRenderer handles events for non-MeshCore adapters."""
        rp = RenderingPipeline()
        rp.register(_make_renderer("meshcore_node"), priority=50)
        rp.register(TextRenderer(), priority=100)

        event = CanonicalEvent(
            event_id="evt-2",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )

        result = await rp.render(event, "some_other_adapter")
        assert result.metadata["renderer"] == "text"

    async def test_inbound_meshcore_event_has_native_ref(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """Inbound MeshCore events preserve native refs through simulation."""
        config = MeshCoreConfig(adapter_id="meshcore-test")
        adapter = FakeMeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-test")
        await adapter.start(ctx)

        packet = _make_contact_packet(timestamp=77777)
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        event = inbound_collector.events[0]
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "77777"
        assert event.source_native_ref.adapter == "meshcore-test"

    async def test_inbound_meshcore_event_kind(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """Inbound MeshCore text packets decode as message.created."""
        config = MeshCoreConfig(adapter_id="meshcore-test")
        adapter = FakeMeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-test")
        await adapter.start(ctx)

        packet = _make_contact_packet()
        await adapter.simulate_inbound(packet)

        event = inbound_collector.events[0]
        assert event.event_kind == "message.created"

    async def test_outbound_delivery_uses_meshcore_renderer(self, temp_storage) -> None:
        """Outbound delivery to MeshCore IDs uses MeshCoreRenderer,
        not TextRenderer. Proves can_render selection via platform registry."""
        in_adapter = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id="mc-in"))
        out_adapter = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id="local-mesh"))

        route = Route(
            id="meshcore-renderer-check",
            source=RouteSource(
                adapter="mc-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="local-mesh", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(_make_renderer("local-mesh", "mc-in"), priority=50)
        rp.register_adapter_platform("local-mesh", "meshcore")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"mc-in": in_adapter, "local-mesh": out_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )

        ctx = _make_adapter_context_for_pipeline("mc-in", runner)
        await in_adapter.start(ctx)

        packet = _make_channel_packet(text="renderer check 42", timestamp=9999)
        await in_adapter.simulate_inbound(packet)

        # After pipeline delivery, verify the outbound adapter's delivered_payloads
        assert len(out_adapter.delivered_payloads) == 1
        payload = out_adapter.delivered_payloads[0]

        # CRITICAL: Prove MeshCoreRenderer rendered this, not TextRenderer.
        assert payload.metadata["renderer"] == "meshcore"
        assert "channel_index" in payload.payload
        assert "meshnet_name" in payload.payload


# ===================================================================
# Native ref persistence tests
# ===================================================================


class TestMeshCoreNativeRefPersistence:
    """Pipeline integration tests for native ref persistence."""

    async def test_inbound_native_ref_persisted(self, temp_storage) -> None:
        """Inbound MeshCore event → pipeline store → NativeMessageRef(direction="inbound")."""
        config = MeshCoreConfig(adapter_id="meshcore-inbound")
        adapter = FakeMeshCoreAdapter(config)

        route = Route(
            id="meshcore-loopback",
            source=RouteSource(
                adapter="meshcore-inbound",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"meshcore-inbound": adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )

        ctx = _make_adapter_context_for_pipeline("meshcore-inbound", runner)
        await adapter.start(ctx)

        packet = _make_channel_packet(timestamp=55555, channel_idx=2)
        await adapter.simulate_inbound(packet)

        # Verify native ref persisted via resolve_native_ref
        resolved = await temp_storage.resolve_native_ref(
            adapter="meshcore-inbound",
            native_channel_id="2",
            native_message_id="55555",
        )
        assert resolved is not None
        assert resolved == adapter.inbound_events[0].event_id

    async def test_outbound_native_ref_persisted(self, temp_storage) -> None:
        """Outbound FakeMeshCoreAdapter deliver → pipeline store → NativeMessageRef(direction="outbound")."""
        in_config = MeshCoreConfig(adapter_id="meshcore-in")
        out_config = MeshCoreConfig(adapter_id="meshcore-out")
        in_adapter = FakeMeshCoreAdapter(in_config)
        out_adapter = FakeMeshCoreAdapter(out_config)

        route = Route(
            id="meshcore-route",
            source=RouteSource(
                adapter="meshcore-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="meshcore-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(_make_renderer("meshcore-out", "meshcore-in"), priority=50)
        rp.register_adapter_platform("meshcore-out", "meshcore")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"meshcore-in": in_adapter, "meshcore-out": out_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )

        ctx = _make_adapter_context_for_pipeline("meshcore-in", runner)
        await in_adapter.start(ctx)

        packet = _make_channel_packet(
            text="outbound test", timestamp=11111, channel_idx=0
        )
        await in_adapter.simulate_inbound(packet)

        # Verify outbound native ref persisted via resolve_native_ref
        # FakeMeshCoreClient first send gets packet_id=1
        resolved = await temp_storage.resolve_native_ref(
            adapter="meshcore-out",
            native_channel_id="0",
            native_message_id="1",
        )
        assert resolved is not None
        assert resolved == in_adapter.inbound_events[0].event_id

    async def test_failed_delivery_no_outbound_native_ref(self, temp_storage) -> None:
        """Failed deliver → no outbound native ref in storage."""
        in_config = MeshCoreConfig(adapter_id="meshcore-fail-in")
        out_config = MeshCoreConfig(adapter_id="meshcore-fail-out")
        in_adapter = FakeMeshCoreAdapter(in_config)
        out_adapter = FakeMeshCoreAdapter(out_config)
        out_adapter.set_deliver_failure(True)

        route = Route(
            id="meshcore-fail-route",
            source=RouteSource(
                adapter="meshcore-fail-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="meshcore-fail-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            _make_renderer("meshcore-fail-out", "meshcore-fail-in"),
            priority=50,
        )
        rp.register_adapter_platform("meshcore-fail-out", "meshcore")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "meshcore-fail-in": in_adapter,
                    "meshcore-fail-out": out_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )

        ctx = _make_adapter_context_for_pipeline("meshcore-fail-in", runner)
        await in_adapter.start(ctx)

        packet = _make_channel_packet(text="fail test", timestamp=22222, channel_idx=0)
        await in_adapter.simulate_inbound(packet)

        # Verify no outbound native ref from failed delivery
        resolved = await temp_storage.resolve_native_ref(
            adapter="meshcore-fail-out",
            native_channel_id="0",
            native_message_id="1",
        )
        assert resolved is None

        # Inbound ref should still exist
        inbound_resolved = await temp_storage.resolve_native_ref(
            adapter="meshcore-fail-in",
            native_channel_id="0",
            native_message_id="22222",
        )
        assert inbound_resolved is not None

    async def test_duplicate_inbound_native_ref_idempotent(self, temp_storage) -> None:
        """Duplicate inbound native refs are idempotent (INSERT OR IGNORE)."""
        config = MeshCoreConfig(adapter_id="meshcore-dup")
        adapter = FakeMeshCoreAdapter(config)

        route = Route(
            id="meshcore-dup-route",
            source=RouteSource(
                adapter="meshcore-dup",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"meshcore-dup": adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )

        ctx = _make_adapter_context_for_pipeline("meshcore-dup", runner)
        await adapter.start(ctx)

        packet = _make_channel_packet(timestamp=33333, channel_idx=0)
        await adapter.simulate_inbound(packet)

        # Manually store a duplicate native ref — should be idempotent
        import uuid as _uuid
        from datetime import timezone as _tz

        from medre.core.events.canonical import NativeMessageRef

        event = adapter.inbound_events[0]
        dup_ref = NativeMessageRef(
            id=f"nref-dup-{_uuid.uuid4()}",
            event_id=event.event_id,
            adapter="meshcore-dup",
            native_channel_id="0",
            native_message_id="33333",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=datetime.now(tz=_tz.utc),
        )
        # This should NOT raise despite the same (adapter, channel, msg_id) triple
        await temp_storage.store_native_ref(dup_ref)

        # Should still resolve to the same event
        resolved = await temp_storage.resolve_native_ref(
            adapter="meshcore-dup",
            native_channel_id="0",
            native_message_id="33333",
        )
        assert resolved is not None
        assert resolved == event.event_id


# ===================================================================
# Platform-aware renderer selection tests
# ===================================================================


class TestMeshCorePlatformRendererSelection:
    """Prove platform-aware renderer selection works for MeshCore
    via the pipeline's platform registry."""

    async def test_platform_aware_renderer_selection(self, temp_storage) -> None:
        """A realistic MeshCore adapter ID that does NOT start with 'meshcore'
        still selects MeshCoreRenderer through the pipeline's platform registry.

        This proves:
        - FakeMeshCoreAdapter.platform == "meshcore" drives dispatch
        - The RenderingPipeline platform registry maps adapter_id -> platform
        - MeshCoreRenderer.can_render matches on target_platform == "meshcore"
        - TextRenderer is NOT selected for MeshCore routes
        """
        # 1. Create adapters with realistic IDs that do NOT start with "meshcore"
        in_adapter = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id="field-node"))

        out_adapter = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id="field-out"))

        # 2. Route: field-node -> field-out
        route = Route(
            id="platform-registry-route",
            source=RouteSource(
                adapter="field-node",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="field-out", channel="0")],
        )
        router = Router(routes=[route])

        # 3. RenderingPipeline with MeshCoreRenderer via platform registry
        rp = RenderingPipeline()
        rp.register(_make_renderer("field-out", "field-node"), priority=50)
        rp.register(TextRenderer(), priority=100)

        # 4. PipelineRunner — start() calls _populate_renderer_platforms()
        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={"field-node": in_adapter, "field-out": out_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        # 5. Wire inbound adapter
        ctx = _make_adapter_context_for_pipeline("field-node", runner)
        await in_adapter.start(ctx)

        # 6. Send inbound packet
        packet = _make_channel_packet(
            text="platform dispatch test", channel_idx=0, timestamp=99999
        )
        await in_adapter.simulate_inbound(packet)

        # 7. Assertions

        # Outbound adapter received the rendered payload
        assert len(out_adapter.delivered_payloads) == 1
        result = out_adapter.delivered_payloads[0]

        # Proves MeshCoreRenderer was selected (not TextRenderer)
        assert result.metadata["renderer"] == "meshcore"

        # Proves MeshCore payload shape (channel_index + meshnet_name)
        assert "channel_index" in result.payload
        assert "meshnet_name" in result.payload

        # Outbound delivery returned a deterministic native_message_id
        assert out_adapter.fake_client.sent_count == 1
        sent_packet_id = out_adapter.fake_client.sent_packets[0]["packet_id"]

        # Native ref was persisted in storage
        resolved = await temp_storage.resolve_native_ref(
            adapter="field-out",
            native_channel_id="0",
            native_message_id=str(sent_packet_id),
        )
        assert resolved is not None
