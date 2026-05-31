"""Conformance tests for lifecycle convergence diagnostics.

Verifies the public contract of ``build_lifecycle_convergence_findings`` and
its output ``OrphanFinding`` objects:
- Each finding kind can be emitted as an ``OrphanFinding``.
- Severity values use exactly the ``safe`` / ``degraded`` / ``inconsistent``
  vocabulary.
- ``to_dict()`` output JSON round-trips.
- Deterministic repeated output.
- Read-only behaviour (no mutation of inputs).

These tests characterise representative examples — exhaustive positive/negative
cases are in ``tests/test_lifecycle_convergence.py``.
"""

from __future__ import annotations

import copy
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
    OrphanFinding,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
_PAST_2H = _NOW - timedelta(hours=2)
_FUTURE_2H = _NOW + timedelta(hours=2)
_VALID_SEVERITIES = frozenset({"safe", "degraded", "inconsistent"})


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
    return build_lifecycle_convergence_findings(now_fn=lambda: _NOW, **kwargs)


# ---------------------------------------------------------------------------
# Finding kind emission — every kind can be produced
# ---------------------------------------------------------------------------


class TestEveryKindEmittable:
    """Each of the 9 lifecycle finding kinds SHALL be producible as OrphanFinding."""

    def test_kind_terminal_receipt_nonterminal_outbox(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="pending")],
            receipts=[_receipt(status="sent")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX in kinds
        finding = next(
            x for x in f if x.kind == KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX
        )
        assert isinstance(finding, OrphanFinding)

    def test_kind_terminal_outbox_nonterminal_receipt(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="sent")],
            receipts=[_receipt(status="failed")],
        )
        kinds = {x.kind for x in f}
        assert KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT in kinds
        finding = next(
            x for x in f if x.kind == KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT
        )
        assert isinstance(finding, OrphanFinding)

    def test_kind_receipt_outbox_mismatch(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="sent")],
            receipts=[_receipt(status="dead_lettered")],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_OUTBOX_MISMATCH in kinds
        finding = next(x for x in f if x.kind == KIND_RECEIPT_OUTBOX_MISMATCH)
        assert isinstance(finding, OrphanFinding)

    def test_kind_retry_wait_missing_next_retry(self) -> None:
        f = _build(outbox_items=[_outbox(status="retry_wait")])
        kinds = {x.kind for x in f}
        assert KIND_RETRY_WAIT_MISSING_NEXT_RETRY in kinds
        finding = next(x for x in f if x.kind == KIND_RETRY_WAIT_MISSING_NEXT_RETRY)
        assert isinstance(finding, OrphanFinding)

    def test_kind_next_retry_in_past(self) -> None:
        f = _build(
            outbox_items=[
                _outbox(status="retry_wait", next_attempt_at=_PAST_2H.isoformat())
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_NEXT_RETRY_IN_PAST in kinds
        finding = next(x for x in f if x.kind == KIND_NEXT_RETRY_IN_PAST)
        assert isinstance(finding, OrphanFinding)

    def test_kind_retryable_without_retry_metadata(self) -> None:
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
        assert isinstance(finding, OrphanFinding)

    def test_kind_stalled_delivery_plan(self) -> None:
        f = _build(
            outbox_items=[_outbox(status="pending", updated_at=_PAST_2H.isoformat())],
        )
        kinds = {x.kind for x in f}
        assert KIND_STALLED_DELIVERY_PLAN in kinds
        finding = next(x for x in f if x.kind == KIND_STALLED_DELIVERY_PLAN)
        assert isinstance(finding, OrphanFinding)

    def test_kind_attempt_count_regression(self) -> None:
        f = _build(
            receipts=[
                _receipt(receipt_id="r-1", sequence=1, attempt_number=3),
                _receipt(receipt_id="r-2", sequence=2, attempt_number=1),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_ATTEMPT_COUNT_REGRESSION in kinds
        finding = next(x for x in f if x.kind == KIND_ATTEMPT_COUNT_REGRESSION)
        assert isinstance(finding, OrphanFinding)

    def test_kind_receipt_sequence_gap(self) -> None:
        f = _build(
            receipts=[
                _receipt(receipt_id="r-1", sequence=1),
                _receipt(receipt_id="r-2", sequence=5),
            ],
        )
        kinds = {x.kind for x in f}
        assert KIND_RECEIPT_SEQUENCE_GAP in kinds
        finding = next(x for x in f if x.kind == KIND_RECEIPT_SEQUENCE_GAP)
        assert isinstance(finding, OrphanFinding)


# ---------------------------------------------------------------------------
# Severity vocabulary
# ---------------------------------------------------------------------------


class TestSeverityVocabulary:
    """All finding severities SHALL be from the safe/degraded/inconsistent set."""

    @staticmethod
    def _all_findings():
        """Produce findings covering multiple kinds for severity check."""
        return _build(
            outbox_items=[
                _outbox(status="pending"),
                _outbox(status="sent"),
                _outbox(outbox_id="ob-2", status="retry_wait"),
                _outbox(
                    outbox_id="ob-3",
                    status="pending",
                    updated_at=_PAST_2H.isoformat(),
                ),
                _outbox(
                    outbox_id="ob-4",
                    status="sent",
                    delivery_plan_id="plan-b",
                ),
                _outbox(
                    outbox_id="ob-5",
                    status="dead_lettered",
                    delivery_plan_id="plan-c",
                ),
            ],
            receipts=[
                _receipt(status="sent"),
                _receipt(
                    receipt_id="r-2",
                    status="failed",
                    delivery_plan_id="plan-b",
                ),
                _receipt(
                    receipt_id="r-3",
                    status="queued",
                    delivery_plan_id="plan-c",
                ),
                _receipt(
                    receipt_id="r-4",
                    status="failed",
                    failure_kind="adapter_transient",
                    delivery_plan_id="plan-d",
                    next_retry_at=None,
                    retry_max_attempts=None,
                    retry_backoff_base=None,
                    retry_max_delay=None,
                    retry_jitter=None,
                ),
                _receipt(
                    receipt_id="r-5",
                    sequence=1,
                    attempt_number=2,
                    delivery_plan_id="plan-e",
                ),
                _receipt(
                    receipt_id="r-6",
                    sequence=2,
                    attempt_number=1,
                    delivery_plan_id="plan-e",
                ),
            ],
        )

    def test_all_severities_are_valid_vocabulary(self) -> None:
        for finding in self._all_findings():
            assert finding.severity in _VALID_SEVERITIES, (
                f"Finding {finding.kind!r} has invalid severity "
                f"{finding.severity!r}; expected one of {sorted(_VALID_SEVERITIES)}"
            )

    def test_no_safe_severity_in_lifecycle_findings(self) -> None:
        """Lifecycle convergence findings represent anomalies, never 'safe'."""
        for finding in self._all_findings():
            assert finding.severity != "safe", (
                f"Lifecycle finding {finding.kind!r} should not have "
                f"severity 'safe' — lifecycle findings are anomalies"
            )


# ---------------------------------------------------------------------------
# JSON round-trip safety
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    """to_dict() output SHALL JSON round-trip without error."""

    def test_all_findings_json_roundtrip(self) -> None:
        findings = _build(
            outbox_items=[
                _outbox(status="pending"),
                _outbox(outbox_id="ob-2", status="retry_wait"),
                _outbox(
                    outbox_id="ob-3",
                    status="pending",
                    updated_at=_PAST_2H.isoformat(),
                ),
            ],
            receipts=[
                _receipt(status="sent"),
                _receipt(
                    receipt_id="r-2",
                    status="failed",
                    failure_kind="adapter_transient",
                    next_retry_at=None,
                    retry_max_attempts=None,
                ),
            ],
        )
        for finding in findings:
            d = finding.to_dict()
            raw = json.dumps(d)
            reloaded = json.loads(raw)
            assert isinstance(reloaded, dict)
            assert reloaded["kind"] == finding.kind
            assert reloaded["severity"] == finding.severity
            assert reloaded["record_id"] == finding.record_id
            assert reloaded["record_type"] == finding.record_type
            assert reloaded["details"] == finding.details
            assert isinstance(reloaded["extra"], dict)

    def test_list_of_findings_json_roundtrip(self) -> None:
        """A list of findings serialises and deserialises cleanly."""
        findings = _build(
            outbox_items=[
                _outbox(status="pending"),
                _outbox(outbox_id="ob-2", status="sent"),
            ],
            receipts=[
                _receipt(status="sent"),
                _receipt(
                    receipt_id="r-2",
                    status="failed",
                    delivery_plan_id="plan-1",
                ),
            ],
        )
        dicts = [f.to_dict() for f in findings]
        raw = json.dumps(dicts)
        reloaded = json.loads(raw)
        assert len(reloaded) == len(findings)
        for original, restored in zip(findings, reloaded, strict=False):
            assert original.kind == restored["kind"]

    def test_no_datetime_objects_in_to_dict(self) -> None:
        """to_dict() must not contain datetime objects (JSON-incompatible)."""
        findings = _build(
            outbox_items=[
                _outbox(status="pending", updated_at=_PAST_2H.isoformat()),
                _outbox(status="retry_wait", next_attempt_at=_PAST_2H.isoformat()),
            ],
        )
        for finding in findings:
            d = finding.to_dict()
            json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# Deterministic reporting
# ---------------------------------------------------------------------------


class TestDeterministicReporting:
    """Repeated calls with identical inputs SHALL produce identical output."""

    def test_identical_inputs_identical_output(self) -> None:
        outbox = [
            _outbox(
                outbox_id="ob-1", status="pending", updated_at=_PAST_2H.isoformat()
            ),
            _outbox(outbox_id="ob-2", status="retry_wait"),
        ]
        receipts = [
            _receipt(receipt_id="r-1", status="sent"),
            _receipt(
                receipt_id="r-2",
                status="failed",
                failure_kind="adapter_transient",
            ),
        ]
        f1 = _build(outbox_items=outbox, receipts=receipts)
        f2 = _build(outbox_items=outbox, receipts=receipts)
        assert [x.kind for x in f1] == [x.kind for x in f2]
        assert [x.record_id for x in f1] == [x.record_id for x in f2]
        assert [x.severity for x in f1] == [x.severity for x in f2]
        assert [x.to_dict() for x in f1] == [x.to_dict() for x in f2]

    def test_three_repeated_calls_identical(self) -> None:
        outbox = [_outbox(status="retry_wait")]
        f1 = _build(outbox_items=outbox)
        f2 = _build(outbox_items=outbox)
        f3 = _build(outbox_items=outbox)
        assert [x.to_dict() for x in f1] == [x.to_dict() for x in f2]
        assert [x.to_dict() for x in f2] == [x.to_dict() for x in f3]


# ---------------------------------------------------------------------------
# Read-only / no mutation
# ---------------------------------------------------------------------------


class TestReadOnlyBehavior:
    """build_lifecycle_convergence_findings SHALL NOT mutate its inputs."""

    def test_outbox_items_not_mutated(self) -> None:
        items = [_outbox(status="pending"), _outbox(status="retry_wait")]
        before = copy.deepcopy(items)
        _build(outbox_items=items)
        assert items == before

    def test_receipts_not_mutated(self) -> None:
        receipts = [
            _receipt(status="sent"),
            _receipt(status="failed", failure_kind="adapter_transient"),
        ]
        before = copy.deepcopy(receipts)
        _build(receipts=receipts)
        assert receipts == before

    def test_outbox_dict_values_not_mutated(self) -> None:
        """Individual dict values inside outbox items must be unchanged."""
        item = {
            "outbox_id": "ob-1",
            "status": "pending",
            "delivery_plan_id": "plan-1",
            "target_adapter": "meshtastic",
            "target_channel": None,
            "attempt_number": 1,
            "event_id": "ev-1",
            "updated_at": _PAST_2H.isoformat(),
        }
        original_status = item["status"]
        original_id = item["outbox_id"]
        _build(outbox_items=[item])
        assert item["status"] == original_status
        assert item["outbox_id"] == original_id

    def test_receipt_dict_values_not_mutated(self) -> None:
        rec = {
            "receipt_id": "r-1",
            "status": "sent",
            "delivery_plan_id": "plan-1",
            "target_adapter": "meshtastic",
            "target_channel": None,
            "attempt_number": 1,
            "sequence": 1,
            "failure_kind": "",
            "created_at": _NOW.isoformat(),
            "event_id": "ev-1",
        }
        original_status = rec["status"]
        _build(receipts=[rec])
        assert rec["status"] == original_status

    def test_tuple_input_preserved(self) -> None:
        """Tuples are immutable — function must not try to mutate them."""
        items = (
            {
                "outbox_id": "ob-1",
                "status": "pending",
                "delivery_plan_id": "plan-1",
                "target_adapter": "meshtastic",
                "target_channel": None,
                "attempt_number": 1,
                "event_id": "ev-1",
            },
        )
        before = copy.deepcopy(items)
        _build(outbox_items=items)
        assert items == before
