"""Process-local bounded runtime event accounting counters.

Provides lightweight, process-local counters for the eight core runtime
event categories.  Counters are **not** persisted across restarts and
have **bounded, constant memory** (exactly 8 integer fields).

Counters tracked
----------------
* ``inbound_accepted``       – inbound events accepted into the pipeline.
* ``outbound_attempts``      – outbound delivery attempts (regardless of
  outcome).
* ``outbound_delivered``     – outbound deliveries that succeeded.
* ``outbound_failed``        – outbound deliveries that failed.
* ``replay_processed``       – replay events processed through the pipeline.
* ``replay_rejected``        – replay events rejected (by filter, mode, or
  policy).
* ``loop_prevented``         – events blocked by the self-loop guard.
* ``capacity_rejections``    – operations rejected by the capacity controller.

These are **global process-level aggregates**, not per-route counters.
For per-route breakdowns, see :class:`~medre.core.routing.stats.RouteStats`
and :class:`~medre.core.diagnostics.replay_metrics.ReplayMetrics`.

Public symbols
--------------
* :class:`RuntimeCounters` – frozen dataclass with all eight counters.
* :class:`RuntimeAccounting` – mutable collector with ``record_*``,
  ``snapshot``, and ``reset`` methods.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

__all__ = ["RuntimeAccounting", "RuntimeCounters"]


# ---------------------------------------------------------------------------
# Frozen counters (immutable snapshot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeCounters:
    """Immutable snapshot of all eight runtime accounting counters.

    Attributes
    ----------
    inbound_accepted:
        Number of inbound events accepted into the pipeline.
    outbound_attempts:
        Number of outbound delivery attempts (regardless of outcome).
    outbound_delivered:
        Number of outbound deliveries that succeeded.
    outbound_failed:
        Number of outbound deliveries that failed.
    replay_processed:
        Number of replay events processed through the pipeline.
    replay_rejected:
        Number of replay events rejected by filter, mode, or policy.
    loop_prevented:
        Number of events blocked by the self-loop guard.
    capacity_rejections:
        Number of operations rejected by the capacity controller.
    """

    inbound_accepted: int = 0
    outbound_attempts: int = 0
    outbound_delivered: int = 0
    outbound_failed: int = 0
    replay_processed: int = 0
    replay_rejected: int = 0
    loop_prevented: int = 0
    capacity_rejections: int = 0


# Fixed ordered tuple of counter field names for deterministic iteration.
_COUNTER_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(RuntimeCounters))


# ---------------------------------------------------------------------------
# Mutable accounting collector
# ---------------------------------------------------------------------------


class RuntimeAccounting:
    """Process-local bounded runtime event accounting counters.

    Holds exactly eight integer counters in a frozen
    :class:`RuntimeCounters` dataclass that is replaced on every
    mutation (copy-on-write).  Memory usage is constant regardless of
    how many events are recorded.

    Counters are **process-local only**: they are not persisted, not
    shared across processes, and reset to zero on process restart.
    The :meth:`snapshot` and :meth:`to_dict` methods produce
    deterministic, JSON-safe output suitable for Wave 2 snapshot
    integration.

    Thread-safety
    ~~~~~~~~~~~~~
    Safe for concurrent increment operations under the CPython GIL.
    Each ``record_*`` call replaces the internal ``RuntimeCounters``
    atomically (single attribute assignment).

    Example
    -------
    >>> acc = RuntimeAccounting()
    >>> acc.record_inbound_accepted()
    >>> acc.record_outbound_attempt()
    >>> acc.record_outbound_delivered()
    >>> acc.snapshot()
    {'capacity_rejections': 0, 'inbound_accepted': 1, 'loop_prevented': 0,
     'outbound_attempts': 1, 'outbound_delivered': 1, 'outbound_failed': 0,
     'replay_processed': 0, 'replay_rejected': 0}
    """

    def __init__(self) -> None:
        self._counters: RuntimeCounters = RuntimeCounters()

    # -- Recording methods ---------------------------------------------------

    def record_inbound_accepted(self) -> None:
        """Increment the inbound-accepted counter."""
        c = self._counters
        self._counters = RuntimeCounters(
            inbound_accepted=c.inbound_accepted + 1,
            outbound_attempts=c.outbound_attempts,
            outbound_delivered=c.outbound_delivered,
            outbound_failed=c.outbound_failed,
            replay_processed=c.replay_processed,
            replay_rejected=c.replay_rejected,
            loop_prevented=c.loop_prevented,
            capacity_rejections=c.capacity_rejections,
        )

    def record_outbound_attempt(self) -> None:
        """Increment the outbound-delivery-attempts counter."""
        c = self._counters
        self._counters = RuntimeCounters(
            inbound_accepted=c.inbound_accepted,
            outbound_attempts=c.outbound_attempts + 1,
            outbound_delivered=c.outbound_delivered,
            outbound_failed=c.outbound_failed,
            replay_processed=c.replay_processed,
            replay_rejected=c.replay_rejected,
            loop_prevented=c.loop_prevented,
            capacity_rejections=c.capacity_rejections,
        )

    def record_outbound_delivered(self) -> None:
        """Increment the outbound-delivered counter."""
        c = self._counters
        self._counters = RuntimeCounters(
            inbound_accepted=c.inbound_accepted,
            outbound_attempts=c.outbound_attempts,
            outbound_delivered=c.outbound_delivered + 1,
            outbound_failed=c.outbound_failed,
            replay_processed=c.replay_processed,
            replay_rejected=c.replay_rejected,
            loop_prevented=c.loop_prevented,
            capacity_rejections=c.capacity_rejections,
        )

    def record_outbound_failed(self) -> None:
        """Increment the outbound-failed counter."""
        c = self._counters
        self._counters = RuntimeCounters(
            inbound_accepted=c.inbound_accepted,
            outbound_attempts=c.outbound_attempts,
            outbound_delivered=c.outbound_delivered,
            outbound_failed=c.outbound_failed + 1,
            replay_processed=c.replay_processed,
            replay_rejected=c.replay_rejected,
            loop_prevented=c.loop_prevented,
            capacity_rejections=c.capacity_rejections,
        )

    def record_replay_processed(self) -> None:
        """Increment the replay-processed counter."""
        c = self._counters
        self._counters = RuntimeCounters(
            inbound_accepted=c.inbound_accepted,
            outbound_attempts=c.outbound_attempts,
            outbound_delivered=c.outbound_delivered,
            outbound_failed=c.outbound_failed,
            replay_processed=c.replay_processed + 1,
            replay_rejected=c.replay_rejected,
            loop_prevented=c.loop_prevented,
            capacity_rejections=c.capacity_rejections,
        )

    def record_replay_rejected(self) -> None:
        """Increment the replay-rejected counter."""
        c = self._counters
        self._counters = RuntimeCounters(
            inbound_accepted=c.inbound_accepted,
            outbound_attempts=c.outbound_attempts,
            outbound_delivered=c.outbound_delivered,
            outbound_failed=c.outbound_failed,
            replay_processed=c.replay_processed,
            replay_rejected=c.replay_rejected + 1,
            loop_prevented=c.loop_prevented,
            capacity_rejections=c.capacity_rejections,
        )

    def record_loop_prevented(self) -> None:
        """Increment the loop-prevented counter."""
        c = self._counters
        self._counters = RuntimeCounters(
            inbound_accepted=c.inbound_accepted,
            outbound_attempts=c.outbound_attempts,
            outbound_delivered=c.outbound_delivered,
            outbound_failed=c.outbound_failed,
            replay_processed=c.replay_processed,
            replay_rejected=c.replay_rejected,
            loop_prevented=c.loop_prevented + 1,
            capacity_rejections=c.capacity_rejections,
        )

    def record_capacity_rejection(self) -> None:
        """Increment the capacity-rejections counter."""
        c = self._counters
        self._counters = RuntimeCounters(
            inbound_accepted=c.inbound_accepted,
            outbound_attempts=c.outbound_attempts,
            outbound_delivered=c.outbound_delivered,
            outbound_failed=c.outbound_failed,
            replay_processed=c.replay_processed,
            replay_rejected=c.replay_rejected,
            loop_prevented=c.loop_prevented,
            capacity_rejections=c.capacity_rejections + 1,
        )

    # -- Read methods --------------------------------------------------------

    def counters(self) -> RuntimeCounters:
        """Return the current frozen counters (zero-copy reference)."""
        return self._counters

    def snapshot(self) -> dict[str, int]:
        """Return a deterministic, JSON-safe dict of all counters.

        Keys are sorted alphabetically for deterministic ordering.
        All values are plain ``int`` (no secrets, no SDK objects).

        Returns
        -------
        dict[str, int]
            Alphabetically-sorted counter names mapped to their current
            values.
        """
        return {k: getattr(self._counters, k) for k in sorted(_COUNTER_FIELDS)}

    def to_dict(self) -> dict[str, int]:
        """Alias for :meth:`snapshot` — deterministic, JSON-safe dict."""
        return self.snapshot()

    # -- Lifecycle -----------------------------------------------------------

    def reset(self) -> RuntimeCounters:
        """Reset all counters to zero and return the previous values.

        Returns
        -------
        RuntimeCounters
            The counter values **before** the reset, allowing callers
            to capture a final snapshot.

        Example
        -------
        >>> acc = RuntimeAccounting()
        >>> acc.record_inbound_accepted()
        >>> previous = acc.reset()
        >>> previous.inbound_accepted
        1
        >>> acc.counters().inbound_accepted
        0
        """
        previous = self._counters
        self._counters = RuntimeCounters()
        return previous

    # -- Dunder methods ------------------------------------------------------

    def __repr__(self) -> str:
        return f"RuntimeAccounting({self._counters!r})"
