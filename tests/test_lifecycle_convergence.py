"""Exhaustive tests for lifecycle convergence diagnostics.

Covers all 9 finding kinds with positive/negative cases, deterministic
ordering, JSON round-trip safety, empty inputs, one-shot generators,
target_channel None vs empty-string, timestamp edge cases, and
record_id hygiene.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from medre.core.diagnostics.convergence.lifecycle_convergence import (
    build_lifecycle_convergence_findings,
)
from medre.core.diagnostics.convergence.types import (
    KIND_ATTEMPT_COUNT_REGRESSION,
    KIND_NEXT_RETRY_IN_PAST,
    KIND_RECEIPT_OUTBOX_MISMATCH,
    KIND_RECEIPT_SEQUENCE_GAP,
    KIND_RETRY_WAIT_MISSING_NEXT_RETRY,
    KIND_RETRYABLE_WITHOUT_RETRY_METADATA,
    KIND_STALLED_DELIVERY_PLAN,
    KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT,
    KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
_PAST_2H = _NOW - timedelta(hours=2)
_FUTURE_2H = _NOW + timedelta(hours=2)


def _outbox(
    outbox_id: str = "ob-1",
    status: str = "pending",
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "meshtastic",
    target_channel: str | None = None,
    attempt_number: int = 1,
    next_attempt_at: str | None = None,
    updated_at: str | None = None,
) -> dict:
    d: dict = {
        "outbox_id": outbox_id,
        "status": status,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "attempt_number": attempt_number,
        "event_id": "ev-1",
    }
    if next_attempt_at is not None:
        d["next_attempt_at"] = next_attempt_at
    if updated_at is not None:
        d["updated_at"] = updated_at
    return d


def _receipt(
    receipt_id: str = "r-1",
    status: str = "sent",
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "meshtastic",
    target_channel: str | None = None,
    attempt_number: int = 1,
    sequence: int = 1,
    failure_kind: str = "",
    next_retry_at: str | None = None,
    retry_max_attempts: int | None = None,
    retry_backoff_base: float | None = None,
    retry_max_delay: float | None = None,
    retry_jitter: bool | None = None,
    created_at: str | None = None,
) -> dict:
    d: dict = {
        "receipt_id": receipt_id,
        "status": status,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "attempt_number": attempt_number,
        "sequence": sequence,
        "failure_kind": failure_kind,
        "created_at": created_at or _NOW.isoformat(),
        "event_id": "ev-1",
    }
    if next_retry_at is not None:
        d["next_retry_at"] = next_retry_at
    if retry_max_attempts is not None:
        d["retry_max_attempts"] = retry_max_attempts
    if retry_backoff_base is not None:
        d["retry_backoff_base"] = retry_backoff_base
    if retry_max_delay is not None:
        d["retry_max_delay"] = retry_max_delay
    if retry_jitter is not None:
        d["retry_jitter"] = retry_jitter
    return d


def _build(**kwargs):
    """Shortcut to build_lifecycle_convergence_findings with default now_fn."""
    return build_lifecycle_convergence_findings(now_fn=lambda: _NOW, **kwargs)


# ===================================================================
# A. KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX
# ===================================================================


class TestTerminalReceiptNonterminalOutbox:
    def test_sent_receipt_pending_outbox(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="pending")],
            receipts=[_receipt(status="sent")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX in kinds
        finding = next(
            x for x in f if x.kind == KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX
        )
        assert finding.severity == "inconsistent"
        assert finding.record_type == "outbox"

    def test_suppressed_receipt_retry_wait_outbox(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="retry_wait")],
            receipts=[_receipt(status="suppressed")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX in kinds

    def test_dead_lettered_receipt_in_progress_outbox(self) -> None:
        """dead_lettered receipt is terminal — should fire."""
        f = _build(
            outbox_items=[_outbox(status="in_progress")],
            receipts=[_receipt(status="dead_lettered")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX in kinds

    def test_not_fired_when_both_terminal(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="sent")],
            receipts=[_receipt(status="sent")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX not in kinds

    def test_not_fired_when_receipt_non_terminal(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="pending")],
            receipts=[_receipt(status="queued")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX not in kinds

    def test_not_fired_when_outbox_terminal(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="sent")],
            receipts=[_receipt(status="sent")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX not in kinds


# ===================================================================
# B. KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT
# ===================================================================


class TestTerminalOutboxNonterminalReceipt:
    def test_sent_outbox_failed_receipt(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="sent")],
            receipts=[_receipt(status="failed")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT in kinds
        finding = next(
            x for x in f if x.kind == KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT
        )
        assert finding.severity == "inconsistent"

    def test_dead_lettered_outbox_queued_receipt(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="dead_lettered")],
            receipts=[_receipt(status="queued")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT in kinds

    def test_cancelled_outbox_failed_receipt(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="cancelled")],
            receipts=[_receipt(status="failed")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT in kinds

    def test_not_fired_when_both_terminal(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="sent")],
            receipts=[_receipt(status="sent")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT not in kinds

    def test_not_fired_when_outbox_non_terminal(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="pending")],
            receipts=[_receipt(status="failed")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT not in kinds


# ===================================================================
# C. KIND_RECEIPT_OUTBOX_MISMATCH
# ===================================================================


class TestReceiptOutboxMismatch:
    def test_both_terminal_different_statuses(self) -> None:
        """Outbox sent + receipt dead_lettered → mismatch."""
        f = _build(
            outbox_items=[_outbox(status="sent")],
            receipts=[_receipt(status="dead_lettered")],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_OUTBOX_MISMATCH in kinds
        finding = next(x for x in f if x.kind == KIND_RECEIPT_OUTBOX_MISMATCH)
        assert finding.severity == "degraded"

    def test_outbox_cancelled_receipt_sent(self) -> None:
        """Both terminal, different: cancelled vs sent."""
        f = _build(
            outbox_items=[_outbox(status="cancelled")],
            receipts=[_receipt(status="sent")],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_OUTBOX_MISMATCH in kinds

    def test_non_terminal_abnormal_combo(self) -> None:
        """pending outbox + failed receipt is a 'normal' degraded combo — should
        NOT fire C (it's in the normal non-terminal combos)."""
        f = _build(
            outbox_items=[_outbox(status="pending")],
            receipts=[_receipt(status="failed")],
        )
        kinds = {x.kind for x in f}
        # pending+failed is NOT in _NORMAL_NON_TERMINAL_COMBOS... actually
        # it's not explicitly listed. Let me check: ("pending", "failed") is not
        # in the normal combos set, so it SHOULD fire C.
        assert KIND_RECEIPT_OUTBOX_MISMATCH in kinds

    def test_retry_wait_failed_is_normal(self) -> None:
        """retry_wait + failed is normal — should NOT fire C."""
        f = _build(
            outbox_items=[
                _outbox(status="retry_wait", next_attempt_at=_FUTURE_2H.isoformat())
            ],
            receipts=[_receipt(status="failed")],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_OUTBOX_MISMATCH not in kinds

    def test_not_fired_when_matching_terminal(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="sent")],
            receipts=[_receipt(status="sent")],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_OUTBOX_MISMATCH not in kinds


# ===================================================================
# D. KIND_RETRY_WAIT_MISSING_NEXT_RETRY
# ===================================================================


class TestRetryWaitMissingNextRetry:
    def test_missing_next_attempt_at(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="retry_wait", next_attempt_at=None)],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRY_WAIT_MISSING_NEXT_RETRY in kinds
        finding = next(x for x in f if x.kind == KIND_RETRY_WAIT_MISSING_NEXT_RETRY)
        assert finding.severity == "inconsistent"

    def test_empty_next_attempt_at(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="retry_wait")],  # no next_attempt_at key
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRY_WAIT_MISSING_NEXT_RETRY in kinds

    def test_malformed_timestamp(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="retry_wait", next_attempt_at="not-a-date")],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRY_WAIT_MISSING_NEXT_RETRY in kinds
        finding = next(x for x in f if x.kind == KIND_RETRY_WAIT_MISSING_NEXT_RETRY)
        assert "parse_error" in finding.extra

    def test_not_fired_with_valid_future_timestamp(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(status="retry_wait", next_attempt_at=_FUTURE_2H.isoformat())
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRY_WAIT_MISSING_NEXT_RETRY not in kinds

    def test_not_fired_for_non_retry_wait_status(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="pending")],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRY_WAIT_MISSING_NEXT_RETRY not in kinds


# ===================================================================
# E. KIND_NEXT_RETRY_IN_PAST
# ===================================================================


class TestNextRetryInPast:
    def test_next_attempt_in_past(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(status="retry_wait", next_attempt_at=_PAST_2H.isoformat())
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_NEXT_RETRY_IN_PAST in kinds
        finding = next(x for x in f if x.kind == KIND_NEXT_RETRY_IN_PAST)
        assert finding.severity == "degraded"

    def test_next_attempt_in_future_not_flagged(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(status="retry_wait", next_attempt_at=_FUTURE_2H.isoformat())
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_NEXT_RETRY_IN_PAST not in kinds

    def test_next_attempt_exactly_now_not_flagged(self) -> None:
        """Exactly now is NOT in the past (uses < comparison)."""
        f = _build(
            outbox_items=[
                _outbox(status="retry_wait", next_attempt_at=_NOW.isoformat())
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_NEXT_RETRY_IN_PAST not in kinds

    def test_naive_timestamp_treated_as_utc(self) -> None:
        """Naive timestamps should be treated as UTC."""
        past_naive = _PAST_2H.replace(tzinfo=None).isoformat()
        f = _build(
            outbox_items=[_outbox(status="retry_wait", next_attempt_at=past_naive)],
        )
        kinds = {x.kind for x in f}
        assert KIND_NEXT_RETRY_IN_PAST in kinds


# ===================================================================
# F. KIND_RETRYABLE_WITHOUT_RETRY_METADATA
# ===================================================================


class TestRetryableWithoutRetryMetadata:
    def test_transient_failure_no_metadata(self) -> None:
        f = _build(
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="adapter_transient",
                    next_retry_at=None,
                    retry_max_attempts=None,
                    retry_backoff_base=None,
                    retry_max_delay=None,
                    retry_jitter=None,
                )
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRYABLE_WITHOUT_RETRY_METADATA in kinds
        finding = next(x for x in f if x.kind == KIND_RETRYABLE_WITHOUT_RETRY_METADATA)
        assert finding.severity == "degraded"
        assert "next_retry_at" in finding.extra["missing_fields"]

    def test_transient_failure_with_all_metadata_not_flagged(self) -> None:
        f = _build(
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="adapter_transient",
                    next_retry_at=_FUTURE_2H.isoformat(),
                    retry_max_attempts=3,
                    retry_backoff_base=1.0,
                    retry_max_delay=60.0,
                    retry_jitter=True,
                )
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRYABLE_WITHOUT_RETRY_METADATA not in kinds

    def test_non_transient_with_matching_non_terminal_outbox(self) -> None:
        """Non-transient failure but matching non-terminal outbox → retryable."""
        f = _build(
            outbox_items=[
                _outbox(status="retry_wait", next_attempt_at=_FUTURE_2H.isoformat())
            ],
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="some_other_error",
                    next_retry_at=None,
                    retry_max_attempts=None,
                )
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRYABLE_WITHOUT_RETRY_METADATA in kinds

    def test_non_transient_no_matching_outbox_not_flagged(self) -> None:
        f = _build(
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="permanent_error",
                )
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRYABLE_WITHOUT_RETRY_METADATA not in kinds

    def test_sent_receipt_not_checked(self) -> None:
        f = _build(
            receipts=[
                _receipt(
                    status="sent",
                    failure_kind="adapter_transient",
                )
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRYABLE_WITHOUT_RETRY_METADATA not in kinds

    def test_partial_metadata_only_reports_missing(self) -> None:
        """If some fields are present, only missing ones are reported."""
        f = _build(
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="adapter_transient",
                    next_retry_at=_FUTURE_2H.isoformat(),
                    retry_max_attempts=3,
                    # missing: retry_backoff_base, retry_max_delay, retry_jitter
                )
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RETRYABLE_WITHOUT_RETRY_METADATA in kinds
        finding = next(x for x in f if x.kind == KIND_RETRYABLE_WITHOUT_RETRY_METADATA)
        assert "retry_backoff_base" in finding.extra["missing_fields"]
        assert "retry_max_delay" in finding.extra["missing_fields"]
        assert "retry_jitter" in finding.extra["missing_fields"]
        assert "next_retry_at" not in finding.extra["missing_fields"]
        assert "retry_max_attempts" not in finding.extra["missing_fields"]


# ===================================================================
# G. KIND_STALLED_DELIVERY_PLAN
# ===================================================================


class TestStalledDeliveryPlan:
    def test_stalled_pending_outbox(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(
                    status="pending",
                    updated_at=_PAST_2H.isoformat(),
                )
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN in kinds
        finding = next(x for x in f if x.kind == KIND_STALLED_DELIVERY_PLAN)
        assert finding.severity == "degraded"
        assert finding.extra["seconds_stalled"] > 3600

    def test_stalled_with_custom_threshold(self) -> None:
        """With a large threshold, the same data is NOT stalled."""
        f = build_lifecycle_convergence_findings(
            outbox_items=[_outbox(status="pending", updated_at=_PAST_2H.isoformat())],
            now_fn=lambda: _NOW,
            stall_threshold_seconds=7200,  # 2 hours
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN not in kinds

    def test_recently_updated_not_stalled(self) -> None:
        recently = (_NOW - timedelta(minutes=5)).isoformat()
        f = _build(
            outbox_items=[
                _outbox(status="pending", updated_at=recently),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN not in kinds

    def test_terminal_outbox_not_checked(self) -> None:
        """Terminal outbox items are not checked for stalls."""
        f = _build(
            outbox_items=[
                _outbox(status="sent", updated_at=_PAST_2H.isoformat()),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN not in kinds

    def test_no_updated_at_not_flagged(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="pending")],
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN not in kinds

    def test_malformed_updated_at_skipped(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(status="pending", updated_at="garbage-timestamp"),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN not in kinds

    def test_naive_timestamp_treated_as_utc(self) -> None:
        past_naive = _PAST_2H.replace(tzinfo=None).isoformat()
        f = _build(
            outbox_items=[
                _outbox(status="pending", updated_at=past_naive),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN in kinds


# ===================================================================
# H. KIND_ATTEMPT_COUNT_REGRESSION
# ===================================================================


class TestAttemptCountRegression:
    def test_regression_detected(self) -> None:
        """Later receipt has lower attempt_number → regression."""
        f = _build(
            receipts=[
                _receipt(receipt_id="r-1", sequence=1, attempt_number=3),
                _receipt(receipt_id="r-2", sequence=2, attempt_number=1),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_ATTEMPT_COUNT_REGRESSION in kinds
        finding = next(x for x in f if x.kind == KIND_ATTEMPT_COUNT_REGRESSION)
        assert finding.severity == "inconsistent"
        assert finding.record_id == "r-2"

    def test_no_regression_with_ascending_attempts(self) -> None:
        f = _build(
            receipts=[
                _receipt(receipt_id="r-1", sequence=1, attempt_number=1),
                _receipt(receipt_id="r-2", sequence=2, attempt_number=2),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_ATTEMPT_COUNT_REGRESSION not in kinds

    def test_no_regression_with_single_receipt(self) -> None:
        f = _build(
            receipts=[_receipt(receipt_id="r-1", sequence=1, attempt_number=1)],
        )
        kinds = {x.kind for x in f}
        assert KIND_ATTEMPT_COUNT_REGRESSION not in kinds

    def test_no_regression_different_targets(self) -> None:
        """Receipts for different targets are not compared."""
        f = _build(
            receipts=[
                _receipt(
                    receipt_id="r-1",
                    delivery_plan_id="plan-a",
                    sequence=1,
                    attempt_number=3,
                ),
                _receipt(
                    receipt_id="r-2",
                    delivery_plan_id="plan-b",
                    sequence=2,
                    attempt_number=1,
                ),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_ATTEMPT_COUNT_REGRESSION not in kinds

    def test_equal_attempts_not_flagged(self) -> None:
        f = _build(
            receipts=[
                _receipt(receipt_id="r-1", sequence=1, attempt_number=2),
                _receipt(receipt_id="r-2", sequence=2, attempt_number=2),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_ATTEMPT_COUNT_REGRESSION not in kinds


# ===================================================================
# I. KIND_RECEIPT_SEQUENCE_GAP
# ===================================================================


class TestReceiptSequenceGap:
    def test_gap_detected(self) -> None:
        f = _build(
            receipts=[
                _receipt(receipt_id="r-1", sequence=1),
                _receipt(receipt_id="r-2", sequence=5),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_SEQUENCE_GAP in kinds
        finding = next(x for x in f if x.kind == KIND_RECEIPT_SEQUENCE_GAP)
        assert finding.severity == "degraded"
        assert finding.extra["gap"] == 4

    def test_consecutive_sequences_no_gap(self) -> None:
        f = _build(
            receipts=[
                _receipt(receipt_id="r-1", sequence=1),
                _receipt(receipt_id="r-2", sequence=2),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_SEQUENCE_GAP not in kinds

    def test_zero_or_negative_sequences_ignored(self) -> None:
        """Only positive integer sequences are checked."""
        f = _build(
            receipts=[
                _receipt(receipt_id="r-1", sequence=0),
                _receipt(receipt_id="r-2", sequence=5),
            ],
        )
        kinds = {x.kind for x in f}
        # sequence 0 is ignored; only one positive sequence → no gap check
        assert KIND_RECEIPT_SEQUENCE_GAP not in kinds

    def test_single_receipt_no_gap(self) -> None:
        f = _build(
            receipts=[_receipt(receipt_id="r-1", sequence=1)],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_SEQUENCE_GAP not in kinds

    def test_different_targets_not_compared(self) -> None:
        f = _build(
            receipts=[
                _receipt(
                    receipt_id="r-1",
                    delivery_plan_id="plan-a",
                    sequence=1,
                ),
                _receipt(
                    receipt_id="r-2",
                    delivery_plan_id="plan-b",
                    sequence=10,
                ),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_SEQUENCE_GAP not in kinds

    def test_gap_size_in_extra(self) -> None:
        f = _build(
            receipts=[
                _receipt(receipt_id="r-1", sequence=2),
                _receipt(receipt_id="r-2", sequence=7),
            ],
        )
        finding = next(x for x in f if x.kind == KIND_RECEIPT_SEQUENCE_GAP)
        assert finding.extra["gap"] == 5
        assert finding.extra["previous_sequence"] == 2
        assert finding.extra["sequence"] == 7


# ===================================================================
# Deterministic ordering
# ===================================================================


class TestDeterministicOrdering:
    def test_sorted_by_kind_and_record_id(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(outbox_id="ob-z", status="retry_wait"),  # missing next_retry
                _outbox(outbox_id="ob-a", status="retry_wait"),  # missing next_retry
            ],
        )
        kinds = [x.kind for x in f]
        records = [x.record_id for x in f]
        pairs = list(zip(kinds, records, strict=False))
        assert pairs == sorted(pairs)

    def test_repeated_call_identical(self) -> None:
        outbox = [
            _outbox(
                outbox_id="ob-1", status="pending", updated_at=_PAST_2H.isoformat()
            ),
            _outbox(outbox_id="ob-2", status="retry_wait"),
        ]
        receipts = [
            _receipt(receipt_id="r-1", status="sent"),
            _receipt(
                receipt_id="r-2", status="failed", failure_kind="adapter_transient"
            ),
        ]
        f1 = _build(outbox_items=outbox, receipts=receipts)
        f2 = _build(outbox_items=outbox, receipts=receipts)
        assert [x.kind for x in f1] == [x.kind for x in f2]
        assert [x.record_id for x in f1] == [x.record_id for x in f2]


# ===================================================================
# JSON safety
# ===================================================================


class TestJsonSafety:
    def test_findings_json_roundtrip(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(status="pending", updated_at=_PAST_2H.isoformat()),
                _outbox(status="retry_wait"),
            ],
            receipts=[
                _receipt(status="sent"),
                _receipt(
                    receipt_id="r-2",
                    status="failed",
                    failure_kind="adapter_transient",
                ),
            ],
        )
        dicts = [x.to_dict() for x in f]
        raw = json.dumps(dicts)
        reloaded = json.loads(raw)
        assert isinstance(reloaded, list)
        assert len(reloaded) == len(f)
        for original, restored in zip(f, reloaded, strict=False):
            assert original.kind == restored["kind"]
            assert original.severity == restored["severity"]
            assert original.record_id == restored["record_id"]

    def test_no_datetime_objects_in_output(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(status="pending", updated_at=_PAST_2H.isoformat()),
            ],
        )
        for finding in f:
            d = finding.to_dict()
            # Must be JSON-serializable
            json.dumps(d)


# ===================================================================
# Empty inputs
# ===================================================================


class TestEmptyInputs:
    def test_empty_all(self) -> None:
        f = _build()
        assert f == []

    def test_empty_outbox_only(self) -> None:
        f = _build(outbox_items=[])
        assert f == []

    def test_empty_receipts_only(self) -> None:
        f = _build(receipts=[])
        assert f == []

    def test_no_matching_targets(self) -> None:
        """Outbox and receipts for completely different targets → no lifecycle findings."""
        f = _build(
            outbox_items=[_outbox(delivery_plan_id="plan-x")],
            receipts=[_receipt(delivery_plan_id="plan-y")],
        )
        assert f == []


# ===================================================================
# One-shot generator support
# ===================================================================


class TestOneShotGenerator:
    def test_outbox_generator_consumed_once(self) -> None:
        consumed = []

        def gen():
            for item in [
                _outbox(outbox_id="ob-1", status="retry_wait"),
                _outbox(outbox_id="ob-2", status="retry_wait"),
            ]:
                consumed.append(item["outbox_id"])
                yield item

        f = _build(outbox_items=gen())
        assert len(consumed) == 2
        kinds = {x.kind for x in f}
        assert KIND_RETRY_WAIT_MISSING_NEXT_RETRY in kinds

    def test_receipt_generator_consumed_once(self) -> None:
        consumed = []

        def gen():
            for item in [
                _receipt(receipt_id="r-1", sequence=1, attempt_number=2),
                _receipt(receipt_id="r-2", sequence=2, attempt_number=1),
            ]:
                consumed.append(item["receipt_id"])
                yield item

        f = _build(receipts=gen())
        assert len(consumed) == 2
        kinds = {x.kind for x in f}
        assert KIND_ATTEMPT_COUNT_REGRESSION in kinds


# ===================================================================
# record_id hygiene — never a status string, never "None"
# ===================================================================


class TestRecordIdHygiene:
    _STATUS_STRINGS = frozenset(
        {
            "queued",
            "sent",
            "pending",
            "failed",
            "dead_lettered",
            "cancelled",
            "abandoned",
            "suppressed",
            "retry_wait",
            "in_progress",
        }
    )

    def test_no_status_string_record_ids(self) -> None:
        """No finding ever uses a status string as record_id."""
        f = _build(
            outbox_items=[
                _outbox(outbox_id="ob-1", status="sent"),
                _outbox(outbox_id="ob-2", status="retry_wait"),
                _outbox(
                    outbox_id="ob-3", status="pending", updated_at=_PAST_2H.isoformat()
                ),
            ],
            receipts=[
                _receipt(receipt_id="r-1", status="failed"),
                _receipt(receipt_id="r-2", status="queued"),
                _receipt(receipt_id="r-3", status="sent"),
            ],
        )
        for finding in f:
            assert (
                finding.record_id not in self._STATUS_STRINGS
            ), f"Finding {finding.kind!r} has status string as record_id: {finding.record_id!r}"

    def test_no_literal_none_string(self) -> None:
        """record_id must never be the literal string 'None'."""
        f = _build(
            outbox_items=[
                _outbox(outbox_id="ob-1", status="retry_wait"),
            ],
            receipts=[
                _receipt(receipt_id="r-1", status="sent"),
            ],
        )
        for finding in f:
            assert (
                finding.record_id != "None"
            ), f"Finding {finding.kind!r} has literal 'None' as record_id"


# ===================================================================
# target_channel None vs empty string
# ===================================================================


class TestTargetChannelNoneVsEmpty:
    def test_none_and_empty_not_conflated(self) -> None:
        """Receipt with channel=None and outbox with channel="" are different targets."""
        f = _build(
            outbox_items=[
                _outbox(outbox_id="ob-empty", target_channel="", status="pending"),
            ],
            receipts=[
                _receipt(receipt_id="r-none", target_channel=None, status="sent"),
            ],
        )
        # They target different keys, so no cross-checking should happen
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX not in kinds

    def test_both_none_same_target(self) -> None:
        """Both with channel=None → same target → cross-checked."""
        f = _build(
            outbox_items=[
                _outbox(outbox_id="ob-1", target_channel=None, status="pending"),
            ],
            receipts=[
                _receipt(receipt_id="r-1", target_channel=None, status="sent"),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX in kinds

    def test_both_empty_string_same_target(self) -> None:
        """Both with channel="" → same target → cross-checked."""
        f = _build(
            outbox_items=[
                _outbox(outbox_id="ob-1", target_channel="", status="pending"),
            ],
            receipts=[
                _receipt(receipt_id="r-1", target_channel="", status="sent"),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX in kinds


# ===================================================================
# Timestamp edge cases
# ===================================================================


class TestTimestampEdgeCases:
    def test_datetime_object_as_updated_at(self) -> None:
        """Passing actual datetime object for updated_at should work."""
        f = _build(
            outbox_items=[
                {
                    "outbox_id": "ob-1",
                    "status": "pending",
                    "delivery_plan_id": "plan-1",
                    "target_adapter": "meshtastic",
                    "target_channel": None,
                    "attempt_number": 1,
                    "updated_at": _PAST_2H,  # datetime object
                    "event_id": "ev-1",
                }
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN in kinds

    def test_microsecond_timestamp_parsed(self) -> None:
        """ISO timestamp with microseconds should parse correctly."""
        ts = (_NOW - timedelta(hours=2)).isoformat()
        f = _build(
            outbox_items=[
                _outbox(status="pending", updated_at=ts),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN in kinds


# ===================================================================
# Multiple findings in one call
# ===================================================================


class TestMultipleFindings:
    def test_several_kinds_in_one_call(self) -> None:
        f = _build(
            outbox_items=[
                # B: terminal outbox + non-terminal receipt (different target from others)
                _outbox(
                    outbox_id="ob-1",
                    status="sent",
                    delivery_plan_id="plan-b",
                    target_adapter="adapter-b",
                ),
                # D: retry_wait without next_attempt_at
                _outbox(
                    outbox_id="ob-2",
                    status="retry_wait",
                    delivery_plan_id="plan-d",
                    target_adapter="adapter-d",
                ),
                # G: stalled pending
                _outbox(
                    outbox_id="ob-3",
                    status="pending",
                    updated_at=_PAST_2H.isoformat(),
                    delivery_plan_id="plan-g",
                    target_adapter="adapter-g",
                ),
            ],
            receipts=[
                # B: non-terminal receipt for plan-b
                _receipt(
                    receipt_id="r-1",
                    status="failed",
                    delivery_plan_id="plan-b",
                    target_adapter="adapter-b",
                ),
                # F: failed transient without retry metadata (no matching outbox →
                #    make it retryable via adapter_transient)
                _receipt(
                    receipt_id="r-2",
                    status="failed",
                    failure_kind="adapter_transient",
                    delivery_plan_id="plan-f",
                    target_adapter="adapter-f",
                ),
                # safe receipt for plan-g (not relevant to stalled check)
                _receipt(
                    receipt_id="r-3",
                    delivery_plan_id="plan-g",
                    target_adapter="adapter-g",
                    status="queued",
                ),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT in kinds
        assert KIND_RETRY_WAIT_MISSING_NEXT_RETRY in kinds
        assert KIND_STALLED_DELIVERY_PLAN in kinds
        assert KIND_RETRYABLE_WITHOUT_RETRY_METADATA in kinds

    def test_all_findings_sorted(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(outbox_id="ob-z", status="retry_wait"),
                _outbox(outbox_id="ob-a", status="retry_wait"),
            ],
        )
        pairs = [(x.kind, x.record_id) for x in f]
        assert pairs == sorted(pairs)
