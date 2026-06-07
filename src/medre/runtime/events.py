"""Compact runtime event surface for lifecycle observation.

Provides a tiny, bounded, deterministic event record for runtime lifecycle
state transitions and adapter/route readiness metadata.  This module creates
insertion points for future supervision/orchestration layers without
implementing active supervision, restart loops, or async pub/sub.

Design constraints
------------------
* Bounded: backed by a :class:`collections.deque` with a fixed ``maxlen``.
* Deterministic: sequence numbers are monotonically increasing integers;
  timestamps use an injectable monotonic clock for test determinism.
* JSON-safe: every event serialises to a plain dict with ``str``/``int``/
  ``float``/``dict`` values — no SDK objects, no secrets.  Detail dicts
  are sanitised via the central diagnostics sanitizer
  (:mod:`~medre.core.supervision.diagnostic_contract`).
* Read-only surface: events are emitted by the runtime; external consumers
  read via :meth:`EventBuffer.snapshot` or the runtime snapshot.
* No async: all operations are synchronous.

Public symbols
--------------
* :class:`RuntimeEventType` — str-enum of recognised event types.
* :class:`RuntimeEvent` — frozen dataclass representing a single event.
* :class:`EventBuffer` — bounded, sequence-numbered event container.
"""

from __future__ import annotations

import enum
import time as _time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from medre.core.supervision.diagnostic_contract import sanitize_diagnostic_mapping

__all__ = ["RuntimeEvent", "RuntimeEventType", "EventBuffer"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_EVENT_BUFFER_MAXLEN: int = 256
"""Default maximum number of events retained in the buffer."""


# ---------------------------------------------------------------------------
# Event type enum
# ---------------------------------------------------------------------------


class RuntimeEventType(str, enum.Enum):
    """Recognised runtime event types.

    Each value is a lowercase, underscore-separated string that is directly
    JSON-safe.  The set is intentionally small — only low-risk internal
    events are emitted in this tranche.

    Event taxonomy
    ~~~~~~~~~~~~~~

    Events are grouped by the lifecycle phase they observe.  Each group is
    listed below with a brief summary of what operators should expect.

    **Adapter lifecycle** — adapter instantiation through teardown:

    * ``adapter_started`` — adapter completed its start sequence.
    * ``adapter_start_failed`` — adapter failed during start; detail
      carries the error context.
    * ``adapter_stopped`` — adapter completed its stop sequence (normal
      or error-driven).

    **Startup classification** — post-startup readiness outcome:

    * ``startup_classified`` — runtime has classified the overall startup
      result (e.g. all adapters up, partial failure, degraded).
    * ``route_skipped`` — a route was skipped during startup, typically
      because its adapter is not ready or is misconfigured.
    * ``route_unavailable`` — a previously available route is now
      unavailable (adapter down, session closed, etc.).

    **Runtime state** — internal state-machine transitions:

    * ``state_transition`` — the runtime state machine moved between
      states (e.g. STARTING → RUNNING → STOPPING).  Detail carries
      ``from_state`` and ``to_state``.

    **Retry lifecycle** — per-message retry progression:

    * ``retry_started`` — a retry loop began for a message.
    * ``retry_attempted`` — a single retry attempt was dispatched.
    * ``retry_succeeded`` — the message was delivered successfully on
      a retry attempt.
    * ``retry_failed`` — a retry attempt failed; more attempts may
      follow.
    * ``retry_dead_lettered`` — all retries exhausted; message moved to
      dead-letter store.  This is a terminal outcome for that message.
    * ``retry_stopped`` — the retry loop completed normally (success or
      exhaustion).

    **Retry cancellation / abandonment** — mid-flight termination:

    * ``retry_abandoned`` — a running retry loop was abandoned mid-flight.
      This fires when the runtime is shutting down, a cancellation signal
      is received, or an upstream decision drops the message.  The retry
      loop was *already executing* when abandonment was triggered.
    * ``retry_start_refused`` — a retry was *requested* but refused
      before it could begin, because an abandon/cancellation was already
      in effect for that message.  Unlike ``retry_abandoned``, no retry
      loop was running at the time of refusal.

    **Diagnostics** — health and observational signals only:

    * ``health_refreshed`` — a periodic health check completed.  This is
      a read-only diagnostic signal and does **not** imply any change in
      runtime execution state.
    """

    # -- Runtime state -------------------------------------------------------
    STATE_TRANSITION = (
        "state_transition"  # runtime FSM moved (detail: from_state, to_state)
    )

    # -- Adapter lifecycle ---------------------------------------------------
    ADAPTER_STARTED = "adapter_started"  # adapter completed start sequence
    ADAPTER_START_FAILED = (
        "adapter_start_failed"  # adapter raised during start (detail: error context)
    )
    ADAPTER_STOPPED = (
        "adapter_stopped"  # adapter completed stop sequence (normal or error)
    )

    # -- Startup classification ----------------------------------------------
    STARTUP_CLASSIFIED = "startup_classified"  # overall startup outcome classified
    ROUTE_SKIPPED = "route_skipped"  # route skipped during startup (adapter not ready / misconfigured)
    ROUTE_UNAVAILABLE = "route_unavailable"  # route became unavailable at runtime

    # -- Diagnostics (read-only signals) -------------------------------------
    HEALTH_REFRESHED = (
        "health_refreshed"  # periodic health check result (no execution state change)
    )

    # -- Retry lifecycle: progression ----------------------------------------
    RETRY_STARTED = "retry_started"  # retry loop began for a message
    RETRY_ATTEMPTED = "retry_attempted"  # single retry attempt dispatched
    RETRY_SUCCEEDED = "retry_succeeded"  # message delivered on a retry attempt
    RETRY_FAILED = "retry_failed"  # retry attempt failed (more may follow)
    RETRY_DEAD_LETTERED = (
        "retry_dead_lettered"  # all retries exhausted; message dead-lettered (terminal)
    )
    RETRY_STOPPED = (
        "retry_stopped"  # retry loop completed normally (success or exhaustion)
    )

    # -- Retry cancellation / abandonment ------------------------------------
    RETRY_ABANDONED = "retry_abandoned"  # running retry loop abandoned mid-flight (shutdown/cancellation)
    RETRY_START_REFUSED = "retry_start_refused"  # retry refused: abandon already in effect, loop never started


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


def _sanitize_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe, bounded, secret-free copy of *detail*.

    Delegates to the central diagnostics sanitizer
    (:func:`~medre.core.supervision.diagnostic_contract.sanitize_diagnostic_mapping`)
    which:

    * Strips keys matching known secret patterns (password, api_key, etc.).
    * Recursively sanitises nested dicts and collections.
    * Replaces non-serialisable objects (exceptions, bytes, SDK objects)
      with safe type-name placeholders.
    * Truncates oversized string values.
    """
    return sanitize_diagnostic_mapping(detail)


@dataclass(frozen=True)
class RuntimeEvent:
    """A single, immutable runtime lifecycle event.

    Attributes
    ----------
    sequence:
        Monotonically increasing sequence number (0-based).
    event_type:
        The type of event.
    timestamp:
        Monotonic timestamp (seconds).  Not wall-clock — use the runtime
        snapshot for wall-clock times.
    detail:
        JSON-safe metadata dict.  Keys are lowercase strings; values are
        plain types (``str``, ``int``, ``float``, ``bool``, ``None``).
    """

    sequence: int
    event_type: RuntimeEventType
    timestamp: float
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation."""
        return {
            "detail": dict(sorted(self.detail.items())),
            "event_type": self.event_type.value,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Bounded event buffer
# ---------------------------------------------------------------------------


class EventBuffer:
    """Bounded, sequence-numbered runtime event container.

    Parameters
    ----------
    maxlen:
        Maximum number of events to retain.  Oldest events are discarded
        when the buffer is full.
    clock:
        Callable returning a monotonic float (seconds).  Defaults to
        :func:`time.monotonic`.  Inject a fixed value for deterministic
        tests.
    """

    def __init__(
        self,
        maxlen: int = DEFAULT_EVENT_BUFFER_MAXLEN,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._events: deque[RuntimeEvent] = deque(maxlen=maxlen)
        self._sequence: int = 0
        self._clock: Callable[[], float] = clock or _time.monotonic

    @property
    def maxlen(self) -> int:
        """Return the maximum number of events retained."""
        return self._events.maxlen  # type: ignore[no-any-return]

    def emit(
        self,
        event_type: RuntimeEventType,
        detail: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        """Record a new event and return it.

        If the buffer is full, the oldest event is discarded.
        """
        sanitized = _sanitize_detail(detail) if detail else {}
        event = RuntimeEvent(
            sequence=self._sequence,
            event_type=event_type,
            timestamp=self._clock(),
            detail=sanitized,
        )
        self._sequence += 1
        self._events.append(event)
        return event

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        return iter(self._events)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the buffer.

        The snapshot is deterministic: events are in insertion order and
        each event dict has alphabetically sorted keys.
        """
        return {
            "count": len(self._events),
            "events": [ev.to_dict() for ev in self._events],
            "maxlen": self.maxlen,
        }
