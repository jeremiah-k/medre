"""Deterministic runtime health classification and supervision semantics.

Provides pure functions that project adapter lifecycle states into runtime-level
health, failure severity, and startup outcome classifications.  All functions
are deterministic, side-effect-free, and transport-agnostic.

The module does **not** add health polling, circuit breakers, auto-restart,
supervisor trees, or any state mutation.  It is classification and
observability only.

Classification hierarchy
------------------------
* :class:`RuntimeHealth` — runtime-level health (``HEALTHY``, ``DEGRADED``,
  ``FAILED``) derived from aggregate adapter states.
* :class:`AdapterFailureSeverity` — fatal vs non-fatal classification of
  adapter failures.
* :class:`StartupOutcome` — startup result (``SUCCESS``, ``PARTIAL``,
  ``TOTAL_FAILURE``).

Public symbols
--------------
* :func:`classify_runtime_health` — pure function mapping adapter states to
  runtime health.
* :func:`classify_adapter_failure_severity` — pure function classifying
  failure severity from adapter counts.
* :func:`classify_startup_outcome` — pure function classifying startup
  outcome from adapter start counts.
* :func:`runtime_supervision_snapshot` — diagnostic snapshot of runtime
  supervision state.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Sequence

from medre.core.lifecycle.states import AdapterState

__all__ = [
    "RuntimeHealth",
    "AdapterFailureSeverity",
    "StartupOutcome",
    "classify_runtime_health",
    "classify_adapter_failure_severity",
    "classify_startup_outcome",
    "count_adapter_state_categories",
    "runtime_supervision_snapshot",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RuntimeHealth(Enum):
    """Runtime-level health classification derived from adapter states.

    Attributes
    ----------
    HEALTHY:
        All adapters are operational (``READY``).
    DEGRADED:
        At least one adapter is operational, but at least one is not.
    FAILED:
        All adapters are failed or zero adapters are registered.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class AdapterFailureSeverity(Enum):
    """Classification of adapter failure impact on the runtime.

    Attributes
    ----------
    FATAL:
        The failure makes the runtime inoperable (all adapters down).
    NON_FATAL:
        The failure degrades the runtime but at least one adapter remains
        operational.
    """

    FATAL = "fatal"
    NON_FATAL = "non_fatal"


class StartupOutcome(Enum):
    """Classification of adapter startup outcome.

    Attributes
    ----------
    SUCCESS:
        All configured adapters started successfully.
    PARTIAL:
        Some adapters started, some failed.  Runtime is allowed to run
        in degraded mode.
    TOTAL_FAILURE:
        Zero adapters started.  This is a fatal startup failure.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    TOTAL_FAILURE = "total_failure"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# States that count as "operational" for runtime health purposes.
_OPERATIONAL_STATES: frozenset[AdapterState] = frozenset({AdapterState.READY})

# States that indicate partial capability (not healthy, not dead).
_PARTIAL_STATES: frozenset[AdapterState] = frozenset(
    {
        AdapterState.DEGRADED,
        AdapterState.BACKPRESSURED,
        AdapterState.DISCONNECTED,
    }
)

# States that indicate complete failure or clean shutdown (adapter unavailable).
_DEAD_STATES: frozenset[AdapterState] = frozenset(
    {AdapterState.FAILED, AdapterState.STOPPED}
)


def count_adapter_state_categories(
    states: Sequence[AdapterState],
) -> tuple[int, int, int, int]:
    """Count adapter states by operational / partial / failed / transitional.

    Public helper used by the runtime and live-health refresh to build
    aggregate adapter summary counts.

    Returns
    -------
    tuple[int, int, int, int]
        ``(operational_count, partial_count, failed_count, transitional_count)``
    """
    operational = 0
    partial = 0
    failed = 0
    transitional = 0
    for state in states:
        if state in _OPERATIONAL_STATES:
            operational += 1
        elif state in _PARTIAL_STATES:
            partial += 1
        elif state in _DEAD_STATES:
            failed += 1
        else:
            # INITIALIZING, STOPPING — transitional (not STOPPED, which is dead)
            transitional += 1
    return operational, partial, failed, transitional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_runtime_health(
    adapter_states: Sequence[AdapterState],
) -> RuntimeHealth:
    """Classify runtime health from aggregate adapter states.

    Deterministic, pure, transport-agnostic classification.

    Parameters
    ----------
    adapter_states:
        Sequence of current :class:`AdapterState` values for all registered
        adapters.  Empty sequence means zero adapters registered.

    Returns
    -------
    RuntimeHealth
        The classified runtime health.

    Rules (priority-ordered)
    ------------------------
    1. Empty sequence → ``FAILED`` (zero adapters).
    2. All ``READY`` → ``HEALTHY``.
    3. All ``FAILED`` → ``FAILED``.
    4. At least one ``READY`` and any non-``READY`` → ``DEGRADED``.
    5. No ``READY`` but at least one partial state → ``DEGRADED``.
    6. Only transitional states → ``FAILED``.
    """
    if not adapter_states:
        return RuntimeHealth.FAILED

    operational, partial, failed, transitional = count_adapter_state_categories(
        adapter_states,
    )
    total = len(adapter_states)

    # All operational → healthy
    if operational == total:
        return RuntimeHealth.HEALTHY

    # All failed → failed
    if failed == total:
        return RuntimeHealth.FAILED

    # At least one operational and some non-operational → degraded
    if operational > 0:
        return RuntimeHealth.DEGRADED

    # No operational, but some partial capability → degraded
    if partial > 0:
        return RuntimeHealth.DEGRADED

    # Only transitional (INITIALIZING, STOPPING) and/or failed
    # If any failed with no operational, it's failed
    if failed > 0:
        return RuntimeHealth.FAILED

    # Only transitional states → not yet operational
    return RuntimeHealth.FAILED


def classify_adapter_failure_severity(
    healthy_count: int,
    total_count: int,
) -> AdapterFailureSeverity:
    """Classify the severity of an adapter failure.

    Parameters
    ----------
    healthy_count:
        Number of adapters currently in a healthy/operational state.
    total_count:
        Total number of registered adapters.

    Returns
    -------
    AdapterFailureSeverity
        ``FATAL`` if no adapters remain operational, ``NON_FATAL`` otherwise.
    """
    if total_count == 0:
        return AdapterFailureSeverity.FATAL
    if healthy_count == 0:
        return AdapterFailureSeverity.FATAL
    return AdapterFailureSeverity.NON_FATAL


def classify_startup_outcome(
    started: int,
    failed: int,
    total: int,
) -> StartupOutcome:
    """Classify the outcome of adapter startup.

    Parameters
    ----------
    started:
        Number of adapters that started successfully.
    failed:
        Number of adapters that failed to start.
    total:
        Total number of adapters that were attempted.

    Returns
    -------
    StartupOutcome

    Rules
    -----
    - ``total == 0`` → ``TOTAL_FAILURE`` (nothing configured).
    - ``started == 0`` → ``TOTAL_FAILURE`` (zero adapters started).
    - ``started == total`` → ``SUCCESS``.
    - ``started > 0`` and ``failed > 0`` → ``PARTIAL``.
    """
    if total == 0:
        return StartupOutcome.TOTAL_FAILURE
    if started == 0:
        return StartupOutcome.TOTAL_FAILURE
    if started == total:
        return StartupOutcome.SUCCESS
    # started > 0 and failed > 0 (since started != total and started > 0)
    return StartupOutcome.PARTIAL


def runtime_supervision_snapshot(
    adapter_states: Sequence[AdapterState],
) -> dict[str, Any]:
    """Build a deterministic supervision diagnostic snapshot.

    This is a pure, observational function.  It reads adapter states and
    returns a JSON-safe dictionary suitable for structured logging and
    diagnostics.  It does not trigger restarts, alerts, or state changes.

    Parameters
    ----------
    adapter_states:
        Sequence of current :class:`AdapterState` values for all registered
        adapters.

    Returns
    -------
    dict[str, Any]
        Dictionary with:
        - ``runtime_health``: classified :class:`RuntimeHealth` value string.
        - ``adapter_summary``: counts by state category.
        - ``startup_fingerprint``: deterministic state distribution string.
    """
    health = classify_runtime_health(adapter_states)
    operational, partial, failed, transitional = count_adapter_state_categories(
        adapter_states,
    )

    # Build deterministic state distribution fingerprint
    state_counts: dict[str, int] = {}
    for state in adapter_states:
        state_counts[state.value] = state_counts.get(state.value, 0) + 1
    # Sort for determinism
    fingerprint = ", ".join(f"{k}={v}" for k, v in sorted(state_counts.items()))

    return {
        "runtime_health": health.value,
        "adapter_summary": {
            "healthy": operational,
            "degraded": partial,
            "failed": failed,
            "transitional": transitional,
            "total": len(adapter_states),
        },
        "startup_fingerprint": fingerprint,
    }
