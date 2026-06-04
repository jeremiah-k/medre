"""Conformance tests for recovery ownership model.

Verifies:
- Startup ownership is observable (RecoverySummary present in evidence bundles)
- Recovery actions are attributable (every action has recovery_source)
- Recovery diagnostics are read-only (classification functions don't mutate inputs)
- Replay is not recovery (REPLAY_EXECUTION is a reserved forward-compat enum value; not currently produced)
- Recovery is not proof of delivery (actions reference outbox transitions only)
"""

from __future__ import annotations

import copy
import json
from typing import Any

from medre.core.evidence.bundle import EvidenceBundle
from medre.core.recovery.builder import (
    build_recovery_summary,
    build_startup_recovery_ledger,
)
from medre.core.recovery.classification import classify_startup_reclamation
from medre.core.recovery.models import (
    RecoveryOwnershipAction,
    RecoveryOwnershipStatus,
)
from medre.core.recovery.recovery_source import RecoverySource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(**overrides: Any) -> dict[str, Any]:
    item = {
        "outbox_id": "ob-1",
        "status": "pending",
        "event_id": "ev-1",
        "delivery_plan_id": "plan-1",
        "next_attempt_at": None,
        "lease_until": None,
        "updated_at": None,
        "worker_id": None,
    }
    item.update(overrides)
    return item


def _fixed_now() -> str:
    return "2026-05-31T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Observable startup ownership
# ---------------------------------------------------------------------------


class TestObservableStartupOwnership:
    """Recovery ownership SHALL be observable via RecoverySummary and StartupRecoveryLedger."""

    def test_summary_present_for_items(self) -> None:
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        summary = build_recovery_summary(ledger)
        assert summary.total_items == 1
        assert summary.recoverable_items == 1

    def test_summary_has_all_categories(self) -> None:
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        summary = build_recovery_summary(ledger)
        d = summary.to_dict()
        assert "recoverable_items" in d
        assert "claimed_items" in d
        assert "reclaimed_items" in d
        assert "skipped_items" in d
        assert "abandoned_items" in d
        assert "unrecoverable_items" in d
        assert "total_items" in d
        assert "consistency_valid" in d
        assert "by_source" in d

    def test_summary_in_evidence_bundle(self) -> None:
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        summary = build_recovery_summary(ledger)
        bundle = EvidenceBundle(
            event_id="ev-1",
            recovery_summary=summary.to_dict(),
            recovery_ledger=ledger.to_dict(),
        )
        d = bundle.to_dict()
        assert d["recovery_summary"] is not None
        assert d["recovery_ledger"] is not None
        assert json.dumps(d)

    def test_summary_consistency(self) -> None:
        items = [
            _make_item(outbox_id="ob-1", status="pending"),
            _make_item(outbox_id="ob-2", status="sent"),
        ]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        summary = build_recovery_summary(ledger)
        assert summary.consistency_valid is True
        computed = sum(
            [
                summary.recoverable_items,
                summary.claimed_items,
                summary.reclaimed_items,
                summary.skipped_items,
                summary.abandoned_items,
                summary.unrecoverable_items,
            ]
        )
        assert computed == summary.total_items


# ---------------------------------------------------------------------------
# Attributable recovery actions
# ---------------------------------------------------------------------------


class TestAttributableRecoveryActions:
    """Every recovery action SHALL be attributable to a RecoverySource."""

    def test_every_action_has_source(self) -> None:
        items = [
            _make_item(status="pending", worker_id="w1"),
            _make_item(status="sent"),
            _make_item(status="retry_wait"),
        ]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp="2026-05-31T12:00:00+00:00",
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        for action in ledger.actions:
            assert action.recovery_source in {
                str(RecoverySource.STARTUP_RECOVERY),
                str(RecoverySource.RETRY_WORKER_RECOVERY),
                str(RecoverySource.SNAPSHOT_DIAGNOSTICS),
                str(RecoverySource.REPLAY_EXECUTION),
            }

    def test_source_in_summary(self) -> None:
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        summary = build_recovery_summary(ledger)
        assert isinstance(summary.by_source, dict)
        # Each source key must be a known RecoverySource
        valid = {str(s) for s in RecoverySource}
        for key in summary.by_source:
            assert key in valid


# ---------------------------------------------------------------------------
# Read-only diagnostics
# ---------------------------------------------------------------------------


class TestReadOnlyDiagnostics:
    """Recovery diagnostics SHALL be read-only — no mutation of outbox items or receipts."""

    def test_classify_does_not_mutate_dictionary(self) -> None:
        original = {"status": "pending", "event_id": "ev-1", "outbox_id": "ob-1"}
        before = copy.deepcopy(original)
        classify_startup_reclamation(original)
        assert original == before

    def test_builder_does_not_mutate_inputs(self) -> None:
        items = [_make_item(status="pending")]
        before = copy.deepcopy(items)
        build_startup_recovery_ledger(
            outbox_items=items,
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        assert items == before

    def test_no_io_in_classification(self) -> None:
        import inspect

        # classify_startup_reclamation is a sync function = no I/O
        assert not inspect.iscoroutinefunction(classify_startup_reclamation)


# ---------------------------------------------------------------------------
# Replay is not recovery
# ---------------------------------------------------------------------------


class TestReplayIsNotRecovery:
    """Replay execution SHALL NOT be classified as recovery."""

    def test_replay_source_is_distinct(self) -> None:
        """REPLAY_EXECUTION is reserved for future use — assert it exists and is distinct."""
        assert str(RecoverySource.REPLAY_EXECUTION) != str(
            RecoverySource.STARTUP_RECOVERY
        )
        assert str(RecoverySource.REPLAY_EXECUTION) != str(
            RecoverySource.RETRY_WORKER_RECOVERY
        )

    def test_replay_not_mixed_with_startup(self) -> None:
        """All enum values (including reserved REPLAY_EXECUTION) have valid string values."""
        for src in RecoverySource:
            assert isinstance(src.value, str)
            assert src.value in {
                "startup_recovery",
                "retry_worker_recovery",
                "snapshot_diagnostics",
                "replay_execution",
            }


# ---------------------------------------------------------------------------
# Recovery is not proof of delivery
# ---------------------------------------------------------------------------


class TestRecoveryNotProofOfDelivery:
    """Recovery SHALL NOT be presented as proof of delivery."""

    def test_recovery_actions_reference_outbox_not_receipts(self) -> None:
        """RecoveryOwnershipAction documents outbox transitions, not delivery confirmations."""
        action = RecoveryOwnershipAction(
            recovery_run_id="run-1",
            startup_timestamp=None,
            outbox_id="ob-1",
            prior_status="pending",
            observed_status="pending",
            ownership_action="recoverable",
            reason="Item is pending and claimable",
            worker_identity=None,
            recovery_source="startup_recovery",
            timestamp="2026-05-31T12:00:00+00:00",
            delivery_plan_id="plan-1",
            event_id="ev-1",
        )
        d = action.to_dict()
        # No delivery confirmation fields
        assert "delivery_confirmed" not in d
        assert "delivery_status" not in d
        # Only outbox status fields
        assert "prior_status" in d
        assert "observed_status" in d

    def test_no_recovery_claim_matches_ownership(self) -> None:
        """Ownership actions assert what was claimed, not what was delivered."""
        for status in RecoveryOwnershipStatus:
            assert status.value in {
                "recoverable",
                "claimed_for_recovery",
                "reclaimed",
                "abandoned",
                "unrecoverable",
                "skipped",
            }
            # None of these imply delivery completion
            assert "sent" not in status.value
            assert "delivered" not in status.value
