"""Integration tests verifying PipelineRunner delegates to DeliveryLifecycleService.

Exercises the delegation wiring through PipelineRunner private methods
for suppression, queued→sent, and outbox finalization.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.routing import Router
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend

from .conftest import _make_receipt


def _make_runner(storage: StorageBackend) -> PipelineRunner:
    """Build a PipelineRunner wired to the given storage."""
    config = PipelineConfig(
        storage=storage,
        router=Router(routes=[]),
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={},
        event_bus=EventBus(),
    )
    return PipelineRunner(config)


# ===================================================================
# PipelineRunner → DeliveryLifecycleService delegation
# ===================================================================


class TestDelegationIntegration:
    """Verify PipelineRunner delegates to DeliveryLifecycleService."""

    async def test_runner_uses_lifecycle_for_suppression(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """PipelineRunner._persist_suppression_receipt delegates to lifecycle."""
        runner = _make_runner(temp_storage)

        receipt = await runner._persist_suppression_receipt(
            event_id="evt-s",
            delivery_plan_id="plan-s",
            target_adapter="dest",
            target_channel=None,
            route_id="route-s",
            failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
            error="loop_prevented",
        )

        assert receipt.status == "suppressed"
        assert receipt.failure_kind == "loop_suppressed"

        # Verify receipt persisted via lifecycle → storage.
        stored = await temp_storage.list_receipts_for_event("evt-s")
        assert len(stored) == 1
        assert stored[0].receipt_id == receipt.receipt_id

    async def test_runner_uses_lifecycle_for_queued_to_sent(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """PipelineRunner._append_queued_to_sent_receipt delegates to lifecycle."""
        now = datetime.now(tz=timezone.utc)
        # Pre-populate a queued receipt.
        queued = _make_receipt(
            receipt_id="rcpt-q",
            status="queued",
            adapter="mesh",
            channel="0",
        )
        await temp_storage.append_receipt(queued)

        # Create matching outbox item for exact correlation.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-delegate-qs",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-001",
            target_adapter="mesh",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-delegate-qs")

        runner = _make_runner(temp_storage)

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh",
            native_channel_id="0",
            native_message_id="pkt-42",
            delivery_plan_id="plan-001",
            outbox_id="obox-delegate-qs",
        )
        await runner._append_queued_to_sent_receipt(record=record, now=now)

        stored = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in stored if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-q"
        assert sent[0].adapter_message_id == "pkt-42"
        assert sent[0].delivery_plan_id == "plan-001"


# ===================================================================
# PipelineRunner._finalize_outbox_outcome delegates to lifecycle
# ===================================================================


class TestRunnerFinalizeOutboxDelegation:
    """Verify PipelineRunner._finalize_outbox_outcome delegates to lifecycle."""

    async def test_delegates_to_lifecycle(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Runner._finalize_outbox_outcome calls lifecycle.finalize_outbox_outcome."""
        runner = _make_runner(temp_storage)

        # Create an outbox item.
        item = DeliveryOutboxItem(
            outbox_id="obox-delegate",
            event_id="evt-delegate",
            route_id="route-d",
            delivery_plan_id="plan-d",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="sent", event_id="evt-delegate")
        await runner._finalize_outbox_outcome(
            "obox-delegate",
            True,
            receipt,
            None,
            None,
            None,
        )

        updated = await temp_storage.get_outbox_item("obox-delegate")
        assert updated is not None
        assert updated.status == "sent"
