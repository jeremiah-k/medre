"""Tests for recovery ownership model, classification, and builders.

Covers:
- RecoveryOwnershipStatus values and serialization
- RecoveryOwnershipAction, StartupRecoveryLedger, RecoverySummary — frozen, JSON-safe, deterministic
- classify_startup_reclamation() — all 6 classification labels for all 8 outbox statuses
- build_startup_recovery_ledger() — empty, single, mixed, terminal → unrecoverable
- build_recovery_summary() — consistency validation
- RecoverySource disambiguation
- Deterministic time: naive and aware timestamps, reference time injection
- Queued stale-grace: recent, stale, missing updated_at
- Edge cases: missing startup_timestamp, missing known_event_ids, unrecognised status
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.recovery.builder import (
    build_recovery_summary,
    build_startup_recovery_ledger,
)
from medre.core.recovery.classification import classify_startup_reclamation
from medre.core.recovery.models import (
    RecoveryOwnershipAction,
    RecoveryOwnershipStatus,
    RecoverySummary,
    StartupRecoveryLedger,
)
from medre.core.recovery.recovery_source import RecoverySource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    outbox_id: str = "ob-1",
    status: str = "pending",
    event_id: str = "ev-1",
    delivery_plan_id: str = "plan-1",
    next_attempt_at: str | None = None,
    lease_until: str | None = None,
    updated_at: str | None = None,
    worker_id: str | None = None,
) -> dict[str, str | None]:
    return {
        "outbox_id": outbox_id,
        "status": status,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "next_attempt_at": next_attempt_at,
        "lease_until": lease_until,
        "updated_at": updated_at,
        "worker_id": worker_id,
    }


def _fixed_now() -> str:
    return "2026-05-31T12:00:00+00:00"


def _fixed_dt() -> datetime:
    return datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# RecoveryOwnershipStatus
# ---------------------------------------------------------------------------


class TestRecoveryOwnershipStatus:
    def test_all_members_present(self) -> None:
        members = set(RecoveryOwnershipStatus)
        assert members == {
            RecoveryOwnershipStatus.RECOVERABLE,
            RecoveryOwnershipStatus.CLAIMED_FOR_RECOVERY,
            RecoveryOwnershipStatus.RECLAIMED,
            RecoveryOwnershipStatus.ABANDONED,
            RecoveryOwnershipStatus.UNRECOVERABLE,
            RecoveryOwnershipStatus.SKIPPED,
        }

    def test_is_string_enum(self) -> None:
        assert str(RecoveryOwnershipStatus.RECOVERABLE) == "recoverable"
        assert str(RecoveryOwnershipStatus.UNRECOVERABLE) == "unrecoverable"

    def test_equality_with_strings(self) -> None:
        assert RecoveryOwnershipStatus.RECOVERABLE == "recoverable"
        assert RecoveryOwnershipStatus.RECOVERABLE != "claimed_for_recovery"

    def test_skipped_value(self) -> None:
        assert RecoveryOwnershipStatus.SKIPPED == "skipped"
        assert str(RecoveryOwnershipStatus.SKIPPED) == "skipped"


# ---------------------------------------------------------------------------
# RecoverySource
# ---------------------------------------------------------------------------


class TestRecoverySource:
    def test_all_members_present(self) -> None:
        members = set(RecoverySource)
        assert members == {
            RecoverySource.STARTUP_RECOVERY,
            RecoverySource.RETRY_WORKER_RECOVERY,
            RecoverySource.REPLAY_EXECUTION,
        }

    def test_is_string_enum(self) -> None:
        assert str(RecoverySource.STARTUP_RECOVERY) == "startup_recovery"
        assert str(RecoverySource.REPLAY_EXECUTION) == "replay_execution"


# ---------------------------------------------------------------------------
# RecoveryOwnershipAction
# ---------------------------------------------------------------------------


class TestRecoveryOwnershipAction:
    def test_frozen(self) -> None:
        action = RecoveryOwnershipAction(
            recovery_run_id="run-1",
            startup_timestamp="2026-05-31T12:00:00+00:00",
            outbox_id="ob-1",
            prior_status="pending",
            recovered_status="in_progress",
            ownership_action="reclaimed",
            reason="Reclaimed at startup",
            worker_identity=None,
            recovery_source="startup_recovery",
            timestamp="2026-05-31T12:00:01+00:00",
            delivery_plan_id="plan-1",
            event_id="ev-1",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            action.outbox_id = "modified"  # type: ignore[misc]

    def test_to_dict(self) -> None:
        action = RecoveryOwnershipAction(
            recovery_run_id="run-1",
            startup_timestamp="2026-05-31T12:00:00+00:00",
            outbox_id="ob-1",
            prior_status="pending",
            recovered_status="pending",
            ownership_action="recoverable",
            reason="Test",
            worker_identity=None,
            recovery_source="startup_recovery",
            timestamp="2026-05-31T12:00:01+00:00",
            delivery_plan_id="plan-1",
            event_id="ev-1",
        )
        d = action.to_dict()
        assert json.dumps(d)  # JSON-safe
        # Keys sorted
        keys = list(d.keys())
        assert keys == sorted(keys)

    def test_json_roundtrip(self) -> None:
        action = RecoveryOwnershipAction(
            recovery_run_id="run-1",
            startup_timestamp=None,
            outbox_id="ob-1",
            prior_status="pending",
            recovered_status="in_progress",
            ownership_action="claimed_for_recovery",
            reason="Stale in_progress",
            worker_identity="worker-1",
            recovery_source="retry_worker_recovery",
            timestamp="2026-05-31T12:00:01+00:00",
            delivery_plan_id="plan-1",
            event_id="ev-1",
        )
        d = action.to_dict()
        s = json.dumps(d)
        reloaded = json.loads(s)
        assert reloaded["outbox_id"] == "ob-1"
        assert reloaded["ownership_action"] == "claimed_for_recovery"
        assert reloaded["worker_identity"] == "worker-1"
        assert reloaded["startup_timestamp"] is None

    def test_none_recovery_run_id(self) -> None:
        action = RecoveryOwnershipAction(
            recovery_run_id=None,
            startup_timestamp=None,
            outbox_id="ob-1",
            prior_status="pending",
            recovered_status="pending",
            ownership_action="skipped",
            reason="Deferred",
            worker_identity=None,
            recovery_source="startup_recovery",
            timestamp="2026-05-31T12:00:01+00:00",
            delivery_plan_id="plan-1",
            event_id="ev-1",
        )
        d = action.to_dict()
        assert d["recovery_run_id"] is None
        assert json.dumps(d)


# ---------------------------------------------------------------------------
# StartupRecoveryLedger
# ---------------------------------------------------------------------------


class TestStartupRecoveryLedger:
    def test_frozen(self) -> None:
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp="2026-05-31T12:00:00+00:00",
            actions=(),
            generated_at="2026-05-31T12:00:01+00:00",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ledger.actions = ()  # type: ignore[misc]

    def test_to_dict_empty(self) -> None:
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        d = ledger.to_dict()
        assert d["actions"] == []
        assert json.dumps(d)

    def test_to_dict_with_actions(self) -> None:
        action = RecoveryOwnershipAction(
            recovery_run_id="run-1",
            startup_timestamp=None,
            outbox_id="ob-1",
            prior_status="pending",
            recovered_status="pending",
            ownership_action="recoverable",
            reason="Test",
            worker_identity=None,
            recovery_source="retry_worker_recovery",
            timestamp="2026-05-31T12:00:01+00:00",
            delivery_plan_id="plan-1",
            event_id="ev-1",
        )
        ledger = StartupRecoveryLedger(
            recovery_run_id="run-1",
            startup_timestamp=None,
            actions=(action,),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        d = ledger.to_dict()
        assert len(d["actions"]) == 1
        assert json.dumps(d)
        keys = list(d.keys())
        assert keys == sorted(keys)

    def test_actions_sorted(self) -> None:
        """Builder sorts actions by (outbox_id, timestamp)."""
        items = [
            _make_item(outbox_id="ob-b", status="pending"),
            _make_item(outbox_id="ob-a", status="pending"),
        ]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        sorted_ids = [a.outbox_id for a in ledger.actions]
        assert sorted_ids == ["ob-a", "ob-b"]

    def test_none_recovery_run_id(self) -> None:
        ledger = StartupRecoveryLedger(
            recovery_run_id=None,
            startup_timestamp=None,
            actions=(),
            generated_at="2026-05-31T12:00:00+00:00",
        )
        assert ledger.recovery_run_id is None
        d = ledger.to_dict()
        assert d["recovery_run_id"] is None
        assert json.dumps(d)


# ---------------------------------------------------------------------------
# RecoverySummary
# ---------------------------------------------------------------------------


class TestRecoverySummary:
    def test_consistent(self) -> None:
        rs = RecoverySummary(
            recoverable_items=3,
            claimed_items=1,
            reclaimed_items=2,
            skipped_items=1,
            abandoned_items=0,
            unrecoverable_items=1,
            total_items=8,
            consistency_valid=True,
            by_source={"startup_recovery": 5, "retry_worker_recovery": 3},
            recovery_run_id="run-1",
        )
        assert rs.consistency_valid is True
        assert rs.total_items == 8

    def test_inconsistent(self) -> None:
        rs = RecoverySummary(
            recoverable_items=1,
            claimed_items=1,
            reclaimed_items=1,
            skipped_items=0,
            abandoned_items=0,
            unrecoverable_items=0,
            total_items=10,  # Doesn't match sum
            consistency_valid=False,
            by_source={},
            recovery_run_id=None,
        )
        assert rs.consistency_valid is False

    def test_to_dict(self) -> None:
        rs = RecoverySummary(
            recoverable_items=0,
            claimed_items=0,
            reclaimed_items=0,
            skipped_items=0,
            abandoned_items=0,
            unrecoverable_items=0,
            total_items=0,
            consistency_valid=True,
            by_source={},
            recovery_run_id="run-1",
        )
        d = rs.to_dict()
        assert json.dumps(d)
        keys = list(d.keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# classify_startup_reclamation
# ---------------------------------------------------------------------------


class TestClassifyStartupReclamation:
    def test_immediately_claimable_pending(self) -> None:
        item = _make_item(status="pending")
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "immediately_claimable"
        assert "pending" in reason.lower()

    def test_immediately_claimable_retry_wait_due(self) -> None:
        now = _fixed_dt()
        past = (now - timedelta(hours=1)).isoformat()
        item = _make_item(status="retry_wait", next_attempt_at=past)
        label, _ = classify_startup_reclamation(item, now=now)
        assert label == "immediately_claimable"

    def test_retry_eligible_future(self) -> None:
        now = _fixed_dt()
        future = (now + timedelta(hours=1)).isoformat()
        item = _make_item(status="retry_wait", next_attempt_at=future)
        label, reason = classify_startup_reclamation(item, now=now)
        assert label == "retry_eligible"
        assert "not yet due" in reason.lower()

    def test_stale_in_progress_lease_expired(self) -> None:
        now = _fixed_dt()
        past = (now - timedelta(hours=1)).isoformat()
        item = _make_item(status="in_progress", lease_until=past)
        label, reason = classify_startup_reclamation(item, now=now)
        assert label == "stale"
        assert "expired" in reason.lower()

    def test_stale_in_progress_no_lease(self) -> None:
        item = _make_item(status="in_progress")
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "stale"
        assert "missing" in reason.lower()

    def test_in_progress_active_lease_not_stale(self) -> None:
        """Item 9: Future non-expired lease must NOT be stale/reclaimable."""
        now = _fixed_dt()
        future = (now + timedelta(hours=1)).isoformat()
        item = _make_item(status="in_progress", lease_until=future)
        label, reason = classify_startup_reclamation(item, now=now)
        assert label == "retry_eligible"
        assert "active lease" in reason.lower()

    def test_orphaned(self) -> None:
        item = _make_item(status="pending", event_id="ev-missing")
        label, reason = classify_startup_reclamation(
            item, known_event_ids={"ev-1", "ev-2"}, now=_fixed_dt()
        )
        assert label == "orphaned"
        assert "orphaned" in reason.lower()

    def test_orphaned_skip_when_none(self) -> None:
        item = _make_item(status="pending", event_id="ev-missing")
        label, _ = classify_startup_reclamation(
            item, known_event_ids=None, now=_fixed_dt()
        )
        assert label == "immediately_claimable"

    def test_terminal_sent(self) -> None:
        item = _make_item(status="sent")
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "terminal"
        assert "terminal" in reason.lower()

    def test_terminal_dead_lettered(self) -> None:
        item = _make_item(status="dead_lettered")
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "terminal"
        assert "dead_lettered" in reason.lower()

    def test_terminal_cancelled(self) -> None:
        item = _make_item(status="cancelled")
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "terminal"
        assert "cancelled" in reason.lower()

    def test_terminal_abandoned(self) -> None:
        item = _make_item(status="abandoned")
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "terminal"
        assert "abandoned" in reason.lower()

    def test_inconsistent_unrecognised(self) -> None:
        item = _make_item(status="garbage")
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "inconsistent"
        assert "unrecognised" in reason.lower()

    # -- Item 9: Naive timestamp handling -----------------------------------

    def test_lease_until_naive_treated_as_utc(self) -> None:
        """Naive lease_until parsed as UTC — future lease is not stale."""
        now = _fixed_dt()
        # Naive timestamp 1 hour in the future
        future_naive = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        item = _make_item(status="in_progress", lease_until=future_naive)
        label, _ = classify_startup_reclamation(item, now=now)
        assert label == "retry_eligible"

    def test_lease_until_naive_expired(self) -> None:
        """Naive lease_until parsed as UTC — past lease is stale."""
        now = _fixed_dt()
        past_naive = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        item = _make_item(status="in_progress", lease_until=past_naive)
        label, _ = classify_startup_reclamation(item, now=now)
        assert label == "stale"

    def test_next_attempt_at_naive_future(self) -> None:
        """Naive next_attempt_at parsed as UTC — future is retry_eligible."""
        now = _fixed_dt()
        future_naive = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        item = _make_item(status="retry_wait", next_attempt_at=future_naive)
        label, _ = classify_startup_reclamation(item, now=now)
        assert label == "retry_eligible"

    def test_next_attempt_at_naive_past(self) -> None:
        """Naive next_attempt_at parsed as UTC — past is immediately_claimable."""
        now = _fixed_dt()
        past_naive = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        item = _make_item(status="retry_wait", next_attempt_at=past_naive)
        label, _ = classify_startup_reclamation(item, now=now)
        assert label == "immediately_claimable"

    # -- Item 10: Queued stale-grace ----------------------------------------

    def test_queued_recent_within_grace(self) -> None:
        """Queued item with recent updated_at is within grace — not claimable."""
        now = _fixed_dt()
        recent = (now - timedelta(minutes=1)).isoformat()
        item = _make_item(status="queued", updated_at=recent)
        label, reason = classify_startup_reclamation(item, now=now)
        assert label == "retry_eligible"
        assert "grace" in reason.lower()

    def test_queued_stale_beyond_grace(self) -> None:
        """Queued item with old updated_at exceeds grace — claimable."""
        now = _fixed_dt()
        old = (now - timedelta(hours=1)).isoformat()
        item = _make_item(status="queued", updated_at=old)
        label, reason = classify_startup_reclamation(item, now=now)
        assert label == "immediately_claimable"
        assert "stale" in reason.lower()

    def test_queued_missing_updated_at(self) -> None:
        """Queued item with no updated_at is stale."""
        item = _make_item(status="queued", updated_at=None)
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "stale"
        assert "no updated_at" in reason.lower()

    def test_queued_stale_custom_grace(self) -> None:
        """Custom grace period is respected."""
        now = _fixed_dt()
        updated = (now - timedelta(minutes=3)).isoformat()
        item = _make_item(status="queued", updated_at=updated)
        # Default grace (5 min) → not stale
        label_default, _ = classify_startup_reclamation(item, now=now)
        assert label_default == "retry_eligible"
        # 2 min grace → stale
        label_short, _ = classify_startup_reclamation(
            item, now=now, stale_queued_grace=timedelta(minutes=2)
        )
        assert label_short == "immediately_claimable"

    def test_queued_stale_old_timestamp(self) -> None:
        """Legacy test: queued with very old timestamp is claimable."""
        item = _make_item(status="queued", updated_at="2020-01-01T00:00:00+00:00")
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "immediately_claimable"

    def test_queued_unparseable_updated_at(self) -> None:
        """Queued item with unparseable updated_at is stale."""
        item = _make_item(status="queued", updated_at="not-a-date")
        label, reason = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "stale"


# ---------------------------------------------------------------------------
# build_startup_recovery_ledger
# ---------------------------------------------------------------------------


class TestBuildStartupRecoveryLedger:
    def test_empty(self) -> None:
        ledger = build_startup_recovery_ledger(
            outbox_items=[],
            startup_timestamp="2026-05-31T12:00:00+00:00",
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        assert len(ledger.actions) == 0
        assert ledger.recovery_run_id == "run-1"
        assert json.loads(json.dumps(ledger.to_dict()))

    def test_single_pending(self) -> None:
        items = [_make_item(outbox_id="ob-1", status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp="2026-05-31T12:00:00+00:00",
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        assert len(ledger.actions) == 1
        action = ledger.actions[0]
        assert action.outbox_id == "ob-1"
        assert action.ownership_action == "recoverable"
        assert action.recovery_source == "startup_recovery"

    def test_mixed_statuses(self) -> None:
        items = [
            _make_item(outbox_id="ob-1", status="pending"),
            _make_item(outbox_id="ob-2", status="sent"),
            _make_item(outbox_id="ob-3", status="retry_wait"),
        ]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp="2026-05-31T12:00:00+00:00",
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        assert len(ledger.actions) == 3
        statuses = {a.ownership_action for a in ledger.actions}
        assert "recoverable" in statuses
        assert "unrecoverable" in statuses

    def test_terminal_becomes_unrecoverable(self) -> None:
        items = [
            _make_item(outbox_id="ob-1", status="dead_lettered"),
            _make_item(outbox_id="ob-2", status="cancelled"),
            _make_item(outbox_id="ob-3", status="abandoned"),
        ]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        for action in ledger.actions:
            assert action.ownership_action == "unrecoverable"

    def test_auto_generates_run_id(self) -> None:
        ledger = build_startup_recovery_ledger(outbox_items=[], now_fn=_fixed_now)
        assert ledger.recovery_run_id
        assert len(ledger.recovery_run_id) == 32  # uuid hex is 32 chars

    def test_respects_known_event_ids(self) -> None:
        items = [
            _make_item(outbox_id="ob-1", event_id="ev-1"),
            _make_item(outbox_id="ob-2", event_id="ev-missing"),
        ]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            known_event_ids={"ev-1"},
            now_fn=_fixed_now,
        )
        ob2_action = next(a for a in ledger.actions if a.outbox_id == "ob-2")
        assert ob2_action.ownership_action == "unrecoverable"

    def test_deterministic_ordering(self) -> None:
        items = [
            _make_item(outbox_id="c", status="pending"),
            _make_item(outbox_id="a", status="pending"),
            _make_item(outbox_id="b", status="pending"),
        ]
        l1 = build_startup_recovery_ledger(
            outbox_items=items, now_fn=_fixed_now, recovery_run_id="run-1"
        )
        l2 = build_startup_recovery_ledger(
            outbox_items=items, now_fn=_fixed_now, recovery_run_id="run-1"
        )
        ids1 = [a.outbox_id for a in l1.actions]
        ids2 = [a.outbox_id for a in l2.actions]
        assert ids1 == ids2
        assert ids1 == ["a", "b", "c"]

    def test_retry_eligible_skipped(self) -> None:
        future = (datetime(2026, 5, 31, 13, 0, 0, tzinfo=timezone.utc)).isoformat()
        items = [_make_item(status="retry_wait", next_attempt_at=future)]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            now_fn=_fixed_now,
        )
        assert ledger.actions[0].ownership_action == "skipped"

    def test_in_progress_active_lease_skipped(self) -> None:
        """Item 9: in_progress with active lease should be skipped."""
        future = (datetime(2026, 5, 31, 13, 0, 0, tzinfo=timezone.utc)).isoformat()
        items = [_make_item(status="in_progress", lease_until=future)]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            now_fn=_fixed_now,
        )
        assert ledger.actions[0].ownership_action == "skipped"


# ---------------------------------------------------------------------------
# build_recovery_summary
# ---------------------------------------------------------------------------


class TestBuildRecoverySummary:
    def test_empty_ledger(self) -> None:
        ledger = build_startup_recovery_ledger(
            outbox_items=[], now_fn=_fixed_now, recovery_run_id="run-1"
        )
        summary = build_recovery_summary(ledger)
        assert summary.total_items == 0
        assert summary.consistency_valid is True
        assert summary.recoverable_items == 0
        assert summary.recovery_run_id == "run-1"

    def test_counts_add_up(self) -> None:
        items = [
            _make_item(outbox_id="ob-1", status="pending"),
            _make_item(outbox_id="ob-2", status="sent"),
            _make_item(outbox_id="ob-3", status="pending"),
            _make_item(outbox_id="ob-4", status="dead_lettered"),
        ]
        ledger = build_startup_recovery_ledger(
            outbox_items=items, now_fn=_fixed_now, recovery_run_id="run-1"
        )
        summary = build_recovery_summary(ledger)
        assert summary.total_items == 4
        assert summary.consistency_valid is True
        computed = (
            summary.recoverable_items
            + summary.claimed_items
            + summary.reclaimed_items
            + summary.skipped_items
            + summary.abandoned_items
            + summary.unrecoverable_items
        )
        assert computed == summary.total_items

    def test_by_source_populated(self) -> None:
        items = [
            _make_item(outbox_id="ob-1", status="pending", worker_id="w1"),
            _make_item(outbox_id="ob-2", status="pending"),
        ]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp="2026-05-31T12:00:00+00:00",
            now_fn=_fixed_now,
            recovery_run_id="run-1",
        )
        summary = build_recovery_summary(ledger)
        assert "startup_recovery" in summary.by_source

    def test_json_safe(self) -> None:
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items, now_fn=_fixed_now, recovery_run_id="run-1"
        )
        summary = build_recovery_summary(ledger)
        s = json.dumps(summary.to_dict())
        reloaded = json.loads(s)
        assert reloaded["total_items"] == 1
        assert reloaded["consistency_valid"] is True


# ---------------------------------------------------------------------------
# RecoverySource logic
# ---------------------------------------------------------------------------


class TestRecoverySourceInference:
    def test_startup_recovery_with_timestamp(self) -> None:
        """Item 8: startup_timestamp present → always startup_recovery."""
        # Use a timestamp far in the past — source should still be startup
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp="2020-01-01T00:00:00+00:00",
            now_fn=_fixed_now,
            recovery_run_id="run-1",
        )
        assert ledger.actions[0].recovery_source == "startup_recovery"

    def test_startup_recovery_with_recent_timestamp(self) -> None:
        """Startup source even with recent timestamp."""
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp="2026-05-31T12:00:00+00:00",
            now_fn=_fixed_now,
            recovery_run_id="run-1",
        )
        assert ledger.actions[0].recovery_source == "startup_recovery"

    def test_retry_worker_without_timestamp(self) -> None:
        items = [_make_item(status="pending", worker_id="worker-1")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp=None,
            now_fn=_fixed_now,
            recovery_run_id="run-1",
        )
        assert ledger.actions[0].recovery_source == "retry_worker_recovery"

    def test_default_retry_worker(self) -> None:
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp=None,
            now_fn=_fixed_now,
            recovery_run_id="run-1",
        )
        assert ledger.actions[0].recovery_source == "retry_worker_recovery"

    def test_no_age_based_source_flip(self) -> None:
        """Item 8: Source does not flip to retry_worker after 60s."""
        # startup_timestamp is 1 hour before now_fn — still startup_recovery
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp="2026-05-31T11:00:00+00:00",
            now_fn=_fixed_now,
            recovery_run_id="run-1",
        )
        assert ledger.actions[0].recovery_source == "startup_recovery"


# ---------------------------------------------------------------------------
# JSON safety / Determinism
# ---------------------------------------------------------------------------


class TestJsonSafety:
    def test_ledger_json_roundtrip(self) -> None:
        items = [
            _make_item(outbox_id="ob-1", status="pending"),
            _make_item(outbox_id="ob-2", status="sent"),
            _make_item(outbox_id="ob-3", status="retry_wait"),
        ]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            startup_timestamp="2026-05-31T12:00:00+00:00",
            recovery_run_id="run-1",
            now_fn=_fixed_now,
        )
        d = ledger.to_dict()
        s = json.dumps(d)
        reloaded = json.loads(s)
        assert reloaded["recovery_run_id"] == "run-1"
        assert len(reloaded["actions"]) == 3

    def test_summary_json_roundtrip(self) -> None:
        items = [_make_item(status="pending")]
        ledger = build_startup_recovery_ledger(
            outbox_items=items,
            now_fn=_fixed_now,
            recovery_run_id="run-1",
        )
        summary = build_recovery_summary(ledger)
        d = summary.to_dict()
        s = json.dumps(d)
        reloaded = json.loads(s)
        assert reloaded["total_items"] == 1
        assert reloaded["consistency_valid"] is True

    def test_deterministic_output(self) -> None:
        items = [_make_item(outbox_id="a", status="pending")]
        l1 = build_startup_recovery_ledger(
            outbox_items=items,
            now_fn=_fixed_now,
            recovery_run_id="run-1",
        )
        l2 = build_startup_recovery_ledger(
            outbox_items=items,
            now_fn=_fixed_now,
            recovery_run_id="run-1",
        )
        assert json.dumps(l1.to_dict(), sort_keys=True) == json.dumps(
            l2.to_dict(), sort_keys=True
        )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_string_status(self) -> None:
        item = _make_item(status="")
        label, _ = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "inconsistent"

    def test_none_fields(self) -> None:
        item: dict[str, str | None] = {
            "outbox_id": "ob-1",
            "status": None,
            "event_id": None,
            "delivery_plan_id": None,
            "next_attempt_at": None,
            "lease_until": None,
            "updated_at": None,
            "worker_id": None,
        }
        label, _ = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "inconsistent"  # None → "" → "" → unrecognised

    def test_dict_input(self) -> None:
        # classify_startup_reclamation accepts dict directly
        item = {"status": "pending", "event_id": "ev-1", "outbox_id": "ob-d"}
        label, _ = classify_startup_reclamation(item, now=_fixed_dt())
        assert label == "immediately_claimable"
