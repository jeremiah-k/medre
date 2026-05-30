"""Replay data types: mode enum, request/result/state dataclasses, and route attribution."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from medre.core.storage.backend import DEFAULT_QUERY_LIMIT


class ReplayMode(Enum):
    """Behavioural mode controlling which pipeline stages are executed.

    Attributes
    ----------
    STRICT:
        Exact replay -- verify event existence and integrity without
        invoking any pipeline stages.  No side effects.  Useful for
        integrity checks and migration validation.

        **Guarantees:** read-only; no storage mutations; deterministic
        for the same stored events; re-raises unexpected exceptions.

    RE_RENDER:
        Re-run transforms and rendering, capture output.  Routing,
        planning, and delivery are **not** executed.  Useful for testing
        new renderers and metadata evolution.

        **Guarantees:** read-only; no storage mutations; rendering output
        is captured in :attr:`ReplayResult.output`; deterministic for the
        same stored events and renderer configuration; re-raises
        unexpected exceptions.

    RE_ROUTE:
        Re-run transforms, routing, and planning with current routes.
        Rendering and delivery are **not** executed.  Useful for testing
        route changes and planning changes.

        **Guarantees:** read-only; no storage mutations; route and plan
        outputs are captured in :attr:`ReplayResult.output`; deterministic
        for the same stored events and route configuration; re-raises
        unexpected exceptions.

    BEST_EFFORT:
        Full re-processing including delivery to adapters.  Same as
        normal processing but sourced from historical events.  Useful
        for migration and testing adapters with real data.

        **Guarantees:** **only** mode with side effects (adapter delivery);
        individual event failures are captured as ``"error"`` results
        without crashing the replay; crashed events are recorded via
        the :class:`Diagnostician`; results are yielded in storage query
        order for deterministic iteration.

    DRY_RUN:
        Execute all pipeline stages up to and including rendering, but
        **skip delivery**.  Equivalent to BEST_EFFORT minus the deliver
        stage.  Useful for previewing what a BEST_EFFORT replay would do
        without any side effects.

        **Guarantees:** read-only; no storage mutations; route, plan, and
        render outputs are captured; delivery stage is always ``"skipped"``
        with the reason ``"dry_run: delivery suppressed"``; re-raises
        unexpected exceptions.
    """

    STRICT = "strict"
    RE_RENDER = "re_render"
    RE_ROUTE = "re_route"
    BEST_EFFORT = "best_effort"
    DRY_RUN = "dry_run"


# ---------------------------------------------------------------------------
# Mode-to-stages mapping
# ---------------------------------------------------------------------------

# Ordered pipeline stages per mode.
_MODE_STAGES: dict[ReplayMode, tuple[str, ...]] = {
    ReplayMode.STRICT: ("store",),
    ReplayMode.RE_RENDER: ("store", "render"),
    ReplayMode.RE_ROUTE: ("store", "route", "plan"),
    ReplayMode.BEST_EFFORT: ("store", "route", "plan", "render", "deliver"),
    ReplayMode.DRY_RUN: ("store", "route", "plan", "render", "deliver"),
}

# Modes that produce side effects (adapter delivery).
_SIDE_EFFECT_MODES: frozenset[ReplayMode] = frozenset({ReplayMode.BEST_EFFORT})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ReplayRequest:
    """Filter and targeting specification for a replay operation.

    All filter fields are optional; ``None`` means *no restriction*.

    Attributes
    ----------
    time_start:
        Earliest event timestamp to include (inclusive).
    time_end:
        Latest event timestamp to include (inclusive).
    event_kinds:
        Restrict to these event kind strings.  ``None`` = all kinds.
    source_adapters:
        Restrict to events from these adapters.  ``None`` = all adapters.
    target_stages:
        Pipeline stages to replay.  ``None`` = all stages allowed by the
        selected :class:`ReplayMode`.  Valid values are ``"store"``,
        ``"route"``, ``"plan"``, ``"render"``, ``"deliver"``.
    correlation_ids:
        Restrict to events whose ``event_id`` appears in this list.
        ``None`` = no ID filtering.  When set, events are fetched by
        individual ID rather than via a storage query, and remaining
        filter fields are applied as post-filters.
    mode:
        The replay behavioural mode.
    limit:
        Maximum number of events to replay.  Defaults to
        :data:`~medre.core.storage.backend.DEFAULT_QUERY_LIMIT`
        (``1000``), shared with :class:`EventFilter`.
    target_adapters:
        Restrict delivery to these adapter names.  ``None`` = all
        adapters resolved by routing.  Only meaningful for modes that
        include the ``deliver`` stage (BEST_EFFORT, DRY_RUN).  Events
        whose delivery plans target adapters not in this list have their
        deliver stage result set to ``"skipped"``.
    route_ids:
        Restrict routing to only these route IDs.  ``()`` (empty) means
        all routes are considered.  When non-empty,
        only routes whose ``id`` appears in this tuple are used during
        replay.  If a requested route ID is disabled or does not match
        the event, a warning is recorded in the route attribution.
    run_id:
        Optional operator-assigned identifier for this replay execution.
        When set, it is recorded in :class:`ReplayRouteAttribution` and
        :class:`ReplaySummary` so operators can correlate replay runs.

        **Idempotency note:** Replay may intentionally redeliver events.
        Duplicate-send risk exists by design for mesh/radio transports
        where at-least-once delivery is the norm.  Operators should use
        ``run_id`` to track and deduplicate at the application layer.
    """

    time_start: datetime | None = None
    time_end: datetime | None = None
    event_kinds: list[str] | None = None
    source_adapters: list[str] | None = None
    target_stages: list[str] | None = None
    correlation_ids: list[str] | None = None
    mode: ReplayMode = ReplayMode.STRICT
    limit: int = DEFAULT_QUERY_LIMIT
    target_adapters: list[str] | None = None
    route_ids: tuple[str, ...] = ()
    run_id: str = ""


@dataclass
class ReplayResult:
    """Outcome of replaying a single event through one pipeline stage.

    Attributes
    ----------
    event_id:
        The canonical event identifier.
    stage:
        The pipeline stage that produced this result (``"store"``,
        ``"route"``, ``"plan"``, ``"render"``, ``"deliver"``).
    status:
        ``"passed"`` -- stage completed successfully.
        ``"skipped"`` -- stage was not executed because an upstream
        dependency was unavailable, delivery was suppressed (dry_run),
        or the target adapter was excluded by ``target_adapters``.
        ``"failed"`` -- stage ran but the result was negative (e.g.
        integrity check failed, no routes matched).
        ``"error"`` -- an exception was raised during stage execution.
    output:
        Stage-specific output, if applicable.
    error:
        Human-readable error message when *status* is ``"error"`` or
        ``"failed"``.
    duration_ms:
        Wall-clock time spent in this stage, in milliseconds.
    lineage:
        Lineage chain from the source canonical event.
    """

    event_id: str
    stage: str
    status: Literal["passed", "skipped", "failed", "error"]
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    lineage: list[str] = field(default_factory=list)
    route_attribution: ReplayRouteAttribution | None = None


@dataclass(frozen=True)
class ReplayRouteAttribution:
    """Route attribution captured during route-aware replay.

    Stored in :attr:`ReplayResult.route_attribution` as namespaced metadata
    alongside the route-target pairs.  This preserves route attribution
    without altering the canonical event schema.

    Determinism guarantee: for the same stored event and route
    configuration, the attribution is identical across replay runs.

    Attributes
    ----------
    route_ids:
        Identifiers of the routes that matched this event during replay.
    source_adapter:
        The ``source_adapter`` of the replayed canonical event.
    target_adapters:
        Adapter names of all resolved targets across matched routes.
    replay_mode:
        The :class:`ReplayMode` used for this replay.
    is_replay:
        Always ``True`` -- distinguishes replay attribution from
        live-routing metadata.
    loop_warnings:
        Tuple of human-readable loop-prevention warnings, if any routes
        were skipped.  Empty when no loops were detected.
    run_id:
        Operator-assigned identifier for the replay execution that
        produced this attribution.  Empty string when not provided.
        Use this to correlate replay runs and deduplicate at the
        application layer.
    """

    route_ids: tuple[str, ...] = ()
    source_adapter: str = ""
    target_adapters: tuple[str, ...] = ()
    replay_mode: str = ""
    is_replay: bool = True
    loop_warnings: tuple[str, ...] = ()
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, deterministic representation."""
        return {
            "is_replay": self.is_replay,
            "loop_warnings": list(self.loop_warnings),
            "replay_mode": self.replay_mode,
            "route_ids": list(self.route_ids),
            "run_id": self.run_id,
            "source_adapter": self.source_adapter,
            "target_adapters": list(self.target_adapters),
        }


@dataclass
class ReplayState:
    """Aggregate state tracker for a replay operation.

    Accumulates counters and diagnostics across all :class:`ReplayResult`
    items produced by a single :meth:`ReplayEngine.replay` call.

    Attributes
    ----------
    events_processed:
        Total number of ``(event, stage)`` results recorded.
    events_passed:
        Count of results with ``status == "passed"``.
    events_skipped:
        Count of results with ``status == "skipped"``.
    events_failed:
        Count of results with ``status in ("failed", "error")``.
    current_lineage:
        Lineage of the most recently processed event.  Updated on
        every :meth:`record` call so callers can track derivation
        ancestry across the replay.
    errors:
        Collected error messages from ``"failed"`` and ``"error"``
        results.  In :attr:`ReplayMode.BEST_EFFORT` mode this list
        doubles as the diagnostic log.
    """

    events_processed: int = 0
    events_passed: int = 0
    events_skipped: int = 0
    events_failed: int = 0
    current_lineage: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def record(self, result: ReplayResult) -> None:
        """Update state from a single *result*.

        Increments the appropriate counter, appends error messages
        when present, and refreshes ``current_lineage`` from the
        result's :attr:`~ReplayResult.lineage` field.
        """
        self.events_processed += 1
        if result.status == "passed":
            self.events_passed += 1
        elif result.status == "skipped":
            self.events_skipped += 1
        elif result.status in ("failed", "error"):
            self.events_failed += 1
            if result.error:
                self.errors.append(result.error)
        if result.lineage:
            self.current_lineage = list(result.lineage)


async def collect_replay_state(
    results: AsyncIterator[ReplayResult],
) -> ReplayState:
    """Consume all *results* and return the accumulated :class:`ReplayState`.

    Convenience wrapper that iterates over an async iterator of
    :class:`ReplayResult` items (as returned by
    :meth:`ReplayEngine.replay`) and accumulates them into a single
    :class:`ReplayState`.
    """
    state = ReplayState()
    async for result in results:
        state.record(result)
    return state
