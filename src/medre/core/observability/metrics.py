"""Lightweight metrics counters and diagnostic recorder for pipeline events.

Provides a single :class:`EventMetrics` dataclass that tracks counts
per pipeline stage and per event kind using :class:`collections.Counter`.
Thread-safe for concurrent increment operations under the CPython GIL.

Also provides :class:`Diagnostician`, which records structured diagnostic
events emitted during delivery failures, replay skips, and correlation
misses.

And :class:`RouteMetrics`, which tracks per-route delivery counters
(``delivered``, ``failed``, ``skipped``, ``loop_prevented``) for
observability.

Public symbols
--------------
* :class:`EventMetrics` – per-stage counters with snapshot support.
* :class:`Diagnostician` – structured failure and diagnostic event recorder.
* :class:`RouteMetrics` – per-route delivery counters with snapshot support.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from medre.core.observability.logging import diagnostic_event


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


# ---------------------------------------------------------------------------
# Diagnostician
# ---------------------------------------------------------------------------


@dataclass
class Diagnostician:
    """Structured diagnostic recorder for pipeline failure events.

    Each ``record_*`` method emits a :func:`diagnostic_event` log entry
    and increments the corresponding internal counter.  Use
    :meth:`snapshot` to retrieve a summary of all recorded diagnostics.

    Example
    -------
    >>> diag = Diagnostician()
    >>> diag.record_adapter_failure("evt-1", "discord", "ConnectionRefused")
    >>> diag.snapshot()["adapter_failures"]
    {'discord': 1}
    """

    planner_failures: Counter = field(default_factory=Counter)
    renderer_failures: Counter = field(default_factory=Counter)
    storage_failures: Counter = field(default_factory=Counter)
    adapter_failures: Counter = field(default_factory=Counter)
    replay_skips: Counter = field(default_factory=Counter)
    replay_downgrades: Counter = field(default_factory=Counter)
    correlation_misses: Counter = field(default_factory=Counter)

    # -- Recording methods --------------------------------------------------

    def record_planner_failure(self, event_id: str, error: str) -> None:
        """Record a failure during route/planning resolution.

        Parameters
        ----------
        event_id:
            The canonical event ID whose planning failed.
        error:
            Human-readable description of the failure.
        """
        self.planner_failures[event_id] += 1
        diagnostic_event(event_id, "planner_failure", error)

    def record_renderer_failure(
        self, event_id: str, target: str, error: str
    ) -> None:
        """Record a failure during message rendering.

        Parameters
        ----------
        event_id:
            The canonical event ID.
        target:
            The target adapter that lacked a suitable renderer.
        error:
            Description of the rendering failure.
        """
        self.renderer_failures[target] += 1
        diagnostic_event(event_id, "renderer_failure", error, target=target)

    def record_storage_failure(
        self, event_id: str, operation: str, error: str
    ) -> None:
        """Record a failure during a storage operation.

        Parameters
        ----------
        event_id:
            The canonical event ID.
        operation:
            The storage operation that failed (e.g. ``"append"``).
        error:
            Description of the storage failure.
        """
        self.storage_failures[operation] += 1
        diagnostic_event(
            event_id, "storage_failure", error, operation=operation
        )

    def record_adapter_failure(
        self, event_id: str, adapter: str, error: str
    ) -> None:
        """Record a failure during adapter delivery.

        Parameters
        ----------
        event_id:
            The canonical event ID.
        adapter:
            The adapter that failed to accept the event.
        error:
            Description of the adapter failure.
        """
        self.adapter_failures[adapter] += 1
        diagnostic_event(event_id, "adapter_failure", error, adapter=adapter)

    def record_replay_skip(self, event_id: str, reason: str) -> None:
        """Record a replay event that was skipped.

        Parameters
        ----------
        event_id:
            The canonical event ID.
        reason:
            Why the replay was skipped.
        """
        self.replay_skips[reason] += 1
        diagnostic_event(event_id, "replay_skip", reason)

    def record_replay_downgrade(
        self, event_id: str, original_mode: str, fallback_mode: str
    ) -> None:
        """Record a replay mode downgrade.

        Parameters
        ----------
        event_id:
            The canonical event ID.
        original_mode:
            The initially requested replay mode.
        fallback_mode:
            The mode that was used instead.
        """
        self.replay_downgrades[f"{original_mode}->{fallback_mode}"] += 1
        diagnostic_event(
            event_id,
            "replay_downgrade",
            f"Downgraded from {original_mode} to {fallback_mode}",
            original_mode=original_mode,
            fallback_mode=fallback_mode,
        )

    def record_correlation_miss(
        self, event_id: str, native_ref: str
    ) -> None:
        """Record a native-reference correlation miss.

        Parameters
        ----------
        event_id:
            The canonical event ID.
        native_ref:
            The native reference that could not be resolved.
        """
        self.correlation_misses[native_ref] += 1
        diagnostic_event(
            event_id, "correlation_miss", native_ref, native_ref=native_ref
        )

    # -- Reporting ----------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a plain-dict copy of all diagnostic counters.

        Returns
        -------
        dict
            Mapping of diagnostic category to ``{key: count}`` dicts.
        """
        return {
            "planner_failures": dict(self.planner_failures),
            "renderer_failures": dict(self.renderer_failures),
            "storage_failures": dict(self.storage_failures),
            "adapter_failures": dict(self.adapter_failures),
            "replay_skips": dict(self.replay_skips),
            "replay_downgrades": dict(self.replay_downgrades),
            "correlation_misses": dict(self.correlation_misses),
        }


# ---------------------------------------------------------------------------
# Per-route delivery metrics
# ---------------------------------------------------------------------------


@dataclass
class RouteMetrics:
    """Per-route delivery counters for observability.

    Tracks ``delivered``, ``failed``, ``skipped``, and
    ``loop_prevented`` counts per route ID using
    :class:`collections.Counter`.

    Example
    -------
    >>> rm = RouteMetrics()
    >>> rm.record_delivered("route-a")
    >>> rm.record_failed("route-b", "timeout")
    >>> rm.snapshot()
    {'route-a': {'delivered': 1, 'failed': 0, 'skipped': 0, 'loop_prevented': 0}, ...}
    """

    route_delivered: Counter = field(default_factory=Counter)
    route_failed: Counter = field(default_factory=Counter)
    route_skipped: Counter = field(default_factory=Counter)
    route_loop_prevented: Counter = field(default_factory=Counter)

    # -- Recording methods --------------------------------------------------

    def record_delivered(self, route_id: str) -> None:
        """Record a successful delivery for *route_id*."""
        self.route_delivered[route_id] += 1

    def record_failed(self, route_id: str, error: str = "") -> None:
        """Record a failed delivery for *route_id*."""
        self.route_failed[route_id] += 1

    def record_skipped(self, route_id: str) -> None:
        """Record a skipped delivery for *route_id*."""
        self.route_skipped[route_id] += 1

    def record_loop_prevented(self, route_id: str) -> None:
        """Record a loop-prevented skip for *route_id*."""
        self.route_loop_prevented[route_id] += 1

    # -- Reporting ----------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Return a deterministic per-route snapshot of all counters.

        Returns
        -------
        dict
            Keys are route IDs sorted alphabetically.  Each value is a
            dict with ``delivered``, ``failed``, ``skipped``, and
            ``loop_prevented`` counts.
        """
        all_ids: set[str] = set()
        all_ids.update(self.route_delivered)
        all_ids.update(self.route_failed)
        all_ids.update(self.route_skipped)
        all_ids.update(self.route_loop_prevented)

        result: dict[str, dict[str, int]] = {}
        for route_id in sorted(all_ids):
            result[route_id] = {
                "delivered": self.route_delivered.get(route_id, 0),
                "failed": self.route_failed.get(route_id, 0),
                "skipped": self.route_skipped.get(route_id, 0),
                "loop_prevented": self.route_loop_prevented.get(route_id, 0),
            }
        return result
