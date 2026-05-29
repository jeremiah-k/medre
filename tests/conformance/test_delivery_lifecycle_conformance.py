"""Delivery lifecycle conformance tests.

Asserts delivery receipt contracts using DeliveryLifecycleService
and direct DeliveryReceipt construction:

* Direct sent delivery creates sent receipt.
* Queued delivery creates queued receipt.
* Queued -> sent supplemental receipt correlates by delivery_plan_id.
* Supplemental receipt preserves parent/plan/route/channel/evidence.
* Suppressed receipt does not include rendering_evidence.

Uses an in-memory storage backend to persist and inspect receipts.
No real adapters or network involved.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events.canonical import DeliveryReceipt
from medre.core.storage.backend import StorageBackend

# ---------------------------------------------------------------------------
# In-memory storage for receipt inspection
# ---------------------------------------------------------------------------


class _MemoryStorage(StorageBackend):
    """Minimal in-memory storage for delivery lifecycle conformance tests."""

    def __init__(self) -> None:
        self._receipts: list[DeliveryReceipt] = []

    async def append_receipt(self, receipt: DeliveryReceipt) -> None:
        self._receipts.append(receipt)

    async def list_receipts_for_event(self, event_id: str) -> list[DeliveryReceipt]:
        return [r for r in self._receipts if r.event_id == event_id]

    # -- Required by abstract protocol but unused in these tests --

    async def append(self, event) -> None:
        raise NotImplementedError

    async def get(self, event_id: str):
        raise NotImplementedError

    async def store_native_ref(self, ref) -> None:
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

    @pytest.mark.asyncio()
    async def test_direct_sent_receipt(self, storage, lifecycle):
        """Direct sent delivery creates a receipt with status='sent'."""
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

    @pytest.mark.asyncio()
    async def test_queued_receipt(self, storage, lifecycle):
        """Queued delivery creates a receipt with status='queued'."""
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

    @pytest.mark.asyncio()
    async def test_queued_to_sent_supplemental_receipt(self, storage, lifecycle):
        """Queued -> sent supplemental receipt correlates by delivery_plan_id."""
        event_id = str(uuid.uuid4())
        plan_id = f"plan-{uuid.uuid4()}"
        now = datetime.now(tz=timezone.utc)

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
        )
        await storage.append_receipt(queued)

        # Simulate the queued->sent callback
        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh_conf",
            native_channel_id="0",
            native_message_id="pkt-999",
            delivery_plan_id=plan_id,
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

    @pytest.mark.asyncio()
    async def test_supplemental_preserves_evidence(self, storage, lifecycle):
        """Supplemental sent receipt carries rendering_evidence from queued."""
        event_id = str(uuid.uuid4())
        plan_id = f"plan-{uuid.uuid4()}"
        now = datetime.now(tz=timezone.utc)
        evidence_json = '{"schema_version":"1","renderer":"meshtastic"}'

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
        )
        await storage.append_receipt(queued)

        record = OutboundNativeRefRecord(
            event_id=event_id,
            adapter="mesh_conf",
            native_channel_id="0",
            native_message_id="pkt-888",
            delivery_plan_id=plan_id,
        )
        await lifecycle.append_queued_to_sent_receipt(storage, record, now)

        receipts = await storage.list_receipts_for_event(event_id)
        sent = [r for r in receipts if r.status == "sent"][0]
        assert sent.rendering_evidence == evidence_json

    @pytest.mark.asyncio()
    async def test_suppressed_receipt_no_rendering_evidence(self, storage, lifecycle):
        """Suppressed receipt does not carry rendering_evidence."""
        event_id = str(uuid.uuid4())
        plan_id = f"plan-{uuid.uuid4()}"

        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-sup-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id=plan_id,
            target_adapter="matrix_conf",
            target_channel="!room:example.com",
            route_id="route-1",
            status="suppressed",
            source="live",
            error="capability_suppressed",
            failure_kind="capability",
        )
        assert receipt.rendering_evidence is None
        assert receipt.status == "suppressed"
