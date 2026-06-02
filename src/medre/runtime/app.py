"""Runtime container that holds all MEDRE subsystem references.

:class:`MedreApp` is the top-level runtime object returned by
:class:`~medre.runtime.builder.RuntimeBuilder`.  It coordinates the
startup and shutdown lifecycle of every subsystem in dependency order.

Lifecycle
---------

::

    builder = RuntimeBuilder(config, paths)
    app = builder.build()       # construct, but do NOT start
    await app.start()           # initialise storage, start pipeline & adapters
    await app.wait_for_shutdown()
    await app.stop()            # graceful shutdown in reverse order
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from medre.core.lifecycle.states import AdapterState, require_valid_transition
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.health import (
    AdapterLiveHealth,
    LiveHealthSnapshot,
    health_to_adapter_state,
    normalize_adapter_health,
    truncate_health_error,
)
from medre.core.supervision.supervision import (
    StartupOutcome,
    classify_runtime_health,
    classify_startup_outcome,
    count_adapter_state_categories,
    runtime_supervision_snapshot,
)
from medre.runtime.boot_summary import BootSummary, build_boot_summary
from medre.runtime.errors import (
    RuntimeShutdownError,
    RuntimeStartupError,
)
from medre.runtime.events import EventBuffer, RuntimeEventType
from medre.runtime.retry import RetryWorkerState

if TYPE_CHECKING:
    from medre.config.model import RuntimeConfig
    from medre.config.paths import MedrePaths
    from medre.core.contracts.adapter import AdapterContract
    from medre.core.engine.pipeline import PipelineRunner
    from medre.core.engine.replay.engine import ReplayEngine
    from medre.core.events.bus import EventBus
    from medre.core.observability.metrics import Diagnostician
    from medre.core.planning.fallback_resolution import FallbackResolver
    from medre.core.planning.relation_resolution import RelationResolver
    from medre.core.rendering.renderer import RenderingPipeline
    from medre.core.routing.router import Router
    from medre.core.routing.stats import RouteStats
    from medre.core.storage.sqlite.storage import SQLiteStorage
    from medre.core.supervision.capacity import CapacityController
    from medre.runtime.builder import AdapterBuildFailure
    from medre.runtime.retry import RetryWorker
    from medre.runtime.route_engine import RouteEligibility, RouteStartupReadiness

__all__ = ["MedreApp", "RuntimeState"]

_logger = logging.getLogger(__name__)


class RuntimeState(enum.Enum):
    """Deterministic lifecycle states for the MEDRE runtime.

    Transitions::

        INITIALIZED → STARTING → RUNNING → STOPPING → STOPPED
                                ↘ FAILED
        Any state → FAILED on unrecoverable error.
    """

    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


def _utc_now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(timezone.utc)


def _monotonic_ms() -> float:
    """Return a monotonic timestamp in milliseconds."""
    return _time.monotonic() * 1000


def _drain_pending_cancellations() -> int:
    """Drain all pending cancellation requests from the current task.

    ``Task.uncancel()`` decrements the cancellation count by one and
    returns the **remaining** count (not the number removed).  To drain
    every pending request we must loop while ``cancelling()`` is
    non-zero.

    Returns the number of cancellation requests removed, or 0 when
    called outside an asyncio task context.
    """
    current = asyncio.current_task()
    if current is None:
        return 0
    count = 0
    while current.cancelling() > 0:
        current.uncancel()
        count += 1
    return count


def _outcome_from_task(
    task: asyncio.Task[object],
    default_outcome: str,
) -> tuple[str, BaseException | None, bool]:
    """Extract ``(outcome, exception, cancelled_outer)`` from a finished task.

    Caller must have already confirmed ``task.done()``.  We never ``await``
    the task, so the adapter's exception (if any) is stored on the task
    object and inspected here.  Mapping:

    * Task ended with ``CancelledError`` (either ``task.cancel()`` was
      called and the coroutine handled it, or the coroutine raised
      ``CancelledError`` on its own): **propagate** by raising
      ``CancelledError`` so the caller can apply its cancellation
      policy.  This matches the behaviour callers expect from
      ``await task`` and from ``asyncio.wait_for``.
    * No exception    -> ``default_outcome`` with ``None``
    * ``TimeoutError`` -> ``"timeout"`` with the exception
    * Anything else   -> ``"error"`` with the exception
    """
    if task.cancelled():
        raise asyncio.CancelledError("adapter stop cancelled")
    exc = task.exception()
    if exc is None:
        return (default_outcome, None, False)
    if isinstance(exc, asyncio.TimeoutError):
        return ("timeout", exc, False)
    return ("error", exc, False)


def _outcome_from_cancelled_task(
    task: asyncio.Task[object],
) -> tuple[str, BaseException | None, bool]:
    """Extract outcome from a task that *we* cancelled (stage 2).

    The caller has already called ``task.cancel()`` and the task has
    finished.  In this stage a cancelled task is the *expected* result
    of our forced cancellation — it is not an external cancellation to
    propagate.  If the task raised an exception during cancellation
    cleanup, that exception is still a timeout outcome.

    Caller must have already confirmed ``task.done()``.
    """
    if task.cancelled():
        return ("timeout", None, False)
    exc = task.exception()
    if exc is None:
        return ("timeout", None, False)
    return ("timeout", exc, False)


@dataclass
class MedreApp:
    """Holds all runtime subsystem references and coordinates lifecycle.

    Constructed by :class:`~medre.runtime.builder.RuntimeBuilder`.  Callers
    should use :meth:`start` and :meth:`stop` to manage the runtime
    lifecycle.

    Attributes
    ----------
    config:
        The resolved runtime configuration.
    paths:
        Fully-resolved filesystem paths.
    storage:
        SQLite storage backend (may be ``None`` when storage is disabled).
    event_bus:
        Central async event bus.
    rendering_pipeline:
        Rendering pipeline for converting events to adapter payloads.
    router:
        Route matching engine.
    fallback_resolver:
        Adapter capability fallback resolver.
    relation_resolver:
        Cross-adapter relation resolver.
    pipeline_runner:
        Central pipeline orchestrator.
    diagnostician:
        Diagnostic event recorder.
    adapters:
        Mapping of adapter ID to adapter instance.
    shutdown_event:
        Async event set when graceful shutdown is requested.
    build_failures:
        Adapters that failed during construction.
    adapter_start_monotonic:
        Per-adapter monotonic start timestamps in milliseconds
        (``time.monotonic() * 1000``).  Populated during :meth:`start`;
        process-local only — not wall-clock, not persisted, not refreshed.
    adapter_start_duration_ms:
        Per-adapter startup duration in milliseconds. Populated alongside
        :attr:`adapter_start_monotonic` for operator summaries.
    started_adapter_ids:
        Ordered list of adapter IDs that successfully started.
    route_stats:
        Per-route delivery statistics owned by the runtime (live counters
        populated by the pipeline runner).  ``None`` when routing is
        disabled or the runtime was built without a stats collector.
    route_eligibility:
        Structured route readiness metadata populated by the builder.
        ``None`` when not yet built.
    """

    config: RuntimeConfig
    paths: MedrePaths
    storage: SQLiteStorage | None
    event_bus: EventBus
    rendering_pipeline: RenderingPipeline
    router: Router
    fallback_resolver: FallbackResolver
    relation_resolver: RelationResolver
    pipeline_runner: PipelineRunner
    diagnostician: Diagnostician
    adapters: dict[str, AdapterContract]
    shutdown_event: asyncio.Event
    route_stats: RouteStats | None = None
    build_failures: list[AdapterBuildFailure] = field(default_factory=list)
    adapter_start_monotonic: dict[str, float] = field(default_factory=dict)
    adapter_start_duration_ms: dict[str, float] = field(default_factory=dict)
    started_adapter_ids: list[str] = field(default_factory=list)
    _state: RuntimeState = field(default=RuntimeState.INITIALIZED, init=False)
    _capacity_controller: CapacityController | None = field(default=None, init=False)
    _replay_engine: ReplayEngine | None = field(default=None, init=False)
    _retry_worker: RetryWorker | None = field(default=None, init=False)
    _runtime_accounting: RuntimeAccounting | None = field(default=None, init=False)
    _startup_wall: str | None = field(default=None, init=False)
    _startup_monotonic: float | None = field(default=None, init=False)
    _health_state: dict[str, Any] | None = field(default=None, init=False)
    _boot_summary: BootSummary | None = field(default=None, init=False)
    _recovery_run_id: str | None = field(default=None, init=False)
    _failed_adapter_ids: list[str] = field(default_factory=list, init=False)
    _route_eligibility: RouteEligibility | None = field(default=None, init=False)
    _route_provenance: dict[str, str] = field(default_factory=dict, init=False)
    _registered_routes: tuple = field(default=(), init=False)  # tuple[Route, ...]
    _startup_readiness: RouteStartupReadiness | None = field(default=None, init=False)
    _event_buffer: EventBuffer | None = field(
        default=None, init=False
    )  # set in __post_init__
    _adapter_states: dict[str, AdapterState] = field(default_factory=dict, init=False)
    # Set of still-alive adapter stop tasks that were abandoned by
    # ``_stop_adapter_with_deadline`` because the adapter's ``stop()``
    # suppressed cancellation.  Retained so the event loop does not
    # garbage-collect the task reference while it is still running;
    # tasks are removed via a done callback when they complete.
    _abandoned_adapter_stop_tasks: set[asyncio.Task[object]] = field(
        default_factory=set, init=False
    )
    _live_health_state: LiveHealthSnapshot | None = field(default=None, init=False)
    _live_health_poll_count: int = field(default=0, init=False)
    _outbox_state: dict[str, int] = field(default_factory=dict, init=False)
    _outbox_storage_authoritative: bool = field(default=False, init=False)

    # -- Post-init --------------------------------------------------------------

    def __post_init__(self) -> None:  # type: ignore[override]
        """Initialise mutable containers that cannot use field defaults."""
        self._event_buffer = EventBuffer()

    # -- State management -------------------------------------------------------

    @property
    def state(self) -> RuntimeState:
        """Return the current runtime lifecycle state."""
        return self._state

    @property
    def replay_engine(self) -> ReplayEngine | None:
        """Return the replay engine, or ``None`` if not wired."""
        return self._replay_engine

    @property
    def boot_summary(self) -> BootSummary | None:
        """Return the boot summary, or ``None`` if not yet started."""
        return self._boot_summary

    @property
    def route_eligibility(self) -> RouteEligibility | None:
        """Return route eligibility metadata, or ``None`` if not yet built."""
        return self._route_eligibility

    @property
    def startup_readiness(self) -> RouteStartupReadiness | None:
        """Return startup-derived route readiness, or ``None`` if not yet started."""
        return self._startup_readiness

    @property
    def event_buffer(self) -> EventBuffer:
        """Return the bounded runtime event buffer."""
        assert self._event_buffer is not None  # always set in __post_init__
        return self._event_buffer

    @property
    def retry_state(self) -> RetryWorkerState:
        """Return the retry worker state, or a default disabled state."""
        if self._retry_worker is not None:
            return self._retry_worker.state
        return RetryWorkerState()

    @property
    def outbox_state(self) -> dict[str, int]:
        """Return the last-known outbox status counts.

        Seeded from storage on startup.  Refreshed from storage after each
        retry worker cycle.  After :meth:`refresh_outbox_state_from_storage`
        is called, storage counts are authoritative for one read (typically
        a snapshot) — the retry worker cache is bypassed to prevent a
        stale ``{}`` from overwriting freshly queried storage counts.

        When no storage refresh is pending, the retry worker cache is used
        as the authoritative source (including ``{}`` when the worker has
        completed a cycle with no outbox items).
        """
        if self._outbox_storage_authoritative:
            # Storage refresh was called — return storage counts and clear
            # the flag so subsequent reads resume normal worker-cache logic.
            self._outbox_storage_authoritative = False
            return dict(self._outbox_state)
        if self._retry_worker is not None:
            latest = self._retry_worker.outbox_counts
            if latest is not None:
                # Worker has fresh counts from a completed cycle.
                self._outbox_state = dict(latest)
                return dict(latest)
            # Worker exists but hasn't completed a cycle yet.
            # Prefer storage-seeded counts over empty worker cache.
            return dict(self._outbox_state)
        return dict(self._outbox_state)

    async def refresh_outbox_state_from_storage(self) -> None:
        """Refresh outbox counts from storage if available.

        Called by diagnostics and runtime snapshot paths to ensure
        outbox counts reflect current storage state, not just the
        retry worker cache.  After a successful refresh, storage
        counts are marked authoritative so that the next read of
        :attr:`outbox_state` returns storage data instead of
        potentially stale worker cache.
        """
        if self.storage is not None:
            try:
                self._outbox_state = await self.storage.count_outbox_by_status()
                self._outbox_storage_authoritative = True
            except Exception:
                _logger.debug(
                    "Failed to refresh outbox state from storage", exc_info=True
                )

    @property
    def adapter_states(self) -> dict[str, AdapterState]:
        """Return a read-only copy of per-adapter lifecycle states."""
        return dict(self._adapter_states)

    def _set_adapter_state(self, adapter_id: str, target_state: AdapterState) -> None:
        """Transition *adapter_id* to *target_state*, validating the move.

        * Initial assignment (adapter not yet in registry) is always allowed.
        * Same-state assignments are silently ignored (idempotent).
        * Otherwise :func:`require_valid_transition` is consulted.
        """
        current = self._adapter_states.get(adapter_id)
        if current is None:
            # Initial assignment — no transition to validate.
            self._adapter_states[adapter_id] = target_state
            return
        if current is target_state:
            return
        require_valid_transition(current, target_state)
        self._adapter_states[adapter_id] = target_state

    def _emit_event(
        self,
        event_type: RuntimeEventType,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Emit a runtime event into the bounded buffer."""
        assert self._event_buffer is not None  # always set in __post_init__
        self._event_buffer.emit(event_type, detail)

    def _set_state(self, new_state: RuntimeState) -> None:
        """Transition to *new_state*, logging the change and emitting an event."""
        old_state = self._state
        if old_state is new_state:
            return
        _logger.debug(
            "Runtime state transition: %s → %s",
            old_state.value,
            new_state.value,
        )
        self._state = new_state
        self._emit_event(
            RuntimeEventType.STATE_TRANSITION,
            {"from": old_state.value, "to": new_state.value},
        )

    # -- Diagnostics ------------------------------------------------------------

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return a diagnostics dict including the current runtime state.

        This augments the subsystem-level diagnostics (route stats, replay
        metrics) with runtime-level metadata including capacity counters
        and shutdown fields.
        """
        snap: dict[str, Any] = {
            "accepting_work": (
                self._capacity_controller.accepting_work
                if self._capacity_controller is not None
                else True
            ),
            "capacity": (
                self._capacity_controller.snapshot()
                if self._capacity_controller is not None
                else None
            ),
            "runtime_state": self._state.value,
            "shutdown_drain_timeout_seconds": (
                self.config.limits.shutdown_drain_timeout_seconds
            ),
        }
        return snap

    # -- Live Health ------------------------------------------------------------

    async def refresh_live_health(self) -> LiveHealthSnapshot:
        """Perform a one-shot, caller-triggered health refresh of all adapters.

        This is a **manual** operation — there is no background polling,
        no scheduler, and no automatic refresh.  The caller (e.g. ``medre
        diagnostics --refresh-health``) invokes this explicitly when live
        adapter health state is needed.

        Callable only when the runtime is :attr:`RUNNING`; non-``RUNNING``
        states raise :class:`RuntimeError`.

        Iterates adapters in deterministic ``adapter_id`` order (same as
        startup),         calls each adapter's :meth:`~medre.core.contracts.adapter.AdapterContract.health_check`
        once, builds per-adapter :class:`~medre.core.supervision.health.AdapterLiveHealth`
        entries, classifies aggregate runtime health from the live results,
        and stores the resulting :class:`LiveHealthSnapshot` on the app.

        Per-adapter ``health_check()`` exceptions are caught and recorded
        with a bounded error string; they do not abort the refresh.
        :class:`asyncio.CancelledError` propagates immediately and does
        not emit a success event.

        Returns
        -------
        LiveHealthSnapshot
            The newly built snapshot (also stored as ``_live_health_state``).

        Raises
        ------
        RuntimeError
            If the runtime is not in the ``RUNNING`` state.
        """
        if self._state is not RuntimeState.RUNNING:
            raise RuntimeError(
                f"refresh_live_health requires RUNNING state, "
                f"current state is {self._state.value!r}"
            )

        # Compute the next poll count upfront but only assign after
        # the snapshot is successfully built and stored.  If a
        # CancelledError propagates from the polling loop, the count
        # (and _live_health_state) remain unchanged.
        next_poll_count = self._live_health_poll_count + 1

        adapter_ids = sorted(self.adapters.keys())

        _time.monotonic()
        adapter_entries: dict[str, AdapterLiveHealth] = {}
        live_states: list[AdapterState] = []
        failed_adapter_ids: list[str] = []

        for adapter_id in adapter_ids:
            adapter = self.adapters[adapter_id]
            poll_mono = _time.monotonic()
            poll_wall = _utc_now().isoformat()
            try:
                info = await adapter.health_check()
                # Derive adapter state from the live health_check result.
                live_state = health_to_adapter_state(info.health)
                live_states.append(live_state)

                # Detect fake/live mode.
                norm = normalize_adapter_health(
                    info,
                    lifecycle_state=self._adapter_states.get(adapter_id),
                    adapter=adapter,
                )

                adapter_entries[adapter_id] = AdapterLiveHealth(
                    adapter_id=adapter_id,
                    health=norm["health"],
                    adapter_state=live_state,
                    fake_or_live=norm["fake_or_live"],
                    poll_timestamp_monotonic=poll_mono,
                    poll_timestamp_wall=poll_wall,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_str = truncate_health_error(str(exc))
                live_state = AdapterState.FAILED
                live_states.append(live_state)
                failed_adapter_ids.append(adapter_id)

                adapter_entries[adapter_id] = AdapterLiveHealth(
                    adapter_id=adapter_id,
                    health="failed",
                    adapter_state=live_state,
                    fake_or_live="unknown",
                    poll_timestamp_monotonic=poll_mono,
                    poll_timestamp_wall=poll_wall,
                    error=error_str,
                )

        # Classify aggregate runtime health from live poll results.
        runtime_health = classify_runtime_health(live_states)
        operational, partial, failed, transitional = count_adapter_state_categories(
            live_states
        )

        poll_mono_end = _time.monotonic()
        poll_wall_end = _utc_now().isoformat()

        snapshot = LiveHealthSnapshot(
            runtime_health=runtime_health.value,
            adapter_summary={
                "healthy": operational,
                "degraded": partial,
                "failed": failed,
                "transitional": transitional,
                "total": len(live_states),
            },
            adapters=dict(sorted(adapter_entries.items())),
            poll_timestamp_monotonic=poll_mono_end,
            poll_timestamp_wall=poll_wall_end,
            poll_count=next_poll_count,
        )

        # Commit: save previous snapshot before overwriting for change detection.
        prev_snapshot = self._live_health_state
        self._live_health_state = snapshot
        self._live_health_poll_count = next_poll_count

        # Build event detail (cheap derived data only).
        event_detail: dict[str, Any] = {
            "runtime_health": runtime_health.value,
            "poll_count": next_poll_count,
            "adapter_summary": snapshot.adapter_summary,
        }
        if failed_adapter_ids:
            event_detail["failed_adapters"] = sorted(failed_adapter_ids)

        # Compute changed_adapters by comparing to previous snapshot.
        if prev_snapshot is not None:
            changed: list[str] = []
            for aid in adapter_ids:
                old_entry = prev_snapshot.adapters.get(aid)
                new_entry = snapshot.adapters.get(aid)
                if old_entry is None or new_entry is None:
                    continue
                if old_entry.health != new_entry.health:
                    changed.append(aid)
            if changed:
                event_detail["changed_adapters"] = sorted(changed)

        self._emit_event(RuntimeEventType.HEALTH_REFRESHED, event_detail)

        return snapshot

    # -- Lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Start all subsystems in dependency order.

        Order: storage → pipeline runner → adapters.

        Adapters are started in deterministic order (sorted by adapter_id).
        Note: this differs from build order, which sorts by
        ``(transport, adapter_id)`` — see
        :mod:`~medre.runtime.builder` for details.
        Individual adapter start failures are logged with adapter_id
        attribution but do **not** abort the remaining adapters.  On
        catastrophic core subsystem failure, any already-started adapters
        are stopped in reverse order.

        Startup semantics
        -----------------
        * **Zero adapters started** (including build failures) →
          ``RuntimeStartupError`` (total failure).  Pipeline runner and
          storage are cleaned up before raising; callers do **not** need
          to call ``stop()``.
        * **Partial adapter startup** → allowed; runtime enters ``RUNNING``
          with ``DEGRADED`` health.  Callers should inspect
          :attr:`boot_summary` for details.
        * **All adapters started** → ``RUNNING`` with ``HEALTHY`` health.

        Raises
        ------
        RuntimeError
            If the app has already been started or is shutting down.
        RuntimeStartupError
            If a core subsystem (storage, pipeline runner) fails to start,
            or if zero adapters started (including build failures).  Core
            resources are cleaned up before raising.
        """
        if self._state is not RuntimeState.INITIALIZED:
            raise RuntimeError(
                f"App is already started or in state {self._state.value!r}"
            )

        self._set_state(RuntimeState.STARTING)
        _logger.info("Starting MEDRE runtime %s", self.config.runtime.name)

        # Record startup timestamps.
        self._startup_wall = _utc_now().isoformat()
        self._startup_monotonic = _time.monotonic()

        # Generate recovery run ID for this startup cycle.
        self._recovery_run_id = uuid.uuid4().hex

        # 0. Create required directories.
        self._ensure_dirs()

        # 1. Initialise storage.
        if self.storage is not None:
            try:
                await self.storage.initialize()
                _logger.info("Storage initialised")
            except Exception as exc:
                self._set_state(RuntimeState.FAILED)
                raise RuntimeStartupError(
                    f"Failed to initialise storage: {exc}"
                ) from exc

        # 1.5 Seed outbox counts from storage so snapshot has data before
        #     the first retry-worker cycle populates the worker cache.
        if self.storage is not None:
            try:
                self._outbox_state = await self.storage.count_outbox_by_status()
                self._outbox_storage_authoritative = True
            except Exception:
                _logger.debug("Failed to seed outbox state from storage", exc_info=True)

        # 2. Start the pipeline runner.
        try:
            await self.pipeline_runner.start()
            _logger.info("Pipeline runner started")
        except Exception as exc:
            # Storage was already initialised; clean it up before raising.
            await self._cleanup_storage_safely()
            self._set_state(RuntimeState.FAILED)
            raise RuntimeStartupError(
                f"Failed to start pipeline runner: {exc}"
            ) from exc

        # 2.5. Start the retry worker (if enabled).
        if self.config.retry.enabled and self.storage is not None:
            from medre.runtime.retry import RetryWorker as _RW

            self._retry_worker = _RW(
                storage=self.storage,
                pipeline=self.pipeline_runner,
                capacity_controller=self._capacity_controller,
                enabled=self.config.retry.enabled,
                interval_seconds=self.config.retry.interval_seconds,
                batch_size=self.config.retry.batch_size,
                max_attempts=self.config.retry.max_attempts,
                event_buffer=self._event_buffer,
                stop_timeout_seconds=float(
                    self.config.runtime.shutdown_timeout_seconds
                ),
            )
            await self._retry_worker.start()

        # 3. Start each adapter in deterministic order.
        #    Sort by adapter_id for reproducible startup sequence.
        adapter_ids = sorted(self.adapters.keys())
        total = len(adapter_ids)

        _logger.info(
            "Starting %d adapter(s): %s",
            total,
            ", ".join(adapter_ids) if adapter_ids else "(none)",
        )

        # Initialize per-adapter lifecycle states.
        for adapter_id in adapter_ids:
            self._set_adapter_state(adapter_id, AdapterState.INITIALIZING)
        for bf in self.build_failures:
            self._set_adapter_state(bf.adapter_id, AdapterState.FAILED)

        failed_adapter_ids: list[str] = []
        # Track cancellation requests drained during per-adapter
        # start-failure cleanup stops.  These are accumulated in the
        # inner ``except asyncio.CancelledError`` branch so they can
        # be restored if the outer CancelledError handler fires.
        _startup_cleanup_drained: int = 0
        try:
            for adapter_id in adapter_ids:
                adapter = self.adapters[adapter_id]
                transport = getattr(adapter, "platform", "unknown")
                t0 = _monotonic_ms()
                try:
                    from medre.core.contracts.adapter import AdapterContext

                    ctx = AdapterContext(
                        adapter_id=adapter_id,
                        event_bus=self.event_bus,
                        publish_inbound=self._make_publish_inbound(),
                        logger=logging.getLogger(f"medre.adapters.{adapter_id}"),
                        clock=_utc_now,
                        shutdown_event=self.shutdown_event,
                        record_outbound_native_ref=self.pipeline_runner._record_outbound_native_ref,
                    )
                    await adapter.start(ctx)
                    elapsed = _monotonic_ms() - t0
                    self.adapter_start_monotonic[adapter_id] = t0
                    self.adapter_start_duration_ms[adapter_id] = elapsed
                    self.started_adapter_ids.append(adapter_id)
                    self._set_adapter_state(adapter_id, AdapterState.READY)
                    _logger.info(
                        "Adapter %s.%s started in %.0fms",
                        transport,
                        adapter_id,
                        elapsed,
                    )
                    self._emit_event(
                        RuntimeEventType.ADAPTER_STARTED,
                        {"adapter_id": adapter_id, "platform": transport},
                    )
                except Exception as exc:
                    elapsed = _monotonic_ms() - t0
                    failed_adapter_ids.append(adapter_id)
                    # Best-effort cleanup: stop the adapter so it can
                    # release any resources acquired during the partial
                    # start.  Cleanup errors are logged but suppressed
                    # so that the original start-failure error is preserved.
                    try:
                        outcome, _, _ = await self._stop_adapter_with_deadline(
                            adapter=adapter,
                            adapter_id=adapter_id,
                            transport=transport,
                            timeout=float(self.config.runtime.shutdown_timeout_seconds),
                        )
                    except asyncio.CancelledError as cleanup_exc:
                        # Best-effort startup cleanup: suppress the
                        # CancelledError so the original start-failure
                        # error is preserved.  Drain the cancellation
                        # state so subsequent adapter stops in the loop
                        # actually get a chance to run.  The drained
                        # count is accumulated into
                        # ``_startup_cleanup_drained`` so the outer
                        # CancelledError handler can restore the full
                        # cancellation depth if it fires.
                        #
                        # Safety of suppression when no outer
                        # CancelledError fires: the caller's cancellation
                        # intent was to abort startup, which already
                        # happened (this adapter's start failed).  The
                        # start-failure path will raise
                        # RuntimeStartupError (total failure) or proceed
                        # to RUNNING (partial failure) — both are the
                        # correct outcomes for a cancelled startup.
                        # The drain is required so subsequent adapter
                        # stops in this loop are not immediately
                        # cancelled before they can release resources.
                        _startup_cleanup_drained += _drain_pending_cancellations()
                        _logger.debug(
                            "Cancelled stopping adapter %s.%s after start failure: %s",
                            transport,
                            adapter_id,
                            cleanup_exc,
                        )
                    else:
                        if outcome == "timeout":
                            _logger.debug(
                                "Timeout stopping adapter %s.%s after start failure",
                                transport,
                                adapter_id,
                            )
                        elif outcome == "abandoned":
                            _logger.debug(
                                "Abandoned adapter %s.%s after start failure "
                                "(event loop will reclaim when shutting down)",
                                transport,
                                adapter_id,
                            )
                    self._set_adapter_state(adapter_id, AdapterState.FAILED)
                    _logger.error(
                        "Adapter %s.%s failed to start (%.0fms): %s",
                        transport,
                        adapter_id,
                        elapsed,
                        exc,
                    )
                    self._emit_event(
                        RuntimeEventType.ADAPTER_START_FAILED,
                        {
                            "adapter_id": adapter_id,
                            "platform": transport,
                            "error": str(exc),
                        },
                    )
        except asyncio.CancelledError as c_exc:
            # Cancellation arrived during the startup loop.  Drain the
            # pending cancellation so cleanup awaits can actually run,
            # then run the same cleanup as the ``Exception`` handler.
            # The cancellation is restored after cleanup so it propagates
            # to the caller.  The ``except CancelledError`` branch MUST
            # come first — Python evaluates except clauses in order, and
            # ``asyncio.CancelledError`` is a ``BaseException`` (not an
            # ``Exception``).
            #
            # The restore includes:
            #   _cleared                — cancellations drained by this handler
            #   _cleanup_drained        — cancellations drained by
            #                             _start_failure_cleanup() →
            #                             _cleanup_started_adapters()
            #   _startup_cleanup_drained — cancellations drained during
            #                             per-adapter start-failure cleanup
            #                             stops inside the loop body
            _cleared = _drain_pending_cancellations()
            _cleanup_drained = await self._start_failure_cleanup()
            _total = _cleared + _cleanup_drained + _startup_cleanup_drained
            if _total:
                current = asyncio.current_task()
                if current is not None:
                    for _ in range(_total):
                        current.cancel()
            raise c_exc
        except Exception:
            # Catastrophic failure during the loop itself (not an adapter
            # failure).  Clean up already-started adapters in reverse order,
            # then clean up core resources (pipeline runner + storage).
            await self._start_failure_cleanup()
            raise

        # Store failed adapter IDs for diagnostics.
        self._failed_adapter_ids = failed_adapter_ids

        # -- Classify startup outcome -----------------------------------------
        started_count = len(self.started_adapter_ids)
        failed_count = len(failed_adapter_ids)
        build_failed = len(self.build_failures)
        attempted_total = total + build_failed
        effective_failed = failed_count + build_failed
        outcome = classify_startup_outcome(
            started_count, effective_failed, attempted_total
        )

        # -- Classify runtime health from adapter states ----------------------
        adapter_states: list[AdapterState] = list(self._adapter_states.values())
        health = classify_runtime_health(adapter_states)

        # Store health state (supervision snapshot) for downstream consumers.
        self._health_state = runtime_supervision_snapshot(adapter_states)

        # -- Persisted events count -------------------------------------------
        persisted_count: int | None = None
        if self.storage is not None and hasattr(self.storage, "count_events"):
            try:
                persisted_count = await self.storage.count_events()
            except Exception:
                persisted_count = None

        # -- Count disabled adapters ------------------------------------------
        disabled_count = 0
        for _transport, _adapter_id, rtc in self.config.adapters.all_configs():
            if not rtc.enabled:
                disabled_count += 1

        # -- Route count ------------------------------------------------------
        route_count = len(self._registered_routes) if self._registered_routes else 0

        # -- Storage backend name ---------------------------------------------
        storage_backend = "none"
        if self.storage is not None:
            storage_backend = getattr(self.config.storage, "backend", "unknown")

        # -- Build boot summary -----------------------------------------------
        self._boot_summary = build_boot_summary(
            startup_timestamp=self._startup_wall,
            startup_outcome=outcome.value,
            runtime_health=health.value,
            adapters_started=started_count,
            adapters_failed=effective_failed,
            adapters_total=attempted_total,
            adapters_disabled=disabled_count,
            build_failure_count=build_failed,
            build_failure_ids=[bf.adapter_id for bf in self.build_failures],
            failed_adapter_ids=failed_adapter_ids,
            started_adapter_ids=list(self.started_adapter_ids),
            route_count=route_count,
            storage_backend=storage_backend,
            replay_available=self._replay_engine is not None,
            persisted_events_count=persisted_count,
            recovery_run_id=self._recovery_run_id or "",
        )

        # -- Compute startup-derived route readiness ----------------------------
        if self._route_eligibility is not None and self._route_provenance is not None:
            from medre.runtime.route_engine import compute_startup_readiness

            self._startup_readiness = compute_startup_readiness(
                eligibility=self._route_eligibility,
                adapter_states=dict(self._adapter_states),
                provenance=self._route_provenance,
                registered_routes=self._registered_routes,
                config_routes=self.config.routes,
            )

        # -- Emit startup classified event ------------------------------------
        self._emit_event(
            RuntimeEventType.STARTUP_CLASSIFIED,
            {
                "health": health.value,
                "outcome": outcome.value,
                "started_count": started_count,
                "failed_count": effective_failed,
            },
        )

        # -- Summary logging (DEBUG — run_commands prints the same to stdout) --
        if failed_count > 0 or build_failed > 0:
            _logger.debug(
                "Runtime started with %d/%d adapter(s)%s (%d start failed, %d build failed)",
                started_count,
                attempted_total,
                (
                    f" — {', '.join(self.started_adapter_ids)}"
                    if self.started_adapter_ids
                    else ""
                ),
                failed_count,
                build_failed,
            )
        else:
            _logger.debug(
                "Runtime started with %d/%d adapter(s)",
                started_count,
                attempted_total,
            )

        # -- Handle startup outcome -------------------------------------------
        if outcome == StartupOutcome.TOTAL_FAILURE:
            # Clean up all started resources before raising so callers
            # do not need to call stop() after a failed start.
            await self._cleanup_started_adapters()
            await self._cleanup_core_resources()
            self._set_state(RuntimeState.FAILED)
            raise RuntimeStartupError(
                f"Total startup failure: 0 of {attempted_total} adapter(s) started "
                f"({failed_count} start failed, {build_failed} build failed)"
            )

        if outcome == StartupOutcome.PARTIAL:
            degradation_parts: list[str] = []
            if failed_count > 0:
                degradation_parts.append(f"{failed_count} start failed")
            if build_failed > 0:
                degradation_parts.append(f"{build_failed} build failed")
            degradation_cause = ", ".join(degradation_parts)
            _logger.warning(
                "Runtime running in DEGRADED mode: %d/%d adapter(s) started (%s)",
                started_count,
                attempted_total,
                degradation_cause,
            )

        self._set_state(RuntimeState.RUNNING)

        # -- Emit route eligibility events (passive observation) ---------------
        if self._route_eligibility is not None:
            for sr in self._route_eligibility.skipped:
                self._emit_event(
                    RuntimeEventType.ROUTE_SKIPPED,
                    {
                        "route_id": sr.route_id,
                        "reason": sr.reason,
                    },
                )
            for ur in self._route_eligibility.unavailable:
                self._emit_event(
                    RuntimeEventType.ROUTE_UNAVAILABLE,
                    {
                        "route_id": ur.route_id,
                        "reason": ur.reason,
                    },
                )

    async def stop(self) -> None:
        """Stop all subsystems in reverse dependency order.

        Order: adapters → pipeline runner → storage.

        Adapters are stopped in reverse start order.  Individual stop
        failures are logged but do not prevent other subsystems from
        shutting down.

        This method is idempotent: calling it when the runtime is in
        ``STOPPED``, ``STOPPING``, or ``INITIALIZED`` state returns
        immediately.  A concurrent ``stop()`` call during an in-flight
        ``STOPPING`` transition is guarded by this early-return so the
        second caller does not race the first.

        Raises
        ------
        RuntimeShutdownError
            If one or more subsystems fail to shut down cleanly.
        """
        if self._state in (
            RuntimeState.STOPPED,
            RuntimeState.STOPPING,
            RuntimeState.INITIALIZED,
        ):
            return

        self._set_state(RuntimeState.STOPPING)
        timeout = self.config.runtime.shutdown_timeout_seconds
        _logger.info(
            "Stopping MEDRE runtime %s (timeout=%.1fs)",
            self.config.runtime.name,
            timeout,
        )

        # Deferred cancellation: saved here and re-raised after core
        # cleanup so pipeline_runner.stop() and storage.close() always
        # run, even when an external cancellation arrives during Phase 1
        # (retry worker stop) or adapter stops.
        _cancelled: asyncio.CancelledError | None = None
        _deferred_cancel_count: int = 0

        # Phase 1: Stop accepting new work.
        if self._capacity_controller is not None:
            self._capacity_controller.stop_accepting()
        if self._replay_engine is not None:
            self._replay_engine.cancel()
            _logger.info("Runtime stopping — replay engine cancelled, capacity stopped")

        # Stop the retry worker before draining work.
        if self._retry_worker is not None:
            try:
                await self._retry_worker.stop()
            except asyncio.CancelledError as c_exc:
                # Cancellation during retry worker stop must NOT skip
                # pipeline_runner.stop() or storage.close().  Defer
                # the cancellation so core cleanup still runs.
                _cancelled = c_exc
                _deferred_cancel_count += _drain_pending_cancellations()
                _logger.debug("Cancelled while stopping retry worker (deferred)")
            except Exception as exc:
                _logger.error("Error stopping retry worker: %s", exc)
            # Visibility for the abandonment case: ``stop()`` returns
            # normally even when the worker task was abandoned (i.e.
            # cancellation-resistant), so the caller would otherwise
            # have no signal.  Do not escalate to
            # ``RuntimeShutdownError`` — abandonment is best-effort
            # cleanup, the same as abandoned adapter stops.
            if self._retry_worker.state.abandoned:
                _logger.warning(
                    "RetryWorker was abandoned during shutdown: "
                    "background task did not finish within timeout; "
                    "state.running=True, abandoned=True. "
                    "Subprocess-driven retries may still be in-flight."
                )

        _logger.info("Runtime stopping — accepting no new work")

        # Phase 2: Drain in-flight work with timeout.
        if self._capacity_controller is not None:
            drain_deadline = (
                _time.monotonic() + self.config.limits.shutdown_drain_timeout_seconds
            )
            drain_snap: dict | None = None
            while _time.monotonic() < drain_deadline:
                drain_snap = self._capacity_controller.snapshot()
                if (
                    drain_snap["delivery_current"] == 0
                    and drain_snap["replay_current"] == 0
                ):
                    _logger.info("In-flight work drained")
                    break
                await asyncio.sleep(0.1)
            else:
                if drain_snap is None:
                    drain_snap = self._capacity_controller.snapshot()
                _logger.warning(
                    "Drain timed out — %d delivery, %d replay in-flight abandoned",
                    drain_snap["delivery_current"],
                    drain_snap["replay_current"],
                )
                # Persist structured abandonment evidence for in-flight
                # deliveries that could not complete before the drain deadline.
                await self._persist_drain_abandoned_evidence()

        # Signal shutdown to adapters and waiters.
        self.shutdown_event.set()

        # 1. Stop adapters in reverse start order for clean teardown.
        errors: list[tuple[str, BaseException]] = []
        _terminal = {AdapterState.FAILED, AdapterState.STOPPED}
        for adapter_id in reversed(self.started_adapter_ids):
            if self._adapter_states.get(adapter_id) in _terminal:
                continue
            adapter = self.adapters.get(adapter_id)
            if adapter is None:
                continue
            transport = getattr(adapter, "platform", "unknown")
            _logger.debug("Adapter %s.%s stopping", transport, adapter_id)
            self._set_adapter_state(adapter_id, AdapterState.STOPPING)
            try:
                outcome, exc, _ = await self._stop_adapter_with_deadline(
                    adapter=adapter,
                    adapter_id=adapter_id,
                    transport=transport,
                    timeout=float(timeout),
                )
            except asyncio.CancelledError as c_exc:
                # External cancellation during the stop.  Defer until
                # after pipeline + storage cleanup so the close still
                # runs.  Drain the cancellation so we can continue
                # best-effort cleanup of remaining adapters.
                _deferred_cancel_count += _drain_pending_cancellations()
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.debug(
                    "Cancelled while stopping adapter %s.%s (deferred)",
                    transport,
                    adapter_id,
                )
                if _cancelled is None:
                    _cancelled = c_exc
                continue
            if outcome == "stopped":
                _logger.info("Adapter %s.%s stopped", transport, adapter_id)
                self._set_adapter_state(adapter_id, AdapterState.STOPPED)
                self._emit_event(
                    RuntimeEventType.ADAPTER_STOPPED,
                    {"adapter_id": adapter_id},
                )
            elif outcome == "timeout":
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.error(
                    "Timeout stopping adapter %s.%s after %.1fs",
                    transport,
                    adapter_id,
                    timeout,
                )
                errors.append((adapter_id, exc))
            else:  # outcome == "abandoned" or "error"
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                errors.append((adapter_id, exc))

        # Also stop any adapters that were in self.adapters but not in
        # started_adapter_ids (e.g. if start() was never called, or start()
        # failed and the adapter is still in INITIALIZING).
        # Skip adapters already in a terminal state (FAILED, STOPPED).
        _terminal = {AdapterState.FAILED, AdapterState.STOPPED}
        for adapter_id, adapter in self.adapters.items():
            if adapter_id in self.started_adapter_ids:
                continue
            if self._adapter_states.get(adapter_id) in _terminal:
                continue
            transport = getattr(adapter, "platform", "unknown")
            _logger.debug(
                "Adapter %s.%s stopping (never started)", transport, adapter_id
            )
            self._set_adapter_state(adapter_id, AdapterState.STOPPING)
            try:
                outcome, _, _ = await self._stop_adapter_with_deadline(
                    adapter=adapter,
                    adapter_id=adapter_id,
                    transport=transport,
                    timeout=float(timeout),
                )
            except asyncio.CancelledError as c_exc:
                _deferred_cancel_count += _drain_pending_cancellations()
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.debug(
                    "Cancelled while stopping never-started adapter %s.%s (deferred)",
                    transport,
                    adapter_id,
                )
                if _cancelled is None:
                    _cancelled = c_exc
                continue
            if outcome == "stopped":
                self._set_adapter_state(adapter_id, AdapterState.STOPPED)
                _logger.info(
                    "Adapter %s.%s stopped (never started)", transport, adapter_id
                )
            elif outcome == "timeout":
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.debug(
                    "Timeout stopping never-started adapter %s.%s after %.1fs",
                    transport,
                    adapter_id,
                    timeout,
                )
                # Intentionally not appended to `errors`: started-adapter
                # cleanup failures are shutdown-visible because they may
                # indicate data-loss or partial-delivery states.  A
                # never-started adapter has no such side-effects, so its
                # cleanup is best-effort and should not mask the primary
                # shutdown result.
            else:  # outcome == "abandoned" or "error"
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.debug(
                    "Never-started adapter %s.%s did not stop cleanly "
                    "(see earlier log for details)",
                    transport,
                    adapter_id,
                )

        # 2. Stop the pipeline runner.
        try:
            await self.pipeline_runner.stop()
            _logger.info("Pipeline runner stopped")
        except Exception as exc:
            _logger.error("Error stopping pipeline runner: %s", exc)
            errors.append(("pipeline", exc))

        # 3. Close storage.
        if self.storage is not None:
            try:
                await self.storage.close()
                _logger.info("Storage closed")
            except Exception as exc:
                _logger.error("Error closing storage: %s", exc)
                errors.append(("storage", exc))

        # Re-raise deferred CancelledError after cleanup is complete.
        # Set a terminal state first so a subsequent stop() call does
        # not get trapped in the "STOPPING" early-return guard above.
        if _cancelled is not None:
            # External cancellation interrupted normal completion;
            # FAILED is the honest terminal state, since cleanup was
            # driven by a cancellation rather than a normal stop.
            self._set_state(RuntimeState.FAILED)
            if _deferred_cancel_count:
                # Restore the exact number of cancellation requests
                # that were drained so the caller's next await still
                # sees the full cancellation depth.  One ``cancel()``
                # is not sufficient when multiple requests were pending.
                current = asyncio.current_task()
                if current is not None:
                    for _ in range(_deferred_cancel_count):
                        current.cancel()
            raise _cancelled

        if errors:
            summary = "; ".join(f"{name}: {exc}" for name, exc in errors)
            self._set_state(RuntimeState.FAILED)
            raise RuntimeShutdownError(f"Errors during shutdown: {summary}")

        self._set_state(RuntimeState.STOPPED)
        _logger.info("Runtime stopped")

    async def wait_for_shutdown(self, timeout: float | None = None) -> None:
        """Wait until the shutdown signal is set.

        Parameters
        ----------
        timeout:
            Optional timeout in seconds. If set, raises asyncio.TimeoutError
            when the timeout is reached without the shutdown event being set.
        """
        if timeout is not None:
            await asyncio.wait_for(self.shutdown_event.wait(), timeout=timeout)
        else:
            await self.shutdown_event.wait()

    # -- Helpers -----------------------------------------------------------------

    async def _stop_adapter_with_deadline(
        self,
        adapter: Any,
        adapter_id: str,
        transport: str,
        timeout: float,
    ) -> tuple[str, BaseException | None, bool]:
        """Stop *adapter* with a hard-bounded two-stage deadline.

        A simple ``asyncio.wait_for(adapter.stop(...), timeout=...)`` is
        not a hard deadline: ``wait_for`` cancels the awaited task on
        timeout and then waits for its cancellation/cleanup to finish,
        but if ``adapter.stop`` suppresses ``CancelledError`` or blocks
        during its own cleanup, ``wait_for`` can overrun the timeout or
        hang indefinitely.  This helper provides a true hard deadline by
        driving the stop on an explicit asyncio task and polling
        ``task.done()`` at a short cadence.  Polling is
        required because ``asyncio.wait_for`` cannot terminate a
        coroutine that suppresses ``CancelledError`` indefinitely —
        the cancel is consumed by an inner ``except`` block and the
        await never raises, leaving ``wait_for`` to wait forever.

        Two-stage deadline:

        1. **Cooperative.**  Poll ``stop_task.done()`` at 10 ms
           intervals until either the task finishes (clean stop) or
           ``timeout`` seconds elapse.
        2. **Forced cancellation.**  If the cooperative stage times out,
           call ``stop_task.cancel()`` and poll again for a second
           ``timeout``-second grace period.  If the task is still
           alive after both stages, the task is **abandoned** and the
           event loop reclaims it on shutdown.

        External ``CancelledError`` delivered to this helper itself is
        handled by giving the adapter one bounded cancel grace and
        then re-raising, so the caller's cleanup still runs.

        Returns
        -------
        (``outcome``, ``exception``, ``cancelled_outer``)
            * ``outcome`` -- one of ``"stopped"``, ``"timeout"``,
              ``"abandoned"``, ``"error"``.
            * ``exception`` -- the exception raised by the adapter, or
              ``None`` for a clean stop.
            * ``cancelled_outer`` -- ``True`` if an external
              ``CancelledError`` was observed; the caller decides
              whether to propagate, defer, or swallow it.
        """
        stop_task = asyncio.create_task(adapter.stop(timeout=float(timeout)))
        loop = asyncio.get_running_loop()
        try:
            # Stage 1: cooperative.  Poll until done or deadline.
            deadline = loop.time() + float(timeout)
            while not stop_task.done():
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(0.01)
            if stop_task.done():
                if stop_task.cancelled():
                    # Adapter's stop() raised CancelledError on its own
                    # (we never called task.cancel()).  Propagate so the
                    # caller can apply its cancellation policy.
                    raise asyncio.CancelledError("adapter stop cancelled")
                return _outcome_from_task(stop_task, "stopped")
            # Stage 2: forced cancellation with bounded grace.
            _logger.error(
                "Timeout stopping adapter %s.%s after %.1fs, cancelling",
                transport,
                adapter_id,
                timeout,
            )
            stop_task.cancel()
            cancel_deadline = loop.time() + float(timeout)
            while not stop_task.done():
                if loop.time() >= cancel_deadline:
                    # Still alive after cancel grace — abandon.
                    _logger.error(
                        "Adapter %s.%s did not stop after cancel within "
                        "%.1fs; abandoning",
                        transport,
                        adapter_id,
                        timeout,
                    )
                    self._retain_abandoned_stop_task(stop_task)
                    return (
                        "abandoned",
                        TimeoutError("adapter stop abandoned"),
                        False,
                    )
                await asyncio.sleep(0.01)
            # Task finished during cancel grace.  We called
            # task.cancel(), so the task ended because of our cancel.
            # If the task's exception is non-None, it raised during
            # cancellation — still a timeout outcome, not a propagation.
            return _outcome_from_cancelled_task(stop_task)
        except asyncio.CancelledError:
            # External cancellation arrived during the stop.  Try to
            # give the adapter its own bounded cancel grace before
            # propagating, so the adapter can do a minimal cleanup if
            # it cooperates.  Polling is used so cancellation-resistant
            # adapters cannot hang the helper indefinitely.
            if not stop_task.done():
                stop_task.cancel()
                cancel_deadline = loop.time() + float(timeout)
                while not stop_task.done():
                    if loop.time() >= cancel_deadline:
                        # Still alive after cancel grace — retain the
                        # task so the event loop does not garbage-collect
                        # the reference while it is still running.
                        self._retain_abandoned_stop_task(stop_task)
                        break
                    await asyncio.sleep(0.01)
            raise

    def _retain_abandoned_stop_task(self, stop_task: asyncio.Task[object]) -> None:
        """Retain *stop_task* in :attr:`_abandoned_adapter_stop_tasks`.

        Called when :meth:`_stop_adapter_with_deadline` abandons a
        still-running adapter stop task (the adapter's ``stop()``
        suppressed cancellation and the cancel grace expired).  Without
        this retention, the local ``stop_task`` reference would go out
        of scope when the helper returns, allowing the event loop to
        garbage-collect the task while it is still running.  The task
        is removed from the retained set when it finishes via a done
        callback, so the set does not grow unboundedly.

        The done callback also consumes the task's exception (if any)
        so Python does not emit ``Task exception was never retrieved``.
        """
        self._abandoned_adapter_stop_tasks.add(stop_task)

        def _on_done(task: asyncio.Task[object]) -> None:
            try:
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        _logger.warning(
                            "Abandoned adapter stop task raised: %s",
                            exc,
                        )
            except Exception:
                pass
            finally:
                self._abandoned_adapter_stop_tasks.discard(task)

        stop_task.add_done_callback(_on_done)

    async def _persist_drain_abandoned_evidence(self) -> None:
        """Persist structured abandonment receipts for in-flight deliveries.

        Called from :meth:`stop` when the drain deadline expires with
        deliveries still in-flight.  Produces one ``status="suppressed"``
        :class:`DeliveryReceipt` per abandoned delivery with
        ``failure_kind="shutdown_rejection"`` and
        ``error="shutdown_drain_timeout"``, enabling operators to audit
        what was lost via ``medre inspect receipts``.

        Silently skips persistence when storage is unavailable or when no
        in-flight deliveries are tracked by the pipeline runner.
        """
        from medre.core.events.canonical import DeliveryReceipt
        from medre.core.planning.delivery_plan import DeliveryFailureKind

        abandoned = self.pipeline_runner.drain_abandoned_deliveries()
        if not abandoned or self.storage is None:
            return

        now = datetime.now(tz=timezone.utc)
        persisted_count = 0
        for inflight in abandoned:
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=f"rcpt-{uuid.uuid4()}",
                event_id=inflight.event_id,
                delivery_plan_id=inflight.delivery_plan_id,
                target_adapter=inflight.target_adapter,
                target_channel=inflight.target_channel,
                route_id=inflight.route_id,
                status="suppressed",
                error="shutdown_drain_timeout",
                failure_kind=DeliveryFailureKind.SHUTDOWN_REJECTION.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=1,
                parent_receipt_id=None,
                source=inflight.source,
                replay_run_id=inflight.replay_run_id,
            )
            try:
                await self.storage.append_receipt(receipt)
                persisted_count += 1
            except Exception as exc:
                _logger.error(
                    "Failed to persist drain-abandoned receipt for "
                    "event_id=%s target_adapter=%s: %s",
                    inflight.event_id,
                    inflight.target_adapter,
                    exc,
                )

        if persisted_count > 0:
            _logger.info(
                "Persisted %d drain-abandoned receipt(s) as shutdown_rejection evidence",
                persisted_count,
            )

    async def _cleanup_started_adapters(self) -> int:
        """Stop already-started adapters in reverse order during failed startup.

        Used for partial-startup cleanup: if an unrecoverable error occurs
        after some adapters have started, this ensures they are torn down
        cleanly rather than left in a half-started state.

        Also stops adapters that were built but never started (still in
        INITIALIZING state) so they are not leaked on total failure.

        Returns the total number of cancellation requests drained during
        adapter cleanup so the caller can restore them for the outer
        cancellation path.

        CancelledError policy
        ---------------------
        Startup cleanup is *best-effort*: if adapter ``stop()`` raises
        ``CancelledError``, the error is logged and suppressed so the
        caller's ``_cleanup_core_resources()`` (pipeline runner + storage
        close) still runs and the original startup failure is preserved.
        This is **distinct** from the normal ``stop()`` path, which
        defers ``CancelledError`` and re-raises it after core cleanup so
        that an externally-cancelled shutdown still propagates the
        cancellation to the caller.
        """
        _total_drained: int = 0
        timeout = self.config.runtime.shutdown_timeout_seconds
        for adapter_id in reversed(self.started_adapter_ids):
            adapter = self.adapters.get(adapter_id)
            if adapter is None:
                continue
            transport = getattr(adapter, "platform", "unknown")
            self._set_adapter_state(adapter_id, AdapterState.STOPPING)
            try:
                outcome, _, _ = await self._stop_adapter_with_deadline(
                    adapter=adapter,
                    adapter_id=adapter_id,
                    transport=transport,
                    timeout=float(timeout),
                )
            except asyncio.CancelledError:
                # Best-effort cleanup: do not re-raise — the caller's
                # _cleanup_core_resources() must still run.  Drain the
                # cancellation state so subsequent adapter stops in the
                # loop actually get a chance to run.
                _total_drained += _drain_pending_cancellations()
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.debug(
                    "Cancelled while cleaning up adapter %s.%s during "
                    "failed startup (best-effort: suppressed)",
                    transport,
                    adapter_id,
                )
                continue
            if outcome == "stopped":
                self._set_adapter_state(adapter_id, AdapterState.STOPPED)
                _logger.info(
                    "Cleaned up adapter %s.%s during failed startup",
                    transport,
                    adapter_id,
                )
            elif outcome == "timeout":
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.error(
                    "Timeout cleaning up adapter %s.%s during failed startup after %.1fs",
                    transport,
                    adapter_id,
                    timeout,
                )
            else:  # outcome == "abandoned" or "error"
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.error(
                    "Adapter %s.%s did not stop cleanly during failed "
                    "startup (see earlier log for details)",
                    transport,
                    adapter_id,
                )

        # Also clean up adapters that were built but never started (still
        # in INITIALIZING state).  These are in self.adapters but not in
        # started_adapter_ids.  Skip adapters already in a terminal state
        # (FAILED, STOPPED) from earlier handling.
        started_set = set(self.started_adapter_ids)
        terminal = {AdapterState.FAILED, AdapterState.STOPPED}
        for adapter_id, adapter in self.adapters.items():
            if adapter_id in started_set:
                continue
            if self._adapter_states.get(adapter_id) in terminal:
                continue
            transport = getattr(adapter, "platform", "unknown")
            self._set_adapter_state(adapter_id, AdapterState.STOPPING)
            try:
                outcome, _, _ = await self._stop_adapter_with_deadline(
                    adapter=adapter,
                    adapter_id=adapter_id,
                    transport=transport,
                    timeout=float(timeout),
                )
            except asyncio.CancelledError:
                # Best-effort cleanup: do not re-raise — the caller's
                # _cleanup_core_resources() must still run.  Drain the
                # cancellation state so subsequent adapter stops in the
                # loop actually get a chance to run.
                _total_drained += _drain_pending_cancellations()
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.debug(
                    "Cancelled while cleaning up never-started adapter "
                    "%s.%s during failed startup (best-effort: suppressed)",
                    transport,
                    adapter_id,
                )
                continue
            if outcome == "stopped":
                self._set_adapter_state(adapter_id, AdapterState.STOPPED)
                _logger.info(
                    "Cleaned up never-started adapter %s.%s during failed startup",
                    transport,
                    adapter_id,
                )
            elif outcome == "timeout":
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.error(
                    "Timeout cleaning up never-started adapter %s.%s during failed startup after %.1fs",
                    transport,
                    adapter_id,
                    timeout,
                )
            else:  # outcome == "abandoned" or "error"
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.error(
                    "Never-started adapter %s.%s did not stop cleanly "
                    "during failed startup (see earlier log for details)",
                    transport,
                    adapter_id,
                )

        self.started_adapter_ids.clear()
        self.adapter_start_monotonic.clear()
        return _total_drained

    async def _start_failure_cleanup(self) -> int:
        """Run the full startup-failure cleanup sequence.

        Used by both the catastrophic ``Exception`` catch-all in ``start()``
        and the parallel ``CancelledError`` handler.  Ensures the same
        cleanup runs regardless of which exception type escapes the
        startup loop.

        Returns the total number of cancellation requests drained during
        adapter and core-resource cleanup so the ``CancelledError`` handler
        in ``start()`` can restore them.
        """
        drained = await self._cleanup_started_adapters()
        try:
            await self._cleanup_core_resources()
        except asyncio.CancelledError:
            # ``_cleanup_core_resources`` may defer a CancelledError from
            # retry_worker.stop(), restore its own drained cancellation
            # count, and then re-raise.  Drain those restored requests here
            # and fold them into our return value so ``start()`` reaches its
            # single restore path and raises the original startup
            # cancellation instead of a cleanup artifact.
            drained += _drain_pending_cancellations()
        self._set_state(RuntimeState.FAILED)
        return drained

    async def _cleanup_core_resources(self) -> None:
        """Stop pipeline runner and close storage during failed startup.

        CancelledError policy
        ---------------------
        If the retry worker's ``stop()`` raises ``CancelledError``, the
        cancellation is drained so that the pipeline runner and storage
        cleanup below still execute.  The deferred cancellation is then
        re-raised to the caller so the original cancellation propagates
        correctly.  This mirrors the Phase 1 pattern in ``MedreApp.stop()``.

        Other cleanup errors are logged and suppressed so the original
        startup failure remains the raised exception.
        """
        # Stop retry worker if it was started.  Defer CancelledError so
        # pipeline runner stop and storage close can still run.
        _cancelled: asyncio.CancelledError | None = None
        if self._retry_worker is not None:
            try:
                await self._retry_worker.stop()
                _logger.info("Retry worker stopped during startup cleanup")
            except asyncio.CancelledError as c_exc:
                _cancelled = c_exc
                _logger.debug("Cancelled while stopping retry worker (deferred)")
            except Exception as exc:
                _logger.error(
                    "Error stopping retry worker during startup cleanup: %s", exc
                )

        # If the retry worker stop raised CE, the task likely has a
        # pending cancellation request that would prevent the awaits
        # below from actually running.  Drain it.
        _cleared_cancels = 0
        if _cancelled is not None:
            _cleared_cancels = _drain_pending_cancellations()

        try:
            await self.pipeline_runner.stop()
            _logger.info("Pipeline runner stopped during startup cleanup")
        except Exception as exc:
            _logger.error(
                "Error stopping pipeline runner during startup cleanup: %s", exc
            )

        await self._cleanup_storage_safely()

        # Restore the deferred cancellation count and re-raise so the
        # caller's cancellation propagates correctly.
        if _cancelled is not None:
            self._set_state(RuntimeState.FAILED)
            if _cleared_cancels:
                current = asyncio.current_task()
                if current is not None:
                    for _ in range(_cleared_cancels):
                        current.cancel()
            assert _cancelled is not None  # for type checker
            raise _cancelled

    async def _cleanup_storage_safely(self) -> None:
        """Close storage during failed startup, suppressing errors."""
        if self.storage is not None:
            try:
                await self.storage.close()
                _logger.info("Storage closed during startup cleanup")
            except Exception as exc:
                _logger.error("Error closing storage during startup cleanup: %s", exc)

    def _ensure_dirs(self) -> None:
        """Create required runtime directories.

        Pure path resolution does NOT create directories (see medre.config.paths).
        This is where the runtime creates the directories it needs.
        """
        dirs_to_create = [
            self.paths.state_dir,
            self.paths.data_dir,
            self.paths.cache_dir,
            self.paths.log_dir,
        ]
        # SQLite parent directory (database_path.parent may not exist)
        dirs_to_create.append(self.paths.database_path.parent)

        # Per-adapter state roots and transport-specific subdirs.
        # Each enabled adapter gets {state}/adapters/{adapter_id}/.
        # Matrix adapters additionally get {state}/adapters/{adapter_id}/matrix/store.
        for transport, adapter_id, rtc in self.config.adapters.all_configs():
            if not rtc.enabled:
                continue
            adapter_root = self.paths.adapter_state_dir(adapter_id)
            dirs_to_create.append(adapter_root)
            if transport == "matrix":
                store = (
                    self.paths.adapter_transport_state_dir(adapter_id, "matrix")
                    / "store"
                )
                dirs_to_create.append(store)

        for d in dirs_to_create:
            d.mkdir(parents=True, exist_ok=True)

    def _make_publish_inbound(self) -> Any:
        """Return a publish_inbound callable wired to the pipeline runner.

        Wraps :meth:`PipelineRunner.handle_ingress` so that the return
        value (``list[DeliveryOutcome]``) is discarded, matching the
        ``Callable[[CanonicalEvent], Awaitable[None]]`` protocol expected
        by :class:`AdapterContext`.
        """

        runner = self.pipeline_runner

        async def _publish(event: Any) -> None:
            await runner.handle_ingress(event)

        return _publish
