"""Tests for retry/outbox accountability evidence helpers.

Covers: queued item summary, retry_wait summary, retry attempt summary
from failed receipt, retry exhaustion/dead_lettered, suppressed excluded
from retry queue, cancelled/abandoned outbox status, pending shutdown-like
work from non-terminal outbox rows, JSON safety, and deterministic ordering.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from medre.core.evidence.retry_outbox import (
    build_retry_outbox_summary,
)

# ---------------------------------------------------------------------------
# Shared test fixtures (plain dicts — no storage infrastructure needed)
# ---------------------------------------------------------------------------

_TS = datetime(2025, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
_TS_ISO = _TS.isoformat()
_TS_LATER_ISO = (_TS + timedelta(seconds=30)).isoformat()


def _outbox(
    *,
    outbox_id: str = "obx-1",
    event_id: str = "evt-1",
    route_id: str = "route-1",
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "adapter_a",
    target_channel: str | None = "ch-1",
    attempt_number: int = 1,
    status: str = "pending",
    failure_kind: str | None = None,
    failure_kind_detail: str | None = None,
    next_attempt_at: str | None = None,
    worker_id: str | None = None,
    receipt_id: str | None = None,
    parent_receipt_id: str | None = None,
    error_summary: str | None = None,
) -> dict:
    """Build a minimal outbox-item dict for testing."""
    return {
        "outbox_id": outbox_id,
        "event_id": event_id,
        "route_id": route_id,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "attempt_number": attempt_number,
        "status": status,
        "failure_kind": failure_kind,
        "failure_kind_detail": failure_kind_detail,
        "next_attempt_at": next_attempt_at,
        "worker_id": worker_id,
        "receipt_id": receipt_id,
        "parent_receipt_id": parent_receipt_id,
        "error_summary": error_summary,
    }


def _receipt(
    *,
    receipt_id: str = "rcpt-1",
    event_id: str = "evt-1",
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "adapter_a",
    target_channel: str | None = "ch-1",
    route_id: str = "route-1",
    status: str = "queued",
    attempt_number: int = 1,
    parent_receipt_id: str | None = None,
    error: str | None = None,
    failure_kind: str | None = None,
    next_retry_at: datetime | None = None,
) -> dict:
    """Build a minimal receipt dict for testing."""
    return {
        "receipt_id": receipt_id,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "route_id": route_id,
        "status": status,
        "attempt_number": attempt_number,
        "parent_receipt_id": parent_receipt_id,
        "error": error,
        "failure_kind": failure_kind,
        "next_retry_at": next_retry_at,
    }


def _retry_state(
    *,
    enabled: bool = True,
    running: bool = True,
    last_run_at: str | None = _TS_ISO,
    processed: int = 5,
    succeeded: int = 3,
    failed: int = 1,
    dead_lettered: int = 1,
) -> dict:
    """Build a minimal retry-worker-state dict for testing."""
    return {
        "enabled": enabled,
        "running": running,
        "last_run_at": last_run_at,
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "dead_lettered": dead_lettered,
    }


# ===================================================================
# Tests: queued item summary
# ===================================================================


class TestQueuedItemSummary:
    """Queued outbox items produce correct item summaries."""

    def test_queued_outbox_item_summary(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-q-1",
                    status="queued",
                    delivery_plan_id="plan-q",
                    receipt_id="rcpt-q-1",
                    event_id="evt-q",
                    target_adapter="adapter_q",
                    target_channel="ch-q",
                    attempt_number=1,
                ),
            ],
        )
        assert summary.counts["queued"] == 1
        assert len(summary.items) == 1
        item = summary.items[0]
        assert item.outbox_id == "obx-q-1"
        assert item.status == "queued"
        assert item.retry_state == "queued"
        assert item.reason_pending == "Queued in adapter-local queue"
        assert item.delivery_plan_id == "plan-q"
        assert item.event_id == "evt-q"
        assert item.target_adapter == "adapter_q"

    def test_queued_counts_in_shutdown_pending(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[_outbox(status="queued")],
        )
        assert summary.counts["shutdown_pending"] >= 1


# ===================================================================
# Tests: retry_wait summary
# ===================================================================


class TestRetryWaitSummary:
    """retry_wait outbox items show scheduled retry info."""

    def test_retry_wait_with_next_attempt_at(self) -> None:
        next_at = (_TS + timedelta(seconds=30)).isoformat()
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-rw-1",
                    status="retry_wait",
                    next_attempt_at=next_at,
                    failure_kind="adapter_transient",
                    attempt_number=2,
                ),
            ],
        )
        assert summary.counts["retry_wait"] == 1
        item = summary.items[0]
        assert item.status == "retry_wait"
        assert item.retry_state == "retrying"
        assert item.next_attempt_at == next_at
        assert item.next_retry_at == next_at
        assert item.failure_kind == "adapter_transient"
        assert item.reason_pending == f"Scheduled retry at {next_at}"

    def test_retry_wait_without_next_attempt_at(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(status="retry_wait", next_attempt_at=None),
            ],
        )
        item = summary.items[0]
        assert item.reason_pending == "Awaiting retry scheduling"


# ===================================================================
# Tests: retry attempt summary from failed receipt
# ===================================================================


class TestRetryAttemptFromFailedReceipt:
    """Failed receipt without matching outbox item appears as receipt-only evidence."""

    def test_failed_receipt_without_outbox(self) -> None:
        summary = build_retry_outbox_summary(
            receipts=[
                _receipt(
                    receipt_id="rcpt-fail-1",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="ConnectionError: timeout",
                    attempt_number=2,
                    next_retry_at=_TS + timedelta(seconds=10),
                ),
            ],
        )
        assert summary.counts["failed"] == 1
        assert len(summary.items) == 1
        item = summary.items[0]
        assert item.outbox_id is None
        assert item.status == "failed"
        assert item.retry_state == "failed"
        assert item.failure_kind == "adapter_transient"
        assert item.failure_taxon == "adapter_transient"
        assert item.failure_category == "retryable"
        assert item.failure_kind_detail == "adapter_transient"
        assert item.reason_pending is not None
        assert "Failed" in item.reason_pending

    def test_failed_receipt_with_outbox_not_duplicated(self) -> None:
        """Failed receipt with matching outbox item should NOT add receipt-only entry."""
        obx = _outbox(
            delivery_plan_id="plan-dup",
            target_adapter="adp",
            target_channel="ch",
            status="retry_wait",
        )
        rcpt = _receipt(
            delivery_plan_id="plan-dup",
            target_adapter="adp",
            target_channel="ch",
            status="failed",
            failure_kind="adapter_transient",
        )
        summary = build_retry_outbox_summary(
            receipts=[rcpt],
            outbox_items=[obx],
        )
        # Only 1 item — the outbox item; receipt is already represented.
        assert len(summary.items) == 1
        assert summary.items[0].outbox_id == obx["outbox_id"]
        # failed count should be 0 (receipt matches outbox item).
        assert summary.counts["failed"] == 0


# ===================================================================
# Tests: retry exhaustion / dead_lettered
# ===================================================================


class TestRetryExhaustionDeadLettered:
    """Dead-lettered outbox items and receipts are terminal."""

    def test_dead_lettered_outbox_item(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-dl-1",
                    status="dead_lettered",
                    failure_kind="retry_exhausted",
                    attempt_number=3,
                ),
            ],
        )
        assert summary.counts["dead_lettered"] == 1
        item = summary.items[0]
        assert item.status == "dead_lettered"
        assert item.failure_kind == "retry_exhausted"
        assert item.failure_taxon == "retry_exhausted"
        assert item.failure_category == "derived_terminal"
        assert item.reason_pending is None  # terminal — no reason

    def test_dead_lettered_outbox_in_shutdown_pending_zero(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[_outbox(status="dead_lettered")],
        )
        assert summary.counts["shutdown_pending"] == 0


# ===================================================================
# Tests: suppressed excluded from retry queue
# ===================================================================


class TestSuppressedExcludedFromRetryQueue:
    """Suppressed receipts appear as no-retry evidence, not in retry counts."""

    def test_suppressed_receipt_appears_as_suppressed(self) -> None:
        summary = build_retry_outbox_summary(
            receipts=[
                _receipt(
                    receipt_id="rcpt-supp-1",
                    status="suppressed",
                    failure_kind="loop_suppressed",
                    error="loop_prevented",
                ),
            ],
        )
        assert summary.counts["suppressed"] == 1
        assert summary.counts["pending"] == 0
        assert summary.counts["retry_wait"] == 0
        assert len(summary.items) == 1
        item = summary.items[0]
        assert item.status == "suppressed"
        assert item.retry_state == "suppressed"
        assert item.failure_taxon == "loop_suppressed"
        assert item.failure_category == "permanent"
        assert item.reason_pending == "Suppressed, not retryable"

    def test_suppressed_not_counted_as_queued_or_retrying(self) -> None:
        summary = build_retry_outbox_summary(
            receipts=[
                _receipt(
                    receipt_id="rcpt-s1",
                    delivery_plan_id="plan-supp-1",
                    target_adapter="adp-supp-1",
                    target_channel="ch-supp-1",
                    status="suppressed",
                    failure_kind="capability_suppressed",
                ),
                _receipt(
                    receipt_id="rcpt-s2",
                    delivery_plan_id="plan-supp-2",
                    target_adapter="adp-supp-2",
                    target_channel="ch-supp-2",
                    status="suppressed",
                    failure_kind="policy_suppressed",
                ),
            ],
            outbox_items=[
                _outbox(status="pending"),
                _outbox(
                    outbox_id="obx-rw",
                    status="retry_wait",
                    next_attempt_at=_TS_LATER_ISO,
                ),
            ],
        )
        assert summary.counts["suppressed"] == 2
        assert summary.counts["pending"] == 1
        assert summary.counts["retry_wait"] == 1
        # 4 items: 2 suppressed receipts + 1 outbox (pending) + 1 outbox (retry_wait)
        suppressed_items = [i for i in summary.items if i.status == "suppressed"]
        assert len(suppressed_items) == 2

    def test_suppressed_with_matching_outbox_not_duplicated(self) -> None:
        """Suppressed receipt with matching outbox item — outbox wins."""
        obx = _outbox(
            delivery_plan_id="plan-supp",
            target_adapter="adp-s",
            target_channel="ch-s",
            status="cancelled",
        )
        rcpt = _receipt(
            delivery_plan_id="plan-supp",
            target_adapter="adp-s",
            target_channel="ch-s",
            status="suppressed",
            failure_kind="loop_suppressed",
        )
        summary = build_retry_outbox_summary(
            receipts=[rcpt],
            outbox_items=[obx],
        )
        # Only 1 item — the outbox item.
        assert len(summary.items) == 1
        assert summary.items[0].status == "cancelled"
        # Suppressed count is 0 because the receipt key matched an outbox item.
        assert summary.counts["suppressed"] == 0


# ===================================================================
# Tests: cancelled / abandoned outbox status
# ===================================================================


class TestCancelledAbandonedOutboxStatus:
    """Cancelled and abandoned outbox items are terminal."""

    def test_cancelled_outbox_item(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-cancel",
                    status="cancelled",
                    error_summary="Operator cancelled delivery",
                ),
            ],
        )
        assert summary.counts["cancelled"] == 1
        item = summary.items[0]
        assert item.status == "cancelled"
        assert item.retry_state == "cancelled"
        assert item.reason_pending is None  # terminal

    def test_abandoned_outbox_item(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-abandon",
                    status="abandoned",
                    error_summary="Drain timeout",
                ),
            ],
        )
        assert summary.counts["abandoned"] == 1
        item = summary.items[0]
        assert item.status == "abandoned"
        assert item.retry_state == "abandoned"
        assert item.reason_pending is None  # terminal

    def test_cancelled_and_abandoned_not_in_shutdown_pending(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(outbox_id="obx-c", status="cancelled"),
                _outbox(outbox_id="obx-a", status="abandoned"),
            ],
        )
        assert summary.counts["shutdown_pending"] == 0


# ===================================================================
# Tests: pending shutdown-like work from non-terminal outbox rows
# ===================================================================


class TestPendingShutdownWork:
    """Non-terminal outbox items contribute to shutdown_pending count."""

    def test_shutdown_pending_includes_all_non_terminal(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(outbox_id="obx-p", status="pending"),
                _outbox(outbox_id="obx-ip", status="in_progress", worker_id="w-1"),
                _outbox(
                    outbox_id="obx-rw",
                    status="retry_wait",
                    next_attempt_at=_TS_LATER_ISO,
                ),
                _outbox(outbox_id="obx-q", status="queued"),
                _outbox(outbox_id="obx-s", status="sent"),
                _outbox(outbox_id="obx-dl", status="dead_lettered"),
            ],
        )
        assert summary.counts["shutdown_pending"] == 4
        assert summary.counts["pending"] == 1
        assert summary.counts["in_progress"] == 1
        assert summary.counts["retry_wait"] == 1
        assert summary.counts["queued"] == 1

    def test_shutdown_pending_zero_when_all_terminal(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(outbox_id="obx-s1", status="sent"),
                _outbox(outbox_id="obx-dl1", status="dead_lettered"),
                _outbox(outbox_id="obx-c1", status="cancelled"),
                _outbox(outbox_id="obx-a1", status="abandoned"),
            ],
        )
        assert summary.counts["shutdown_pending"] == 0

    def test_in_progress_shows_worker_id(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-wip",
                    status="in_progress",
                    worker_id="retry-worker-abc12345",
                ),
            ],
        )
        item = summary.items[0]
        assert item.reason_pending == "Claimed by worker retry-worker-abc12345"

    def test_empty_inputs_give_zero_counts(self) -> None:
        summary = build_retry_outbox_summary()
        assert summary.counts["shutdown_pending"] == 0
        assert summary.counts["pending"] == 0
        assert summary.counts["retry_wait"] == 0
        assert summary.items == []


# ===================================================================
# Tests: retry worker state inclusion
# ===================================================================


class TestRetryWorkerState:
    """Retry worker counters are included when state is provided."""

    def test_retry_state_included(self) -> None:
        state = _retry_state(processed=10, succeeded=7, failed=2, dead_lettered=1)
        summary = build_retry_outbox_summary(retry_state=state)
        assert summary.retry_worker is not None
        assert summary.retry_worker["enabled"] is True
        assert summary.retry_worker["running"] is True
        assert summary.retry_worker["processed"] == 10
        assert summary.retry_worker["succeeded"] == 7
        assert summary.retry_worker["failed"] == 2
        assert summary.retry_worker["dead_lettered"] == 1
        assert summary.retry_worker["last_run_at"] == _TS_ISO

    def test_retry_state_null_when_absent(self) -> None:
        summary = build_retry_outbox_summary(retry_state=None)
        assert summary.retry_worker is None

    def test_retry_state_from_dict_with_missing_keys(self) -> None:
        """Partial dict defaults missing keys to zero/false/null."""
        state = {"enabled": True}
        summary = build_retry_outbox_summary(retry_state=state)
        assert summary.retry_worker is not None
        assert summary.retry_worker["enabled"] is True
        assert summary.retry_worker["running"] is False
        assert summary.retry_worker["processed"] == 0


# ===================================================================
# Tests: JSON safety
# ===================================================================


class TestJsonSafety:
    """All summary values must be JSON-serializable."""

    def test_summary_is_json_serializable(self) -> None:
        summary = build_retry_outbox_summary(
            receipts=[
                _receipt(
                    receipt_id="rcpt-supp-json",
                    delivery_plan_id="plan-supp-json",
                    target_adapter="adp-supp-json",
                    target_channel="ch-supp-json",
                    status="suppressed",
                    failure_kind="loop_suppressed",
                    error="loop_prevented",
                ),
                _receipt(
                    receipt_id="rcpt-fail-json",
                    delivery_plan_id="plan-fail-json",
                    target_adapter="adp-fail-json",
                    target_channel="ch-fail-json",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="ConnectionError: timeout",
                    next_retry_at=_TS + timedelta(seconds=10),
                ),
            ],
            outbox_items=[
                _outbox(
                    status="retry_wait",
                    next_attempt_at=_TS_LATER_ISO,
                    failure_kind="adapter_transient",
                    attempt_number=2,
                ),
                _outbox(status="pending"),
                _outbox(
                    status="dead_lettered",
                    failure_kind="retry_exhausted",
                    attempt_number=3,
                ),
            ],
            retry_state=_retry_state(),
        )
        as_dict = asdict(summary)
        # Must not raise.
        serialized = json.dumps(as_dict)
        assert isinstance(serialized, str)

        # Round-trip.
        parsed = json.loads(serialized)
        assert parsed["counts"]["pending"] == 1
        assert parsed["counts"]["suppressed"] == 1
        assert parsed["retry_worker"]["processed"] == 5

    def test_no_datetime_objects_in_output(self) -> None:
        """No raw datetime objects leak into the summary."""
        import dataclasses

        summary = build_retry_outbox_summary(
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="adapter_transient",
                    next_retry_at=_TS,
                ),
            ],
        )
        as_dict = dataclasses.asdict(summary)

        def _check_no_datetime(obj: object) -> None:
            if isinstance(obj, dict):
                for v in obj.values():
                    _check_no_datetime(v)
            elif isinstance(obj, list):
                for v in obj:
                    _check_no_datetime(v)
            else:
                assert not isinstance(obj, datetime), f"Found datetime in output: {obj}"

        _check_no_datetime(as_dict)

    def test_empty_summary_is_json_safe(self) -> None:
        summary = build_retry_outbox_summary()
        serialized = json.dumps(asdict(summary))
        parsed = json.loads(serialized)
        assert parsed["items"] == []
        assert parsed["retry_worker"] is None


# ===================================================================
# Tests: deterministic ordering
# ===================================================================


class TestDeterministicOrdering:
    """Items are sorted deterministically regardless of input order."""

    def test_ordering_by_event_id_plan_adapter(self) -> None:
        items_in = [
            _outbox(
                outbox_id="obx-3",
                event_id="evt-z",
                delivery_plan_id="plan-z",
                target_adapter="adapter_z",
                target_channel="ch-z",
                status="pending",
            ),
            _outbox(
                outbox_id="obx-1",
                event_id="evt-a",
                delivery_plan_id="plan-a",
                target_adapter="adapter_a",
                target_channel="ch-a",
                status="pending",
            ),
            _outbox(
                outbox_id="obx-2",
                event_id="evt-a",
                delivery_plan_id="plan-a",
                target_adapter="adapter_a",
                target_channel="ch-b",
                status="retry_wait",
            ),
        ]
        summary = build_retry_outbox_summary(outbox_items=items_in)
        ids = [i.outbox_id for i in summary.items]
        assert ids == ["obx-1", "obx-2", "obx-3"]

    def test_ordering_includes_receipt_only_items(self) -> None:
        """Receipt-only items are interleaved by sort key."""
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-zz",
                    event_id="evt-zz",
                    delivery_plan_id="plan-zz",
                    target_adapter="adp_zz",
                    status="pending",
                ),
            ],
            receipts=[
                _receipt(
                    receipt_id="rcpt-aa",
                    event_id="evt-aa",
                    delivery_plan_id="plan-aa",
                    target_adapter="adp_aa",
                    status="suppressed",
                    failure_kind="loop_suppressed",
                ),
            ],
        )
        # evt-aa < evt-zz so suppressed receipt should come first.
        assert summary.items[0].event_id == "evt-aa"
        assert summary.items[0].status == "suppressed"
        assert summary.items[1].event_id == "evt-zz"
        assert summary.items[1].status == "pending"

    def test_same_sort_key_resolved_by_outbox_id(self) -> None:
        """When other keys are equal, outbox_id breaks ties."""
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-b",
                    event_id="evt-same",
                    delivery_plan_id="plan-same",
                    target_adapter="adp-same",
                    target_channel="ch-same",
                    attempt_number=1,
                    status="pending",
                ),
                _outbox(
                    outbox_id="obx-a",
                    event_id="evt-same",
                    delivery_plan_id="plan-same",
                    target_adapter="adp-same",
                    target_channel="ch-same",
                    attempt_number=1,
                    status="pending",
                ),
            ],
        )
        assert summary.items[0].outbox_id == "obx-a"
        assert summary.items[1].outbox_id == "obx-b"


# ===================================================================
# Tests: failure taxonomy enrichment
# ===================================================================


class TestFailureTaxonomyEnrichment:
    """Outbox items and receipts are enriched with failure taxonomy."""

    def test_adapter_transient_outbox(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    status="retry_wait",
                    failure_kind="adapter_transient",
                ),
            ],
        )
        item = summary.items[0]
        assert item.failure_taxon == "adapter_transient"
        assert item.failure_category == "retryable"

    def test_capacity_rejection_outbox(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    status="retry_wait",
                    failure_kind="capacity_rejection",
                ),
            ],
        )
        item = summary.items[0]
        assert item.failure_taxon == "capacity_rejection"
        assert item.failure_category == "operational"

    def test_shutdown_drain_timeout_enrichment(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    status="abandoned",
                    failure_kind="shutdown_rejection",
                    error_summary="shutdown_drain_timeout exceeded",
                ),
            ],
        )
        item = summary.items[0]
        # derive_failure_kind_detail should refine to shutdown_drain_timeout.
        assert item.failure_kind_detail == "shutdown_drain_timeout"

    def test_suppressed_receipt_capability(self) -> None:
        summary = build_retry_outbox_summary(
            receipts=[
                _receipt(
                    status="suppressed",
                    failure_kind="capability_suppressed",
                    error="Adapter capability missing",
                ),
            ],
        )
        item = summary.items[0]
        assert item.failure_taxon == "capability_suppressed"
        assert item.failure_category == "permanent"

    def test_no_failure_kind_gives_null_taxon(self) -> None:
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(status="pending"),
            ],
        )
        item = summary.items[0]
        assert item.failure_taxon is None
        assert item.failure_category is None
        assert item.failure_kind_detail is None


# ===================================================================
# Tests: mixed scenario
# ===================================================================


class TestMixedScenario:
    """Full scenario with receipts, outbox items, and retry state."""

    def test_full_mixed_scenario(self) -> None:
        summary = build_retry_outbox_summary(
            receipts=[
                _receipt(
                    receipt_id="rcpt-supp",
                    delivery_plan_id="plan-supp",
                    target_adapter="adp-s",
                    target_channel="ch-s",
                    status="suppressed",
                    failure_kind="loop_suppressed",
                    error="loop_prevented",
                ),
                _receipt(
                    receipt_id="rcpt-fail-orphan",
                    delivery_plan_id="plan-orphan",
                    target_adapter="adp-o",
                    target_channel="ch-o",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="ConnectionError",
                    next_retry_at=_TS,
                ),
            ],
            outbox_items=[
                _outbox(
                    outbox_id="obx-pending",
                    delivery_plan_id="plan-p",
                    target_adapter="adp-p",
                    status="pending",
                ),
                _outbox(
                    outbox_id="obx-rw",
                    delivery_plan_id="plan-rw",
                    target_adapter="adp-rw",
                    status="retry_wait",
                    next_attempt_at=_TS_LATER_ISO,
                    failure_kind="adapter_transient",
                    attempt_number=2,
                ),
                _outbox(
                    outbox_id="obx-sent",
                    delivery_plan_id="plan-s",
                    target_adapter="adp-s",
                    status="sent",
                ),
                _outbox(
                    outbox_id="obx-dl",
                    delivery_plan_id="plan-dl",
                    target_adapter="adp-dl",
                    status="dead_lettered",
                    failure_kind="retry_exhausted",
                    attempt_number=3,
                ),
            ],
            retry_state=_retry_state(
                processed=20, succeeded=15, failed=4, dead_lettered=1
            ),
        )

        # Counts.
        assert summary.counts["pending"] == 1
        assert summary.counts["retry_wait"] == 1
        assert summary.counts["sent"] == 1
        assert summary.counts["dead_lettered"] == 1
        assert summary.counts["suppressed"] == 1
        assert summary.counts["failed"] == 1  # orphan failed receipt
        assert summary.counts["shutdown_pending"] == 2  # pending + retry_wait

        # Items: 4 outbox + 2 receipt-only (suppressed + failed orphan).
        assert len(summary.items) == 6

        # Retry worker.
        assert summary.retry_worker is not None
        assert summary.retry_worker["processed"] == 20

        # JSON safety.
        json.dumps(asdict(summary))


# ===================================================================
# Tests: uncorrelated queued items (Wave 2 T5)
# ===================================================================


class TestUncorrelatedQueuedItems:
    """Queued items lacking delivery_plan_id or receipt linkage get explicit
    operator-visible pending/recovery reasons.

    Wave 2 T5: operator visibility for queued outbox items that cannot be
    correlated because callback/native ref lacked delivery_plan_id or
    receipt linkage.
    """

    def test_queued_no_plan_id_no_receipt(self) -> None:
        """Queued item with no delivery_plan_id and no receipt_id gets
        uncorrelated reason mentioning both missing fields."""
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-uncorr-1",
                    status="queued",
                    delivery_plan_id="",  # empty → no plan correlation
                    receipt_id=None,
                    event_id="evt-uncorr",
                    target_adapter="meshtastic",
                    target_channel="ch-msh",
                    attempt_number=1,
                ),
            ],
        )
        assert summary.counts["queued"] == 1
        item = summary.items[0]
        assert item.status == "queued"
        assert item.reason_pending is not None
        assert "uncorrelated" in item.reason_pending
        assert "no delivery_plan_id" in item.reason_pending
        assert "no receipt linkage" in item.reason_pending
        assert "stale-grace reclaim" in item.reason_pending

    def test_queued_no_plan_id_with_receipt(self) -> None:
        """Queued item with receipt_id but no delivery_plan_id gets
        plan-specific uncorrelated reason."""
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-uncorr-2",
                    status="queued",
                    delivery_plan_id="",
                    receipt_id="rcpt-existing",
                    event_id="evt-uncorr-2",
                    target_adapter="lxmf",
                    target_channel="ch-lxmf",
                    attempt_number=1,
                ),
            ],
        )
        item = summary.items[0]
        assert item.reason_pending is not None
        assert "uncorrelated" in item.reason_pending
        assert "no delivery_plan_id" in item.reason_pending
        # Should NOT mention receipt linkage since receipt exists.
        assert "no receipt linkage" not in item.reason_pending

    def test_queued_with_plan_id_no_receipt(self) -> None:
        """Queued item with delivery_plan_id but no receipt_id gets
        receipt-specific uncorrelated reason."""
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-uncorr-3",
                    status="queued",
                    delivery_plan_id="plan-has-plan",
                    receipt_id=None,
                    event_id="evt-uncorr-3",
                    target_adapter="meshcore",
                    target_channel="ch-mc",
                    attempt_number=1,
                ),
            ],
        )
        item = summary.items[0]
        assert item.reason_pending is not None
        assert "uncorrelated" in item.reason_pending
        assert "no receipt linkage" in item.reason_pending
        # Should NOT mention delivery_plan_id since it exists.
        assert "no delivery_plan_id" not in item.reason_pending

    def test_queued_with_plan_id_and_receipt_is_normal(self) -> None:
        """Queued item with both delivery_plan_id and receipt_id gets the
        standard 'Queued in adapter-local queue' reason (no uncorrelated
        suffix)."""
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-correlated",
                    status="queued",
                    delivery_plan_id="plan-corr",
                    receipt_id="rcpt-corr",
                    event_id="evt-corr",
                    target_adapter="matrix",
                    target_channel="ch-matrix",
                    attempt_number=1,
                ),
            ],
        )
        item = summary.items[0]
        assert item.reason_pending == "Queued in adapter-local queue"

    def test_uncorrelated_queued_counts_in_shutdown_pending(self) -> None:
        """Uncorrelated queued items still count in shutdown_pending."""
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-unc-sp",
                    status="queued",
                    delivery_plan_id="",
                    receipt_id=None,
                ),
            ],
        )
        assert summary.counts["queued"] == 1
        assert summary.counts["shutdown_pending"] >= 1

    def test_uncorrelated_queued_is_json_safe(self) -> None:
        """Uncorrelated queued item evidence is JSON-safe."""
        summary = build_retry_outbox_summary(
            outbox_items=[
                _outbox(
                    outbox_id="obx-unc-json",
                    status="queued",
                    delivery_plan_id="",
                    receipt_id=None,
                    event_id="evt-unc-json",
                ),
            ],
        )
        serialized = json.dumps(asdict(summary))
        parsed = json.loads(serialized)
        assert parsed["items"][0]["reason_pending"] is not None
        assert "uncorrelated" in parsed["items"][0]["reason_pending"]
