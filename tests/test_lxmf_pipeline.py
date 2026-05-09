"""Tests for LXMF adapter pipeline integration: ingress through the
pipeline with LXMF adapters, renderer registration, and end-to-end
event flow including fields envelope in the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import os
from datetime import datetime, timezone

import pytest

from medre.adapters.fake_lxmf import FakeLxmfAdapter
from medre.adapters.lxmf.config import LxmfConfig
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.lxmf.fields import FIELD_MEDRE_ENVELOPE, LXMF_NAMESPACE
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner


def _make_text_packet(
    content: str = "hello pipeline",
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


def _make_adapter_context_for_pipeline(
    adapter_id: str, runner: PipelineRunner
):
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""
    from medre.adapters.base import AdapterContext
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=runner.ingress_handler,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


class TestLxmfPipelineIntegration:
    """Pipeline integration with LXMF adapters."""

    async def test_lxmf_renderer_registered(self) -> None:
        """LxmfRenderer can be registered in the rendering pipeline."""
        rp = RenderingPipeline()
        rp.register(LxmfRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

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

        result = await rp.render(event, "lxmf_node")
        assert result.payload["content"] == "hello"
        assert result.metadata["renderer"] == "lxmf"

    async def test_text_renderer_fallback_for_non_lxmf(self) -> None:
        """TextRenderer handles events for non-LXMF adapters."""
        rp = RenderingPipeline()
        rp.register(LxmfRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

        event = CanonicalEvent(
            event_id="evt-2",
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

        result = await rp.render(event, "some_other_adapter")
        assert result.metadata["renderer"] == "text"

    async def test_inbound_lxmf_event_has_native_ref(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """Inbound LXMF events preserve native refs through simulation."""
        config = LxmfConfig(adapter_id="lxmf-test")
        adapter = FakeLxmfAdapter(config)
        ctx = make_adapter_context("lxmf-test")
        await adapter.start(ctx)

        packet = _make_text_packet(msg_id="aa" * 32)
        await adapter.simulate_inbound(packet)

        assert len(inbound_collector.events) == 1
        event = inbound_collector.events[0]
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "aa" * 32
        assert event.source_native_ref.adapter == "lxmf-test"

    async def test_inbound_lxmf_event_kind(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """Inbound LXMF text packets decode as message.created."""
        config = LxmfConfig(adapter_id="lxmf-test")
        adapter = FakeLxmfAdapter(config)
        ctx = make_adapter_context("lxmf-test")
        await adapter.start(ctx)

        packet = _make_text_packet()
        await adapter.simulate_inbound(packet)

        event = inbound_collector.events[0]
        assert event.event_kind == "message.created"

    async def test_outbound_delivery_uses_lxmf_renderer(
        self, temp_storage
    ) -> None:
        """Outbound delivery to LXMF IDs uses LxmfRenderer."""
        in_adapter = FakeLxmfAdapter(LxmfConfig(adapter_id="lxmf-in"))
        out_adapter = FakeLxmfAdapter(LxmfConfig(adapter_id="local-lxmf"))

        route = Route(
            id="lxmf-renderer-check",
            source=RouteSource(
                adapter="lxmf-in",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="local-lxmf", channel=None)],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(LxmfRenderer(known_adapters={"local-lxmf"}), priority=50)
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"lxmf-in": in_adapter, "local-lxmf": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("lxmf-in", runner)
        await in_adapter.start(ctx)

        packet = _make_text_packet(content="renderer check 42")
        await in_adapter.simulate_inbound(packet)

        assert len(out_adapter.delivered_payloads) == 1
        payload = out_adapter.delivered_payloads[0]

        # CRITICAL: Prove LxmfRenderer rendered this, not TextRenderer.
        assert payload.metadata["renderer"] == "lxmf"
        assert "title" in payload.payload
        assert "fields" in payload.payload

    async def test_fields_envelope_in_pipeline(
        self, temp_storage
    ) -> None:
        """Outbound rendered payload contains MEDRE envelope in fields."""
        in_adapter = FakeLxmfAdapter(LxmfConfig(adapter_id="lxmf-fields-in"))
        out_adapter = FakeLxmfAdapter(LxmfConfig(adapter_id="lxmf-fields-out"))

        route = Route(
            id="lxmf-fields-route",
            source=RouteSource(
                adapter="lxmf-fields-in",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lxmf-fields-out", channel=None)],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(LxmfRenderer(known_adapters={"lxmf-fields-out"}), priority=50)
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"lxmf-fields-in": in_adapter, "lxmf-fields-out": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("lxmf-fields-in", runner)
        await in_adapter.start(ctx)

        packet = _make_text_packet(content="envelope test")
        await in_adapter.simulate_inbound(packet)

        assert len(out_adapter.delivered_payloads) == 1
        payload = out_adapter.delivered_payloads[0]
        fields = payload.payload["fields"]
        assert FIELD_MEDRE_ENVELOPE in fields
        envelope = fields[FIELD_MEDRE_ENVELOPE]
        assert LXMF_NAMESPACE in envelope


# ===================================================================
# Native ref persistence tests
# ===================================================================


class TestLxmfNativeRefPersistence:
    """Pipeline integration tests for native ref persistence."""

    async def test_inbound_native_ref_persisted(
        self, temp_storage
    ) -> None:
        """Inbound LXMF event → pipeline store → NativeMessageRef(direction="inbound")."""
        config = LxmfConfig(adapter_id="lxmf-inbound")
        adapter = FakeLxmfAdapter(config)

        route = Route(
            id="lxmf-loopback",
            source=RouteSource(
                adapter="lxmf-inbound",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"lxmf-inbound": adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("lxmf-inbound", runner)
        await adapter.start(ctx)

        packet = _make_text_packet(msg_id="bb" * 32)
        await adapter.simulate_inbound(packet)

        # Verify native ref persisted
        resolved = await temp_storage.resolve_native_ref(
            adapter="lxmf-inbound",
            native_channel_id=None,
            native_message_id="bb" * 32,
        )
        assert resolved is not None
        assert resolved == adapter.inbound_events[0].event_id

    async def test_outbound_native_ref_persisted(
        self, temp_storage
    ) -> None:
        """Outbound FakeLxmfAdapter deliver → pipeline store → NativeMessageRef(direction="outbound")."""
        in_config = LxmfConfig(adapter_id="lxmf-in")
        out_config = LxmfConfig(adapter_id="lxmf-out")
        in_adapter = FakeLxmfAdapter(in_config)
        out_adapter = FakeLxmfAdapter(out_config)

        route = Route(
            id="lxmf-route",
            source=RouteSource(
                adapter="lxmf-in",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lxmf-out", channel=None)],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(LxmfRenderer(known_adapters={"lxmf-out"}), priority=50)
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"lxmf-in": in_adapter, "lxmf-out": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("lxmf-in", runner)
        await in_adapter.start(ctx)

        packet = _make_text_packet(content="outbound test")
        await in_adapter.simulate_inbound(packet)

        # FakeLxmfClient first send gets a deterministic message_id
        first_sent = out_adapter.fake_client.sent_messages[0]
        sent_msg_id = first_sent["message_id"]

        resolved = await temp_storage.resolve_native_ref(
            adapter="lxmf-out",
            native_channel_id=None,
            native_message_id=sent_msg_id,
        )
        assert resolved is not None
        assert resolved == in_adapter.inbound_events[0].event_id

    async def test_failed_delivery_no_outbound_native_ref(
        self, temp_storage
    ) -> None:
        """Failed deliver → no outbound native ref in storage."""
        in_config = LxmfConfig(adapter_id="lxmf-fail-in")
        out_config = LxmfConfig(adapter_id="lxmf-fail-out")
        in_adapter = FakeLxmfAdapter(in_config)
        out_adapter = FakeLxmfAdapter(out_config)
        out_adapter.set_deliver_failure(True)

        route = Route(
            id="lxmf-fail-route",
            source=RouteSource(
                adapter="lxmf-fail-in",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lxmf-fail-out", channel=None)],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(LxmfRenderer(known_adapters={"lxmf-fail-out"}), priority=50)
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"lxmf-fail-in": in_adapter, "lxmf-fail-out": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("lxmf-fail-in", runner)
        await in_adapter.start(ctx)

        packet = _make_text_packet(content="fail test")
        await in_adapter.simulate_inbound(packet)

        # Verify no outbound native ref from failed delivery
        # Fake client didn't send anything
        assert out_adapter.fake_client.sent_count == 0

        # Inbound ref should still exist
        inbound_resolved = await temp_storage.resolve_native_ref(
            adapter="lxmf-fail-in",
            native_channel_id=None,
            native_message_id="cd" * 32,
        )
        assert inbound_resolved is not None

    async def test_duplicate_inbound_native_ref_idempotent(
        self, temp_storage
    ) -> None:
        """Duplicate inbound native refs are idempotent (INSERT OR IGNORE)."""
        config = LxmfConfig(adapter_id="lxmf-dup")
        adapter = FakeLxmfAdapter(config)

        route = Route(
            id="lxmf-dup-route",
            source=RouteSource(
                adapter="lxmf-dup",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"lxmf-dup": adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))

        ctx = _make_adapter_context_for_pipeline("lxmf-dup", runner)
        await adapter.start(ctx)

        packet = _make_text_packet(msg_id="ee" * 32)
        await adapter.simulate_inbound(packet)

        # Manually store a duplicate native ref — should be idempotent
        from medre.core.events.canonical import NativeMessageRef
        import uuid as _uuid
        from datetime import timezone as _tz

        event = adapter.inbound_events[0]
        dup_ref = NativeMessageRef(
            id=f"nref-dup-{_uuid.uuid4()}",
            event_id=event.event_id,
            adapter="lxmf-dup",
            native_channel_id=None,
            native_message_id="ee" * 32,
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=datetime.now(tz=_tz.utc),
        )
        # This should NOT raise despite the same (adapter, channel, msg_id) triple
        await temp_storage.store_native_ref(dup_ref)

        # Should still resolve to the same event
        resolved = await temp_storage.resolve_native_ref(
            adapter="lxmf-dup",
            native_channel_id=None,
            native_message_id="ee" * 32,
        )
        assert resolved is not None
        assert resolved == event.event_id


# ===================================================================
# Platform-aware renderer selection tests
# ===================================================================


class TestLxmfPlatformRendererSelection:
    """Prove platform-aware renderer selection works for LXMF
    without relying on adapter-name prefixes or known_adapters."""

    async def test_platform_aware_renderer_selection(
        self, temp_storage
    ) -> None:
        """A realistic LXMF adapter ID that does NOT start with 'lxmf'
        still selects LxmfRenderer through the pipeline's platform registry.

        This proves:
        - FakeLxmfAdapter.platform == "lxmf" drives dispatch
        - The RenderingPipeline platform registry maps adapter_id -> platform
        - LxmfRenderer.can_render matches on target_platform == "lxmf"
        - TextRenderer is NOT selected for LXMF routes
        - known_adapters is NOT required
        """
        # 1. Create adapters with realistic IDs that do NOT start with "lxmf"
        in_adapter = FakeLxmfAdapter(LxmfConfig(adapter_id="field-node"))
        in_adapter.platform = "lxmf"

        out_adapter = FakeLxmfAdapter(LxmfConfig(adapter_id="rnode-out"))
        out_adapter.platform = "lxmf"

        # 2. Route: field-node -> rnode-out
        route = Route(
            id="platform-registry-route",
            source=RouteSource(
                adapter="field-node",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="rnode-out", channel=None)],
        )
        router = Router(routes=[route])

        # 3. RenderingPipeline with LxmfRenderer — NO known_adapters (critical!)
        rp = RenderingPipeline()
        rp.register(LxmfRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

        # 4. PipelineRunner — start() calls _populate_renderer_platforms()
        runner = PipelineRunner(PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"field-node": in_adapter, "rnode-out": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))
        await runner.start()

        # 5. Wire inbound adapter
        ctx = _make_adapter_context_for_pipeline("field-node", runner)
        await in_adapter.start(ctx)

        # 6. Send inbound packet
        packet = _make_text_packet(content="platform dispatch test")
        await in_adapter.simulate_inbound(packet)

        # 7. Assertions

        # Outbound adapter received the rendered payload
        assert len(out_adapter.delivered_payloads) == 1
        result = out_adapter.delivered_payloads[0]

        # Proves LxmfRenderer was selected (not TextRenderer)
        assert result.metadata["renderer"] == "lxmf"

        # Proves LXMF payload shape (content + destination_hash)
        assert "content" in result.payload
        assert (
            "channel_index" not in result.payload
            or "destination_hash" in result.payload
        )

        # Outbound delivery returned a deterministic native_message_id
        assert out_adapter.fake_client.sent_count == 1
        sent_msg_id = out_adapter.fake_client.sent_messages[0]["message_id"]

        # Native ref was persisted in storage
        resolved = await temp_storage.resolve_native_ref(
            adapter="rnode-out",
            native_channel_id=None,
            native_message_id=sent_msg_id,
        )
        assert resolved is not None
