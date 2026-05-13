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
  (:mod:`~medre.core.runtime.diagnostic_contract`).
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

from medre.core.runtime.diagnostic_contract import _sanitize_dict

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
    """

    STATE_TRANSITION = "state_transition"
    ADAPTER_STARTED = "adapter_started"
    ADAPTER_START_FAILED = "adapter_start_failed"
    ADAPTER_STOPPED = "adapter_stopped"
    STARTUP_CLASSIFIED = "startup_classified"
    ROUTE_SKIPPED = "route_skipped"
    ROUTE_UNAVAILABLE = "route_unavailable"


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


def _sanitize_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe, bounded, secret-free copy of *detail*.

    Delegates to the central diagnostics sanitizer
    (:func:`~medre.core.runtime.diagnostic_contract._sanitize_dict`)
    which:

    * Strips keys matching known secret patterns (password, api_key, etc.).
    * Recursively sanitises nested dicts and collections.
    * Replaces non-serialisable objects (exceptions, bytes, SDK objects)
      with safe type-name placeholders.
    * Truncates oversized string values.
    """
    return _sanitize_dict(detail)


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
