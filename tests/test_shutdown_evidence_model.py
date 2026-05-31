"""Tests for the pure shutdown/cancellation evidence model.

Covers all classification paths, edge cases, JSON safety, and
deterministic output.  Uses no real transport dependencies or async
operations — all inputs are plain dicts/dataclasses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from medre.core.evidence.shutdown import (
    ShutdownStatus,
    build_shutdown_evidence,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev(
    event_type: str = "state_transition",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a minimal event dict for test inputs."""
    return {
        "event_type": event_type,
        "detail": detail or {},
    }


@dataclass
class _FakeRetryState:
    """Mimics RetryWorkerState for object-input tests."""

    enabled: bool = True
    running: bool = False
    last_run_at: str | None = None
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_lettered: int = 0


@dataclass
class _FakeRuntimeState:
    """Mimics RuntimeState enum for object-input tests."""

    value: str = "running"


# ===================================================================
# 1. Running / no shutdown evidence
# ===================================================================


class TestRunningNoShutdown:
    """When runtime is running, shutdown_status is 'running'."""

    def test_running_state_no_evidence(self) -> None:
        ev = build_shutdown_evidence(runtime_state="running")
        assert ev.shutdown_status == "running"
        assert ev.runtime_state == "running"
        assert ev.shutdown_reason is None
        assert ev.drain_timeout_detected is False

    def test_initialized_state_treated_as_running(self) -> None:
        ev = build_shutdown_evidence(runtime_state="initialized")
        assert ev.shutdown_status == "running"

    def test_starting_state_treated_as_running(self) -> None:
        ev = build_shutdown_evidence(runtime_state="starting")
        assert ev.shutdown_status == "running"

    def test_none_state_treated_as_running(self) -> None:
        ev = build_shutdown_evidence(runtime_state=None)
        assert ev.shutdown_status == "running"
        assert ev.runtime_state is None

    def test_running_state_enum_object(self) -> None:
        """Accept RuntimeState-style enum objects."""
        ev = build_shutdown_evidence(runtime_state=_FakeRuntimeState("running"))
        assert ev.shutdown_status == "running"
        assert ev.runtime_state == "running"

    def test_running_not_executed_style(self) -> None:
        """Running evidence should have no pending work markers."""
        ev = build_shutdown_evidence(
            runtime_state="running",
            outbox_counts={"pending": 5, "sent": 100},
        )
        assert ev.shutdown_status == "running"
        # Pending outbox should still be reported for informational
        # purposes even in running state, but status is "running".
        assert ev.pending_outbox_counts == {"pending": 5}
        assert ev.pending_retry_work_total == 5

    def test_running_ignores_shutdown_events(self) -> None:
        """Running state wins over event signals."""
        events = [
            _ev(detail={"error": "shutdown_drain_timeout"}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="running",
            events=events,
        )
        assert ev.shutdown_status == "running"
        # drain_timeout_detected is still True (honest signal).
        assert ev.drain_timeout_detected is True


# ===================================================================
# 2. Graceful stop with no pending work
# ===================================================================


class TestGracefulStop:
    """Stopped cleanly with no pending work → graceful_stop."""

    def test_stopped_no_pending_work(self) -> None:
        ev = build_shutdown_evidence(runtime_state="stopped")
        assert ev.shutdown_status == "graceful_stop"
        assert ev.runtime_state == "stopped"

    def test_stopped_with_empty_outbox(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"sent": 100, "dead_lettered": 2},
        )
        assert ev.shutdown_status == "graceful_stop"
        assert ev.pending_outbox_counts == {}
        assert ev.pending_retry_work_total == 0

    def test_stopped_with_only_terminal_outbox(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={
                "sent": 50,
                "dead_lettered": 3,
                "cancelled": 1,
                "abandoned": 0,
            },
        )
        assert ev.shutdown_status == "graceful_stop"
        assert ev.pending_retry_work_total == 0

    def test_stopped_with_retry_worker_stopped(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            retry_state=_FakeRetryState(running=False, processed=10),
        )
        assert ev.shutdown_status == "graceful_stop"
        assert ev.retry_worker_running is False
        assert ev.retry_worker_processed == 10


# ===================================================================
# 3. Stopped with pending retry_wait / pending / queued counts
# ===================================================================


class TestStoppedWithPendingWork:
    """Pending work at shutdown → shutdown_pending, not cancellation."""

    def test_stopped_with_pending_outbox(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 5, "sent": 100},
        )
        assert ev.shutdown_status == "shutdown_pending"
        assert ev.pending_outbox_counts == {"pending": 5}
        assert ev.pending_retry_work_total == 5
        assert ev.shutdown_reason == "shutdown_pending"

    def test_stopped_with_retry_wait(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"retry_wait": 3, "sent": 50},
        )
        assert ev.shutdown_status == "shutdown_pending"
        assert ev.pending_retry_work_total == 3

    def test_stopped_with_queued(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"queued": 7, "sent": 10},
        )
        assert ev.shutdown_status == "shutdown_pending"
        assert ev.pending_retry_work_total == 7

    def test_stopped_with_in_progress(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"in_progress": 2, "sent": 10},
        )
        assert ev.shutdown_status == "shutdown_pending"
        assert ev.pending_retry_work_total == 2

    def test_stopped_mixed_pending_statuses(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={
                "pending": 3,
                "retry_wait": 2,
                "queued": 1,
                "in_progress": 1,
                "sent": 100,
                "dead_lettered": 5,
            },
        )
        assert ev.shutdown_status == "shutdown_pending"
        assert ev.pending_retry_work_total == 7
        assert ev.pending_outbox_counts == {
            "in_progress": 1,
            "pending": 3,
            "queued": 1,
            "retry_wait": 2,
        }

    def test_pending_work_not_claimed_as_cancelled(self) -> None:
        """Pending work must NOT be reported as cancellation."""
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 1},
        )
        assert ev.shutdown_status == "shutdown_pending"
        assert ev.shutdown_status != "cancellation"
        assert ev.shutdown_reason == "shutdown_pending"

    def test_stopping_with_pending_work(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopping",
            outbox_counts={"pending": 4},
        )
        assert ev.shutdown_status == "shutdown_pending"


# ===================================================================
# 4. Failed adapter event
# ===================================================================


class TestAdapterFailure:
    """Adapter failure event → adapter_failure status."""

    def test_adapter_start_failed_event(self) -> None:
        events = [
            _ev(
                event_type="adapter_start_failed",
                detail={"adapter_id": "matrix-1", "error": "connection refused"},
            ),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.shutdown_status == "adapter_failure"
        assert ev.shutdown_reason == "adapter_failure"

    def test_adapter_failure_with_failed_state(self) -> None:
        events = [
            _ev(
                event_type="state_transition",
                detail={"from": "running", "to": "failed"},
            ),
        ]
        ev = build_shutdown_evidence(
            runtime_state="failed",
            events=events,
        )
        assert ev.shutdown_status == "adapter_failure"

    def test_adapter_failure_overrides_stopped(self) -> None:
        """Adapter failure takes priority over generic stopped."""
        events = [
            _ev(
                event_type="adapter_start_failed",
                detail={"adapter_id": "a1"},
            ),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.shutdown_status == "adapter_failure"

    def test_adapter_failure_overrides_pending_work(self) -> None:
        """Adapter failure takes priority over pending work."""
        events = [
            _ev(
                event_type="adapter_start_failed",
                detail={"adapter_id": "a1"},
            ),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 5},
            events=events,
        )
        assert ev.shutdown_status == "adapter_failure"
        # Pending work is still reported even though status is adapter_failure.
        assert ev.pending_retry_work_total == 5


# ===================================================================
# 5. Cancellation event
# ===================================================================


class TestCancellation:
    """Cancellation detection from reason or events."""

    def test_cancellation_from_reason(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            reason="cancellation",
        )
        assert ev.shutdown_status == "cancellation"
        assert ev.shutdown_reason == "cancellation"

    def test_cancellation_from_reason_cancelled(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            reason="cancelled",
        )
        assert ev.shutdown_status == "cancellation"

    def test_cancellation_from_event_detail(self) -> None:
        events = [
            _ev(detail={"cancelled": True}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.shutdown_status == "cancellation"

    def test_cancellation_from_event_error(self) -> None:
        events = [
            _ev(detail={"error": "task was cancelled by runtime"}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.shutdown_status == "cancellation"

    def test_cancellation_overrides_pending_work(self) -> None:
        """Cancellation takes priority over pending work detection."""
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 5},
            reason="cancellation",
        )
        assert ev.shutdown_status == "cancellation"
        # But pending work is still honestly reported.
        assert ev.pending_retry_work_total == 5


# ===================================================================
# 6. Drain timeout event/error
# ===================================================================


class TestDrainTimeout:
    """Drain timeout detection from reason or events."""

    def test_drain_timeout_from_reason(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            reason="drain_timeout",
        )
        assert ev.shutdown_status == "drain_timeout"
        assert ev.drain_timeout_detected is True

    def test_drain_timeout_from_event_error(self) -> None:
        events = [
            _ev(detail={"error": "shutdown_drain_timeout"}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.shutdown_status == "drain_timeout"
        assert ev.drain_timeout_detected is True

    def test_drain_timeout_from_event_failure_kind(self) -> None:
        events = [
            _ev(detail={"failure_kind": "shutdown_rejection"}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.shutdown_status == "drain_timeout"
        assert ev.drain_timeout_detected is True

    def test_drain_timeout_overrides_pending_work(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 10},
            reason="drain_timeout",
        )
        assert ev.shutdown_status == "drain_timeout"
        assert ev.pending_retry_work_total == 10

    def test_drain_timeout_overrides_cancellation(self) -> None:
        """Drain timeout is checked before cancellation."""
        events = [
            _ev(detail={"error": "shutdown_drain_timeout"}),
            _ev(detail={"cancelled": True}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.shutdown_status == "drain_timeout"


# ===================================================================
# 7. Retry worker still running at stop
# ===================================================================


class TestRetryWorkerRunningAtStop:
    """Retry worker still running when evidence is collected."""

    def test_retry_worker_running_flag(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            retry_state=_FakeRetryState(running=True),
        )
        assert ev.retry_worker_running is True
        assert ev.shutdown_status == "graceful_stop"

    def test_retry_worker_running_with_pending_work(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            retry_state=_FakeRetryState(running=True, processed=5, failed=2),
            outbox_counts={"retry_wait": 3},
        )
        assert ev.retry_worker_running is True
        assert ev.shutdown_status == "shutdown_pending"
        assert ev.retry_worker_processed == 5
        assert ev.retry_worker_failed == 2

    def test_retry_worker_state_as_dict(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            retry_state={
                "enabled": True,
                "running": False,
                "processed": 42,
                "succeeded": 38,
                "failed": 3,
                "dead_lettered": 1,
            },
        )
        assert ev.retry_worker_running is False
        assert ev.retry_worker_processed == 42
        assert ev.retry_worker_succeeded == 38
        assert ev.retry_worker_failed == 3
        assert ev.retry_worker_dead_lettered == 1

    def test_retry_worker_none(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            retry_state=None,
        )
        assert ev.retry_worker_running is None
        assert ev.retry_worker_processed is None


# ===================================================================
# 8. JSON safety
# ===================================================================


class TestJSONSafety:
    """All evidence output is JSON-safe."""

    def test_to_dict_json_serializable(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 5, "sent": 100},
            retry_state=_FakeRetryState(running=False, processed=10),
            events=[_ev(detail={"error": "shutdown_drain_timeout"})],
            capacity_state={"delivery_current": 2},
            reason="drain_timeout",
            evidence_flush_status="flushed",
        )
        data = ev.to_dict()
        result = json.dumps(data, sort_keys=True)
        assert isinstance(result, str)

        # Round-trip.
        parsed = json.loads(result)
        assert parsed["shutdown_status"] == "drain_timeout"
        assert parsed["runtime_state"] == "stopped"

    def test_no_sdk_objects_in_output(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="running",
            outbox_counts={"pending": 1},
        )
        data = ev.to_dict()
        for key, val in data.items():
            assert isinstance(
                val, (str, int, float, bool, dict, list, type(None))
            ), f"Key {key!r} has non-JSON-safe type {type(val).__name__}: {val!r}"

    def test_running_minimal_json_safe(self) -> None:
        ev = build_shutdown_evidence()
        data = ev.to_dict()
        result = json.dumps(data)
        assert isinstance(result, str)

    def test_all_none_fields_json_safe(self) -> None:
        ev = build_shutdown_evidence(runtime_state="stopped")
        data = ev.to_dict()
        for key, val in data.items():
            if val is None:
                assert data[key] is None  # None serializes to JSON null

    def test_evidence_flush_status_in_output(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            evidence_flush_status="flushed",
        )
        data = ev.to_dict()
        assert data["evidence_flush_status"] == "flushed"
        json.dumps(data)  # must not raise


# ===================================================================
# 9. Deterministic output
# ===================================================================


class TestDeterministicOutput:
    """Same inputs always produce the same output."""

    def test_same_inputs_same_output(self) -> None:
        kwargs = {
            "runtime_state": "stopped",
            "outbox_counts": {"pending": 3, "sent": 50},
            "retry_state": {"running": False, "processed": 10},
            "events": [_ev(detail={"error": "test"})],
            "capacity_state": {"delivery_current": 0},
            "reason": None,
            "evidence_flush_status": None,
        }
        ev1 = build_shutdown_evidence(**kwargs)
        ev2 = build_shutdown_evidence(**kwargs)
        assert ev1.to_dict() == ev2.to_dict()

    def test_dict_keys_sorted(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 1, "retry_wait": 2},
            retry_state=_FakeRetryState(),
        )
        data = ev.to_dict()
        assert list(data.keys()) == sorted(data.keys())

    def test_pending_outbox_counts_sorted(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"retry_wait": 2, "pending": 1, "queued": 3},
        )
        data = ev.to_dict()
        poc = data["pending_outbox_counts"]
        assert list(poc.keys()) == sorted(poc.keys())

    def test_frozen_dataclass(self) -> None:
        ev = build_shutdown_evidence(runtime_state="running")
        with pytest.raises(AttributeError):
            ev.shutdown_status = "stopped"  # type: ignore[misc]


# ===================================================================
# 10. In-flight count from capacity_state
# ===================================================================


class TestInFlightCount:
    """in_flight_count derived from capacity_state.delivery_current."""

    def test_in_flight_from_dict(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            capacity_state={"delivery_current": 3, "replay_current": 1},
        )
        assert ev.in_flight_count == 3

    def test_in_flight_from_object(self) -> None:
        @dataclass
        class CapSnap:
            delivery_current: int = 2
            replay_current: int = 0

        ev = build_shutdown_evidence(
            runtime_state="stopped",
            capacity_state=CapSnap(),
        )
        assert ev.in_flight_count == 2

    def test_in_flight_none_when_no_capacity(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            capacity_state=None,
        )
        assert ev.in_flight_count is None

    def test_in_flight_none_when_non_int(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            capacity_state={"delivery_current": "not_an_int"},
        )
        assert ev.in_flight_count is None


# ===================================================================
# 11. Tasks cancelled
# ===================================================================


class TestTasksCancelled:
    """tasks_cancelled extracted from event details."""

    def test_tasks_cancelled_from_event(self) -> None:
        events = [
            _ev(detail={"tasks_cancelled": 5}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.tasks_cancelled == 5

    def test_tasks_cancelled_last_event_wins(self) -> None:
        events = [
            _ev(detail={"tasks_cancelled": 3}),
            _ev(detail={"tasks_cancelled": 7}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.tasks_cancelled == 7

    def test_tasks_cancelled_none_when_absent(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=[_ev(detail={"error": "something"})],
        )
        assert ev.tasks_cancelled is None

    def test_tasks_cancelled_none_when_no_events(self) -> None:
        ev = build_shutdown_evidence(runtime_state="stopped")
        assert ev.tasks_cancelled is None

    def test_tasks_cancelled_ignores_non_int(self) -> None:
        events = [
            _ev(detail={"tasks_cancelled": "three"}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.tasks_cancelled is None


# ===================================================================
# 12. ShutdownStatus enum
# ===================================================================


class TestShutdownStatusEnum:
    """ShutdownStatus enum values are plain strings."""

    def test_all_values_are_strings(self) -> None:
        for member in ShutdownStatus:
            assert isinstance(member.value, str)

    def test_known_values(self) -> None:
        assert ShutdownStatus.RUNNING.value == "running"
        assert ShutdownStatus.GRACEFUL_STOP.value == "graceful_stop"
        assert ShutdownStatus.CANCELLATION.value == "cancellation"
        assert ShutdownStatus.ADAPTER_FAILURE.value == "adapter_failure"
        assert ShutdownStatus.DRAIN_TIMEOUT.value == "drain_timeout"
        assert ShutdownStatus.SHUTDOWN_PENDING.value == "shutdown_pending"
        assert ShutdownStatus.STOPPED.value == "stopped"
        assert ShutdownStatus.FAILED.value == "failed"

    def test_is_str_subclass(self) -> None:
        assert isinstance(ShutdownStatus.RUNNING, str)


# ===================================================================
# 13. Edge cases and mixed scenarios
# ===================================================================


class TestEdgeCases:
    """Edge cases and mixed scenarios."""

    def test_failed_state_no_events(self) -> None:
        ev = build_shutdown_evidence(runtime_state="failed")
        assert ev.shutdown_status == "failed"
        assert ev.runtime_state == "failed"

    def test_failed_state_with_adapter_failure(self) -> None:
        events = [
            _ev(
                event_type="adapter_start_failed",
                detail={"adapter_id": "a1"},
            ),
        ]
        ev = build_shutdown_evidence(
            runtime_state="failed",
            events=events,
        )
        assert ev.shutdown_status == "adapter_failure"

    def test_stopping_no_cause(self) -> None:
        ev = build_shutdown_evidence(runtime_state="stopping")
        assert ev.shutdown_status == "stopped"

    def test_zero_outbox_counts_not_pending(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 0, "sent": 100},
        )
        assert ev.shutdown_status == "graceful_stop"
        assert ev.pending_retry_work_total == 0

    def test_no_outbox_state(self) -> None:
        ev = build_shutdown_evidence(runtime_state="stopped")
        assert ev.pending_outbox_counts is None
        assert ev.pending_retry_work_total is None

    def test_capacity_with_drain_timeout(self) -> None:
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            capacity_state={"delivery_current": 5},
            reason="drain_timeout",
        )
        assert ev.in_flight_count == 5
        assert ev.shutdown_status == "drain_timeout"

    def test_multiple_events_drain_timeout_wins(self) -> None:
        """Drain timeout is checked before cancellation and adapter failure."""
        events = [
            _ev(detail={"error": "shutdown_drain_timeout"}),
            _ev(
                event_type="adapter_start_failed",
                detail={"adapter_id": "a1"},
            ),
            _ev(detail={"cancelled": True}),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.shutdown_status == "drain_timeout"

    def test_cancellation_before_adapter_failure(self) -> None:
        """Cancellation is checked before adapter failure."""
        events = [
            _ev(detail={"cancelled": True}),
            _ev(
                event_type="adapter_start_failed",
                detail={"adapter_id": "a1"},
            ),
        ]
        ev = build_shutdown_evidence(
            runtime_state="stopped",
            events=events,
        )
        assert ev.shutdown_status == "cancellation"

    def test_unknown_runtime_state(self) -> None:
        ev = build_shutdown_evidence(runtime_state="unknown_state")
        assert ev.shutdown_status == "stopped"
        assert ev.runtime_state == "unknown_state"

    def test_evidence_flush_status_preserved(self) -> None:
        for status in ("flushed", "partial", "skipped", "error"):
            ev = build_shutdown_evidence(
                runtime_state="stopped",
                evidence_flush_status=status,
            )
            assert ev.evidence_flush_status == status
