"""Tests for recovery convergence findings.

Covers all 4 new finding kinds:
- recovered_not_progressed
- repeatedly_reclaimed
- reclaimed_then_terminal
- reclaimed_then_orphaned
"""

from __future__ import annotations

import json

from medre.core.diagnostics.convergence.recovery_convergence import (
    build_recovery_convergence_findings,
)
from medre.core.diagnostics.convergence.types import (
    KIND_RECLAIMED_THEN_ORPHANED,
    KIND_RECLAIMED_THEN_TERMINAL,
    KIND_RECOVERED_NOT_PROGRESSED,
    KIND_REPEATEDLY_RECLAIMED,
)
from medre.core.recovery.models import (
    RecoveryOwnershipAction,
    StartupRecoveryLedger,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outbox(
    outbox_id: str = "ob-1",
    status: str = "pending",
    event_id: str = "ev-1",
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "meshtastic",
    target_channel: str | None = None,
    attempt_number: int = 1,
) -> dict:
    return {
        "outbox_id": outbox_id,
        "status": status,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "attempt_number": attempt_number,
    }


def _make_receipt(
    receipt_id: str = "r-1",
    status: str = "sent",
    event_id: str = "ev-1",
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "meshtastic",
    target_channel: str | None = None,
    attempt_number: int = 1,
    sequence: int = 1,
    source: str = "live",
) -> dict:
    return {
        "receipt_id": receipt_id,
        "status": status,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "attempt_number": attempt_number,
        "sequence": sequence,
        "source": source,
        "created_at": "2026-05-31T12:00:00+00:00",
    }


def _make_action(
    outbox_id: str = "ob-1",
    ownership_action: str = "recoverable",
    prior_status: str = "pending",
    recovery_run_id: str = "run-1",
) -> RecoveryOwnershipAction:
    return RecoveryOwnershipAction(
        recovery_run_id=recovery_run_id,
        startup_timestamp=None,
        outbox_id=outbox_id,
        prior_status=prior_status,
        recovered_status=prior_status,
        ownership_action=ownership_action,
        reason="Test action",
        worker_identity=None,
        recovery_source="startup_recovery",
        timestamp="2026-05-31T12:00:00+00:00",
        delivery_plan_id="plan-1",
        event_id="ev-1",
    )


# ---------------------------------------------------------------------------
# recovered_not_progressed
# ---------------------------------------------------------------------------


class TestRecoveredNotProgressed:
    def test_flagged_when_no_progress(self) -> None:
        """Genuine no-progress: outbox was queued, receipt still queued."""
        outbox = [_make_outbox(status="queued")]
        receipts = [_make_receipt(status="queued")]  # receipt still queued
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(
                _make_action(ownership_action="recoverable", prior_status="queued"),
            ),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECOVERED_NOT_PROGRESSED in kinds
        f = next(f for f in findings if f.kind == KIND_RECOVERED_NOT_PROGRESSED)
        assert f.severity == "degraded"
        assert "no progress" in f.details.lower()

    def test_not_flagged_when_progressed(self) -> None:
        """Receipt progressed to sent — should not fire."""
        outbox = [_make_outbox(status="queued")]
        receipts = [_make_receipt(status="sent")]  # progressed to terminal
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(
                _make_action(ownership_action="recoverable", prior_status="queued"),
            ),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECOVERED_NOT_PROGRESSED not in kinds

    def test_not_flagged_when_no_receipt(self) -> None:
        outbox = [_make_outbox(status="queued")]
        receipts = []
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(
                _make_action(ownership_action="recoverable", prior_status="queued"),
            ),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECOVERED_NOT_PROGRESSED not in kinds

    def test_not_flagged_on_vocabulary_mismatch(self) -> None:
        """Outbox 'pending' has no receipt equivalent — must not fire
        even if a receipt happens to carry the same string value."""
        outbox = [_make_outbox(status="pending")]
        receipts = [
            _make_receipt(status="pending")
        ]  # invalid receipt vocab, but defensive
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(
                _make_action(ownership_action="recoverable", prior_status="pending"),
            ),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECOVERED_NOT_PROGRESSED not in kinds


# ---------------------------------------------------------------------------
# repeatedly_reclaimed
# ---------------------------------------------------------------------------


class TestRepeatedlyReclaimed:
    def test_flagged_when_multiple_distinct_runs(self) -> None:
        outbox = [_make_outbox(status="pending")]
        receipts = []
        action1 = _make_action(recovery_run_id="run-1")
        action2 = _make_action(recovery_run_id="run-2")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-2",
            startup_timestamp=None,
            actions=(action1, action2),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_REPEATEDLY_RECLAIMED in kinds
        f = next(f for f in findings if f.kind == KIND_REPEATEDLY_RECLAIMED)
        assert f.severity == "degraded"
        assert "2 distinct recovery runs" in f.details.lower()

    def test_not_flagged_when_single_run(self) -> None:
        outbox = [_make_outbox(status="pending")]
        receipts = []
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(_make_action(recovery_run_id="run-1"),),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_REPEATEDLY_RECLAIMED not in kinds

    def test_not_flagged_when_duplicate_same_run(self) -> None:
        """Same run_id appearing multiple times is NOT repeatedly reclaimed."""
        outbox = [_make_outbox(status="pending")]
        receipts = []
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(
                _make_action(recovery_run_id="run-1"),
                _make_action(recovery_run_id="run-1"),
            ),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_REPEATEDLY_RECLAIMED not in kinds


# ---------------------------------------------------------------------------
# reclaimed_then_terminal
# ---------------------------------------------------------------------------


class TestReclaimedThenTerminal:
    def test_flagged_when_terminal_outbox_non_terminal_receipt_with_ledger(
        self,
    ) -> None:
        """Fires when outbox is terminal, receipt non-terminal, AND item is in recovery ledger."""
        outbox = [_make_outbox(outbox_id="ob-1", status="dead_lettered")]
        receipts = [_make_receipt(status="failed")]
        action = _make_action(outbox_id="ob-1", ownership_action="reclaimed")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action,),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECLAIMED_THEN_TERMINAL in kinds
        f = next(f for f in findings if f.kind == KIND_RECLAIMED_THEN_TERMINAL)
        assert f.severity == "inconsistent"

    def test_not_flagged_without_ledger(self) -> None:
        """Without a recovery ledger, reclaimed_then_terminal must NOT fire."""
        outbox = [_make_outbox(status="dead_lettered")]
        receipts = [_make_receipt(status="failed")]
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECLAIMED_THEN_TERMINAL not in kinds

    def test_not_flagged_when_both_terminal(self) -> None:
        outbox = [_make_outbox(outbox_id="ob-1", status="dead_lettered")]
        receipts = [_make_receipt(status="dead_lettered")]
        action = _make_action(outbox_id="ob-1", ownership_action="reclaimed")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action,),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECLAIMED_THEN_TERMINAL not in kinds

    def test_not_flagged_when_no_receipt(self) -> None:
        outbox = [_make_outbox(outbox_id="ob-1", status="dead_lettered")]
        action = _make_action(outbox_id="ob-1", ownership_action="reclaimed")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action,),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=[],
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECLAIMED_THEN_TERMINAL not in kinds

    def test_not_flagged_when_not_in_ledger(self) -> None:
        """Terminal outbox + non-terminal receipt but item NOT in recovery ledger → skip."""
        outbox = [_make_outbox(outbox_id="ob-1", status="dead_lettered")]
        receipts = [_make_receipt(status="failed")]
        # Ledger has a DIFFERENT outbox_id, so ob-1 is not in recovery ledger.
        action = _make_action(outbox_id="ob-other", ownership_action="reclaimed")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action,),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECLAIMED_THEN_TERMINAL not in kinds


# ---------------------------------------------------------------------------
# reclaimed_then_orphaned
# ---------------------------------------------------------------------------


class TestReclaimedThenOrphaned:
    def test_flagged_when_recovered_but_event_gone(self) -> None:
        outbox = [
            _make_outbox(outbox_id="ob-o", event_id="ev-missing", status="pending")
        ]
        receipts = []
        action = _make_action(outbox_id="ob-o", ownership_action="reclaimed")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action,),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
            known_event_ids={"ev-1"},  # ev-missing not in catalogue
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECLAIMED_THEN_ORPHANED in kinds
        f = next(f for f in findings if f.kind == KIND_RECLAIMED_THEN_ORPHANED)
        assert f.severity == "inconsistent"
        assert "absent from the known event catalogue" in f.details.lower()

    def test_not_flagged_when_event_present(self) -> None:
        outbox = [_make_outbox(outbox_id="ob-1", event_id="ev-1", status="pending")]
        receipts = []
        action = _make_action(outbox_id="ob-1", ownership_action="reclaimed")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action,),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
            known_event_ids={"ev-1"},
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECLAIMED_THEN_ORPHANED not in kinds

    def test_not_flagged_without_ledger(self) -> None:
        outbox = [
            _make_outbox(outbox_id="ob-o", event_id="ev-missing", status="pending")
        ]
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=[],
            known_event_ids={"ev-1"},
        )
        kinds = {f.kind for f in findings}
        assert KIND_RECLAIMED_THEN_ORPHANED not in kinds


# ---------------------------------------------------------------------------
# Determinism and JSON safety
# ---------------------------------------------------------------------------


class TestRecoveryConvergenceDeterminism:
    def test_deterministic_output(self) -> None:
        outbox = [_make_outbox(outbox_id="ob-1", status="dead_lettered")]
        receipts = [_make_receipt(status="failed")]
        action = _make_action(outbox_id="ob-1", ownership_action="reclaimed")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action,),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        f1 = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        f2 = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        assert [x.kind for x in f1] == [x.kind for x in f2]
        assert [x.record_id for x in f1] == [x.record_id for x in f2]

    def test_json_safe(self) -> None:
        outbox = [_make_outbox(outbox_id="ob-1", status="dead_lettered")]
        receipts = [_make_receipt(status="failed")]
        action = _make_action(outbox_id="ob-1", ownership_action="reclaimed")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action,),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        for f in findings:
            d = f.to_dict()
            s = json.dumps(d)
            reloaded = json.loads(s)
            assert reloaded["kind"] == f.kind
            assert reloaded["severity"] == f.severity

    def test_sorted_findings(self) -> None:
        outbox = [
            _make_outbox(outbox_id="z", status="dead_lettered"),
            _make_outbox(outbox_id="a", status="dead_lettered"),
        ]
        receipts = [
            _make_receipt(
                delivery_plan_id="plan-z", target_adapter="adapter-z", status="failed"
            ),
            _make_receipt(
                delivery_plan_id="plan-a", target_adapter="adapter-a", status="failed"
            ),
        ]
        action1 = _make_action(outbox_id="z", ownership_action="reclaimed")
        action2 = _make_action(outbox_id="a", ownership_action="reclaimed")
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action1, action2),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(
            outbox_items=outbox,
            receipts=receipts,
            recovery_ledger=ledger,
            known_event_ids={"ev-1"},
        )
        kinds = [f.kind for f in findings]
        assert kinds == sorted(kinds)


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    def test_empty_all(self) -> None:
        findings = build_recovery_convergence_findings()
        assert findings == []

    def test_empty_with_ledger(self) -> None:
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        findings = build_recovery_convergence_findings(recovery_ledger=ledger)
        assert findings == []
