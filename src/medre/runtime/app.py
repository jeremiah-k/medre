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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from medre.core.lifecycle.states import AdapterState, require_valid_transition
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.runtime.health import (
    AdapterLiveHealth,
    LiveHealthSnapshot,
    health_to_adapter_state,
    normalize_adapter_health,
    truncate_health_error,
)
from medre.core.runtime.supervision import (
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

if TYPE_CHECKING:
    from medre.config.model import RuntimeConfig
    from medre.config.paths import MedrePaths
    from medre.core.contracts.adapter import AdapterContract
    from medre.core.engine.pipeline import PipelineRunner
    from medre.core.events.bus import EventBus
    from medre.core.observability.metrics import Diagnostician
    from medre.core.planning.fallback_resolution import FallbackResolver
    from medre.core.planning.relation_resolution import RelationResolver
    from medre.core.rendering.renderer import RenderingPipeline
    from medre.core.routing.router import Router
    from medre.core.routing.stats import RouteStats
    from medre.core.storage.replay import ReplayEngine
    from medre.core.storage.sqlite import SQLiteStorage
    from medre.runtime.builder import AdapterBuildFailure
    from medre.runtime.capacity import CapacityController
    from medre.runtime.retry import RetryWorker, RetryWorkerState
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
    _failed_adapter_ids: list[str] = field(default_factory=list, init=False)
    _route_eligibility: RouteEligibility | None = field(default=None, init=False)
    _route_provenance: dict[str, str] = field(default_factory=dict, init=False)
    _registered_routes: tuple = field(default=(), init=False)  # tuple[Route, ...]
    _startup_readiness: RouteStartupReadiness | None = field(default=None, init=False)
    _event_buffer: EventBuffer | None = field(
        default=None, init=False
    )  # set in __post_init__
    _adapter_states: dict[str, AdapterState] = field(default_factory=dict, init=False)
    _live_health_state: LiveHealthSnapshot | None = field(default=None, init=False)
    _live_health_poll_count: int = field(default=0, init=False)

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
        once, builds per-adapter :class:`~medre.core.runtime.health.AdapterLiveHealth`
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
                        await adapter.stop(
                            timeout=float(self.config.runtime.shutdown_timeout_seconds)
                        )
                    except Exception as cleanup_exc:
                        _logger.debug(
                            "Error stopping adapter %s.%s after start failure: %s",
                            transport,
                            adapter_id,
                            cleanup_exc,
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
        except Exception:
            # Catastrophic failure during the loop itself (not an adapter
            # failure).  Clean up already-started adapters in reverse order,
            # then clean up core resources (pipeline runner + storage).
            await self._cleanup_started_adapters()
            await self._cleanup_core_resources()
            self._set_state(RuntimeState.FAILED)
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

        # -- Summary logging --------------------------------------------------
        if failed_count > 0 or build_failed > 0:
            _logger.info(
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
            _logger.info(
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
        ``INITIALIZED`` or ``STOPPED`` state returns immediately.

        Raises
        ------
        RuntimeShutdownError
            If one or more subsystems fail to shut down cleanly.
        """
        if self._state in (RuntimeState.STOPPED, RuntimeState.INITIALIZED):
            return

        self._set_state(RuntimeState.STOPPING)
        timeout = self.config.runtime.shutdown_timeout_seconds
        _logger.info(
            "Stopping MEDRE runtime %s (timeout=%ds)",
            self.config.runtime.name,
            timeout,
        )

        # Phase 1: Stop accepting new work.
        if self._capacity_controller is not None:
            self._capacity_controller.stop_accepting()
        if self._replay_engine is not None:
            _logger.info("Runtime stopping — replay engine present, capacity stopped")

        # Stop the retry worker before draining work.
        if self._retry_worker is not None:
            try:
                await self._retry_worker.stop()
            except Exception as exc:
                _logger.error("Error stopping retry worker: %s", exc)

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

        # Signal shutdown to adapters and waiters.
        self.shutdown_event.set()

        # 1. Stop adapters in reverse start order for clean teardown.
        errors: list[tuple[str, Exception]] = []
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
                await adapter.stop(timeout=float(timeout))
                _logger.info("Adapter %s.%s stopped", transport, adapter_id)
                self._set_adapter_state(adapter_id, AdapterState.STOPPED)
                self._emit_event(
                    RuntimeEventType.ADAPTER_STOPPED,
                    {"adapter_id": adapter_id},
                )
            except Exception as exc:
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.error(
                    "Error stopping adapter %s.%s: %s",
                    transport,
                    adapter_id,
                    exc,
                )
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
                await adapter.stop(timeout=float(timeout))
                self._set_adapter_state(adapter_id, AdapterState.STOPPED)
                _logger.info(
                    "Adapter %s.%s stopped (never started)", transport, adapter_id
                )
            except Exception as exc:
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.debug(
                    "Error stopping never-started adapter %s.%s: %s",
                    transport,
                    adapter_id,
                    exc,
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

    async def _cleanup_started_adapters(self) -> None:
        """Stop already-started adapters in reverse order during failed startup.

        Used for partial-startup cleanup: if an unrecoverable error occurs
        after some adapters have started, this ensures they are torn down
        cleanly rather than left in a half-started state.

        Also stops adapters that were built but never started (still in
        INITIALIZING state) so they are not leaked on total failure.
        """
        timeout = self.config.runtime.shutdown_timeout_seconds
        for adapter_id in reversed(self.started_adapter_ids):
            adapter = self.adapters.get(adapter_id)
            if adapter is None:
                continue
            transport = getattr(adapter, "platform", "unknown")
            self._set_adapter_state(adapter_id, AdapterState.STOPPING)
            try:
                await adapter.stop(timeout=float(timeout))
                self._set_adapter_state(adapter_id, AdapterState.STOPPED)
                _logger.info(
                    "Cleaned up adapter %s.%s during failed startup",
                    transport,
                    adapter_id,
                )
            except Exception as exc:
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.error(
                    "Error cleaning up adapter %s.%s during failed startup: %s",
                    transport,
                    adapter_id,
                    exc,
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
                await adapter.stop(timeout=float(timeout))
                self._set_adapter_state(adapter_id, AdapterState.STOPPED)
                _logger.info(
                    "Cleaned up never-started adapter %s.%s during failed startup",
                    transport,
                    adapter_id,
                )
            except Exception as exc:
                self._set_adapter_state(adapter_id, AdapterState.FAILED)
                _logger.error(
                    "Error cleaning up never-started adapter %s.%s during failed startup: %s",
                    transport,
                    adapter_id,
                    exc,
                )

        self.started_adapter_ids.clear()
        self.adapter_start_monotonic.clear()

    async def _cleanup_core_resources(self) -> None:
        """Stop pipeline runner and close storage during failed startup.

        Logs but suppresses individual cleanup errors so that the original
        startup failure remains the raised exception.
        """
        # Stop retry worker if it was started.
        if self._retry_worker is not None:
            try:
                await self._retry_worker.stop()
                _logger.info("Retry worker stopped during startup cleanup")
            except Exception as exc:
                _logger.error(
                    "Error stopping retry worker during startup cleanup: %s", exc
                )

        try:
            await self.pipeline_runner.stop()
            _logger.info("Pipeline runner stopped during startup cleanup")
        except Exception as exc:
            _logger.error(
                "Error stopping pipeline runner during startup cleanup: %s", exc
            )

        await self._cleanup_storage_safely()

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
