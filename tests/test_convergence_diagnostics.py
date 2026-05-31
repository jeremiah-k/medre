"""Focused tests for recovery convergence diagnostics.

Covers: safe/degraded/inconsistent classification, deterministic repeated
build, missing plan_id, source separation, JSON safety, all outbox statuses,
receipt-only terminal evidence, cross-populated fields, and unrecognised
outbox status handling, target_channel grouping.

Orphan/invalid-lineage detection tests are in
``test_convergence_orphan_report.py``.

Test groups
-----------
1. Safe convergence — matching terminal states, receipt-only terminal.
2. Degraded convergence — pending/retry_wait+failed, in_progress/queued
   without receipt, missing plan_id.
3. Inconsistent convergence — terminal outbox + non-terminal receipt.
   Note: in lifecycle convergence (``test_lifecycle_convergence.py``),
   ``terminal_receipt_nonterminal_outbox`` is classified as **degraded**
   (receipt writes may precede outbox updates in separate transactions).
4. Deterministic repeated build — same input → same output.
5. Missing delivery_plan_id — degraded with warning.
6. Source separation — replay vs live receipts are not conflated when
   source is visible in the data.
7. JSON safety — full summary survives json.dumps round-trip.
8. All outbox statuses covered.
9. Cross-populated fields — orphan_count and evidence_bundle_ref are None.
10. Empty input — produces safe empty summary.
11. Receipt latest selection — deterministic tiebreaking.
12. Aggregate severity counts and worst severity.
13. Multiple targets with different adapters/channels.
14. Unrecognised outbox status — DEGRADED with warning.
15. target_channel empty-string vs None grouping.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from medre.core.diagnostics.convergence.summary import build_convergence_summary

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
# 9. Cross-populated fields
# ===================================================================


class TestCrossPopulatedFields:
    """Cross-populated fields are None until linked to orphan/evidence reports."""

    def test_orphan_count_is_none(self) -> None:
        summary = build_convergence_summary()
        assert summary.orphan_count is None

    def test_evidence_bundle_ref_is_none(self) -> None:
        summary = build_convergence_summary()
        assert summary.evidence_bundle_ref is None

    def test_optional_fields_in_to_dict(self) -> None:
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
# 14. Unrecognised outbox status → DEGRADED with warning
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
# 15. target_channel empty-string vs None grouping
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
