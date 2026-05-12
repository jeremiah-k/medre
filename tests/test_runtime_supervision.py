"""Tests for runtime supervision classification (Contract 56).

Covers:
- RuntimeHealth classification for all meaningful adapter state combinations.
- AdapterFailureSeverity classification (fatal vs non-fatal).
- StartupOutcome classification (success, partial, total failure).
- Deterministic runtime supervision snapshot output.
- Edge cases: empty adapter set, single adapter, transitional states.
- Key invariant: classify_runtime_health() correctly identifies a single
  FAILED adapter among READY adapters as DEGRADED (not FAILED), confirming
  that classification treats single-adapter failure as non-fatal.

Uses only AdapterState values and pure classification functions; no live
transport dependencies. These tests verify classification correctness only —
they do not exercise active runtime state transitions or post-start failure
detection.
"""

from __future__ import annotations

import json

import pytest

from medre.core.lifecycle.states import AdapterState
from medre.core.runtime.supervision import (
    AdapterFailureSeverity,
    RuntimeHealth,
    StartupOutcome,
    classify_adapter_failure_severity,
    classify_runtime_health,
    classify_startup_outcome,
    runtime_supervision_snapshot,
)


# ===================================================================
# RuntimeHealth classification
# ===================================================================


class TestClassifyRuntimeHealth:
    """classify_runtime_health() produces correct RuntimeHealth values."""

    # -- Empty / zero adapters -----------------------------------------

    def test_empty_sequence_returns_failed(self) -> None:
        """Zero adapters means FAILED — no routing capability."""
        assert classify_runtime_health([]) == RuntimeHealth.FAILED

    # -- Single adapter ------------------------------------------------

    def test_single_ready_is_healthy(self) -> None:
        assert classify_runtime_health([AdapterState.READY]) == RuntimeHealth.HEALTHY

    def test_single_failed_is_failed(self) -> None:
        assert classify_runtime_health([AdapterState.FAILED]) == RuntimeHealth.FAILED

    def test_single_degraded_is_degraded(self) -> None:
        assert classify_runtime_health([AdapterState.DEGRADED]) == RuntimeHealth.DEGRADED

    def test_single_backpressured_is_degraded(self) -> None:
        assert classify_runtime_health([AdapterState.BACKPRESSURED]) == RuntimeHealth.DEGRADED

    def test_single_disconnected_is_degraded(self) -> None:
        assert classify_runtime_health([AdapterState.DISCONNECTED]) == RuntimeHealth.DEGRADED

    def test_single_initializing_is_failed(self) -> None:
        """INITIALIZING only — not yet operational."""
        assert classify_runtime_health([AdapterState.INITIALIZING]) == RuntimeHealth.FAILED

    def test_single_stopping_is_failed(self) -> None:
        """STOPPING — transitional, not operational."""
        assert classify_runtime_health([AdapterState.STOPPING]) == RuntimeHealth.FAILED

    # -- Multiple adapters, all same state -----------------------------

    def test_all_ready_is_healthy(self) -> None:
        states = [AdapterState.READY, AdapterState.READY, AdapterState.READY]
        assert classify_runtime_health(states) == RuntimeHealth.HEALTHY

    def test_all_failed_is_failed(self) -> None:
        states = [AdapterState.FAILED, AdapterState.FAILED]
        assert classify_runtime_health(states) == RuntimeHealth.FAILED

    def test_all_degraded_is_degraded(self) -> None:
        states = [AdapterState.DEGRADED, AdapterState.DEGRADED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_all_backpressured_is_degraded(self) -> None:
        states = [AdapterState.BACKPRESSURED, AdapterState.BACKPRESSURED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_all_initializing_is_failed(self) -> None:
        states = [AdapterState.INITIALIZING, AdapterState.INITIALIZING]
        assert classify_runtime_health(states) == RuntimeHealth.FAILED

    def test_all_stopping_is_failed(self) -> None:
        states = [AdapterState.STOPPING, AdapterState.STOPPING]
        assert classify_runtime_health(states) == RuntimeHealth.FAILED

    # -- Mixed states: healthy + failures ------------------------------

    def test_ready_plus_failed_is_degraded(self) -> None:
        """classify_runtime_health() correctly classifies [READY, FAILED] as DEGRADED."""
        states = [AdapterState.READY, AdapterState.FAILED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_one_healthy_two_failed_is_degraded(self) -> None:
        """Majority failed but at least one operational → degraded."""
        states = [AdapterState.READY, AdapterState.FAILED, AdapterState.FAILED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_ready_plus_degraded_is_degraded(self) -> None:
        states = [AdapterState.READY, AdapterState.DEGRADED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_ready_plus_backpressured_is_degraded(self) -> None:
        states = [AdapterState.READY, AdapterState.BACKPRESSURED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_ready_plus_disconnected_is_degraded(self) -> None:
        states = [AdapterState.READY, AdapterState.DISCONNECTED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_ready_plus_initializing_is_degraded(self) -> None:
        states = [AdapterState.READY, AdapterState.INITIALIZING]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_ready_plus_stopping_is_degraded(self) -> None:
        states = [AdapterState.READY, AdapterState.STOPPING]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    # -- Mixed states: no READY, but partial ---------------------------

    def test_degraded_plus_failed_is_degraded(self) -> None:
        """No READY, but partial capability → degraded."""
        states = [AdapterState.DEGRADED, AdapterState.FAILED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_backpressured_plus_failed_is_degraded(self) -> None:
        states = [AdapterState.BACKPRESSURED, AdapterState.FAILED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_disconnected_plus_failed_is_degraded(self) -> None:
        states = [AdapterState.DISCONNECTED, AdapterState.FAILED]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    # -- Mixed transitional states -------------------------------------

    def test_initializing_plus_failed_is_failed(self) -> None:
        """INITIALIZING + FAILED — no operational capability."""
        states = [AdapterState.INITIALIZING, AdapterState.FAILED]
        assert classify_runtime_health(states) == RuntimeHealth.FAILED

    def test_stopping_plus_failed_is_failed(self) -> None:
        states = [AdapterState.STOPPING, AdapterState.FAILED]
        assert classify_runtime_health(states) == RuntimeHealth.FAILED

    def test_initializing_plus_stopping_is_failed(self) -> None:
        states = [AdapterState.INITIALIZING, AdapterState.STOPPING]
        assert classify_runtime_health(states) == RuntimeHealth.FAILED

    # -- Larger mixed scenarios ----------------------------------------

    def test_large_mixed_with_one_ready_is_degraded(self) -> None:
        """Even with many failures, one READY keeps runtime degraded."""
        states = [
            AdapterState.READY,
            AdapterState.FAILED,
            AdapterState.DEGRADED,
            AdapterState.DISCONNECTED,
            AdapterState.BACKPRESSURED,
        ]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED

    def test_all_four_partial_states_degraded(self) -> None:
        states = [
            AdapterState.DEGRADED,
            AdapterState.BACKPRESSURED,
            AdapterState.DISCONNECTED,
            AdapterState.DEGRADED,
        ]
        assert classify_runtime_health(states) == RuntimeHealth.DEGRADED


# ===================================================================
# AdapterFailureSeverity classification
# ===================================================================


class TestClassifyAdapterFailureSeverity:
    """classify_adapter_failure_severity() produces correct severity."""

    def test_zero_healthy_zero_total_is_fatal(self) -> None:
        """Zero adapters at all → fatal."""
        assert classify_adapter_failure_severity(0, 0) == AdapterFailureSeverity.FATAL

    def test_zero_healthy_nonzero_total_is_fatal(self) -> None:
        """All adapters failed → fatal."""
        assert classify_adapter_failure_severity(0, 3) == AdapterFailureSeverity.FATAL

    def test_one_healthy_one_total_is_non_fatal(self) -> None:
        assert classify_adapter_failure_severity(1, 1) == AdapterFailureSeverity.NON_FATAL

    def test_one_healthy_three_total_is_non_fatal(self) -> None:
        """One adapter in FAILED state with others healthy is NON_FATAL (classification invariant)."""
        assert classify_adapter_failure_severity(1, 3) == AdapterFailureSeverity.NON_FATAL

    def test_all_healthy_is_non_fatal(self) -> None:
        assert classify_adapter_failure_severity(5, 5) == AdapterFailureSeverity.NON_FATAL

    def test_severity_reclassifies_as_adapters_recover(self) -> None:
        """When adapters recover, severity goes back to non-fatal."""
        # Initially all down
        assert classify_adapter_failure_severity(0, 3) == AdapterFailureSeverity.FATAL
        # One recovers
        assert classify_adapter_failure_severity(1, 3) == AdapterFailureSeverity.NON_FATAL
        # All recover
        assert classify_adapter_failure_severity(3, 3) == AdapterFailureSeverity.NON_FATAL


# ===================================================================
# StartupOutcome classification
# ===================================================================


class TestClassifyStartupOutcome:
    """classify_startup_outcome() produces correct outcomes."""

    # -- Zero adapters -------------------------------------------------

    def test_zero_total_is_total_failure(self) -> None:
        assert classify_startup_outcome(0, 0, 0) == StartupOutcome.TOTAL_FAILURE

    # -- All succeed ---------------------------------------------------

    def test_all_started_is_success(self) -> None:
        assert classify_startup_outcome(3, 0, 3) == StartupOutcome.SUCCESS

    def test_single_started_is_success(self) -> None:
        assert classify_startup_outcome(1, 0, 1) == StartupOutcome.SUCCESS

    # -- Total failure -------------------------------------------------

    def test_zero_started_nonzero_total_is_total_failure(self) -> None:
        """Zero adapters started at startup => runtime startup failure."""
        assert classify_startup_outcome(0, 3, 3) == StartupOutcome.TOTAL_FAILURE

    # -- Partial startup -----------------------------------------------

    def test_partial_startup_is_partial(self) -> None:
        """Partial startup => degraded runtime allowed."""
        assert classify_startup_outcome(2, 1, 3) == StartupOutcome.PARTIAL

    def test_one_of_three_started_is_partial(self) -> None:
        assert classify_startup_outcome(1, 2, 3) == StartupOutcome.PARTIAL

    def test_two_of_three_started_is_partial(self) -> None:
        assert classify_startup_outcome(2, 1, 3) == StartupOutcome.PARTIAL

    # -- Edge cases ----------------------------------------------------

    def test_started_exceeds_total_still_success(self) -> None:
        """Even if counts are inconsistent, started == total → success."""
        assert classify_startup_outcome(3, 0, 3) == StartupOutcome.SUCCESS

    def test_no_failures_all_started(self) -> None:
        assert classify_startup_outcome(5, 0, 5) == StartupOutcome.SUCCESS


# ===================================================================
# Runtime supervision snapshot
# ===================================================================


class TestRuntimeSupervisionSnapshot:
    """runtime_supervision_snapshot() produces deterministic diagnostic dicts."""

    def test_snapshot_is_json_serializable(self) -> None:
        states = [AdapterState.READY, AdapterState.FAILED]
        snapshot = runtime_supervision_snapshot(states)
        # Must not raise
        serialized = json.dumps(snapshot, sort_keys=True)
        assert isinstance(serialized, str)

    def test_snapshot_empty_states(self) -> None:
        snapshot = runtime_supervision_snapshot([])
        assert snapshot["runtime_health"] == "failed"
        assert snapshot["adapter_summary"]["total"] == 0
        assert snapshot["startup_fingerprint"] == ""

    def test_snapshot_all_healthy(self) -> None:
        states = [AdapterState.READY, AdapterState.READY]
        snapshot = runtime_supervision_snapshot(states)
        assert snapshot["runtime_health"] == "healthy"
        assert snapshot["adapter_summary"]["healthy"] == 2
        assert snapshot["adapter_summary"]["failed"] == 0

    def test_snapshot_mixed_states(self) -> None:
        states = [AdapterState.READY, AdapterState.FAILED, AdapterState.DEGRADED]
        snapshot = runtime_supervision_snapshot(states)
        assert snapshot["runtime_health"] == "degraded"
        assert snapshot["adapter_summary"]["healthy"] == 1
        assert snapshot["adapter_summary"]["failed"] == 1
        assert snapshot["adapter_summary"]["degraded"] == 1
        assert snapshot["adapter_summary"]["total"] == 3

    def test_snapshot_deterministic(self) -> None:
        """Two calls with same input produce identical output."""
        states = [AdapterState.READY, AdapterState.FAILED]
        snap1 = runtime_supervision_snapshot(states)
        snap2 = runtime_supervision_snapshot(states)
        assert snap1 == snap2

    def test_snapshot_fingerprint_is_sorted(self) -> None:
        """Startup fingerprint keys are sorted deterministically."""
        states = [
            AdapterState.FAILED,
            AdapterState.READY,
            AdapterState.DEGRADED,
        ]
        snapshot = runtime_supervision_snapshot(states)
        # Fingerprint should be "degraded=1, failed=1, ready=1" (sorted)
        assert snapshot["startup_fingerprint"] == "degraded=1, failed=1, ready=1"

    def test_snapshot_all_failed(self) -> None:
        states = [AdapterState.FAILED, AdapterState.FAILED, AdapterState.FAILED]
        snapshot = runtime_supervision_snapshot(states)
        assert snapshot["runtime_health"] == "failed"
        assert snapshot["adapter_summary"]["failed"] == 3


# ===================================================================
# Enum value tests
# ===================================================================


class TestRuntimeHealthEnum:
    """RuntimeHealth enum has expected values."""

    def test_healthy_value(self) -> None:
        assert RuntimeHealth.HEALTHY.value == "healthy"

    def test_degraded_value(self) -> None:
        assert RuntimeHealth.DEGRADED.value == "degraded"

    def test_failed_value(self) -> None:
        assert RuntimeHealth.FAILED.value == "failed"

    def test_enum_has_exactly_three_members(self) -> None:
        assert len(RuntimeHealth) == 3


class TestAdapterFailureSeverityEnum:
    """AdapterFailureSeverity enum has expected values."""

    def test_fatal_value(self) -> None:
        assert AdapterFailureSeverity.FATAL.value == "fatal"

    def test_non_fatal_value(self) -> None:
        assert AdapterFailureSeverity.NON_FATAL.value == "non_fatal"

    def test_enum_has_exactly_two_members(self) -> None:
        assert len(AdapterFailureSeverity) == 2


class TestStartupOutcomeEnum:
    """StartupOutcome enum has expected values."""

    def test_success_value(self) -> None:
        assert StartupOutcome.SUCCESS.value == "success"

    def test_partial_value(self) -> None:
        assert StartupOutcome.PARTIAL.value == "partial"

    def test_total_failure_value(self) -> None:
        assert StartupOutcome.TOTAL_FAILURE.value == "total_failure"

    def test_enum_has_exactly_three_members(self) -> None:
        assert len(StartupOutcome) == 3


# ===================================================================
# Integration: health transitions during adapter lifecycle
# ===================================================================


class TestRuntimeHealthTransitionIntegration:
    """Classification correctness across varying adapter state sequences.

    These tests verify that the pure classification functions return correct
    results when called with different adapter state combinations. They call
    classify_runtime_health() and runtime_supervision_snapshot() with
    explicitly supplied AdapterState sequences — no live transport dependencies,
    no runtime state transitions, and no active failure detection.

    The "transition" tested is classification correctness: the same function
    returns different (correct) results given different inputs.
    """

    def test_healthy_to_degraded_on_single_failure(self) -> None:
        """classify_runtime_health() returns DEGRADED when one READY is replaced with FAILED."""
        # Initially all healthy.
        assert classify_runtime_health([AdapterState.READY, AdapterState.READY]) == RuntimeHealth.HEALTHY

        # One adapter crashes.
        assert classify_runtime_health([AdapterState.READY, AdapterState.FAILED]) == RuntimeHealth.DEGRADED

    def test_degraded_to_failed_on_last_adapter_failure(self) -> None:
        """classify_runtime_health() returns FAILED when all states become FAILED."""
        # Degraded: one healthy, one failed.
        assert classify_runtime_health([AdapterState.READY, AdapterState.FAILED]) == RuntimeHealth.DEGRADED

        # Last adapter fails.
        assert classify_runtime_health([AdapterState.FAILED, AdapterState.FAILED]) == RuntimeHealth.FAILED

    def test_failure_severity_transitions_fatal_to_nonfatal(self) -> None:
        """Failure severity reclassifies as adapters recover."""
        # All down → fatal.
        assert classify_adapter_failure_severity(0, 3) == AdapterFailureSeverity.FATAL

        # One recovers → non-fatal.
        assert classify_adapter_failure_severity(1, 3) == AdapterFailureSeverity.NON_FATAL

        # All recover → non-fatal.
        assert classify_adapter_failure_severity(3, 3) == AdapterFailureSeverity.NON_FATAL

    def test_supervision_snapshot_after_cascade_failure(self) -> None:
        """Supervision snapshot correctly classifies cascade failure states.

        Tests classification with three different state combinations:
        all READY, one READY + three FAILED, and all FAILED.
        """
        # Initial: all healthy.
        initial_states = [AdapterState.READY] * 4
        initial_snap = runtime_supervision_snapshot(initial_states)
        assert initial_snap["runtime_health"] == "healthy"
        assert initial_snap["adapter_summary"]["healthy"] == 4

        # After cascade: 1 healthy, 3 failed.
        cascade_states = [AdapterState.READY, AdapterState.FAILED, AdapterState.FAILED, AdapterState.FAILED]
        cascade_snap = runtime_supervision_snapshot(cascade_states)
        assert cascade_snap["runtime_health"] == "degraded"
        assert cascade_snap["adapter_summary"]["healthy"] == 1
        assert cascade_snap["adapter_summary"]["failed"] == 3

        # Total failure.
        total_failure_states = [AdapterState.FAILED] * 4
        total_snap = runtime_supervision_snapshot(total_failure_states)
        assert total_snap["runtime_health"] == "failed"
        assert total_snap["adapter_summary"]["failed"] == 4

    def test_health_classifications_deterministic_across_repeated_calls(self) -> None:
        """Repeated classifications with same input produce same results."""
        states = [AdapterState.READY, AdapterState.FAILED, AdapterState.DEGRADED]
        results = [classify_runtime_health(states) for _ in range(20)]
        assert all(r == RuntimeHealth.DEGRADED for r in results)

    def test_fingerprint_changes_as_states_change(self) -> None:
        """Supervision fingerprint changes as adapter states evolve."""
        snap_healthy = runtime_supervision_snapshot([AdapterState.READY, AdapterState.READY])
        snap_degraded = runtime_supervision_snapshot([AdapterState.READY, AdapterState.FAILED])
        snap_failed = runtime_supervision_snapshot([AdapterState.FAILED, AdapterState.FAILED])

        assert snap_healthy["startup_fingerprint"] != snap_degraded["startup_fingerprint"]
        assert snap_degraded["startup_fingerprint"] != snap_failed["startup_fingerprint"]
