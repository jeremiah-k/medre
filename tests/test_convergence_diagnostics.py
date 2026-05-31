"""Focused tests for recovery convergence diagnostics.

Covers: safe/degraded/inconsistent classification, deterministic repeated
build, missing plan_id, source separation, JSON safety, all outbox statuses,
receipt-only terminal evidence, reserved fields, and orphan/
invalid-lineage detection.

Test groups
-----------
1. Safe convergence — matching terminal states, receipt-only terminal.
2. Degraded convergence — pending/retry_wait+failed, in_progress/queued
   without receipt, missing plan_id.
3. Inconsistent convergence — terminal outbox + non-terminal receipt,
   non-terminal outbox + terminal receipt sent.
4. Deterministic repeated build — same input → same output.
5. Missing delivery_plan_id — degraded with warning.
6. Source separation — replay vs live receipts are not conflated when
   source is visible in the data.
7. JSON safety — full summary survives json.dumps round-trip.
8. All outbox statuses covered.
9. Reserved fields — orphan_count and evidence_bundle_ref are None.
10. Empty input — produces safe empty summary.
11. Receipt latest selection — deterministic tiebreaking.
12. Aggregate severity counts and worst severity.
13. Multiple targets with different adapters/channels.
14. Orphan report — orphaned outbox items.
15. Orphan report — orphaned parent receipt.
16. Orphan report — cross-plan parent.
17. Orphan report — cross-event parent.
18. Orphan report — missing delivery_plan_id on retry receipts.
19. Orphan report — dead-lettered retryable mismatch.
20. Orphan report — determinism and JSON safety.
21. Orphan report — empty input and no-findings cases.
22. Orphan report — combined findings.
23. Unrecognised outbox status — DEGRADED with warning.
24. target_channel empty-string vs None grouping.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from medre.core.diagnostics.convergence import (
    build_convergence_summary,
    build_orphan_report,
)

# ---------------------------------------------------------------------------
# Helpers
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
# 1. Safe convergence
# ===================================================================


class TestSafeConvergence:
    """Matching terminal outbox + receipt → safe."""

    def test_sent_sent_is_safe(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="sent")],
            outbox_items=[_outbox(status="sent")],
        )
        assert summary.total_targets == 1
        target = summary.targets[0]
        assert target.severity == "safe"
        assert target.outbox_status == "sent"
        assert target.latest_receipt_status == "sent"

    def test_dead_lettered_both_safe(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="dead_lettered")],
            outbox_items=[_outbox(status="dead_lettered")],
        )
        assert summary.targets[0].severity == "safe"

    def test_both_terminal_different_statuses_safe(self) -> None:
        """Both terminal but different statuses → safe with warning."""
        summary = build_convergence_summary(
            receipts=[_receipt(status="dead_lettered")],
            outbox_items=[_outbox(status="sent")],
        )
        target = summary.targets[0]
        assert target.severity == "safe"
        assert len(target.warnings) > 0
        assert "differ" in target.warnings[0]

    def test_terminal_outbox_no_receipt_safe(self) -> None:
        """Terminal outbox without receipt → safe (e.g. cancelled)."""
        summary = build_convergence_summary(
            outbox_items=[_outbox(status="cancelled")],
        )
        assert summary.targets[0].severity == "safe"

    def test_abandoned_outbox_no_receipt_safe(self) -> None:
        summary = build_convergence_summary(
            outbox_items=[_outbox(status="abandoned")],
        )
        assert summary.targets[0].severity == "safe"


class TestReceiptOnlyTerminalSafe:
    """Receipt-only terminal evidence → safe with explicit warning.

    Design choice: a receipt saying 'sent' without an outbox item is
    classified safe because the receipt is durable audit evidence.
    The warning informs the operator that the outbox item is absent,
    which may indicate normal cleanup (outbox row deleted after
    terminal delivery) or a missing outbox entry.
    """

    def test_sent_receipt_only_is_safe_with_warning(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="sent")],
        )
        target = summary.targets[0]
        assert target.severity == "safe"
        assert target.outbox_status is None
        assert any("Receipt-only terminal" in w for w in target.warnings)

    def test_dead_lettered_receipt_only_safe(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="dead_lettered")],
        )
        assert summary.targets[0].severity == "safe"

    def test_suppressed_receipt_only_safe(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="suppressed")],
        )
        assert summary.targets[0].severity == "safe"


# ===================================================================
# 2. Degraded convergence
# ===================================================================


class TestDegradedConvergence:
    """Non-terminal outbox states that are explainable → degraded."""

    def test_pending_outbox_with_failed_receipt(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="failed")],
            outbox_items=[_outbox(status="pending")],
        )
        assert summary.targets[0].severity == "degraded"

    def test_retry_wait_outbox_with_failed_receipt(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="failed")],
            outbox_items=[_outbox(status="retry_wait")],
        )
        assert summary.targets[0].severity == "degraded"

    def test_in_progress_no_receipt(self) -> None:
        """in_progress outbox without receipt → degraded (mid-flight)."""
        summary = build_convergence_summary(
            outbox_items=[_outbox(status="in_progress")],
        )
        target = summary.targets[0]
        assert target.severity == "degraded"
        assert any("mid-flight" in w for w in target.warnings)

    def test_queued_no_receipt(self) -> None:
        """queued outbox without receipt → degraded (mid-flight)."""
        summary = build_convergence_summary(
            outbox_items=[_outbox(status="queued")],
        )
        target = summary.targets[0]
        assert target.severity == "degraded"

    def test_pending_outbox_with_queued_receipt(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="queued")],
            outbox_items=[_outbox(status="pending")],
        )
        assert summary.targets[0].severity == "degraded"

    def test_non_terminal_receipt_only_degraded(self) -> None:
        """Non-terminal receipt without outbox → degraded."""
        summary = build_convergence_summary(
            receipts=[_receipt(status="failed")],
        )
        target = summary.targets[0]
        assert target.severity == "degraded"
        assert any("Receipt-only non-terminal" in w for w in target.warnings)

    def test_queued_receipt_only_degraded(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="queued")],
        )
        assert summary.targets[0].severity == "degraded"

    def test_non_terminal_outbox_dead_lettered_receipt_degraded(self) -> None:
        """Non-terminal outbox + dead_lettered receipt → degraded (retry may change outcome)."""
        summary = build_convergence_summary(
            receipts=[_receipt(status="dead_lettered")],
            outbox_items=[_outbox(status="retry_wait")],
        )
        assert summary.targets[0].severity == "degraded"


# ===================================================================
# 3. Inconsistent convergence
# ===================================================================


class TestInconsistentConvergence:
    """Status mismatches that cannot be explained by normal flow."""

    def test_terminal_outbox_non_terminal_receipt(self) -> None:
        """sent outbox but queued receipt → inconsistent."""
        summary = build_convergence_summary(
            receipts=[_receipt(status="queued")],
            outbox_items=[_outbox(status="sent")],
        )
        target = summary.targets[0]
        assert target.severity == "inconsistent"
        assert any("Terminal outbox" in w for w in target.warnings)

    def test_sent_outbox_failed_receipt_inconsistent(self) -> None:
        """sent outbox but failed receipt → inconsistent."""
        summary = build_convergence_summary(
            receipts=[_receipt(status="failed")],
            outbox_items=[_outbox(status="sent")],
        )
        target = summary.targets[0]
        assert target.severity == "inconsistent"

    def test_non_terminal_outbox_sent_receipt(self) -> None:
        """pending outbox but sent receipt → inconsistent."""
        summary = build_convergence_summary(
            receipts=[_receipt(status="sent")],
            outbox_items=[_outbox(status="pending")],
        )
        target = summary.targets[0]
        assert target.severity == "inconsistent"
        assert any("Non-terminal outbox" in w for w in target.warnings)

    def test_retry_wait_sent_receipt_inconsistent(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="sent")],
            outbox_items=[_outbox(status="retry_wait")],
        )
        assert summary.targets[0].severity == "inconsistent"

    def test_in_progress_suppressed_receipt_inconsistent(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="suppressed")],
            outbox_items=[_outbox(status="in_progress")],
        )
        assert summary.targets[0].severity == "inconsistent"


# ===================================================================
# 4. Deterministic repeated build
# ===================================================================


class TestDeterminism:
    """Same inputs always produce same outputs."""

    def test_repeated_build_identical(self) -> None:
        receipts = [
            _receipt(receipt_id="r-1", attempt_number=1, status="failed"),
            _receipt(receipt_id="r-2", attempt_number=2, status="sent"),
        ]
        outbox = [_outbox(status="sent")]
        s1 = build_convergence_summary(receipts=receipts, outbox_items=outbox)
        s2 = build_convergence_summary(receipts=receipts, outbox_items=outbox)
        assert s1.total_targets == s2.total_targets
        assert s1.severity_counts == s2.severity_counts
        assert s1.worst_severity == s2.worst_severity
        assert [t.to_dict() for t in s1.targets] == [t.to_dict() for t in s2.targets]

    def test_deterministic_target_ordering(self) -> None:
        """Targets are always sorted by group key."""
        s = build_convergence_summary(
            receipts=[
                _receipt(
                    delivery_plan_id="dp-b",
                    target_adapter="z-adapter",
                    target_channel="ch-2",
                    receipt_id="r-2",
                ),
                _receipt(
                    delivery_plan_id="dp-a",
                    target_adapter="a-adapter",
                    target_channel="ch-1",
                    receipt_id="r-1",
                ),
            ],
        )
        assert s.targets[0].delivery_plan_id == "dp-a"
        assert s.targets[1].delivery_plan_id == "dp-b"


# ===================================================================
# 5. Missing delivery_plan_id
# ===================================================================


class TestMissingPlanId:
    """Empty delivery_plan_id → degraded with warning."""

    def test_empty_plan_id_degraded(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(delivery_plan_id="", status="sent")],
            outbox_items=[_outbox(delivery_plan_id="", status="sent")],
        )
        target = summary.targets[0]
        assert target.severity == "degraded"
        assert any("delivery_plan_id is empty" in w for w in target.warnings)

    def test_none_plan_id_degraded(self) -> None:
        """Dict with missing delivery_plan_id key falls back to ''."""
        receipt = {
            "receipt_id": "r-no-plan",
            "delivery_plan_id": "",
            "target_adapter": "radio",
            "target_channel": "ch-0",
            "status": "sent",
            "attempt_number": 1,
            "sequence": 0,
            "created_at": _TS,
        }
        summary = build_convergence_summary(receipts=[receipt])
        target = summary.targets[0]
        assert target.severity == "degraded"
        assert target.delivery_plan_id == ""

    def test_no_plan_id_receipt_only_terminal_degraded(self) -> None:
        """Receipt-only terminal without plan_id → degraded (not safe)."""
        summary = build_convergence_summary(
            receipts=[_receipt(delivery_plan_id="", status="sent")],
        )
        target = summary.targets[0]
        assert target.severity == "degraded"


# ===================================================================
# 6. Source separation — replay vs live
# ===================================================================


class TestSourceSeparation:
    """Replay and live receipts for the same target key are grouped together.

    The convergence model groups by (delivery_plan_id, target_adapter,
    target_channel) regardless of source.  The latest receipt by
    (attempt_number, sequence, created_at, receipt_id) is chosen — source
    is not used as a tiebreaker.  Tests document this behaviour.
    """

    def test_same_target_replay_and_live_grouped(self) -> None:
        """Replay and live receipts for same target key → one target."""
        summary = build_convergence_summary(
            receipts=[
                _receipt(
                    receipt_id="r-live",
                    status="sent",
                    attempt_number=1,
                    source="live",
                ),
                _receipt(
                    receipt_id="r-replay",
                    status="sent",
                    attempt_number=2,
                    source="replay",
                ),
            ],
        )
        assert summary.total_targets == 1
        # Latest by attempt_number: r-replay (attempt 2)
        assert summary.targets[0].latest_receipt_id == "r-replay"

    def test_different_targets_not_conflated(self) -> None:
        """Different (plan, adapter, channel) triples → separate targets."""
        summary = build_convergence_summary(
            receipts=[
                _receipt(
                    receipt_id="r-a",
                    delivery_plan_id="dp-a",
                    target_adapter="adapter-a",
                    target_channel="ch-1",
                    status="sent",
                    source="live",
                ),
                _receipt(
                    receipt_id="r-b",
                    delivery_plan_id="dp-b",
                    target_adapter="adapter-b",
                    target_channel="ch-2",
                    status="sent",
                    source="replay",
                ),
            ],
        )
        assert summary.total_targets == 2


# ===================================================================
# 7. JSON safety
# ===================================================================


class TestJsonSafety:
    """Full summary survives json.dumps round-trip."""

    def test_to_dict_json_roundtrip(self) -> None:
        """Mixed safe/degraded/inconsistent targets survive JSON round-trip."""
        summary = build_convergence_summary(
            receipts=[
                _receipt(
                    receipt_id="r-safe",
                    delivery_plan_id="dp-safe",
                    target_channel="ch-safe",
                    status="sent",
                ),
                _receipt(
                    receipt_id="r-inc",
                    delivery_plan_id="dp-inc",
                    target_channel="ch-inc",
                    status="sent",
                ),
            ],
            outbox_items=[
                _outbox(
                    outbox_id="ob-safe",
                    delivery_plan_id="dp-safe",
                    target_channel="ch-safe",
                    status="sent",
                ),
                _outbox(
                    outbox_id="ob-inc",
                    delivery_plan_id="dp-inc",
                    target_channel="ch-inc",
                    status="pending",
                ),
            ],
        )
        d = summary.to_dict()
        raw = json.dumps(d)
        reloaded = json.loads(raw)
        assert reloaded["total_targets"] == 2
        assert isinstance(reloaded["severity_counts"], dict)
        assert isinstance(reloaded["targets"], list)
        assert len(reloaded["targets"]) == 2

    def test_no_datetime_objects_in_output(self) -> None:
        """Verify no datetime objects survive in to_dict output."""
        summary = build_convergence_summary(
            receipts=[_receipt(status="sent", created_at=_TS)],
        )
        d = summary.to_dict()
        raw = json.dumps(d)
        # If this succeeds, no datetime objects leaked
        assert isinstance(raw, str)

    def test_target_to_dict_json_safe(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="sent")],
            outbox_items=[_outbox(status="sent")],
        )
        d = summary.targets[0].to_dict()
        raw = json.dumps(d)
        reloaded = json.loads(raw)
        assert reloaded["severity"] == "safe"


# ===================================================================
# 8. All outbox statuses covered
# ===================================================================


class TestAllOutboxStatuses:
    """Every known outbox status produces a valid classification."""

    @staticmethod
    def _classify_outbox_only(status: str) -> str:
        summary = build_convergence_summary(
            outbox_items=[_outbox(status=status)],
        )
        return summary.targets[0].severity

    def test_pending_degraded(self) -> None:
        assert self._classify_outbox_only("pending") == "degraded"

    def test_in_progress_degraded(self) -> None:
        assert self._classify_outbox_only("in_progress") == "degraded"

    def test_queued_degraded(self) -> None:
        assert self._classify_outbox_only("queued") == "degraded"

    def test_retry_wait_degraded(self) -> None:
        assert self._classify_outbox_only("retry_wait") == "degraded"

    def test_sent_safe(self) -> None:
        assert self._classify_outbox_only("sent") == "safe"

    def test_dead_lettered_safe(self) -> None:
        assert self._classify_outbox_only("dead_lettered") == "safe"

    def test_cancelled_safe(self) -> None:
        assert self._classify_outbox_only("cancelled") == "safe"

    def test_abandoned_safe(self) -> None:
        assert self._classify_outbox_only("abandoned") == "safe"


# ===================================================================
# 9. Reserved fields
# ===================================================================


class TestReservedFields:
    """Reserved fields are None until linked to orphan/evidence reports."""

    def test_orphan_count_is_none(self) -> None:
        summary = build_convergence_summary()
        assert summary.orphan_count is None

    def test_evidence_bundle_ref_is_none(self) -> None:
        summary = build_convergence_summary()
        assert summary.evidence_bundle_ref is None

    def test_reserved_in_to_dict(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="sent")],
        )
        d = summary.to_dict()
        assert d["orphan_count"] is None
        assert d["evidence_bundle_ref"] is None


# ===================================================================
# 10. Empty input
# ===================================================================


class TestEmptyInput:
    """No receipts or outbox items → empty safe summary."""

    def test_empty_summary(self) -> None:
        summary = build_convergence_summary()
        assert summary.total_targets == 0
        assert summary.targets == ()
        assert summary.worst_severity is None
        assert summary.severity_counts == {
            "safe": 0,
            "degraded": 0,
            "inconsistent": 0,
        }

    def test_empty_json_safe(self) -> None:
        summary = build_convergence_summary()
        d = summary.to_dict()
        raw = json.dumps(d)
        reloaded = json.loads(raw)
        assert reloaded["total_targets"] == 0


# ===================================================================
# 11. Receipt latest selection — deterministic tiebreaking
# ===================================================================


class TestReceiptLatestSelection:
    """Latest receipt is chosen by (attempt_number, sequence, created_at, receipt_id)."""

    def test_highest_attempt_wins(self) -> None:
        summary = build_convergence_summary(
            receipts=[
                _receipt(receipt_id="r-1", attempt_number=1, status="failed"),
                _receipt(receipt_id="r-2", attempt_number=3, status="sent"),
                _receipt(receipt_id="r-3", attempt_number=2, status="failed"),
            ],
        )
        target = summary.targets[0]
        assert target.latest_receipt_id == "r-2"
        assert target.latest_attempt_number == 3
        assert target.latest_receipt_status == "sent"

    def test_sequence_breaks_attempt_tie(self) -> None:
        summary = build_convergence_summary(
            receipts=[
                _receipt(
                    receipt_id="r-low", attempt_number=2, sequence=1, status="failed"
                ),
                _receipt(
                    receipt_id="r-high", attempt_number=2, sequence=5, status="sent"
                ),
            ],
        )
        target = summary.targets[0]
        assert target.latest_receipt_id == "r-high"

    def test_created_at_breaks_further_tie(self) -> None:
        summary = build_convergence_summary(
            receipts=[
                _receipt(
                    receipt_id="r-early",
                    attempt_number=1,
                    sequence=0,
                    status="queued",
                    created_at=_TS,
                ),
                _receipt(
                    receipt_id="r-late",
                    attempt_number=1,
                    sequence=0,
                    status="sent",
                    created_at=_TS_LATER,
                ),
            ],
        )
        target = summary.targets[0]
        assert target.latest_receipt_id == "r-late"

    def test_receipt_id_final_tiebreaker(self) -> None:
        summary = build_convergence_summary(
            receipts=[
                _receipt(
                    receipt_id="rcpt-aaa",
                    attempt_number=1,
                    sequence=0,
                    status="sent",
                    created_at=_TS,
                ),
                _receipt(
                    receipt_id="rcpt-zzz",
                    attempt_number=1,
                    sequence=0,
                    status="sent",
                    created_at=_TS,
                ),
            ],
        )
        target = summary.targets[0]
        # Lexicographically latest receipt_id wins
        assert target.latest_receipt_id == "rcpt-zzz"


# ===================================================================
# 12. Aggregate severity counts and worst severity
# ===================================================================


class TestAggregation:
    """Severity counts and worst_severity are correct."""

    def test_mixed_severities(self) -> None:
        summary = build_convergence_summary(
            receipts=[
                _receipt(
                    receipt_id="r-safe",
                    delivery_plan_id="dp-safe",
                    target_channel="ch-safe",
                    status="sent",
                ),
                _receipt(
                    receipt_id="r-deg",
                    delivery_plan_id="dp-deg",
                    target_channel="ch-deg",
                    status="queued",
                ),
                _receipt(
                    receipt_id="r-inc",
                    delivery_plan_id="dp-inc",
                    target_channel="ch-inc",
                    status="sent",
                ),
            ],
            outbox_items=[
                _outbox(
                    delivery_plan_id="dp-safe",
                    target_channel="ch-safe",
                    status="sent",
                ),
                _outbox(
                    delivery_plan_id="dp-deg",
                    target_channel="ch-deg",
                    status="pending",
                ),
                _outbox(
                    delivery_plan_id="dp-inc",
                    target_channel="ch-inc",
                    status="pending",
                ),
            ],
        )
        assert summary.severity_counts["safe"] == 1
        assert summary.severity_counts["degraded"] == 1
        assert summary.severity_counts["inconsistent"] == 1
        assert summary.worst_severity == "inconsistent"

    def test_all_safe_worst_safe(self) -> None:
        summary = build_convergence_summary(
            receipts=[_receipt(status="sent")],
            outbox_items=[_outbox(status="sent")],
        )
        assert summary.worst_severity == "safe"

    def test_empty_worst_none(self) -> None:
        summary = build_convergence_summary()
        assert summary.worst_severity is None


# ===================================================================
# 13. Multiple targets with different adapters/channels
# ===================================================================


class TestMultipleTargets:
    """Multiple distinct targets are classified independently."""

    def test_three_targets_three_results(self) -> None:
        summary = build_convergence_summary(
            receipts=[
                _receipt(
                    receipt_id="r-1",
                    delivery_plan_id="dp-1",
                    target_adapter="a1",
                    target_channel="c1",
                    status="sent",
                ),
                _receipt(
                    receipt_id="r-2",
                    delivery_plan_id="dp-2",
                    target_adapter="a2",
                    target_channel="c2",
                    status="queued",
                ),
                _receipt(
                    receipt_id="r-3",
                    delivery_plan_id="dp-3",
                    target_adapter="a3",
                    target_channel="c3",
                    status="sent",
                ),
            ],
            outbox_items=[
                _outbox(
                    outbox_id="ob-1",
                    delivery_plan_id="dp-1",
                    target_adapter="a1",
                    target_channel="c1",
                    status="sent",
                ),
                _outbox(
                    outbox_id="ob-2",
                    delivery_plan_id="dp-2",
                    target_adapter="a2",
                    target_channel="c2",
                    status="pending",
                ),
                _outbox(
                    outbox_id="ob-3",
                    delivery_plan_id="dp-3",
                    target_adapter="a3",
                    target_channel="c3",
                    status="pending",
                ),
            ],
        )
        assert summary.total_targets == 3
        results = {t.delivery_plan_id: t.severity for t in summary.targets}
        assert results["dp-1"] == "safe"
        assert results["dp-2"] == "degraded"
        assert results["dp-3"] == "inconsistent"


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


# ===================================================================
# 23. Unrecognised outbox status → DEGRADED with warning
# ===================================================================


class TestUnrecognisedOutboxStatus:
    """Unknown outbox status strings are classified as DEGRADED with a warning."""

    def test_garbage_outbox_status_with_receipt_is_degraded(self) -> None:
        """An outbox item with status='garbage' and a matching receipt → DEGRADED."""
        summary = build_convergence_summary(
            receipts=[_receipt(status="sent")],
            outbox_items=[_outbox(status="garbage")],
        )
        assert summary.total_targets == 1
        target = summary.targets[0]
        assert target.severity == "degraded"
        assert any("Unrecognised outbox status" in w for w in target.warnings)
        assert any("garbage" in w for w in target.warnings)

    def test_unknown_status_outbox_only_degraded(self) -> None:
        """An outbox-only item with an unrecognised status → degraded (non-terminal path)."""
        summary = build_convergence_summary(
            outbox_items=[_outbox(status="weird_status")],
        )
        target = summary.targets[0]
        assert target.severity == "degraded"


# ===================================================================
# 24. target_channel empty-string vs None grouping
# ===================================================================


class TestTargetChannelGrouping:
    """Empty-string and None target_channel values produce separate targets."""

    def test_empty_string_channel_grouped_separately_from_none(self) -> None:
        """An outbox item with target_channel="" and a receipt with target_channel=None
        for the same plan_id and adapter are NOT grouped together."""
        summary = build_convergence_summary(
            outbox_items=[
                _outbox(
                    outbox_id="ob-empty",
                    delivery_plan_id="dp-1",
                    target_adapter="radio",
                    target_channel="",
                    status="sent",
                ),
            ],
            receipts=[
                _receipt(
                    receipt_id="rcpt-none",
                    delivery_plan_id="dp-1",
                    target_adapter="radio",
                    target_channel=None,
                    status="sent",
                ),
            ],
        )
        assert summary.total_targets == 2
        channels = {t.target_channel for t in summary.targets}
        assert "" in channels
        assert None in channels
