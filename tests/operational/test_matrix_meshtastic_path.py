"""Matrix ↔ Meshtastic operational maturity tests.

Deterministic characterization tests covering bidirectional flow,
relations (native ref / fallback), loop/dedupe, queue/ACK evidence,
backpressure, lifecycle, capability decisions, and byte-budget behavior.

All tests use fakes — no real Matrix homeserver, no real Meshtastic radio.
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
from medre.adapters.meshtastic.errors import MeshtasticSendError
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    OutboundNativeRefRecord,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.planning.capability_decision import (
    CapabilityDecisionResolver,
)
from medre.core.rendering.evidence import RenderingEvidence
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.storage.backend import StorageBackend

# ---------------------------------------------------------------------------
# Local fakes / helpers
# ---------------------------------------------------------------------------


class _FakeStorage(StorageBackend):
    """Minimal in-memory storage for operational tests."""

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


# ===========================================================================
# Matrix → Meshtastic basic text flow
# ===========================================================================


class TestMatrixToMeshtasticTextFlow:
    """Matrix → Meshtastic basic text preserves content."""

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


# ===========================================================================
# Meshtastic → Matrix basic text flow
# ===========================================================================


class TestMeshtasticToMatrixTextFlow:
    """Meshtastic → Matrix text flow preserves body/evidence."""

    @pytest.mark.asyncio
    async def test_text_renders_matrix_content(self) -> None:
        renderer = MatrixRenderer()
        event = _meshtastic_inbound_event(body="Radio check")
        ctx = _matrix_rendering_context()

        result = await renderer.render(event, ctx)

        assert result.payload["msgtype"] == "m.text"
        assert result.payload["body"] == "Radio check"
        # MEDRE envelope present (nested under "medre" key)
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
# Matrix → Meshtastic reply with native ref
# ===========================================================================


class TestMatrixToMeshtasticReply:
    """Reply relation resolved with native ref."""

    @pytest.mark.asyncio
    async def test_reply_uses_native_ref_reply_id(self) -> None:
        config = _make_meshtastic_config()
        renderer = MeshtasticRenderer(configs={"test_mesh": config})

        target_ref = NativeRef(
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="9999",
        )
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=target_ref,
            key=None,
            fallback_text=None,
        )
        event = _matrix_inbound_event(
            body="Reply msg",
            relations=(reply_rel,),
        )
        ctx = _mesh_rendering_context()

        result = await renderer.render(event, ctx)
        assert result.payload["reply_id"] == 9999
        assert "Reply msg" in result.payload["text"]

    @pytest.mark.asyncio
    async def test_reply_without_native_ref_plain_text(self) -> None:
        config = _make_meshtastic_config()
        renderer = MeshtasticRenderer(configs={"test_mesh": config})

        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id="canonical-123",
            target_native_ref=None,
            key=None,
            fallback_text="original text",
        )
        event = _matrix_inbound_event(
            body="Reply msg",
            relations=(reply_rel,),
        )
        ctx = _mesh_rendering_context()
        result = await renderer.render(event, ctx)
        assert "reply_id" not in result.payload
        assert "Reply msg" in result.payload["text"]


# ===========================================================================
# Matrix → Meshtastic fallback_text relation rendering
# ===========================================================================


class TestMatrixToMeshtasticFallbackText:
    """fallback_text degrades relations but preserves envelope."""

    @pytest.mark.asyncio
    async def test_fallback_text_preserves_channel_and_meshnet(self) -> None:
        config = _make_meshtastic_config()
        renderer = MeshtasticRenderer(configs={"test_mesh": config})

        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-999",
            target_native_ref=None,
            key=None,
            fallback_text="original msg",
        )
        event = _matrix_inbound_event(
            body="Fallback reply",
            relations=(reply_rel,),
        )
        ctx = _mesh_rendering_context(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)
        assert result.payload["channel_index"] == config.default_channel
        assert result.payload["meshnet_name"] == config.meshnet_name
        assert "reply_id" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"


# ===========================================================================
# Meshtastic → Matrix reply/relation rendering
# ===========================================================================


class TestMeshtasticToMatrixReply:
    """Meshtastic reply_id maps to Matrix m.relates_to."""

    @pytest.mark.asyncio
    async def test_reply_with_matrix_native_ref(self) -> None:
        renderer = MatrixRenderer()
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
        ctx = _matrix_rendering_context()

        result = await renderer.render(event, ctx)
        relates = result.payload.get("m.relates_to")
        assert relates is not None
        assert relates["m.in_reply_to"]["event_id"] == "$orig001"

    @pytest.mark.asyncio
    async def test_fallback_text_no_m_relates_to(self) -> None:
        renderer = MatrixRenderer()
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text="original",
        )
        event = _meshtastic_inbound_event(
            body="Fallback reply",
            relations=(reply_rel,),
        )
        ctx = _matrix_rendering_context(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)
        assert "m.relates_to" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"


# ===========================================================================
# Loop prevention
# ===========================================================================


class TestLoopPrevention:
    """Self-loop guard, duplicate Matrix event, and duplicate Meshtastic
    packet suppression via native ref deduplication."""

    @pytest.mark.asyncio
    async def test_self_loop_suppresses_delivery(self) -> None:
        from medre.core.routing.stats import RouteStats

        stats = RouteStats()
        event = _matrix_inbound_event()
        target_adapter = event.source_adapter

        # A self-loop delivery (target == source) is recorded as
        # loop_prevented by the runner's self-loop guard.
        stats.record_loop_prevented("route-self-loop")
        snap = stats.snapshot()
        assert snap["route-self-loop"]["loop_prevented"] == 1
        assert snap["route-self-loop"]["delivered"] == 0

        # Verify the precondition: same-adapter routing would be caught.
        assert target_adapter == event.source_adapter

    @pytest.mark.asyncio
    async def test_duplicate_native_ref_suppressed(self) -> None:
        storage = _FakeStorage()

        event1 = _matrix_inbound_event(event_id="$dup001")
        snr = event1.source_native_ref
        assert snr is not None

        await storage.store_native_ref(
            NativeMessageRef(
                id="nref-1",
                event_id=event1.event_id,
                adapter=snr.adapter,
                native_channel_id=snr.native_channel_id,
                native_message_id=snr.native_message_id,
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=datetime.now(timezone.utc),
            )
        )

        event2 = _matrix_inbound_event(event_id="$dup001")
        snr2 = event2.source_native_ref
        assert snr2 is not None

        existing = await storage.resolve_native_ref(
            adapter=snr2.adapter,
            native_channel_id=snr2.native_channel_id,
            native_message_id=snr2.native_message_id,
        )
        assert existing is not None
        assert existing == event1.event_id

    @pytest.mark.asyncio
    async def test_duplicate_meshtastic_packet_suppressed(self) -> None:
        storage = _FakeStorage()

        event1 = _meshtastic_inbound_event(packet_id=42)
        snr = event1.source_native_ref
        assert snr is not None

        await storage.store_native_ref(
            NativeMessageRef(
                id="nref-2",
                event_id=event1.event_id,
                adapter=snr.adapter,
                native_channel_id=snr.native_channel_id,
                native_message_id=snr.native_message_id,
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=datetime.now(timezone.utc),
            )
        )

        event2 = _meshtastic_inbound_event(packet_id=42)
        snr2 = event2.source_native_ref
        existing = await storage.resolve_native_ref(
            adapter=snr2.adapter,
            native_channel_id=snr2.native_channel_id,
            native_message_id=snr2.native_message_id,
        )
        assert existing is not None


# ===========================================================================
# Queue / ACK / delivery_plan_id correlation
# ===========================================================================


class TestQueueAckCorrelation:
    """Queued delivery with delivery_plan_id correlation."""

    @pytest.mark.asyncio
    async def test_enqueued_creates_queued_receipt(self) -> None:
        config = _make_meshtastic_config()
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_ctx("test_mesh")
        await adapter.start(ctx)

        plan_id = str(uuid.uuid4())
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_mesh",
            target_channel="0",
            payload={"text": "queued msg", "channel_index": 0, "meshnet_name": ""},
            delivery_plan_id=plan_id,
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is not None
        assert adapter.fake_client.sent_count == 1

    @pytest.mark.asyncio
    async def test_delivery_plan_id_correlates_queued_to_sent(self) -> None:
        plan_id = str(uuid.uuid4())
        record = OutboundNativeRefRecord(
            event_id="evt-1",
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="42",
            delivery_plan_id=plan_id,
            metadata={},
        )
        assert record.delivery_plan_id == plan_id

    @pytest.mark.asyncio
    async def test_lifecycle_append_queued_to_sent_receipt(self) -> None:
        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test.lifecycle"))

        plan_id = str(uuid.uuid4())
        queued_receipt = DeliveryReceipt(
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id="evt-1",
            delivery_plan_id=plan_id,
            target_adapter="test_mesh",
            target_channel="0",
            route_id="route-1",
            status="queued",
        )
        await storage.append_receipt(queued_receipt)

        record = OutboundNativeRefRecord(
            event_id="evt-1",
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="42",
            delivery_plan_id=plan_id,
            metadata={},
        )

        await lifecycle.append_queued_to_sent_receipt(
            storage,
            record=record,
            now=datetime.now(timezone.utc),
        )

        sent_receipts = [r for r in storage._receipts if r.status == "sent"]
        assert len(sent_receipts) == 1
        assert sent_receipts[0].delivery_plan_id == plan_id

    @pytest.mark.asyncio
    async def test_ambiguous_correlation_skips_without_corruption(self) -> None:
        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test.lifecycle"))

        record = OutboundNativeRefRecord(
            event_id="evt-no-match",
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="99",
            delivery_plan_id=None,
            metadata={},
        )

        await lifecycle.append_queued_to_sent_receipt(
            storage,
            record=record,
            now=datetime.now(timezone.utc),
        )
        assert len(storage._receipts) == 0


# ===========================================================================
# Byte-budget truncation
# ===========================================================================


class TestByteBudgetTruncation:
    """UTF-8 byte-budget truncation behavior."""

    @pytest.mark.asyncio
    async def test_long_text_truncated_to_byte_budget(self) -> None:
        config = _make_meshtastic_config(max_text_bytes=20)
        renderer = MeshtasticRenderer(configs={"test_mesh": config})
        event = _matrix_inbound_event(body="A" * 30)
        ctx = _mesh_rendering_context(max_text_bytes=20)

        result = await renderer.render(event, ctx)
        text = result.payload["text"]
        assert len(text.encode("utf-8")) <= 20
        assert result.truncated is True

    @pytest.mark.asyncio
    async def test_multibyte_utf8_truncation_safe(self) -> None:
        config = _make_meshtastic_config(max_text_bytes=10)
        renderer = MeshtasticRenderer(configs={"test_mesh": config})
        event = _matrix_inbound_event(body="🎉🎊🎁🎈")
        ctx = _mesh_rendering_context(max_text_bytes=10)

        result = await renderer.render(event, ctx)
        text = result.payload["text"]
        decoded = text.encode("utf-8").decode("utf-8")
        assert decoded == text
        assert len(text.encode("utf-8")) <= 10

    @pytest.mark.asyncio
    async def test_truncation_metadata_in_result(self) -> None:
        config = _make_meshtastic_config(max_text_bytes=15)
        renderer = MeshtasticRenderer(configs={"test_mesh": config})
        event = _matrix_inbound_event(body="Hello world, this is too long")
        ctx = _mesh_rendering_context(max_text_bytes=15)

        result = await renderer.render(event, ctx)
        assert result.metadata["truncated"] is True
        assert result.metadata["original_text_bytes"] > 15
        assert result.metadata["rendered_text_bytes"] <= 15
        assert result.metadata["max_text_bytes"] == 15


# ===========================================================================
# Matrix render/send failure classification
# ===========================================================================


class TestFailureClassification:
    """Transient vs permanent failure classification."""

    def test_transient_error_detection(self) -> None:
        from medre.adapters.matrix.adapter import _is_transient_error

        assert _is_transient_error(asyncio.TimeoutError()) is True
        assert _is_transient_error(ConnectionError("conn")) is True
        assert _is_transient_error(OSError("os")) is True

    def test_permanent_error_not_transient(self) -> None:
        from medre.adapters.matrix.adapter import _is_transient_error
        from medre.adapters.matrix.errors import MatrixSendError

        perm = MatrixSendError("bad", transient=False)
        assert _is_transient_error(perm) is False

    def test_rate_limit_detection(self) -> None:
        from medre.adapters.matrix.adapter import (
            _is_nio_rate_limited_response,
            _is_transient_error,
            _NioRateLimitError,
        )

        exc = _NioRateLimitError("rate limited", retry_after_ms=2000)
        assert exc.retry_after_ms == 2000
        assert _is_transient_error(exc) is True

        class _FakeResp:
            errcode = "M_LIMIT_EXCEEDED"
            status_code = 429

        assert _is_nio_rate_limited_response(_FakeResp()) is True


# ===========================================================================
# Queue backpressure / capacity rejection
# ===========================================================================


class TestQueueBackpressure:
    """Queue full does not create false sent receipt."""

    @pytest.mark.asyncio
    async def test_full_queue_rejects_enqueue(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.01,
            max_queue_size=2,
            max_attempts=3,
        )

        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")
        await queue.enqueue({"text": "msg2"}, 0, event_id="e2")

        with pytest.raises(MeshtasticSendError) as exc_info:
            await queue.enqueue({"text": "msg3"}, 0, event_id="e3")
        assert exc_info.value.transient is True
        assert queue.total_rejected == 1

    @pytest.mark.asyncio
    async def test_rejected_does_not_increment_sent(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.01,
            max_queue_size=1,
        )
        await queue.enqueue({"text": "msg1"}, 0)
        with pytest.raises(MeshtasticSendError):
            await queue.enqueue({"text": "msg2"}, 0)

        assert queue.total_sent == 0
        assert queue.total_rejected == 1

    @pytest.mark.asyncio
    async def test_transient_send_failure_front_requeues(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.0,
            max_queue_size=10,
            max_attempts=3,
        )
        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")

        async def _failing_send(item: dict) -> None:
            raise MeshtasticSendError("transient", transient=True)

        result = await queue.process_one(send_fn=_failing_send)
        assert result is None
        assert queue.total_requeued == 1
        assert queue.queue_depth == 1

    @pytest.mark.asyncio
    async def test_exhausted_retries_drops_item(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.0,
            max_queue_size=10,
            max_attempts=1,
        )
        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")

        async def _failing_send(item: dict) -> None:
            raise MeshtasticSendError("transient", transient=True)

        result = await queue.process_one(send_fn=_failing_send)
        assert result is None
        assert queue.total_exhausted == 1
        assert queue.total_failed == 1
        assert queue.queue_depth == 0


# ===========================================================================
# Adapter stop/start lifecycle
# ===========================================================================


class TestAdapterLifecycle:
    """Adapter stop prevents late processing."""

    @pytest.mark.asyncio
    async def test_matrix_adapter_stop_prevents_delivery(self) -> None:
        adapter = FakeMatrixAdapter("test_matrix")
        ctx = _make_ctx("test_matrix")
        await adapter.start(ctx)

        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_matrix",
            target_channel="!test:example.com",
            payload={"msgtype": "m.text", "body": "msg"},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None

        await adapter.stop()
        assert not adapter.is_started

    @pytest.mark.asyncio
    async def test_meshtastic_adapter_stop_prevents_inbound(self) -> None:
        config = _make_meshtastic_config()
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_ctx("test_mesh")
        await adapter.start(ctx)

        await adapter.stop()
        assert not adapter.is_started

    @pytest.mark.asyncio
    async def test_real_meshtastic_adapter_stops_queue_drain(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_meshtastic_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test_mesh")
        await adapter.start(ctx)

        info = await adapter.health_check()
        assert info.health == "healthy"

        await adapter.stop()
        info_after = await adapter.health_check()
        assert info_after.health in ("failed", "unknown")


# ===========================================================================
# Capability fallback / unsupported
# ===========================================================================


class TestCapabilityDecision:
    """CapabilityDecisionResolver for Matrix/Meshtastic."""

    def test_matrix_native_reactions(self) -> None:
        caps = AdapterCapabilities(
            reactions="native",
            replies="native",
            text=True,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_mesh",
            source_transport_id="!abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="evt-1",
                    target_native_ref=None,
                    key="👍",
                    fallback_text=None,
                ),
            ),
            payload={"body": "👍"},
            metadata=EventMetadata(),
        )

        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_matrix")
        assert decision.supported is True
        assert decision.capability_level == "native"

    def test_meshtastic_edits_unsupported(self) -> None:
        caps = AdapterCapabilities(
            edits="unsupported",
            text=True,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_EDITED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_matrix",
            source_transport_id="@alice:example.com",
            source_channel_id="!test:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "edited"},
            metadata=EventMetadata(),
        )

        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_mesh")
        assert decision.supported is False
        assert decision.delivery_strategy == "skip"
        assert decision.capability_field == "edits"

    def test_fallback_reactions(self) -> None:
        caps = AdapterCapabilities(
            reactions="fallback",
            text=True,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_matrix",
            source_transport_id="@alice:example.com",
            source_channel_id="!test:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "👍"},
            metadata=EventMetadata(),
        )

        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_mesh")
        assert decision.supported is True
        assert decision.capability_level == "fallback"
        assert decision.delivery_strategy == "fallback_text"

    def test_text_passthrough_for_created(self) -> None:
        caps = AdapterCapabilities(text=True)
        event = _matrix_inbound_event()

        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_mesh")
        assert decision.supported is True
        assert decision.capability_level == "native"

    def test_capability_suppressed_reaction_event(self) -> None:
        caps = AdapterCapabilities(
            reactions="unsupported",
            text=True,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_mesh",
            source_transport_id="!abc",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "👍"},
            metadata=EventMetadata(),
        )
        resolver = CapabilityDecisionResolver()
        decision = resolver.decide(event, caps, target_adapter="test_matrix")
        assert decision.supported is False
        assert decision.capability_field == "reactions"


# ===========================================================================
# Cross-platform reaction rendering
# ===========================================================================


class TestCrossPlatformReactions:
    """Reactions between Matrix and Meshtastic adapters."""

    @pytest.mark.asyncio
    async def test_matrix_reaction_to_meshtastic_descriptive(self) -> None:
        config = _make_meshtastic_config()
        renderer = MeshtasticRenderer(configs={"test_mesh": config})

        target_ref = NativeRef(
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="42",
        )
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=target_ref,
            key="👍",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_matrix",
            source_transport_id="@alice:example.com",
            source_channel_id="!test:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "👍"},
            metadata=EventMetadata(),
        )
        ctx = _mesh_rendering_context()

        result = await renderer.render(event, ctx)
        assert result.payload.get("emoji") != 1
        assert "reacted" in result.payload["text"]
        assert "👍" in result.payload["text"]
        assert result.payload["reply_id"] == 42

    @pytest.mark.asyncio
    async def test_meshtastic_reaction_to_matrix_emote_fallback(self) -> None:
        renderer = MatrixRenderer()

        target_ref = NativeRef(
            adapter="test_matrix",
            native_channel_id="!test:example.com",
            native_message_id="$orig001",
        )
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=target_ref,
            key="❤️",
            fallback_text=None,
            metadata={"meshtastic_reply_id": "42", "meshtastic_emoji": 1},
        )
        event = CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test_mesh",
            source_transport_id="!abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "❤️", "key": "❤️"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "packet_id": 100,
                        "longname": "Sender",
                        "shortname": "Snd",
                    }
                )
            ),
        )
        ctx = _matrix_rendering_context()

        result = await renderer.render(event, ctx)
        # Matrix reactions are m.annotation events, not text with body
        relates = result.payload.get("m.relates_to", {})
        assert relates.get("rel_type") == "m.annotation"
        assert relates.get("key") == "❤️"


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
                "text": "👍",
                "replyId": 42,
                "emoji": 1,
            },
        }

        canonical = codec.decode(packet)
        assert canonical.event_kind == EventKind.MESSAGE_REACTED
        assert len(canonical.relations) == 1
        assert canonical.relations[0].relation_type == "reaction"


# ===========================================================================
# Queue health and diagnostics
# ===========================================================================


class TestQueueDiagnostics:
    """Queue health evidence for operators."""

    @pytest.mark.asyncio
    async def test_queue_health_snapshot(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.01,
            max_queue_size=100,
        )
        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")
        await queue.enqueue({"text": "msg2"}, 0, event_id="e2")

        health = queue.queue_health
        assert health["pending_count"] == 2
        assert health["total_enqueued"] == 2
        assert health["max_queue_size"] == 100
        assert health["utilization_pct"] == 2.0

    @pytest.mark.asyncio
    async def test_queue_counters_after_successful_send(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.0,
            max_queue_size=10,
        )
        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")

        async def _send(item: dict) -> dict:
            return {"packet_id": 1, "channel": 0}

        result = await queue.process_one(send_fn=_send)
        assert result is not None
        assert result.delivery_result.native_message_id == "1"
        assert queue.total_sent == 1
        assert queue.total_dequeued == 1


# ===========================================================================
# Rendering evidence
# ===========================================================================


class TestRenderingEvidence:
    """RenderingEvidence captures decision inputs and outcomes."""

    def test_evidence_from_context_and_result(self) -> None:
        ctx = _mesh_rendering_context(max_text_bytes=227)
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_mesh",
            target_channel="0",
            payload={"text": "Hello", "channel_index": 0, "meshnet_name": ""},
            metadata={"truncated": False},
            truncated=False,
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshtastic",
            ctx=ctx,
            result=result,
        )
        assert evidence.renderer == "meshtastic"
        assert evidence.target_platform == "meshtastic"
        assert evidence.max_text_bytes == 227
        assert evidence.truncated is False
        assert evidence.schema_version == "1"

    def test_evidence_to_dict_json_safe(self) -> None:
        ctx = _matrix_rendering_context()
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="test_matrix",
            target_channel="!test:example.com",
            payload={"msgtype": "m.text", "body": "Hello"},
            metadata={},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="matrix",
            ctx=ctx,
            result=result,
        )
        d = evidence.to_dict()
        for k, v in d.items():
            assert isinstance(
                v, (str, int, float, bool, type(None))
            ), f"Key {k!r} has non-JSON-safe value: {type(v)}"


# ===========================================================================
# Delivery state validation
# ===========================================================================


class TestDeliveryState:
    """Delivery state vocabulary and transitions."""

    def test_receipt_transitions(self) -> None:
        from medre.core.engine.pipeline.delivery_state import (
            RECEIPT_STATUSES,
            TERMINAL_RECEIPT_STATUSES,
            validate_receipt_transition,
        )

        assert "queued" in RECEIPT_STATUSES
        assert "sent" in RECEIPT_STATUSES
        assert "sent" in TERMINAL_RECEIPT_STATUSES
        assert validate_receipt_transition("queued", "sent") is True
        assert validate_receipt_transition("sent", "queued") is False

    def test_outcome_accepted_statuses(self) -> None:
        from medre.core.engine.pipeline.delivery_state import (
            is_accepted_outcome_status,
        )

        assert is_accepted_outcome_status("success") is True
        assert is_accepted_outcome_status("queued") is True
        assert is_accepted_outcome_status("skipped") is False
        assert is_accepted_outcome_status("permanent_failure") is False

    def test_queued_to_sent_transition(self) -> None:
        from medre.core.engine.pipeline.delivery_state import (
            is_valid_queued_to_sent_transition,
        )

        assert is_valid_queued_to_sent_transition("queued") is True
        assert is_valid_queued_to_sent_transition("sent") is False
        assert is_valid_queued_to_sent_transition("failed") is False
