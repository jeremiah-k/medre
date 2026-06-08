"""Delivery lifecycle conformance tests.

Shape characterization tests (phase 2) assert delivery receipt field
contracts using manually-constructed receipts and DeliveryLifecycleService.

Service-path tests (phase 3) assert real delivery execution through
TargetDeliveryService with fake adapters, a real RenderingPipeline,
and real receipt persistence:

* Direct sent delivery via TargetDeliveryService produces a persisted
  receipt with status='sent', correct plan/route/adapter/channel,
  source='live', and canonical RenderingEvidence JSON.
* Queued delivery (adapter returns enqueued) produces a persisted
  receipt with status='queued', correct plan/adapter/channel, and
  canonical RenderingEvidence JSON.

No real network involved.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterDeliveryResult,
    OutboundNativeRefRecord,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.target_delivery import TargetDeliveryService
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    DeliveryStrategy,
)
from medre.core.rendering.renderer import RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend

# ---------------------------------------------------------------------------
# In-memory storage for receipt inspection
# ---------------------------------------------------------------------------


class _MemoryStorage(StorageBackend):
    """Minimal in-memory storage for delivery lifecycle conformance tests."""

    def __init__(self) -> None:
        self._receipts: list[DeliveryReceipt] = []
        self._native_refs: list[NativeMessageRef] = []
        self._outbox: dict[str, DeliveryOutboxItem] = {}

    async def append_receipt(self, receipt: DeliveryReceipt) -> None:
        self._receipts.append(receipt)

    async def list_receipts_for_event(self, event_id: str) -> list[DeliveryReceipt]:
        return [r for r in self._receipts if r.event_id == event_id]

    async def store_native_ref(self, ref: NativeMessageRef) -> None:
        self._native_refs.append(ref)

    async def count_native_refs(self) -> int:
        """Return the number of stored native refs."""
        return len(self._native_refs)

    # -- Outbox stubs for queued→sent correlation tests --

    async def create_outbox_item(self, item: DeliveryOutboxItem) -> DeliveryOutboxItem:
        self._outbox[item.outbox_id] = item
        return item

    async def get_outbox_item(self, outbox_id: str) -> DeliveryOutboxItem | None:
        return self._outbox.get(outbox_id)

    async def mark_outbox_queued(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        item = self._outbox.get(outbox_id)
        if item is not None:
            object.__setattr__(item, "status", "queued")

    async def mark_outbox_sent(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        item = self._outbox.get(outbox_id)
        if item is not None:
            object.__setattr__(item, "status", "sent")

    # -- Required by abstract protocol but unused in these tests --

    async def append(self, event) -> None:
        raise NotImplementedError

    async def get(self, event_id: str):
        raise NotImplementedError

    async def resolve_native_ref(self, adapter, native_channel_id, native_message_id):
        raise NotImplementedError

    async def list_native_refs_for_event(self, event_id: str):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Delivery lifecycle conformance
# ---------------------------------------------------------------------------


class TestDeliveryLifecycleConformance:
    """Assert delivery receipt contracts."""

    @pytest.fixture()
    def storage(self):
        return _MemoryStorage()

    @pytest.fixture()
    def lifecycle(self):
        return DeliveryLifecycleService()

    @pytest.mark.asyncio
    async def test_direct_sent_receipt(self, storage):
        """Direct sent delivery creates a receipt with status='sent'.

        Shape characterization: verifies receipt field contracts for a
        manually-constructed sent receipt.  The lifecycle service is not
        used for initial receipt creation (it creates supplemental and
        suppression receipts only).
        """
        event_id = str(uuid.uuid4())
        plan_id = f"plan-{uuid.uuid4()}"
        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id=plan_id,
            target_adapter="matrix_conf",
            target_channel="!room:example.com",
            route_id="route-1",
            status="sent",
            source="live",
        )
        await storage.append_receipt(receipt)

        receipts = await storage.list_receipts_for_event(event_id)
        assert len(receipts) == 1
        assert receipts[0].status == "sent"
        assert receipts[0].delivery_plan_id == plan_id
        assert receipts[0].source == "live"

    @pytest.mark.asyncio
    async def test_queued_receipt(self, storage):
        """Queued delivery creates a receipt with status='queued'.

        Shape characterization: verifies receipt field contracts for a
        manually-constructed queued receipt.  The lifecycle service is not
        used for initial receipt creation.
        """
        event_id = str(uuid.uuid4())
        plan_id = f"plan-{uuid.uuid4()}"
        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id=plan_id,
            target_adapter="mesh_conf",
            target_channel="0",
            route_id="route-1",
            status="queued",
            source="live",
            rendering_evidence='{"schema_version":"1","renderer":"meshtastic"}',
        )
        await storage.append_receipt(receipt)

        receipts = await storage.list_receipts_for_event(event_id)
        assert len(receipts) == 1
        assert receipts[0].status == "queued"
        assert receipts[0].delivery_plan_id == plan_id

    @pytest.mark.asyncio
    async def test_queued_to_sent_supplemental_receipt(self, storage, lifecycle):
        """Queued -> sent supplemental receipt correlates by delivery_plan_id."""
        event_id = str(uuid.uuid4())
        plan_id = f"plan-{uuid.uuid4()}"
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)

        outbox_id = f"obox-{uuid.uuid4()}"

        # First: queued receipt
        queued = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-queued-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id=plan_id,
            target_adapter="mesh_conf",
            target_channel="0",
            route_id="route-1",
            status="queued",
            source="live",
            attempt_number=1,
            rendering_evidence='{"schema_version":"1"}',
            outbox_id=outbox_id,
        )
        await storage.append_receipt(queued)

        # Create matching outbox item for exact correlation.
        outbox_item = DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event_id,
            route_id="route-1",
            delivery_plan_id=plan_id,
            target_adapter="mesh_conf",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await storage.create_outbox_item(outbox_item)
        await storage.mark_outbox_queued(outbox_id)

        # Simulate the queued->sent callback
        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh_conf",
            native_channel_id="0",
            native_message_id="pkt-999",
            delivery_plan_id=plan_id,
            outbox_id=outbox_id,
        )
        await lifecycle.append_queued_to_sent_receipt(storage, record, now)

        receipts = await storage.list_receipts_for_event(event_id)
        assert len(receipts) == 2

        sent = [r for r in receipts if r.status == "sent"][0]
        assert sent.delivery_plan_id == plan_id
        assert sent.target_adapter == "mesh_conf"
        assert sent.adapter_message_id == "pkt-999"
        assert sent.parent_receipt_id == queued.receipt_id
        assert sent.source == "live"

    @pytest.mark.asyncio
    async def test_supplemental_preserves_evidence(self, storage, lifecycle):
        """Supplemental sent receipt carries rendering_evidence from queued."""
        event_id = str(uuid.uuid4())
        plan_id = f"plan-{uuid.uuid4()}"
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        evidence_json = '{"schema_version":"1","renderer":"meshtastic"}'

        outbox_id = f"obox-{uuid.uuid4()}"

        queued = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-qed-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id=plan_id,
            target_adapter="mesh_conf",
            target_channel="0",
            route_id="route-1",
            status="queued",
            source="live",
            rendering_evidence=evidence_json,
            outbox_id=outbox_id,
        )
        await storage.append_receipt(queued)

        outbox_item = DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event_id,
            route_id="route-1",
            delivery_plan_id=plan_id,
            target_adapter="mesh_conf",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await storage.create_outbox_item(outbox_item)
        await storage.mark_outbox_queued(outbox_id)

        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh_conf",
            native_channel_id="0",
            native_message_id="pkt-888",
            delivery_plan_id=plan_id,
            outbox_id=outbox_id,
        )
        await lifecycle.append_queued_to_sent_receipt(storage, record, now)

        receipts = await storage.list_receipts_for_event(event_id)
        sent = [r for r in receipts if r.status == "sent"][0]
        assert sent.rendering_evidence == evidence_json

    @pytest.mark.asyncio
    async def test_suppressed_receipt_no_rendering_evidence(self, storage, lifecycle):
        """Suppressed receipt via lifecycle service: no rendering_evidence."""
        event_id = str(uuid.uuid4())
        plan_id = f"plan-{uuid.uuid4()}"

        receipt = await lifecycle.build_and_persist_suppression_receipt(
            storage,
            event_id=event_id,
            delivery_plan_id=plan_id,
            target_adapter="matrix_conf",
            target_channel="!room:example.com",
            route_id="route-1",
            failure_kind=DeliveryFailureKind.POLICY_SUPPRESSED,
            error="capability_suppressed",
        )

        assert receipt.rendering_evidence is None
        assert receipt.status == "suppressed"
        assert receipt.failure_kind == DeliveryFailureKind.POLICY_SUPPRESSED.value

        # Verify persisted
        receipts = await storage.list_receipts_for_event(event_id)
        assert len(receipts) == 1
        assert receipts[0].status == "suppressed"


# ---------------------------------------------------------------------------
# Service-path delivery conformance (phase 3)
# ---------------------------------------------------------------------------


class _FakeQueuedAdapter:
    """Minimal adapter that returns delivery_status='enqueued' for queued
    delivery conformance tests.

    Informal duck-type contract (mirrors TargetDeliveryService expectations):
      - ``adapter_id``: str identifier used for adapter lookup.
      - ``platform``: str platform name.
      - ``_capabilities``: AdapterCapabilities instance.
      - ``deliver(result) -> AdapterDeliveryResult``: async, accepts a
        RenderingResult and returns an AdapterDeliveryResult with
        delivery_status='enqueued'.
    """

    adapter_id: str = "queued_adapter"
    platform: str = "fake_queued"
    _capabilities: AdapterCapabilities = AdapterCapabilities(
        text=True,
        reactions="native",
        replies="native",
    )

    def __init__(self, adapter_id: str = "queued_adapter") -> None:
        self.adapter_id = adapter_id
        self.delivered_payloads: list[RenderingResult] = []

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult:
        self.delivered_payloads.append(result)
        return AdapterDeliveryResult(
            native_message_id=None,
            native_channel_id=result.target_channel,
            delivery_status="enqueued",
            delivery_note="queued for async delivery",
        )


def _make_event(
    event_id: str = "svc-evt-001",
    event_kind: str = "message.created",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        source_adapter="src_adapter",
        source_transport_id="node-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "service-path conformance test"},
        metadata=EventMetadata(),
    )


def _make_route_and_plan(
    adapter_id: str = "dest",
    plan_id: str = "plan-svc-001",
    channel: str | None = None,
    method: str = "direct",
) -> tuple[Route, DeliveryPlan]:
    target = RouteTarget(adapter=adapter_id, channel=channel)
    route = Route(
        id="route-svc-001",
        source=RouteSource(
            adapter="src_adapter",
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id=plan_id,
        event_id="svc-evt-001",
        target=target,
        primary_strategy=DeliveryStrategy(method=method),
    )
    return route, plan


def _build_service(
    adapters: dict[str, Any],
    storage: _MemoryStorage,
) -> TargetDeliveryService:
    from medre.core.rendering.renderer import RenderingPipeline

    pipeline = RenderingPipeline()
    pipeline.register(TextRenderer(), priority=100)

    lifecycle = DeliveryLifecycleService(
        logger=logging.getLogger("conformance.delivery"),
    )
    return TargetDeliveryService(
        adapters=adapters,
        rendering_pipeline=pipeline,
        storage=storage,
        diagnostician=Diagnostician(),
        lifecycle=lifecycle,
        logger=logging.getLogger("conformance.delivery"),
    )


class TestServicePathDeliveryConformance:
    """Assert real delivery execution through TargetDeliveryService."""

    @pytest.fixture()
    def storage(self):
        return _MemoryStorage()

    @pytest.mark.asyncio
    async def test_sent_delivery_via_service(self, storage):
        """TargetDeliveryService with FakePresentationAdapter produces a
        persisted sent receipt with correct fields and canonical
        RenderingEvidence JSON.

        Exercises: rendering pipeline, adapter deliver(), receipt
        persistence, native ref storage.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        plan_id = f"plan-sent-{uuid.uuid4()}"
        event = _make_event(event_id="svc-sent-001")
        route, plan = _make_route_and_plan(
            adapter_id="dest",
            plan_id=plan_id,
        )
        plan = DeliveryPlan(
            plan_id=plan_id,
            event_id=event.event_id,
            target=plan.target,
            primary_strategy=DeliveryStrategy(method="direct"),
        )

        svc = _build_service({"dest": adapter}, storage)
        receipt = await svc.deliver_to_target(event, route, plan)

        # Receipt fields.
        assert receipt.status == "sent"
        assert receipt.delivery_plan_id == plan_id
        assert receipt.source == "live"
        assert receipt.target_adapter == "dest"
        assert receipt.target_channel is None
        assert receipt.route_id == "route-svc-001"
        assert receipt.replay_run_id is None

        # Rendering evidence with canonical JSON.
        assert receipt.rendering_evidence is not None
        evidence = json.loads(receipt.rendering_evidence)
        assert evidence["schema_version"] == "1"
        assert evidence["renderer"] == "text"
        assert evidence["delivery_strategy"] == "direct"
        assert evidence["target_adapter"] == "dest"
        assert evidence["capability_level"] == "native"

        # Adapter was called.
        assert len(adapter.delivered_payloads) == 1

        # Persisted in storage.
        stored = await storage.list_receipts_for_event(event.event_id)
        assert len(stored) == 1
        assert stored[0].status == "sent"
        assert stored[0].delivery_plan_id == plan_id

        # Native ref stored (sent delivery).
        assert await storage.count_native_refs() == 1

    @pytest.mark.asyncio
    async def test_queued_delivery_via_service(self, storage):
        """TargetDeliveryService with queued adapter produces a persisted
        queued receipt with correct fields and canonical RenderingEvidence
        JSON.

        Exercises: rendering pipeline, adapter deliver() returning
        enqueued, receipt persistence with status='queued'.
        """
        adapter = _FakeQueuedAdapter(adapter_id="queued_dest")
        plan_id = f"plan-queued-{uuid.uuid4()}"
        event = _make_event(event_id="svc-queued-001")
        route, plan = _make_route_and_plan(
            adapter_id="queued_dest",
            plan_id=plan_id,
            channel="0",
        )
        plan = DeliveryPlan(
            plan_id=plan_id,
            event_id=event.event_id,
            target=plan.target,
            primary_strategy=DeliveryStrategy(method="direct"),
        )

        svc = _build_service({"queued_dest": adapter}, storage)
        receipt = await svc.deliver_to_target(event, route, plan)

        # Receipt fields.
        assert receipt.status == "queued"
        assert receipt.delivery_plan_id == plan_id
        assert receipt.source == "live"
        assert receipt.target_adapter == "queued_dest"
        assert receipt.target_channel == "0"
        assert receipt.route_id == "route-svc-001"
        assert receipt.replay_run_id is None

        # Rendering evidence with canonical JSON.
        assert receipt.rendering_evidence is not None
        evidence = json.loads(receipt.rendering_evidence)
        assert evidence["schema_version"] == "1"
        assert evidence["renderer"] == "text"
        assert evidence["delivery_strategy"] == "direct"
        assert evidence["target_adapter"] == "queued_dest"
        assert evidence["capability_level"] == "native"

        # Adapter was called.
        assert len(adapter.delivered_payloads) == 1

        # Persisted in storage.
        stored = await storage.list_receipts_for_event(event.event_id)
        assert len(stored) == 1
        assert stored[0].status == "queued"
        assert stored[0].delivery_plan_id == plan_id

        # No native ref for queued (no native_message_id).
        assert await storage.count_native_refs() == 0
