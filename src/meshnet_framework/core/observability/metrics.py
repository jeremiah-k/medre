"""Lightweight metrics counters for pipeline event tracking.

Provides a single :class:`EventMetrics` dataclass that tracks counts
per pipeline stage and per event kind using :class:`collections.Counter`.
Thread-safe for concurrent increment operations under the CPython GIL.

Public symbols
--------------
* :class:`EventMetrics` – per-stage counters with snapshot support.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class EventMetrics:
    """Simple metrics counters for pipeline events.

    Each counter is a :class:`collections.Counter` keyed by event kind
    string.  Call the ``record_*`` methods to increment the appropriate
    counter at each pipeline stage.

    Example
    -------
    >>> metrics = EventMetrics()
    >>> metrics.record_ingress("message.created")
    >>> metrics.record_stored("message.created")
    >>> metrics.record_delivered("message.created")
    >>> metrics.snapshot()
    {'ingressed': {'message.created': 1}, 'stored': {'message.created': 1}, ...}
    """

    events_ingressed: Counter = field(default_factory=Counter)
    events_stored: Counter = field(default_factory=Counter)
    events_routed: Counter = field(default_factory=Counter)
    events_delivered: Counter = field(default_factory=Counter)
    events_dropped: Counter = field(default_factory=Counter)
    events_failed: Counter = field(default_factory=Counter)

    # -- Recording methods --------------------------------------------------

    def record_ingress(self, event_kind: str) -> None:
        """Record an event entering the pipeline."""
        self.events_ingressed[event_kind] += 1

    def record_stored(self, event_kind: str) -> None:
        """Record an event successfully persisted."""
        self.events_stored[event_kind] += 1

    def record_routed(self, event_kind: str) -> None:
        """Record an event that was matched to at least one route."""
        self.events_routed[event_kind] += 1

    def record_delivered(self, event_kind: str) -> None:
        """Record an event that was successfully delivered."""
        self.events_delivered[event_kind] += 1

    def record_dropped(self, event_kind: str) -> None:
        """Record an event that was dropped (e.g. by middleware)."""
        self.events_dropped[event_kind] += 1

    def record_failed(self, event_kind: str, error: str = "") -> None:
        """Record an event that failed during processing."""
        self.events_failed[event_kind] += 1

    # -- Reporting ----------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a plain-dict copy of all counters for reporting.

        Returns
        -------
        dict
            Mapping of stage name to ``{event_kind: count}`` dicts.
        """
        return {
            "ingressed": dict(self.events_ingressed),
            "stored": dict(self.events_stored),
            "routed": dict(self.events_routed),
            "delivered": dict(self.events_delivered),
            "dropped": dict(self.events_dropped),
            "failed": dict(self.events_failed),
        }
