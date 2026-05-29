"""Matrix <-> Meshtastic runtime-path operational tests.

Deterministic tests exercising the full PipelineRunner ingress-to-delivery
path using fake adapters.  No real Matrix homeserver, no real Meshtastic
radio.

Tests are split into:
- Runtime-path tests that exercise PipelineRunner handle_ingress through
  to adapter delivery, receipt, and evidence.
- Renderer-level characterization tests for text/reply/fallback rendering.
- Codec decode characterization tests.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import (
    AdapterContext,
)
from medre.core.engine.pipeline.runner import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingPipeline,
)
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats
from medre.core.storage.backend import StorageBackend

# ---------------------------------------------------------------------------
# Local fakes / helpers
# ---------------------------------------------------------------------------


class _FakeStorage(StorageBackend):
    """Minimal in-memory storage for operational tests.

    Supports outbox methods as no-ops for PipelineRunner compatibility.
    """

    def __init__(self) -> None:
        self._events: dict[str, CanonicalEvent] = {}
        self._native_refs: dict[str, NativeMessageRef] = {}
        self._receipts: list[DeliveryReceipt] = []
        self._native_ref_index: dict[tuple[str, str, str], str] = {}

    async def append(self, event: CanonicalEvent) -> None:
        self._events[event.event_id] = event

    async def get(self, event_id: str) -> CanonicalEvent | None:
        return self._events.get(event_id)

    async def store_native_ref(self, ref: NativeMessageRef) -> None:
        self._native_refs[ref.id] = ref
        if ref.native_message_id:
            key = (ref.adapter, ref.native_channel_id or "", ref.native_message_id)
            self._native_ref_index[key] = ref.event_id

    async def resolve_native_ref(
        self, adapter: str, native_channel_id: str | None, native_message_id: str
    ) -> str | None:
        key = (adapter, native_channel_id or "", native_message_id)
        return self._native_ref_index.get(key)

    async def list_native_refs_for_event(self, event_id: str) -> list[NativeMessageRef]:
        return [r for r in self._native_refs.values() if r.event_id == event_id]

    async def append_receipt(self, receipt: DeliveryReceipt) -> None:
        self._receipts.append(receipt)

    async def list_receipts_for_event(self, event_id: str) -> list[DeliveryReceipt]:
        return [r for r in self._receipts if r.event_id == event_id]

    async def query_receipts(self, **kwargs: Any) -> list[DeliveryReceipt]:
        results = list(self._receipts)
        for k, v in kwargs.items():
            results = [r for r in results if getattr(r, k, None) == v]
        return results

    async def update_receipt_status(
        self, receipt_id: str, status: str, **kwargs: Any
    ) -> None:
        for r in self._receipts:
            if r.receipt_id == receipt_id:
                object.__setattr__(r, "status", status)
                for key, value in kwargs.items():
                    object.__setattr__(r, key, value)


def _make_ctx(
    adapter_id: str = "fake",
    logger: logging.Logger | None = None,
) -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=AsyncMock(),
        publish_inbound=AsyncMock(),
        logger=logger or logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        record_outbound_native_ref=AsyncMock(),
    )


def _make_matrix_config(
    adapter_id: str = "test_matrix",
    room_id: str = "!test:example.com",
) -> MatrixConfig:
    return MatrixConfig(
        adapter_id=adapter_id,
        homeserver="https://example.com",
        user_id=f"@bot:{adapter_id}",
        access_token="tok",
        room_allowlist=(room_id,),
    )


def _make_meshtastic_config(
    adapter_id: str = "test_mesh",
    max_text_bytes: int = 227,
) -> MeshtasticConfig:
    return MeshtasticConfig(
        adapter_id=adapter_id,
        connection_type="fake",
        max_text_bytes=max_text_bytes,
    )


def _matrix_inbound_event(
    body: str = "Hello from Matrix",
    event_id: str = "$mx001",
    sender: str = "@alice:example.com",
    room_id: str = "!test:example.com",
    msgtype: str = "m.text",
    relations: tuple[EventRelation, ...] = (),
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=str(uuid.uuid4()),
        event_kind=EventKind.MESSAGE_CREATED,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="test_matrix",
        source_transport_id=sender,
        source_channel_id=room_id,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"body": body, "msgtype": msgtype},
        metadata=EventMetadata(
            native=NativeMetadata(
                data={
                    "room_id": room_id,
                    "event_id": event_id,
                    "sender": sender,
                    "longname": sender,
                    "shortname": sender[:5],
                }
            )
        ),
        source_native_ref=NativeRef(
            adapter="test_matrix",
            native_channel_id=room_id,
            native_message_id=event_id,
        ),
    )


def _meshtastic_inbound_event(
    body: str = "Hello from mesh",
    packet_id: int = 12345,
    sender: str = "!abc123",
    channel: int = 0,
    relations: tuple[EventRelation, ...] = (),
    reply_id: int | None = None,
) -> CanonicalEvent:
    native_data: dict[str, Any] = {
        "packet_id": packet_id,
        "from_id": sender,
        "channel": channel,
        "portnum": "text_message",
        "longname": "TestNode",
        "shortname": "Test",
        "reply_id": reply_id,
    }
    return CanonicalEvent(
        event_id=str(uuid.uuid4()),
        event_kind=EventKind.MESSAGE_CREATED,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="test_mesh",
        source_transport_id=sender,
        source_channel_id=str(channel),
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"body": body},
        metadata=EventMetadata(native=NativeMetadata(data=native_data)),
        source_native_ref=NativeRef(
            adapter="test_mesh",
            native_channel_id=str(channel),
            native_message_id=str(packet_id),
        ),
    )


def _mesh_rendering_context(
    target_adapter: str = "test_mesh",
    delivery_strategy: str = "direct",
    max_text_bytes: int | None = 227,
    target_channel: str | None = "0",
) -> RenderingContext:
    return RenderingContext(
        delivery_strategy=delivery_strategy,  # type: ignore[arg-type]
        target_adapter=target_adapter,
        target_channel=target_channel,
        target_platform="meshtastic",
        max_text_bytes=max_text_bytes,
    )


def _matrix_rendering_context(
    target_adapter: str = "test_matrix",
    delivery_strategy: str = "direct",
    target_channel: str = "!test:example.com",
) -> RenderingContext:
    return RenderingContext(
        delivery_strategy=delivery_strategy,  # type: ignore[arg-type]
        target_adapter=target_adapter,
        target_channel=target_channel,
        target_platform="matrix",
    )


def _build_pipeline_runner(
    source_adapter: str,
    target_adapter_id: str,
    target_channel: str | None = None,
    route_id: str = "route-1",
    storage: _FakeStorage | None = None,
) -> tuple[PipelineRunner, _FakeStorage, Router]:
    """Build a PipelineRunner wired with fake adapters and a single route.

    Returns (runner, storage, router) for test inspection.
    """
    store = storage or _FakeStorage()
    event_bus = EventBus()

    # Create fake adapters.
    matrix_adapter = FakeMatrixAdapter("test_matrix")
    mesh_config = _make_meshtastic_config()
    mesh_adapter = FakeMeshtasticAdapter(mesh_config)

    adapters: dict[str, Any] = {
        "test_matrix": matrix_adapter,
        "test_mesh": mesh_adapter,
    }

    # Build rendering pipeline with both renderers.
    rendering_pipeline = RenderingPipeline()
    rendering_pipeline.register(MatrixRenderer(), priority=10)
    rendering_pipeline.register(
        MeshtasticRenderer(configs={"test_mesh": mesh_config}), priority=10
    )

    # Router with a single route from source to target.
    router = Router()
    route = Route(
        id=route_id,
        source=RouteSource(adapter=source_adapter, event_kinds=(), channel=None),
        targets=[RouteTarget(adapter=target_adapter_id, channel=target_channel)],
    )
    router.add_route(route)

    config = PipelineConfig(
        storage=store,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=store),
        adapters=adapters,
        event_bus=event_bus,
        rendering_pipeline=rendering_pipeline,
        route_stats=RouteStats(),
    )
    runner = PipelineRunner(config)
    return runner, store, router


# ===========================================================================
# A. Matrix -> Meshtastic runtime-path test
# ===========================================================================


class TestMatrixToMeshtasticRuntimePath:
    """Runtime-path: Matrix-origin event delivered to Meshtastic via
    PipelineRunner handle_ingress, exercising routing, rendering,
    adapter delivery, receipt creation, and evidence attachment."""

    @pytest.mark.asyncio
    async def test_matrix_to_mesh_pipeline_delivers_and_creates_receipt(
        self,
    ) -> None:
        runner, storage, _router = _build_pipeline_runner(
            source_adapter="test_matrix",
            target_adapter_id="test_mesh",
            target_channel="0",
        )
        await runner.start()
        try:
            event = _matrix_inbound_event(body="Bridge test message")

            outcomes = await runner.handle_ingress(event)

            # One outcome for one target.
            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.target_adapter == "test_mesh"
            assert outcome.target_channel == "0"
            assert outcome.failure_kind is None
            assert outcome.error is None

            # Receipt was persisted.
            assert outcome.receipt is not None
            receipt = outcome.receipt
            assert receipt.status == "sent"
            assert receipt.target_adapter == "test_mesh"
            assert receipt.event_id == event.event_id
            assert receipt.delivery_plan_id != ""

            # Rendering evidence attached to receipt.
            assert receipt.rendering_evidence is not None
            assert "meshtastic" in receipt.rendering_evidence

            # Fake adapter received the rendered payload.
            mesh_adapter = runner._config.adapters["test_mesh"]
            assert len(mesh_adapter.delivered_payloads) == 1
            delivered = mesh_adapter.delivered_payloads[0]
            assert "Bridge test message" in delivered.payload.get("text", "")

            # Channel index preserved.
            assert delivered.payload.get("channel_index") is not None

            # Native message ref persisted for outbound.
            outbound_refs = [
                r
                for r in storage._native_refs.values()
                if r.event_id == event.event_id and r.direction == "outbound"
            ]
            assert len(outbound_refs) == 1
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_matrix_to_mesh_receipt_stored_in_storage(self) -> None:
        runner, storage, _router = _build_pipeline_runner(
            source_adapter="test_matrix",
            target_adapter_id="test_mesh",
            target_channel="0",
        )
        await runner.start()
        try:
            event = _matrix_inbound_event(body="Storage check")

            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1

            # Verify receipt is queryable from storage.
            receipts = await storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1
            sent_receipts = [r for r in receipts if r.status == "sent"]
            assert len(sent_receipts) == 1
            assert sent_receipts[0].rendering_evidence is not None
            assert sent_receipts[0].delivery_plan_id != ""
        finally:
            await runner.stop()


# ===========================================================================
# B. Meshtastic -> Matrix runtime-path test
# ===========================================================================


class TestMeshtasticToMatrixRuntimePath:
    """Runtime-path: Meshtastic-origin event delivered to Matrix via
    PipelineRunner handle_ingress."""

    @pytest.mark.asyncio
    async def test_mesh_to_matrix_pipeline_delivers_and_creates_receipt(
        self,
    ) -> None:
        runner, storage, _router = _build_pipeline_runner(
            source_adapter="test_mesh",
            target_adapter_id="test_matrix",
            target_channel="!test:example.com",
        )
        await runner.start()
        try:
            event = _meshtastic_inbound_event(body="Radio check")

            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.target_adapter == "test_matrix"
            assert outcome.target_channel == "!test:example.com"
            assert outcome.failure_kind is None

            # Receipt with evidence.
            assert outcome.receipt is not None
            receipt = outcome.receipt
            assert receipt.status == "sent"
            assert receipt.rendering_evidence is not None
            assert "matrix" in receipt.rendering_evidence

            # Fake Matrix adapter received the payload.
            matrix_adapter = runner._config.adapters["test_matrix"]
            assert len(matrix_adapter.delivered_payloads) == 1
            delivered = matrix_adapter.delivered_payloads[0]
            assert delivered.payload.get("msgtype") == "m.text"
            assert delivered.payload.get("body") == "Radio check"

            # Native message ref persisted for outbound.
            outbound_refs = [
                r
                for r in storage._native_refs.values()
                if r.event_id == event.event_id and r.direction == "outbound"
            ]
            assert len(outbound_refs) == 1
            assert outbound_refs[0].adapter == "test_matrix"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_mesh_to_matrix_reply_m_relates_to(self) -> None:
        """Meshtastic reply carries m.relates_to through the pipeline."""
        runner, storage, _router = _build_pipeline_runner(
            source_adapter="test_mesh",
            target_adapter_id="test_matrix",
            target_channel="!test:example.com",
        )
        await runner.start()
        try:
            # Pre-store the original Matrix event and its native ref so
            # relation enrichment can resolve the target.
            original_mx_event = _matrix_inbound_event(
                body="Original",
                event_id="$orig001",
            )
            await storage.append(original_mx_event)
            await storage.store_native_ref(
                NativeMessageRef(
                    id="nref-orig",
                    event_id=original_mx_event.event_id,
                    adapter="test_matrix",
                    native_channel_id="!test:example.com",
                    native_message_id="$orig001",
                    native_thread_id=None,
                    native_relation_id=None,
                    direction="inbound",
                    created_at=datetime.now(timezone.utc),
                )
            )

            # Build a reply relation targeting the Matrix native ref.
            target_ref = NativeRef(
                adapter="test_matrix",
                native_channel_id="!test:example.com",
                native_message_id="$orig001",
            )
            reply_rel = EventRelation(
                relation_type="reply",
                target_event_id=None,
                target_native_ref=target_ref,
                key=None,
                fallback_text=None,
            )
            event = _meshtastic_inbound_event(
                body="Mesh reply",
                relations=(reply_rel,),
            )

            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Matrix adapter received payload with m.relates_to.
            matrix_adapter = runner._config.adapters["test_matrix"]
            assert len(matrix_adapter.delivered_payloads) == 1
            delivered = matrix_adapter.delivered_payloads[0]
            relates = delivered.payload.get("m.relates_to")
            assert relates is not None
            assert relates["m.in_reply_to"]["event_id"] == "$orig001"
        finally:
            await runner.stop()


# ===========================================================================
# Matrix -> Meshtastic renderer-level characterization
# ===========================================================================


class TestMatrixToMeshtasticTextRender:
    """Renderer-level: Matrix -> Meshtastic text preserves content."""

    @pytest.mark.asyncio
    async def test_text_renders_with_channel_index_and_meshnet(self) -> None:
        config = _make_meshtastic_config(adapter_id="test_mesh")
        renderer = MeshtasticRenderer(configs={"test_mesh": config})
        event = _matrix_inbound_event(body="Ping mesh")
        ctx = _mesh_rendering_context()

        result = await renderer.render(event, ctx)

        assert "Ping mesh" in result.payload["text"]
        assert result.payload["channel_index"] == config.default_channel
        assert result.payload["meshnet_name"] == config.meshnet_name
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_text_roundtrip_through_render_and_fake_deliver(self) -> None:
        config = _make_meshtastic_config()
        renderer = MeshtasticRenderer(configs={"test_mesh": config})
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_ctx("test_mesh")
        try:
            await adapter.start(ctx)
            event = _matrix_inbound_event(body="Bridge msg")
            rctx = _mesh_rendering_context()
            result = await renderer.render(event, rctx)

            delivery = await adapter.deliver(result)
            assert delivery is not None
            assert delivery.native_message_id is not None
            assert adapter.fake_client.sent_count == 1
            sent = adapter.fake_client.sent_packets[0]
            assert "Bridge msg" in sent["text"]
        finally:
            await adapter.stop()


# ===========================================================================
# Meshtastic -> Matrix renderer-level characterization
# ===========================================================================


class TestMeshtasticToMatrixTextRender:
    """Renderer-level: Meshtastic -> Matrix text flow preserves body/evidence."""

    @pytest.mark.asyncio
    async def test_text_renders_matrix_content(self) -> None:
        renderer = MatrixRenderer()
        event = _meshtastic_inbound_event(body="Radio check")
        ctx = _matrix_rendering_context()

        result = await renderer.render(event, ctx)

        assert result.payload["msgtype"] == "m.text"
        assert result.payload["body"] == "Radio check"
        assert "medre" in result.payload

    @pytest.mark.asyncio
    async def test_evidence_snapshot_attached(self) -> None:
        pipeline = RenderingPipeline()
        pipeline.register(MatrixRenderer(), priority=10)
        pipeline.register_platforms_from({"test_matrix": "matrix"})

        event = _meshtastic_inbound_event()
        result = await pipeline.render(
            event,
            "test_matrix",
            target_channel="!test:example.com",
            delivery_strategy="direct",
        )
        assert result.rendering_evidence is not None
        ev = result.rendering_evidence
        assert ev.renderer == "matrix"
        assert ev.target_platform == "matrix"
        assert ev.delivery_strategy == "direct"


# ===========================================================================
# Codec decode characterization
# ===========================================================================


class TestCodecDecode:
    """Characterization tests for codec decode paths."""

    def test_matrix_codec_text_message(self) -> None:
        config = _make_matrix_config()
        codec = MatrixCodec("test_matrix", config)
        event_dict = {
            "room_id": "!test:example.com",
            "sender": "@alice:example.com",
            "body": "Hello Matrix",
            "event_id": "$mx001",
            "msgtype": "m.text",
            "server_timestamp": 1700000000000,
            "source": {
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "Hello Matrix"},
                "event_id": "$mx001",
                "sender": "@alice:example.com",
            },
        }

        canonical = codec.decode(event_dict, room_id="!test:example.com")
        assert canonical.event_kind == EventKind.MESSAGE_CREATED
        assert canonical.payload["body"] == "Hello Matrix"
        assert canonical.source_native_ref is not None
        assert canonical.source_native_ref.native_message_id == "$mx001"

    def test_matrix_codec_reply_message(self) -> None:
        config = _make_matrix_config()
        codec = MatrixCodec("test_matrix", config)
        event_dict = {
            "room_id": "!test:example.com",
            "sender": "@alice:example.com",
            "body": "> <@bob:example.com> Original\n\nReply",
            "event_id": "$mx002",
            "msgtype": "m.text",
            "server_timestamp": 1700000000000,
            "source": {
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "> <@bob:example.com> Original\n\nReply",
                    "m.relates_to": {
                        "m.in_reply_to": {"event_id": "$mx001"},
                    },
                },
                "event_id": "$mx002",
                "sender": "@alice:example.com",
            },
        }

        canonical = codec.decode(event_dict, room_id="!test:example.com")
        assert len(canonical.relations) == 1
        assert canonical.relations[0].relation_type == "reply"
        assert canonical.relations[0].target_native_ref is not None
        assert canonical.relations[0].target_native_ref.native_message_id == "$mx001"

    def test_meshtastic_codec_text_packet(self) -> None:
        config = _make_meshtastic_config()
        codec = MeshtasticCodec("test_mesh", config)
        packet = {
            "fromId": "!abc123",
            "toId": "",
            "channel": 0,
            "id": 42,
            "decoded": {
                "portnum": "text_message",
                "text": "Hello mesh",
            },
        }

        canonical = codec.decode(packet)
        assert canonical.event_kind == EventKind.MESSAGE_CREATED
        assert canonical.payload["body"] == "Hello mesh"
        assert canonical.source_native_ref is not None
        assert canonical.source_native_ref.native_message_id == "42"

    def test_meshtastic_codec_reply_packet(self) -> None:
        config = _make_meshtastic_config()
        codec = MeshtasticCodec("test_mesh", config)
        packet = {
            "fromId": "!abc123",
            "toId": "",
            "channel": 0,
            "id": 43,
            "decoded": {
                "portnum": "text_message",
                "text": "Reply msg",
                "replyId": 42,
            },
        }

        canonical = codec.decode(packet)
        assert len(canonical.relations) == 1
        assert canonical.relations[0].relation_type == "reply"
        assert canonical.relations[0].target_native_ref is not None

    def test_meshtastic_codec_reaction_packet(self) -> None:
        config = _make_meshtastic_config()
        codec = MeshtasticCodec("test_mesh", config)
        packet = {
            "fromId": "!abc123",
            "toId": "",
            "channel": 0,
            "id": 44,
            "decoded": {
                "portnum": "text_message",
                "text": "\U0001f44d",
                "replyId": 42,
                "emoji": 1,
            },
        }

        canonical = codec.decode(packet)
        assert canonical.event_kind == EventKind.MESSAGE_REACTED
        assert len(canonical.relations) == 1
        assert canonical.relations[0].relation_type == "reaction"
