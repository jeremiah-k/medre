"""Replay/recovery operator-flow output distinction tests.

Validates that operator-facing output distinguishes the required states
for both replay and recovery flows.  Tests are organised by the
operator-surface PC deliverable requirements:

Replay output distinguishes:
- recreated work (passed)
- skipped terminal work (recovery: terminal -> unrecoverable)
- skipped active/unowned work (recovery: retry_eligible -> skipped)
- missing native relation target (replay: event not found)
- capability-suppressed delivery

Recovery output distinguishes:
- ownership claimed/stale (stale -> claimed_for_recovery)
- orphan detected (orphaned -> unrecoverable)
- no-op safe state (all terminal -> consistency_valid)
- manual intervention (suppression_reason surfaced)

Uses pure-model tests (no I/O, no runtime) exercising the
classification, builder, and summary modules directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from medre.core.engine.replay.summary import (
    ReplaySummary,
    _build_summary,
    _categorize_skip_reason,
)
from medre.core.engine.replay.types import ReplayMode, ReplayResult
from medre.core.recovery.builder import (
    build_recovery_summary,
    build_startup_recovery_ledger,
)
from medre.core.recovery.models import (
    RecoveryOwnershipStatus,
)
from medre.core.recovery.recovery_source import RecoverySource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outbox_item(
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


def _fixed_now_iso() -> str:
    return "2026-06-01T12:00:00+00:00"


def _fixed_now_dt() -> datetime:
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Replay output: skip-reason categorization
# ---------------------------------------------------------------------------


class TestReplaySkipReasonCategorization:
    """_categorize_skip_reason maps error messages to stable categories."""

    def test_dry_run_suppressed(self) -> None:
        assert _categorize_skip_reason("dry_run: delivery suppressed") == "dry_run"

    def test_capability_suppressed(self) -> None:
        assert (
            _categorize_skip_reason(
                "capability_suppressed: message.reaction not supported"
            )
            == "capability_suppressed"
        )

    def test_event_missing_store(self) -> None:
        assert _categorize_skip_reason("Event not found in storage") == "event_missing"

    def test_event_missing_upstream(self) -> None:
        assert (
            _categorize_skip_reason(
                "Event not found in storage; upstream stages failed"
            )
            == "event_missing"
        )

    def test_cancelled(self) -> None:
        assert _categorize_skip_reason("replay_cancelled") == "cancelled"

    def test_target_filter(self) -> None:
        assert (
            _categorize_skip_reason("No delivery plans matched target_adapters filter")
            == "target_filter"
        )

    def test_no_plans(self) -> None:
        assert (
            _categorize_skip_reason(
                "No delivery plans available; planning may have errored"
            )
            == "no_plans"
        )

    def test_none_error_is_other(self) -> None:
        assert _categorize_skip_reason(None) == "other"

    def test_unknown_error_is_other(self) -> None:
        assert _categorize_skip_reason("something unexpected") == "other"


class TestReplaySkipReasonsInSummary:
    """ReplaySummary.skip_reasons aggregates skip counts by category."""

    def test_dry_run_skip(self) -> None:
        results = [
            ReplayResult(
                event_id="e1",
                stage="deliver",
                status="skipped",
                error="dry_run: delivery suppressed",
            ),
        ]
        summary = _build_summary(results, mode=ReplayMode.DRY_RUN)
        assert summary.skip_reasons == {"dry_run": 1}
        d = summary.to_dict()
        assert d["skip_reasons"] == {"dry_run": 1}

    def test_capability_suppressed_skip(self) -> None:
        results = [
            ReplayResult(
                event_id="e1",
                stage="deliver",
                status="skipped",
                error="capability_suppressed: text unsupported by adapter",
            ),
        ]
        summary = _build_summary(results)
        assert summary.skip_reasons == {"capability_suppressed": 1}

    def test_event_missing_skip(self) -> None:
        results = [
            ReplayResult(
                event_id="e1",
                stage="store",
                status="failed",
                error="Event not found in storage",
            ),
            ReplayResult(
                event_id="e1",
                stage="route",
                status="skipped",
                error="Event not found in storage; upstream stages failed",
            ),
        ]
        summary = _build_summary(results)
        assert summary.skip_reasons == {"event_missing": 1}

    def test_mixed_skip_reasons(self) -> None:
        results = [
            ReplayResult(
                event_id="e1",
                stage="deliver",
                status="skipped",
                error="dry_run: delivery suppressed",
            ),
            ReplayResult(
                event_id="e2",
                stage="deliver",
                status="skipped",
                error="capability_suppressed: text unsupported",
            ),
            ReplayResult(
                event_id="e3",
                stage="deliver",
                status="skipped",
                error="replay_cancelled",
            ),
            ReplayResult(
                event_id="e4",
                stage="store",
                status="passed",
            ),
        ]
        summary = _build_summary(results)
        assert summary.skip_reasons == {
            "dry_run": 1,
            "capability_suppressed": 1,
            "cancelled": 1,
        }
        assert summary.skipped_count == 3

    def test_no_skips_means_empty_skip_reasons(self) -> None:
        results = [
            ReplayResult(event_id="e1", stage="store", status="passed"),
        ]
        summary = _build_summary(results)
        assert summary.skip_reasons == {}

    def test_empty_results_means_empty_skip_reasons(self) -> None:
        summary = _build_summary([])
        assert summary.skip_reasons == {}

    def test_skip_reasons_sorted_in_to_dict(self) -> None:
        results = [
            ReplayResult(
                event_id="e1",
                stage="deliver",
                status="skipped",
                error="replay_cancelled",
            ),
            ReplayResult(
                event_id="e2",
                stage="deliver",
                status="skipped",
                error="dry_run: delivery suppressed",
            ),
        ]
        summary = _build_summary(results)
        d = summary.to_dict()
        keys = list(d["skip_reasons"].keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Replay output: recreated work vs failed vs skipped
# ---------------------------------------------------------------------------


class TestReplayOutputDistinguishesWorkStates:
    """ReplaySummary by_status distinguishes passed, skipped, failed, error."""

    def test_recreated_work_is_passed(self) -> None:
        results = [
            ReplayResult(event_id="e1", stage="store", status="passed"),
            ReplayResult(event_id="e1", stage="route", status="passed"),
        ]
        summary = _build_summary(results)
        assert summary.by_status["passed"] == 2
        assert summary.by_status["skipped"] == 0

    def test_failed_store(self) -> None:
        results = [
            ReplayResult(
                event_id="e1",
                stage="store",
                status="failed",
                error="Event not found in storage",
            ),
        ]
        summary = _build_summary(results)
        assert summary.by_status["failed"] == 1
        assert summary.by_status["passed"] == 0

    def test_error_from_exception(self) -> None:
        results = [
            ReplayResult(
                event_id="e1",
                stage="deliver",
                status="error",
                error="ConnectionError: timeout",
            ),
        ]
        summary = _build_summary(results)
        assert summary.by_status["error"] == 1
        assert summary.failure_count == 1


# ---------------------------------------------------------------------------
# Recovery output: ownership claimed/stale
# ---------------------------------------------------------------------------


class TestRecoveryOwnershipClaimedStale:
    """Recovery ownership distinguishes stale (claimed) from active (skipped)."""

    def test_stale_in_progress_claimed(self) -> None:
        """in_progress with expired lease -> CLAIMED_FOR_RECOVERY."""
        past_lease = (_fixed_now_dt() - timedelta(hours=1)).isoformat()
        item = _make_outbox_item(
            outbox_id="ob-stale",
            status="in_progress",
            lease_until=past_lease,
        )
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-stale",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        action = ledger.actions[0]
        assert action.ownership_action == str(
            RecoveryOwnershipStatus.CLAIMED_FOR_RECOVERY
        )
        assert "lease expired" in action.reason or "reclaimable" in action.reason

    def test_stale_no_lease_claimed(self) -> None:
        """in_progress with no lease_until -> CLAIMED_FOR_RECOVERY."""
        item = _make_outbox_item(
            outbox_id="ob-no-lease",
            status="in_progress",
            lease_until=None,
        )
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-no-lease",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        action = ledger.actions[0]
        assert action.ownership_action == str(
            RecoveryOwnershipStatus.CLAIMED_FOR_RECOVERY
        )

    def test_active_lease_skipped(self) -> None:
        """in_progress with valid lease -> SKIPPED."""
        future_lease = (_fixed_now_dt() + timedelta(hours=1)).isoformat()
        item = _make_outbox_item(
            outbox_id="ob-active",
            status="in_progress",
            lease_until=future_lease,
        )
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-active",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        action = ledger.actions[0]
        assert action.ownership_action == str(RecoveryOwnershipStatus.SKIPPED)
        assert "active lease" in action.reason

    def test_summary_counts_claimed_and_skipped(self) -> None:
        """RecoverySummary distinguishes claimed vs skipped counts."""
        past_lease = (_fixed_now_dt() - timedelta(hours=1)).isoformat()
        future_lease = (_fixed_now_dt() + timedelta(hours=1)).isoformat()
        stale_item = _make_outbox_item(
            outbox_id="ob-stale",
            status="in_progress",
            lease_until=past_lease,
        )
        active_item = _make_outbox_item(
            outbox_id="ob-active",
            status="in_progress",
            lease_until=future_lease,
        )
        ledger = build_startup_recovery_ledger(
            [stale_item, active_item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-mixed",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        summary = build_recovery_summary(ledger)
        assert summary.claimed_items == 1
        assert summary.skipped_items == 1
        assert summary.consistency_valid is True


# ---------------------------------------------------------------------------
# Recovery output: orphan detected
# ---------------------------------------------------------------------------


class TestRecoveryOrphanDetected:
    """Recovery ownership flags orphans as unrecoverable."""

    def test_orphan_detected(self) -> None:
        """Non-terminal item whose event_id is not in known_event_ids."""
        item = _make_outbox_item(
            outbox_id="ob-orphan",
            status="pending",
            event_id="ev-ghost",
        )
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-orphan",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},  # ev-ghost is NOT known
        )
        action = ledger.actions[0]
        assert action.ownership_action == str(RecoveryOwnershipStatus.UNRECOVERABLE)
        assert "orphan" in action.reason.lower()

    def test_orphan_summary_unrecoverable(self) -> None:
        item = _make_outbox_item(
            outbox_id="ob-orphan",
            status="pending",
            event_id="ev-missing",
        )
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-orphan-sum",
            now_fn=_fixed_now_iso,
            known_event_ids=set(),  # empty = everything is orphaned
        )
        summary = build_recovery_summary(ledger)
        assert summary.unrecoverable_items == 1
        assert summary.total_items == 1
        assert summary.consistency_valid is True


# ---------------------------------------------------------------------------
# Recovery output: no-op safe state (all terminal)
# ---------------------------------------------------------------------------


class TestRecoveryNoOpSafeState:
    """When all items are terminal, recovery is a safe no-op."""

    def test_all_terminal_unrecoverable(self) -> None:
        items = [
            _make_outbox_item(outbox_id="ob-1", status="sent"),
            _make_outbox_item(outbox_id="ob-2", status="dead_lettered"),
            _make_outbox_item(outbox_id="ob-3", status="cancelled"),
        ]
        ledger = build_startup_recovery_ledger(
            items,
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-terminal",
            now_fn=_fixed_now_iso,
        )
        summary = build_recovery_summary(ledger)
        assert summary.total_items == 3
        assert summary.unrecoverable_items == 3
        assert summary.recoverable_items == 0
        assert summary.claimed_items == 0
        assert summary.skipped_items == 0
        assert summary.consistency_valid is True

    def test_empty_ledger_is_safe_no_op(self) -> None:
        ledger = build_startup_recovery_ledger(
            [],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-empty",
            now_fn=_fixed_now_iso,
        )
        summary = build_recovery_summary(ledger)
        assert summary.total_items == 0
        assert summary.consistency_valid is True

    def test_terminal_reason_in_output(self) -> None:
        item = _make_outbox_item(outbox_id="ob-t", status="sent")
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-term-reason",
            now_fn=_fixed_now_iso,
        )
        action = ledger.actions[0]
        assert "terminal" in action.reason.lower()


# ---------------------------------------------------------------------------
# Recovery output: manual intervention (suppression_reason)
# ---------------------------------------------------------------------------


class TestRecoveryManualIntervention:
    """Suppression reasons surface for operator manual intervention."""

    def test_suppression_kind_in_permanent_set(self) -> None:
        """Verify loop_suppressed and policy_suppressed are in PERMANENT_KINDS.

        The recover command's _build_event_recovery_runbook derives
        suppression_reason from receipts whose failure_kind matches
        capability_suppressed, policy_suppressed, or loop_suppressed.
        loop_suppressed and policy_suppressed are explicitly in PERMANENT_KINDS.
        capability_suppressed is handled via _derive_capability_evidence
        (which inspects error message patterns) rather than failure_category.

        The actual CLI integration is tested in
        test_cli_operator_surface.py::TestF4SuppressionReasonInRecover.
        """
        from medre.core.observability.classification import (
            PERMANENT_KINDS,
            failure_category,
        )

        # loop_suppressed and policy_suppressed are classified as permanent.
        assert "loop_suppressed" in PERMANENT_KINDS
        assert "policy_suppressed" in PERMANENT_KINDS
        assert failure_category("loop_suppressed") == "permanent"
        assert failure_category("policy_suppressed") == "permanent"

        # capability_suppressed is not in the standard category sets;
        # it is handled by _derive_capability_evidence which checks
        # error message patterns directly.
        assert failure_category("capability_suppressed") == "unknown"

    def test_recommended_commands_for_permanent(self) -> None:
        """Permanent failures recommend inspect (manual investigation)."""
        from medre.core.observability.classification import recommended_commands

        cmds = recommended_commands("permanent", "evt-1")
        cmd_text = " ".join(cmds)
        assert "inspect" in cmd_text

    def test_recommended_commands_for_operational(self) -> None:
        """Operational failures recommend diagnostics (manual intervention)."""
        from medre.core.observability.classification import recommended_commands

        cmds = recommended_commands("operational", "evt-1")
        cmd_text = " ".join(cmds)
        assert "diagnostics" in cmd_text or "config" in cmd_text


# ---------------------------------------------------------------------------
# Recovery output: skipped active/unowned (retry_eligible)
# ---------------------------------------------------------------------------


class TestRecoverySkippedActiveUnowned:
    """Items with active leases or future retry are skipped, not claimed."""

    def test_retry_wait_future_skipped(self) -> None:
        """retry_wait with future next_attempt_at -> SKIPPED."""
        future = (_fixed_now_dt() + timedelta(hours=1)).isoformat()
        item = _make_outbox_item(
            outbox_id="ob-retry",
            status="retry_wait",
            next_attempt_at=future,
        )
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-retry",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        action = ledger.actions[0]
        assert action.ownership_action == str(RecoveryOwnershipStatus.SKIPPED)
        assert "not yet due" in action.reason

    def test_queued_recent_skipped(self) -> None:
        """Queued item within grace period -> SKIPPED."""
        recent = (_fixed_now_dt() - timedelta(minutes=1)).isoformat()
        item = _make_outbox_item(
            outbox_id="ob-q",
            status="queued",
            updated_at=recent,
        )
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-q",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        action = ledger.actions[0]
        assert action.ownership_action == str(RecoveryOwnershipStatus.SKIPPED)
        assert "grace" in action.reason.lower()

    def test_summary_skipped_count_matches(self) -> None:
        future = (_fixed_now_dt() + timedelta(hours=1)).isoformat()
        recent = (_fixed_now_dt() - timedelta(minutes=1)).isoformat()
        items = [
            _make_outbox_item(
                outbox_id="ob-r1",
                status="retry_wait",
                next_attempt_at=future,
            ),
            _make_outbox_item(
                outbox_id="ob-q1",
                status="queued",
                updated_at=recent,
            ),
        ]
        ledger = build_startup_recovery_ledger(
            items,
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-skip-count",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        summary = build_recovery_summary(ledger)
        assert summary.skipped_items == 2
        assert summary.consistency_valid is True


# ---------------------------------------------------------------------------
# ReplaySummary to_dict key stability
# ---------------------------------------------------------------------------


class TestReplaySummaryDictStability:
    """ReplaySummary.to_dict has deterministic, sorted keys including skip_reasons."""

    def test_keys_sorted(self) -> None:
        summary = ReplaySummary(
            events_scanned=1,
            events_replayed=1,
            skip_reasons={"dry_run": 1},
        )
        d = summary.to_dict()
        keys = list(d.keys())
        assert keys == sorted(keys)

    def test_skip_reasons_present_even_when_empty(self) -> None:
        summary = ReplaySummary()
        d = summary.to_dict()
        assert "skip_reasons" in d
        assert d["skip_reasons"] == {}

    def test_skip_reasons_sorted_keys(self) -> None:
        summary = ReplaySummary(
            skip_reasons={"cancelled": 1, "dry_run": 2, "event_missing": 1},
        )
        d = summary.to_dict()
        sr_keys = list(d["skip_reasons"].keys())
        assert sr_keys == sorted(sr_keys)


# ---------------------------------------------------------------------------
# RecoverySummary to_dict key stability
# ---------------------------------------------------------------------------


class TestRecoverySummaryDictStability:
    """RecoverySummary.to_dict has deterministic keys."""

    def test_keys_sorted(self) -> None:
        ledger = build_startup_recovery_ledger(
            [],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-dict",
            now_fn=_fixed_now_iso,
        )
        summary = build_recovery_summary(ledger)
        d = summary.to_dict()
        keys = list(d.keys())
        assert keys == sorted(keys)

    def test_all_zero_safe_state(self) -> None:
        ledger = build_startup_recovery_ledger(
            [],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-safe",
            now_fn=_fixed_now_iso,
        )
        summary = build_recovery_summary(ledger)
        d = summary.to_dict()
        assert d["total_items"] == 0
        assert d["consistency_valid"] is True
        assert d["recoverable_items"] == 0
        assert d["claimed_items"] == 0
        assert d["skipped_items"] == 0
        assert d["unrecoverable_items"] == 0


# ---------------------------------------------------------------------------
# Recovery source attribution
# ---------------------------------------------------------------------------


class TestRecoverySourceAttribution:
    """Recovery actions carry correct source attribution."""

    def test_startup_recovery_source(self) -> None:
        item = _make_outbox_item(status="pending")
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-src-startup",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        assert ledger.actions[0].recovery_source == str(RecoverySource.STARTUP_RECOVERY)

    def test_snapshot_diagnostics_source(self) -> None:
        item = _make_outbox_item(status="pending")
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=None,  # No startup -> snapshot diagnostics
            recovery_run_id="run-src-snap",
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        assert ledger.actions[0].recovery_source == str(
            RecoverySource.SNAPSHOT_DIAGNOSTICS
        )

    def test_explicit_source_override(self) -> None:
        item = _make_outbox_item(status="pending")
        ledger = build_startup_recovery_ledger(
            [item],
            startup_timestamp=_fixed_now_iso(),
            recovery_run_id="run-src-override",
            recovery_source=str(RecoverySource.RETRY_WORKER_RECOVERY),
            now_fn=_fixed_now_iso,
            known_event_ids={"ev-1"},
        )
        assert ledger.actions[0].recovery_source == str(
            RecoverySource.RETRY_WORKER_RECOVERY
        )
