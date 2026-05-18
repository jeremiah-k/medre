"""Adapter lifecycle states and legal state transitions.

This module defines:

* :class:`AdapterState` – the finite set of states an adapter can occupy.
* :data:`VALID_TRANSITIONS` – the directed graph of legal state transitions.
* :func:`is_valid_transition` – predicate for checking transition legality.
* :class:`InvalidStateTransition` – exception raised on illegal moves.
"""

from __future__ import annotations

from enum import Enum


class AdapterState(Enum):
    """Finite set of lifecycle states for a registered adapter.

    The state machine models the full lifecycle from initialization through
    shutdown, including degraded and back-pressured operating modes.

    Terminal states (no outgoing transitions): ``FAILED``, ``STOPPED``.

    Attributes
    ----------
    INITIALIZING:
        Adapter is being set up; not yet ready to process events.
    READY:
        Adapter is fully operational.
    DEGRADED:
        Adapter is partially functional (e.g. high latency, missing features).
    BACKPRESSURED:
        Adapter's outbound queue is full; inbound traffic must be throttled.
    DISCONNECTED:
        Adapter has lost its transport connection.
    STOPPING:
        Adapter is shutting down gracefully.
    FAILED:
        Adapter has encountered an unrecoverable error.  Terminal state.
    STOPPED:
        Adapter has shut down cleanly.  Terminal state.
    """

    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    BACKPRESSURED = "backpressured"
    DISCONNECTED = "disconnected"
    STOPPING = "stopping"
    FAILED = "failed"
    STOPPED = "stopped"


# ---------------------------------------------------------------------------
# State-transition graph
# ---------------------------------------------------------------------------

# Each key is a source state; the value is the frozenset of states that
# the source may transition to directly.

VALID_TRANSITIONS: dict[AdapterState, frozenset[AdapterState]] = {
    AdapterState.INITIALIZING: frozenset(
        {
            AdapterState.READY,
            AdapterState.STOPPING,
            AdapterState.STOPPED,
            AdapterState.FAILED,
        }
    ),
    AdapterState.READY: frozenset(
        {
            AdapterState.DEGRADED,
            AdapterState.BACKPRESSURED,
            AdapterState.DISCONNECTED,
            AdapterState.STOPPING,
            AdapterState.FAILED,
        }
    ),
    AdapterState.DEGRADED: frozenset(
        {
            AdapterState.READY,
            AdapterState.BACKPRESSURED,
            AdapterState.DISCONNECTED,
            AdapterState.STOPPING,
            AdapterState.FAILED,
        }
    ),
    AdapterState.BACKPRESSURED: frozenset(
        {
            AdapterState.READY,
            AdapterState.DEGRADED,
            AdapterState.DISCONNECTED,
            AdapterState.STOPPING,
            AdapterState.FAILED,
        }
    ),
    AdapterState.DISCONNECTED: frozenset(
        {
            AdapterState.READY,
            AdapterState.STOPPING,
            AdapterState.FAILED,
        }
    ),
    AdapterState.STOPPING: frozenset(
        {
            AdapterState.STOPPED,
            AdapterState.FAILED,
        }
    ),
    AdapterState.FAILED: frozenset(),
    AdapterState.STOPPED: frozenset(),
}
"""Directed graph of legal state transitions.

``VALID_TRANSITIONS[source]`` is the set of states that *source* may move
to in a single transition.
"""


class InvalidStateTransition(Exception):
    """Raised when a state transition is not in :data:`VALID_TRANSITIONS`.

    Attributes
    ----------
    source:
        The current state.
    target:
        The requested target state.
    """

    def __init__(self, source: AdapterState, target: AdapterState) -> None:
        self.source = source
        self.target = target
        super().__init__(f"Invalid transition: {source.value} -> {target.value}")


def is_valid_transition(source: AdapterState, target: AdapterState) -> bool:
    """Return ``True`` if a transition from *source* to *target* is legal.

    Parameters
    ----------
    source:
        Current adapter state.
    target:
        Desired next state.

    Returns
    -------
    bool
    """
    return target in VALID_TRANSITIONS.get(source, frozenset())


def require_valid_transition(source: AdapterState, target: AdapterState) -> None:
    """Validate a state transition, raising if illegal.

    Parameters
    ----------
    source:
        Current adapter state.
    target:
        Desired next state.

    Raises
    ------
    InvalidStateTransition
        If the transition is not in :data:`VALID_TRANSITIONS`.
    """
    if not is_valid_transition(source, target):
        raise InvalidStateTransition(source, target)
