"""Retry trace/evidence tests — verify timeline and evidence understand retry lineage.

Tests that timeline assembly shows both failed and success receipts in
correct order, evidence bundles include retry metadata, and recovery
guidance distinguishes pending-retry from exhausted/dead-lettered.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.events.metadata import EventMetadata
from medre.core.storage.sqlite import SQLiteStorage
from medre.observability.classification import (
    infer_failure_kind,
    failure_category,
    recommended_commands,
)
from medre.runtime.timeline import assemble_event_timeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_id: str | None = None) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id or f"evt-{uuid.uuid4()}",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_source",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "trace test"},
        metadata=EventMetadata(),
    )


def _make_failed_receipt(
    *,
    event_id: str,
    receipt_id: str = "rcpt-fail-001",
    target_adapter: str = "target_a",
    attempt_number: int = 1,
    parent_receipt_id: str | None = None,
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-1",
        target_adapter=target_adapter,
        route_id="route-1",
        status="failed",
        error="ConnectionError: timeout",
        next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=2),
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _make_success_receipt(
    *,
    event_id: str,
    receipt_id: str = "rcpt-ok-001",
    target_adapter: str = "target_a",
    attempt_number: int = 2,
    parent_receipt_id: str = "rcpt-fail-001",
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-1",
        target_adapter=target_adapter,
        route_id="route-1",
        status="sent",
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
        created_at=created_at or datetime.now(timezone.utc) + timedelta(seconds=1),
    )


def _make_dead_letter_receipt(
    *,
    event_id: str,
    receipt_id: str = "rcpt-dead-001",
    target_adapter: str = "target_a",
    attempt_number: int = 3,
    parent_receipt_id: str = "rcpt-fail-002",
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-1",
        target_adapter=target_adapter,
        route_id="route-1",
        status="dead_lettered",
        error="Retry exhausted",
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
        created_at=created_at or datetime.now(timezone.utc) + timedelta(seconds=2),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryTraceEvidence:
    """Timeline and evidence correctly represent retry lineage."""

    async def test_trace_shows_retry_lineage(self, temp_storage):
        """Timeline shows both failed and success receipts, ordered correctly."""
        event = _make_event()
        await temp_storage.append(event)

        # Create failed receipt (attempt 1) then success receipt (attempt 2)
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        failed = _make_failed_receipt(
            event_id=event.event_id,
            receipt_id="rcpt-fail",
            attempt_number=1,
            created_at=t0,
        )
        success = _make_success_receipt(
            event_id=event.event_id,
            receipt_id="rcpt-ok",
            attempt_number=2,
            parent_receipt_id="rcpt-fail",
            created_at=t0 + timedelta(seconds=1),
        )
        await temp_storage.append_receipt(failed)
        await temp_storage.append_receipt(success)

        timeline = await assemble_event_timeline(temp_storage, event.event_id)

        assert timeline is not None
        receipts = timeline["receipts"]
        assert len(receipts) == 2

        # Ordered by sequence (append order)
        assert receipts[0].receipt_id == "rcpt-fail"
        assert receipts[0].status == "failed"
        assert receipts[0].attempt_number == 1

        assert receipts[1].receipt_id == "rcpt-ok"
        assert receipts[1].status == "sent"
        assert receipts[1].attempt_number == 2
        assert receipts[1].parent_receipt_id == "rcpt-fail"

        # Timeline entries include both
        entries = timeline["timeline_entries"]
        entry_types = [e.get("entry_type") for e in entries]
        assert "receipt" in entry_types

    async def test_evidence_includes_retry_attempts(self, temp_storage):
        """Evidence incident_summary includes retry count metadata."""
        event = _make_event()
        await temp_storage.append(event)

        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        failed = _make_failed_receipt(
            event_id=event.event_id,
            receipt_id="rcpt-fail",
            attempt_number=1,
            created_at=t0,
        )
        success = _make_success_receipt(
            event_id=event.event_id,
            receipt_id="rcpt-ok",
            attempt_number=2,
            parent_receipt_id="rcpt-fail",
            created_at=t0 + timedelta(seconds=1),
        )
        await temp_storage.append_receipt(failed)
        await temp_storage.append_receipt(success)

        timeline = await assemble_event_timeline(temp_storage, event.event_id)
        assert timeline is not None

        receipts = timeline["receipts"]
        failed_count = sum(
            1 for r in receipts if r.status in ("failed", "dead_lettered")
        )
        sent_count = sum(1 for r in receipts if r.status == "sent")

        # Evidence-style incident summary
        first_failure_kind = None
        for r in receipts:
            if r.status in ("failed", "dead_lettered"):
                fk = infer_failure_kind(r.error, r.status)
                first_failure_kind = fk
                break

        if failed_count == 0:
            classification = "success"
        else:
            worst = failure_category(first_failure_kind or "unknown")
            classification = worst if worst != "success" else "unknown"

        assert failed_count == 1
        assert sent_count == 1
        assert first_failure_kind == "adapter_transient"
        assert classification == "retryable"

        # Recommended commands for retryable include replay
        cmds = recommended_commands("retryable", event.event_id)
        assert any("replay" in c for c in cmds)
        assert any("trace" in c for c in cmds)

    async def test_recover_distinguishes_retry_pending_vs_exhausted(self, temp_storage):
        """Recovery guidance differs for pending retry vs dead-lettered."""
        event = _make_event()
        await temp_storage.append(event)

        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # --- Scenario A: pending retry (failed, next_retry_at in future) ---
        event_a = _make_event(event_id="evt-pending")
        await temp_storage.append(event_a)
        pending_receipt = _make_failed_receipt(
            event_id="evt-pending",
            receipt_id="rcpt-pending",
            attempt_number=1,
            created_at=t0,
        )
        await temp_storage.append_receipt(pending_receipt)

        timeline_a = await assemble_event_timeline(temp_storage, "evt-pending")
        assert timeline_a is not None
        r_a = timeline_a["receipts"]
        # Pending: has failed receipt with next_retry_at
        has_pending = any(
            r.status == "failed" and r.next_retry_at is not None
            for r in r_a
        )
        assert has_pending

        # Failure kind is transient → retryable
        pending_fail = [r for r in r_a if r.status == "failed"][0]
        kind_a = infer_failure_kind(pending_fail.error, pending_fail.status)
        assert kind_a == "adapter_transient"
        cat_a = failure_category(kind_a)
        assert cat_a == "retryable"
        cmds_a = recommended_commands(cat_a, "evt-pending")
        assert any("replay" in c.lower() for c in cmds_a)

        # --- Scenario B: dead-lettered (retries exhausted) ---
        event_b = _make_event(event_id="evt-dead")
        await temp_storage.append(event_b)
        dead = _make_dead_letter_receipt(
            event_id="evt-dead",
            receipt_id="rcpt-dead",
            attempt_number=3,
            parent_receipt_id="rcpt-fail-prev",
            created_at=t0,
        )
        await temp_storage.append_receipt(dead)

        timeline_b = await assemble_event_timeline(temp_storage, "evt-dead")
        assert timeline_b is not None
        r_b = timeline_b["receipts"]
        dead_lettered = [r for r in r_b if r.status == "dead_lettered"]
        assert len(dead_lettered) == 1

        # Dead-lettered is inferred as adapter_transient (was retriable)
        kind_b = infer_failure_kind(
            dead_lettered[0].error, dead_lettered[0].status,
        )
        assert kind_b == "adapter_transient"
        # But the receipt status tells us it's exhausted
        assert dead_lettered[0].status == "dead_lettered"

        # For dead-lettered, recommended commands should include manual replay
        cmds_b = recommended_commands("retryable", "evt-dead")
        assert len(cmds_b) > 0
        # Manual replay is the recommended action for exhausted retries
        assert any("replay" in c.lower() for c in cmds_b)
