"""Pure shutdown/cancellation evidence model for the MEDRE framework.

Provides :func:`build_shutdown_evidence` which derives structured shutdown
evidence from runtime state, outbox counts, retry worker state, runtime
events, capacity state, and optional caller-supplied hints.  The function
is **pure**: no I/O, no side effects, no async, no SDK objects.

Design constraints
------------------
* **JSON-safe**: every value is ``str``/``int``/``float``/``bool``/``None``
  or a plain ``dict``/``list`` thereof.
* **Honest**: does not invent causes.  If the runtime did not cancel work,
  the evidence says ``shutdown_pending``, not ``cancellation``.
* **Dict/object tolerant**: accepts both dataclass instances and plain
  ``dict`` values for all inputs, so callers can pass snapshot dicts or
  live runtime objects interchangeably.
* **Deterministic**: output dict keys are alphabetically sorted; the same
  inputs always produce the same output.

Shutdown status values
----------------------
The :class:`ShutdownStatus` enum defines the recognised shutdown statuses:

* ``running`` — runtime is active, no shutdown in progress.
* ``graceful_stop`` — runtime stopped cleanly with no pending work.
* ``cancellation`` — explicit cancellation detected (from reason or events).
* ``adapter_failure`` — adapter failure caused or accompanied shutdown.
* ``drain_timeout`` — drain deadline expired with in-flight work.
* ``shutdown_pending`` — runtime stopped but pending outbox/retry work
  remains; work was **left pending**, not cancelled.
* ``stopped`` — runtime stopped (generic; no further classification).
* ``failed`` — runtime in ``failed`` state.

Public symbols
--------------
* :class:`ShutdownStatus` — canonical shutdown status enum.
* :class:`ShutdownEvidence` — frozen evidence record.
* :func:`build_shutdown_evidence` — main entry point.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, fields
from typing import Any, Mapping, Sequence

__all__ = [
    "OutboxShutdownClassification",
    "ShutdownEvidence",
    "ShutdownStatus",
    "build_shutdown_evidence",
    "classify_outbox_shutdown_policy",
]


# ---------------------------------------------------------------------------
# Shutdown status enum
# ---------------------------------------------------------------------------


class ShutdownStatus(str, enum.Enum):
    """Canonical shutdown status values for evidence outputs.

    Members are plain lowercase strings that serialise directly via
    ``.value``.
    """

    RUNNING = "running"
    GRACEFUL_STOP = "graceful_stop"
    CANCELLATION = "cancellation"
    ADAPTER_FAILURE = "adapter_failure"
    DRAIN_TIMEOUT = "drain_timeout"
    SHUTDOWN_PENDING = "shutdown_pending"
    STOPPED = "stopped"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Pending outbox statuses
# ---------------------------------------------------------------------------

_PENDING_OUTBOX_STATUSES: frozenset[str] = frozenset(
    {"pending", "retry_wait", "queued", "in_progress"}
)
"""Outbox statuses that indicate work has not completed.

Items with these statuses at shutdown time represent pending work that
was **not** processed before the runtime stopped.  The evidence model
reports these honestly as ``shutdown_pending`` rather than claiming they
were cancelled.
"""


# ---------------------------------------------------------------------------
# Internal helpers — input extraction
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read an attribute from *obj*, falling back to dict-style access."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _runtime_state_str(runtime_state: Any) -> str | None:
    """Normalise *runtime_state* to a plain string or ``None``."""
    if runtime_state is None:
        return None
    if isinstance(runtime_state, str):
        return runtime_state
    if hasattr(runtime_state, "value"):
        return runtime_state.value
    return str(runtime_state)


def _extract_event_type(event: Any) -> str | None:
    """Extract the event type string from a RuntimeEvent or dict."""
    et = _attr(event, "event_type", None)
    if et is None:
        return None
    if isinstance(et, str):
        return et
    if hasattr(et, "value"):
        return et.value
    return str(et)


def _extract_event_detail(event: Any) -> dict[str, Any]:
    """Extract the event detail dict from a RuntimeEvent or dict."""
    detail = _attr(event, "detail", None)
    if isinstance(detail, dict):
        return detail
    return {}


# ---------------------------------------------------------------------------
# Internal helpers — event scanning
# ---------------------------------------------------------------------------


def _has_adapter_failure_event(events: Sequence[Any]) -> bool:
    """Return ``True`` if any event signals an adapter failure."""
    for event in events:
        et = _extract_event_type(event)
        if et in ("adapter_start_failed",):
            return True
        detail = _extract_event_detail(event)
        # Adapter failure can appear as a state transition to "failed"
        # with an adapter-related detail.
        if et == "state_transition":
            to_state = detail.get("to", "")
            if to_state == "failed":
                return True
    return False


def _has_drain_timeout_signal(
    events: Sequence[Any],
    reason: str | None,
) -> bool:
    """Return ``True`` if drain timeout is detected from events or reason."""
    if reason is not None and "drain_timeout" in reason.lower():
        return True
    for event in events:
        detail = _extract_event_detail(event)
        # Check error strings for drain timeout.
        error_val = detail.get("error", "")
        if isinstance(error_val, str) and "shutdown_drain_timeout" in error_val:
            return True
        # Check failure_kind for shutdown_rejection (proxy for drain timeout).
        fk = detail.get("failure_kind", "")
        if isinstance(fk, str) and "shutdown_rejection" in fk:
            return True
    return False


def _has_cancellation_signal(
    events: Sequence[Any],
    reason: str | None,
) -> bool:
    """Return ``True`` if cancellation is detected from events or reason."""
    if reason is not None and reason.lower() in ("cancellation", "cancelled"):
        return True
    for event in events:
        detail = _extract_event_detail(event)
        # Explicit cancellation marker in detail.
        if detail.get("cancelled") or detail.get("cancellation"):
            return True
        error_val = detail.get("error", "")
        if isinstance(error_val, str) and "cancel" in error_val.lower():
            return True
    return False


def _count_tasks_cancelled(events: Sequence[Any]) -> int | None:
    """Extract tasks_cancelled from events if supplied; otherwise ``None``.

    Scans events for a ``tasks_cancelled`` key in detail dicts.  Returns
    the value from the last event that carries it (most recent), or
    ``None`` if no event carries it.
    """
    found: int | None = None
    for event in events:
        detail = _extract_event_detail(event)
        tc = detail.get("tasks_cancelled")
        if isinstance(tc, int):
            found = tc
    return found


def _compute_pending_outbox(
    outbox_counts: dict[str, int] | None,
) -> tuple[dict[str, int], int]:
    """Filter outbox counts to pending statuses and compute total.

    Returns ``(pending_counts, pending_total)`` where *pending_counts*
    includes only statuses in :data:`_PENDING_OUTBOX_STATUSES` that have
    a non-zero count, and *pending_total* is the sum of those counts.
    """
    if not outbox_counts:
        return {}, 0
    pending: dict[str, int] = {}
    total = 0
    for status in sorted(outbox_counts):
        count = outbox_counts[status]
        if status in _PENDING_OUTBOX_STATUSES and count > 0:
            pending[status] = count
            total += count
    return pending, total


# ---------------------------------------------------------------------------
# Shutdown evidence record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShutdownEvidence:
    """Immutable shutdown evidence record.

    All fields are JSON-safe plain types.  Use :meth:`to_dict` for
    serialisation with deterministic key ordering.
    """

    runtime_state: str | None
    shutdown_status: str
    shutdown_reason: str | None
    pending_outbox_counts: dict[str, int] | None
    pending_retry_work_total: int | None
    retry_worker_running: bool | None
    retry_worker_processed: int | None
    retry_worker_succeeded: int | None
    retry_worker_failed: int | None
    retry_worker_dead_lettered: int | None
    in_flight_count: int | None
    tasks_cancelled: int | None
    drain_timeout_detected: bool
    evidence_flush_status: str | None
    resume_expected: bool
    outbox_shutdown_policy: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict with alphabetically sorted keys."""
        return _sorted_dict(
            {
                "drain_timeout_detected": self.drain_timeout_detected,
                "evidence_flush_status": self.evidence_flush_status,
                "in_flight_count": self.in_flight_count,
                "outbox_shutdown_policy": self.outbox_shutdown_policy,
                "pending_outbox_counts": self.pending_outbox_counts,
                "pending_retry_work_total": self.pending_retry_work_total,
                "resume_expected": self.resume_expected,
                "retry_worker_dead_lettered": self.retry_worker_dead_lettered,
                "retry_worker_failed": self.retry_worker_failed,
                "retry_worker_processed": self.retry_worker_processed,
                "retry_worker_running": self.retry_worker_running,
                "retry_worker_succeeded": self.retry_worker_succeeded,
                "runtime_state": self.runtime_state,
                "shutdown_reason": self.shutdown_reason,
                "shutdown_status": self.shutdown_status,
                "tasks_cancelled": self.tasks_cancelled,
            }
        )


def _sorted_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with keys sorted alphabetically (recursive)."""
    result: dict[str, Any] = {}
    for key in sorted(d):
        val = d[key]
        if isinstance(val, dict):
            result[key] = _sorted_dict(val)
        elif isinstance(val, list):
            result[key] = [_sorted_dict(v) if isinstance(v, dict) else v for v in val]
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_shutdown_evidence(
    *,
    runtime_state: Any = None,
    outbox_counts: Any = None,
    retry_state: Any = None,
    events: Sequence[Any] = (),
    capacity_state: Any = None,
    reason: str | None = None,
    evidence_flush_status: str | None = None,
) -> ShutdownEvidence:
    """Derive structured shutdown evidence from runtime inputs.

    All parameters accept dict/object values — the function extracts
    fields via attribute or dict-key access.  No I/O, no async, no
    side effects.

    Parameters
    ----------
    runtime_state:
        Current runtime lifecycle state.  Accepts a ``RuntimeState`` enum,
        a plain string (``"running"``, ``"stopped"``, etc.), or ``None``.
    outbox_counts:
        Mapping of outbox status strings to integer counts.  Accepts a
        ``dict`` or any object supporting ``.items()``.
    retry_state:
        Retry worker state.  Accepts a :class:`RetryWorkerState` dataclass
        or a ``dict`` with keys ``enabled``, ``running``, ``processed``,
        ``succeeded``, ``failed``, ``dead_lettered``.
    events:
        Sequence of runtime events.  Each event may be a
        :class:`~medre.runtime.events.RuntimeEvent` dataclass or a plain
        ``dict`` with ``"event_type"`` and ``"detail"`` keys.
    capacity_state:
        Capacity controller snapshot.  Accepts a ``dict`` or object with
        a ``delivery_current`` attribute/key.
    reason:
        Optional caller-supplied shutdown reason hint (e.g.
        ``"drain_timeout"``, ``"cancellation"``).
    evidence_flush_status:
        Optional caller-supplied status of evidence persistence at
        shutdown (e.g. ``"flushed"``, ``"partial"``, ``"skipped"``).

    Returns
    -------
    ShutdownEvidence
        Frozen evidence record with deterministic ``to_dict()`` output.

    Notes
    -----
    Classification priority (first matching rule wins):

    1. Runtime is ``running``, ``initialized``, or ``starting``
       → ``shutdown_status="running"``.
    2. Explicit ``reason="drain_timeout"`` or drain-timeout event detected
       → ``shutdown_status="drain_timeout"``.
    3. ``reason="cancellation"`` or cancellation event detected
       → ``shutdown_status="cancellation"``.
    4. Adapter failure event detected
       → ``shutdown_status="adapter_failure"``.
    5. Runtime is ``failed``
       → ``shutdown_status="failed"``.
    6. Pending outbox/retry work remains at ``stopped``/``stopping``
       → ``shutdown_status="shutdown_pending"``.
    7. Runtime is ``stopped`` with no pending work
       → ``shutdown_status="graceful_stop"``.
    8. Runtime is ``stopping``
       → ``shutdown_status="stopped"`` (still in progress).
    9. Fallback
       → ``shutdown_status="stopped"``.
    """
    # -- Normalise inputs ----------------------------------------------------
    rs = _runtime_state_str(runtime_state)

    # Outbox counts as plain dict[str, int].
    oc: dict[str, int] | None
    if outbox_counts is None:
        oc = None
    elif isinstance(outbox_counts, Mapping):
        oc = {str(k): int(v) for k, v in outbox_counts.items()}
    else:
        # Try to treat as mapping-like.
        try:
            oc = {str(k): int(v) for k, v in outbox_counts.items()}  # type: ignore[union-attr]
        except Exception:
            oc = None

    # Retry state fields.
    rw_running: bool | None = None
    rw_processed: int | None = None
    rw_succeeded: int | None = None
    rw_failed: int | None = None
    rw_dead_lettered: int | None = None
    if retry_state is not None:
        rw_running = _attr(retry_state, "running")
        if not isinstance(rw_running, bool):
            rw_running = None
        rw_processed = _attr(retry_state, "processed")
        if not isinstance(rw_processed, int):
            rw_processed = None
        rw_succeeded = _attr(retry_state, "succeeded")
        if not isinstance(rw_succeeded, int):
            rw_succeeded = None
        rw_failed = _attr(retry_state, "failed")
        if not isinstance(rw_failed, int):
            rw_failed = None
        rw_dead_lettered = _attr(retry_state, "dead_lettered")
        if not isinstance(rw_dead_lettered, int):
            rw_dead_lettered = None

    # Capacity state — in_flight_count.
    in_flight_count: int | None = None
    if capacity_state is not None:
        dc = _attr(capacity_state, "delivery_current")
        if isinstance(dc, int):
            in_flight_count = dc

    # Pending outbox work.
    pending_counts, pending_total = _compute_pending_outbox(oc)
    has_pending_work = pending_total > 0

    # Event scanning.
    evts = list(events)
    drain_timeout_detected = _has_drain_timeout_signal(evts, reason)
    cancellation_detected = _has_cancellation_signal(evts, reason)
    adapter_failure_detected = _has_adapter_failure_event(evts)
    tasks_cancelled = _count_tasks_cancelled(evts)

    # -- Classify shutdown_status --------------------------------------------
    active_states = {"running", "initialized", "starting"}
    terminal_states = {"stopped", "failed"}
    stopping_states = {"stopping"}

    shutdown_status: str
    shutdown_reason: str | None = reason

    if rs is None or rs in active_states:
        # Runtime is still active — no shutdown evidence.
        shutdown_status = ShutdownStatus.RUNNING.value
        # Running state has no shutdown reason.
        shutdown_reason = None
    elif drain_timeout_detected:
        shutdown_status = ShutdownStatus.DRAIN_TIMEOUT.value
        if shutdown_reason is None:
            shutdown_reason = "drain_timeout"
    elif cancellation_detected:
        shutdown_status = ShutdownStatus.CANCELLATION.value
        if shutdown_reason is None:
            shutdown_reason = "cancellation"
    elif adapter_failure_detected:
        shutdown_status = ShutdownStatus.ADAPTER_FAILURE.value
        if shutdown_reason is None:
            shutdown_reason = "adapter_failure"
    elif rs == "failed":
        shutdown_status = ShutdownStatus.FAILED.value
    elif rs in stopping_states or rs in terminal_states:
        # Runtime is stopping or stopped — check for pending work first.
        if has_pending_work:
            shutdown_status = ShutdownStatus.SHUTDOWN_PENDING.value
            if shutdown_reason is None:
                shutdown_reason = "shutdown_pending"
        elif rs == "stopped":
            shutdown_status = ShutdownStatus.GRACEFUL_STOP.value
        else:
            # stopping with no pending work and no specific cause.
            shutdown_status = ShutdownStatus.STOPPED.value
    else:
        # Unknown state — default to stopped.
        shutdown_status = ShutdownStatus.STOPPED.value

    # -- Compute resumable policy fields ------------------------------------
    # resume_expected: True only when non-terminal outbox work exists AND the
    # resolved shutdown_status is one of the resumable cases. Cancellation,
    # drain_timeout, adapter_failure, and failed override even if pending work
    # exists.
    _resumable_statuses = frozenset(("graceful_stop", "shutdown_pending"))
    resume_expected: bool = has_pending_work and shutdown_status in _resumable_statuses

    # outbox_shutdown_policy: "resumable" when outbox_counts were supplied
    # (the operator can expect pending items to resume on next start); None
    # when no outbox data was provided at all.
    outbox_shutdown_policy: str | None = "resumable" if oc is not None else None

    # -- Build evidence record -----------------------------------------------
    # pending_outbox_counts: None when no outbox data was provided;
    # empty dict when outbox exists but nothing is pending.
    poc: dict[str, int] | None
    if oc is None:
        poc = None
    else:
        poc = pending_counts if pending_counts else {}

    return ShutdownEvidence(
        runtime_state=rs,
        shutdown_status=shutdown_status,
        shutdown_reason=shutdown_reason,
        pending_outbox_counts=poc,
        pending_retry_work_total=pending_total if oc is not None else None,
        retry_worker_running=rw_running,
        retry_worker_processed=rw_processed,
        retry_worker_succeeded=rw_succeeded,
        retry_worker_failed=rw_failed,
        retry_worker_dead_lettered=rw_dead_lettered,
        in_flight_count=in_flight_count,
        tasks_cancelled=tasks_cancelled,
        drain_timeout_detected=drain_timeout_detected,
        evidence_flush_status=evidence_flush_status,
        resume_expected=resume_expected,
        outbox_shutdown_policy=outbox_shutdown_policy,
    )


# ---------------------------------------------------------------------------
# Outbox shutdown policy classifier
# ---------------------------------------------------------------------------
#
# Source of truth for outbox status vocabularies and terminal sets:
# ``medre.core.engine.pipeline.delivery_state.OUTBOX_STATUSES`` and
# ``TERMINAL_OUTBOX_STATUSES``.  The mapping below is a static snapshot
# kept local so this module stays leaf-level (no medre imports).


#: Resumable outbox statuses and their shutdown classifications.
#: These are non-terminal statuses where the item is preserved for
#: restart recovery.  Graceful shutdown does **not** mutate or append.
_RESUMABLE_OUTBOX_CLASSIFICATIONS: dict[str, tuple[str, str]] = {
    "pending": (
        "resumable_pending",
        "Non-terminal outbox status; item left pending for restart recovery.",
    ),
    "retry_wait": (
        "resumable_retry_wait",
        "Non-terminal outbox status; item left in retry_wait for restart recovery.",
    ),
    "in_progress": (
        "resumable_in_progress",
        "Non-terminal outbox status; item left in_progress for restart recovery.",
    ),
    "queued": (
        "resumable_queued",
        "Non-terminal outbox status; item left queued for restart recovery.",
    ),
}

#: Terminal outbox statuses and their shutdown classifications.
#: These statuses are already final; no restart recovery needed.
_TERMINAL_OUTBOX_CLASSIFICATIONS: dict[str, tuple[str, str]] = {
    "sent": (
        "terminal_sent",
        "Terminal outbox status; delivery completed before shutdown.",
    ),
    "dead_lettered": (
        "terminal_dead_lettered",
        "Terminal outbox status; item dead-lettered before shutdown.",
    ),
    "cancelled": (
        "terminal_cancelled",
        "Terminal outbox status; item cancelled before shutdown.",
    ),
    "abandoned": (
        "terminal_abandoned",
        "Terminal outbox status; item abandoned before shutdown.",
    ),
}

#: Combined lookup for :func:`classify_outbox_shutdown_policy`.
_OUTBOX_SHUTDOWN_MAP: dict[str, tuple[str, str, bool]] = {
    status: (cls, reason, True)
    for status, (cls, reason) in _RESUMABLE_OUTBOX_CLASSIFICATIONS.items()
}
_OUTBOX_SHUTDOWN_MAP.update(
    {
        status: (cls, reason, False)
        for status, (cls, reason) in _TERMINAL_OUTBOX_CLASSIFICATIONS.items()
    }
)


@dataclass(frozen=True)
class OutboxShutdownClassification:
    """Immutable classification of an outbox status for graceful-shutdown policy.

    Fields
    ------
    status :
        The original outbox status string (e.g. ``"pending"``).
    classification :
        Policy classification label (e.g. ``"resumable_pending"``,
        ``"terminal_sent"``).
    mutate_outbox :
        Whether the shutdown policy requests outbox mutation.  Always
        ``False`` for graceful shutdown.
    append_receipt :
        Whether the shutdown policy requests a receipt append.  Always
        ``False`` for graceful shutdown.
    resume_on_restart :
        ``True`` for resumable (non-terminal) statuses, ``False`` for
        terminal statuses.
    evidence_reason :
        Human-readable explanation of the classification.
    """

    status: str
    classification: str
    mutate_outbox: bool
    append_receipt: bool
    resume_on_restart: bool
    evidence_reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict with alphabetically sorted keys."""
        return {
            name: getattr(self, name) for name in sorted(f.name for f in fields(self))
        }


def classify_outbox_shutdown_policy(status: str) -> OutboxShutdownClassification:
    """Classify an outbox status for graceful-shutdown policy.

    Returns an :class:`OutboxShutdownClassification` describing how the
    shutdown policy treats the given *status*.  The function is **pure**:
    no I/O, no side effects, no runtime state mutation.

    Classification rules:

    * **Resumable** (``pending``, ``retry_wait``, ``in_progress``, ``queued``):
      ``resume_on_restart=True``, ``mutate_outbox=False``,
      ``append_receipt=False``.  The item is preserved for restart recovery.
    * **Terminal** (``sent``, ``dead_lettered``, ``cancelled``, ``abandoned``):
      ``resume_on_restart=False``, ``mutate_outbox=False``,
      ``append_receipt=False``.  The item is already final.

    Parameters
    ----------
    status :
        An outbox status string.

    Returns
    -------
    OutboxShutdownClassification
        Frozen classification record.

    Raises
    ------
    ValueError
        If *status* is not a recognised outbox status.
    """
    entry = _OUTBOX_SHUTDOWN_MAP.get(status)
    if entry is None:
        raise ValueError(
            f"Unknown outbox status: {status!r}. "
            f"Expected one of: {', '.join(sorted(_OUTBOX_SHUTDOWN_MAP))}."
        )
    classification, evidence_reason, resume_on_restart = entry
    return OutboxShutdownClassification(
        status=status,
        classification=classification,
        mutate_outbox=False,
        append_receipt=False,
        resume_on_restart=resume_on_restart,
        evidence_reason=evidence_reason,
    )
