"""Orphan and invalid-lineage detection tests for convergence diagnostics.

Split from ``test_convergence_diagnostics.py`` so that each module stays under
1500 lines.  Helpers are intentionally duplicated so each file remains
independently runnable.

Covers:
- Orphaned outbox items
- Orphaned parent receipts
- Cross-plan / cross-event parent detection
- Missing delivery_plan_id on retry receipts
- Dead-lettered retryable mismatch
- Orphan report determinism, JSON safety, empty/combined cases
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from medre.core.diagnostics.convergence.orphans import build_orphan_report

# ---------------------------------------------------------------------------
# Helpers (duplicated for independent runnability)
# ---------------------------------------------------------------------------

_TS = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
_TS_LATER = datetime(2026, 5, 30, 13, 0, 0, tzinfo=timezone.utc)


def _receipt(
    *,
    receipt_id: str = "rcpt-001",
    event_id: str = "ev-001",
    delivery_plan_id: str = "dp-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-1",
    status: str = "sent",
    attempt_number: int = 1,
    sequence: int = 0,
    source: str = "live",
    created_at: datetime | None = None,
    parent_receipt_id: str | None = None,
) -> dict:
    """Build a receipt dict (duck-typed input)."""
    return {
        "receipt_id": receipt_id,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "route_id": route_id,
        "status": status,
        "attempt_number": attempt_number,
        "sequence": sequence,
        "source": source,
        "created_at": created_at or _TS,
        "parent_receipt_id": parent_receipt_id,
    }


def _outbox(
    *,
    outbox_id: str = "ob-001",
    event_id: str = "ev-001",
    delivery_plan_id: str = "dp-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-1",
    status: str = "pending",
    attempt_number: int = 1,
) -> dict:
    """Build an outbox item dict (duck-typed input)."""
    return {
        "outbox_id": outbox_id,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "route_id": route_id,
        "status": status,
        "attempt_number": attempt_number,
    }


# ===================================================================
# 14. Orphan report — orphaned outbox items
# ===================================================================


class TestOrphanedOutbox:
    """Non-terminal outbox with event_id not in known_event_ids."""

    def test_orphaned_outbox_detected(self) -> None:
        report = build_orphan_report(
            outbox_items=[
                _outbox(outbox_id="ob-1", event_id="ev-missing", status="pending"),
            ],
            known_event_ids={"ev-001", "ev-002"},
        )
        assert report.total_findings == 1
        f = report.findings[0]
        assert f.kind == "orphaned_outbox"
        assert f.severity == "inconsistent"
        assert f.record_id == "ob-1"
        assert f.record_type == "outbox"
        assert f.extra["event_id"] == "ev-missing"

    def test_terminal_outbox_not_orphaned(self) -> None:
        """Terminal outbox items are not flagged as orphaned."""
        report = build_orphan_report(
            outbox_items=[
                _outbox(outbox_id="ob-1", event_id="ev-missing", status="sent"),
            ],
            known_event_ids={"ev-001"},
        )
        assert report.total_findings == 0

    def test_known_event_id_not_orphaned(self) -> None:
        """Non-terminal outbox with known event_id is fine."""
        report = build_orphan_report(
            outbox_items=[
                _outbox(outbox_id="ob-1", event_id="ev-001", status="pending"),
            ],
            known_event_ids={"ev-001"},
        )
        assert report.total_findings == 0

    def test_no_known_event_ids_skips_check(self) -> None:
        """Without known_event_ids, orphaned outbox check is skipped."""
        report = build_orphan_report(
            outbox_items=[
                _outbox(outbox_id="ob-1", event_id="ev-ghost", status="pending"),
            ],
            known_event_ids=None,
        )
        assert report.total_findings == 0

    def test_empty_known_event_ids_flags_all(self) -> None:
        """Empty set of known IDs flags all non-terminal outbox items."""
        report = build_orphan_report(
            outbox_items=[
                _outbox(outbox_id="ob-1", event_id="ev-001", status="pending"),
            ],
            known_event_ids=set(),
        )
        assert report.total_findings == 1

    def test_multiple_orphaned_outbox(self) -> None:
        report = build_orphan_report(
            outbox_items=[
                _outbox(outbox_id="ob-2", event_id="ev-missing2", status="retry_wait"),
                _outbox(outbox_id="ob-1", event_id="ev-missing1", status="in_progress"),
            ],
            known_event_ids=set(),
        )
        assert report.total_findings == 2
        # Sorted by (kind, record_id)
        assert report.findings[0].record_id == "ob-1"
        assert report.findings[1].record_id == "ob-2"


# ===================================================================
# 15. Orphan report — orphaned parent receipt
# ===================================================================


class TestOrphanedParentReceipt:
    """Receipt with parent_receipt_id that is absent."""

    def test_orphaned_parent_detected(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-child",
                    parent_receipt_id="r-nonexistent",
                    status="failed",
                ),
            ],
        )
        assert report.total_findings == 1
        f = report.findings[0]
        assert f.kind == "orphaned_parent_receipt"
        assert f.severity == "inconsistent"
        assert f.record_id == "r-child"
        assert f.record_type == "receipt"
        assert f.extra["parent_receipt_id"] == "r-nonexistent"

    def test_valid_parent_not_flagged(self) -> None:
        """Receipt with existing parent is not orphaned."""
        report = build_orphan_report(
            receipts=[
                _receipt(receipt_id="r-parent", status="failed"),
                _receipt(
                    receipt_id="r-child",
                    parent_receipt_id="r-parent",
                    status="sent",
                ),
            ],
        )
        orphaned = [f for f in report.findings if f.kind == "orphaned_parent_receipt"]
        assert len(orphaned) == 0

    def test_none_parent_not_flagged(self) -> None:
        """Receipt without parent_receipt_id is not checked."""
        report = build_orphan_report(
            receipts=[_receipt(receipt_id="r-1", status="sent")],
        )
        assert report.total_findings == 0


# ===================================================================
# 16. Orphan report — cross-plan parent
# ===================================================================


class TestCrossPlanParent:
    """Receipt parent belongs to different delivery_plan_id."""

    def test_cross_plan_detected(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-parent",
                    delivery_plan_id="dp-alpha",
                    status="failed",
                ),
                _receipt(
                    receipt_id="r-child",
                    delivery_plan_id="dp-beta",
                    parent_receipt_id="r-parent",
                    status="queued",
                ),
            ],
        )
        cross_plan = [f for f in report.findings if f.kind == "cross_plan_parent"]
        assert len(cross_plan) == 1
        f = cross_plan[0]
        assert f.severity == "inconsistent"
        assert f.record_id == "r-child"
        assert f.extra["delivery_plan_id"] == "dp-beta"
        assert f.extra["parent_delivery_plan_id"] == "dp-alpha"

    def test_same_plan_not_flagged(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-parent",
                    delivery_plan_id="dp-001",
                    status="failed",
                ),
                _receipt(
                    receipt_id="r-child",
                    delivery_plan_id="dp-001",
                    parent_receipt_id="r-parent",
                    status="sent",
                ),
            ],
        )
        cross_plan = [f for f in report.findings if f.kind == "cross_plan_parent"]
        assert len(cross_plan) == 0


# ===================================================================
# 17. Orphan report — cross-event parent
# ===================================================================


class TestCrossEventParent:
    """Receipt parent belongs to different event_id."""

    def test_cross_event_detected(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-parent",
                    event_id="ev-100",
                    status="failed",
                ),
                _receipt(
                    receipt_id="r-child",
                    event_id="ev-200",
                    parent_receipt_id="r-parent",
                    status="queued",
                ),
            ],
        )
        cross_event = [f for f in report.findings if f.kind == "cross_event_parent"]
        assert len(cross_event) == 1
        f = cross_event[0]
        assert f.severity == "inconsistent"
        assert f.record_id == "r-child"
        assert f.extra["event_id"] == "ev-200"
        assert f.extra["parent_event_id"] == "ev-100"

    def test_same_event_not_flagged(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-parent",
                    event_id="ev-100",
                    status="failed",
                ),
                _receipt(
                    receipt_id="r-child",
                    event_id="ev-100",
                    parent_receipt_id="r-parent",
                    status="sent",
                ),
            ],
        )
        cross_event = [f for f in report.findings if f.kind == "cross_event_parent"]
        assert len(cross_event) == 0

    def test_cross_plan_and_cross_event_together(self) -> None:
        """Parent differing in both plan and event produces two findings."""
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-parent",
                    event_id="ev-100",
                    delivery_plan_id="dp-a",
                    status="failed",
                ),
                _receipt(
                    receipt_id="r-child",
                    event_id="ev-200",
                    delivery_plan_id="dp-b",
                    parent_receipt_id="r-parent",
                    status="queued",
                ),
            ],
        )
        kinds = {f.kind for f in report.findings}
        assert "cross_plan_parent" in kinds
        assert "cross_event_parent" in kinds


# ===================================================================
# 18. Orphan report — missing delivery_plan_id on retry receipts
# ===================================================================


class TestMissingDeliveryPlanId:
    """Retry receipt with missing/empty delivery_plan_id."""

    def test_retry_missing_plan_id(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-retry",
                    delivery_plan_id="",
                    source="retry",
                    status="failed",
                ),
            ],
        )
        missing = [f for f in report.findings if f.kind == "missing_delivery_plan_id"]
        assert len(missing) == 1
        f = missing[0]
        assert f.severity == "degraded"
        assert f.record_id == "r-retry"
        assert f.record_type == "receipt"

    def test_live_receipt_not_flagged(self) -> None:
        """Live receipt with empty plan_id is not flagged by this check."""
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-live",
                    delivery_plan_id="",
                    source="live",
                    status="sent",
                ),
            ],
        )
        missing = [f for f in report.findings if f.kind == "missing_delivery_plan_id"]
        assert len(missing) == 0

    def test_retry_with_plan_id_not_flagged(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-retry",
                    delivery_plan_id="dp-001",
                    source="retry",
                    status="failed",
                ),
            ],
        )
        missing = [f for f in report.findings if f.kind == "missing_delivery_plan_id"]
        assert len(missing) == 0


# ===================================================================
# 19. Orphan report — dead-lettered retryable mismatch
# ===================================================================


class TestDeadLetteredRetryableMismatch:
    """Dead-lettered outbox with non-terminal latest receipt."""

    def test_dead_lettered_with_failed_receipt(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(status="failed"),
            ],
            outbox_items=[
                _outbox(status="dead_lettered"),
            ],
        )
        dl = [
            f for f in report.findings if f.kind == "dead_lettered_retryable_mismatch"
        ]
        assert len(dl) == 1
        f = dl[0]
        assert f.severity == "degraded"
        assert f.record_type == "outbox"
        assert f.extra["outbox_status"] == "dead_lettered"
        assert f.extra["receipt_status"] == "failed"

    def test_dead_lettered_with_queued_receipt(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(status="queued"),
            ],
            outbox_items=[
                _outbox(status="dead_lettered"),
            ],
        )
        dl = [
            f for f in report.findings if f.kind == "dead_lettered_retryable_mismatch"
        ]
        assert len(dl) == 1

    def test_dead_lettered_with_sent_receipt_no_mismatch(self) -> None:
        """Dead-lettered outbox + terminal receipt is NOT a mismatch."""
        report = build_orphan_report(
            receipts=[
                _receipt(status="sent"),
            ],
            outbox_items=[
                _outbox(status="dead_lettered"),
            ],
        )
        dl = [
            f for f in report.findings if f.kind == "dead_lettered_retryable_mismatch"
        ]
        assert len(dl) == 0

    def test_dead_lettered_no_receipt_no_mismatch(self) -> None:
        """Dead-lettered outbox without any receipt is not flagged."""
        report = build_orphan_report(
            outbox_items=[
                _outbox(status="dead_lettered"),
            ],
        )
        dl = [
            f for f in report.findings if f.kind == "dead_lettered_retryable_mismatch"
        ]
        assert len(dl) == 0


# ===================================================================
# 20. Orphan report — determinism and JSON safety
# ===================================================================


class TestOrphanReportDeterminism:
    """Same inputs → same outputs; deterministic ordering."""

    def test_repeated_build_identical(self) -> None:
        receipts = [
            _receipt(
                receipt_id="r-parent",
                event_id="ev-1",
                delivery_plan_id="dp-1",
                status="failed",
            ),
            _receipt(
                receipt_id="r-child",
                event_id="ev-1",
                delivery_plan_id="dp-2",
                parent_receipt_id="r-parent",
                source="retry",
                status="queued",
            ),
        ]
        r1 = build_orphan_report(receipts=receipts)
        r2 = build_orphan_report(receipts=receipts)
        assert r1.total_findings == r2.total_findings
        assert r1.to_dict() == r2.to_dict()

    def test_findings_sorted_by_kind_and_id(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-z",
                    parent_receipt_id="r-nonexistent",
                    status="failed",
                ),
                _receipt(
                    receipt_id="r-a",
                    parent_receipt_id="r-ghost",
                    status="failed",
                ),
            ],
        )
        # All orphaned_parent_receipt, sorted by record_id
        assert report.findings[0].record_id == "r-a"
        assert report.findings[1].record_id == "r-z"


class TestOrphanReportJsonSafety:
    """Full report survives json.dumps round-trip."""

    def test_to_dict_json_roundtrip(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-child",
                    event_id="ev-200",
                    delivery_plan_id="",
                    parent_receipt_id="r-nonexistent",
                    source="retry",
                    status="failed",
                ),
            ],
            outbox_items=[
                _outbox(outbox_id="ob-1", event_id="ev-missing", status="pending"),
            ],
            known_event_ids={"ev-001"},
        )
        d = report.to_dict()
        raw = json.dumps(d)
        reloaded = json.loads(raw)
        assert reloaded["total_findings"] >= 1
        assert isinstance(reloaded["findings"], list)
        assert isinstance(reloaded["severity_counts"], dict)

    def test_finding_to_dict_json_safe(self) -> None:
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-1",
                    parent_receipt_id="r-ghost",
                    status="failed",
                ),
            ],
        )
        f = report.findings[0]
        d = f.to_dict()
        raw = json.dumps(d)
        reloaded = json.loads(raw)
        assert reloaded["kind"] == "orphaned_parent_receipt"


# ===================================================================
# 21. Orphan report — empty input and no-findings cases
# ===================================================================


class TestOrphanReportEmpty:
    """No findings when input is clean or empty."""

    def test_empty_input_no_findings(self) -> None:
        report = build_orphan_report()
        assert report.total_findings == 0
        assert report.findings == ()
        assert report.worst_severity is None
        assert report.summary == "No orphan or invalid-lineage findings"

    def test_clean_data_no_findings(self) -> None:
        """Valid receipts and outbox items produce no findings."""
        report = build_orphan_report(
            receipts=[
                _receipt(
                    receipt_id="r-1",
                    event_id="ev-1",
                    delivery_plan_id="dp-1",
                    status="sent",
                ),
            ],
            outbox_items=[
                _outbox(
                    outbox_id="ob-1",
                    event_id="ev-1",
                    delivery_plan_id="dp-1",
                    status="sent",
                ),
            ],
            known_event_ids={"ev-1"},
        )
        assert report.total_findings == 0


# ===================================================================
# 22. Orphan report — combined findings
# ===================================================================


class TestOrphanReportCombined:
    """Multiple finding types in a single report."""

    def test_combined_findings_count_and_worst(self) -> None:
        """Multiple finding types produce correct counts and worst severity."""
        report = build_orphan_report(
            outbox_items=[
                # Orphaned outbox (event not in known IDs)
                _outbox(
                    outbox_id="ob-orphan",
                    event_id="ev-ghost",
                    status="pending",
                ),
                # Dead-lettered with non-terminal receipt
                _outbox(
                    outbox_id="ob-dl",
                    delivery_plan_id="dp-c",
                    event_id="ev-3",
                    status="dead_lettered",
                ),
            ],
            receipts=[
                # Parent receipt for lineage
                _receipt(
                    receipt_id="r-parent",
                    event_id="ev-1",
                    delivery_plan_id="dp-a",
                    status="failed",
                ),
                # Cross-plan + cross-event child
                _receipt(
                    receipt_id="r-cross-child",
                    event_id="ev-2",
                    delivery_plan_id="dp-b",
                    parent_receipt_id="r-parent",
                    status="queued",
                ),
                # Orphaned parent receipt
                _receipt(
                    receipt_id="r-orphan-child",
                    parent_receipt_id="r-nonexistent",
                    status="failed",
                ),
                # Retry with missing plan ID
                _receipt(
                    receipt_id="r-retry-no-plan",
                    delivery_plan_id="",
                    source="retry",
                    status="failed",
                ),
                # Receipt for dead-lettered mismatch
                _receipt(
                    receipt_id="r-dl-rec",
                    delivery_plan_id="dp-c",
                    event_id="ev-3",
                    status="failed",
                ),
            ],
            known_event_ids={"ev-1", "ev-2", "ev-3"},
        )
        # Expected: orphaned_outbox, cross_plan_parent, cross_event_parent,
        #           orphaned_parent_receipt, missing_delivery_plan_id,
        #           dead_lettered_retryable_mismatch
        assert report.total_findings == 6
        assert report.severity_counts["inconsistent"] == 4
        assert report.severity_counts["degraded"] == 2
        assert report.worst_severity == "inconsistent"

        kinds = [f.kind for f in report.findings]
        assert "orphaned_outbox" in kinds
        assert "cross_plan_parent" in kinds
        assert "cross_event_parent" in kinds
        assert "orphaned_parent_receipt" in kinds
        assert "missing_delivery_plan_id" in kinds
        assert "dead_lettered_retryable_mismatch" in kinds
