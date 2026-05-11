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
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from medre.runtime.errors import AdapterStartupError, RuntimeShutdownError, RuntimeStartupError

if TYPE_CHECKING:
    from medre.adapters.base import AdapterContext, BaseAdapter
    from medre.config.model import RuntimeConfig
    from medre.config.paths import MedrePaths
    from medre.core.engine.pipeline import PipelineRunner
    from medre.core.events.bus import EventBus
    from medre.core.observability.metrics import Diagnostician
    from medre.core.planning.fallback_resolution import FallbackResolver
    from medre.core.planning.relation_resolution import RelationResolver
    from medre.core.rendering.renderer import RenderingPipeline
    from medre.core.routing.router import Router
    from medre.core.storage.sqlite import SQLiteStorage
    from medre.runtime.builder import AdapterBuildFailure

__all__ = ["MedreApp"]

_logger = logging.getLogger(__name__)


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
    adapter_start_times:
        Per-adapter monotonic start timestamps (populated during start).
    started_adapter_ids:
        Ordered list of adapter IDs that successfully started.
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
    adapters: dict[str, BaseAdapter]
    shutdown_event: asyncio.Event
    build_failures: list[AdapterBuildFailure] = field(default_factory=list)
    adapter_start_times: dict[str, float] = field(default_factory=dict)
    started_adapter_ids: list[str] = field(default_factory=list)

    # -- Lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Start all subsystems in dependency order.

        Order: storage → pipeline runner → adapters.

        Adapters are started in deterministic order (sorted by adapter_id).
        Individual adapter start failures are logged with adapter_id
        attribution but do **not** abort the remaining adapters.  On
        catastrophic core subsystem failure, any already-started adapters
        are stopped in reverse order.

        Raises
        ------
        RuntimeStartupError
            If a core subsystem (storage, pipeline runner) fails to start.
        AdapterStartupError
            If an individual adapter fails to start.  Only the *first*
            adapter error is re-raised; all others are logged.
        """
        _logger.info("Starting MEDRE runtime %s", self.config.runtime.name)

        # 0. Create required directories.
        self._ensure_dirs()

        # 1. Initialise storage.
        if self.storage is not None:
            try:
                await self.storage.initialize()
                _logger.info("Storage initialised")
            except Exception as exc:
                raise RuntimeStartupError(
                    f"Failed to initialise storage: {exc}"
                ) from exc

        # 2. Start the pipeline runner.
        try:
            await self.pipeline_runner.start()
            _logger.info("Pipeline runner started")
        except Exception as exc:
            raise RuntimeStartupError(
                f"Failed to start pipeline runner: {exc}"
            ) from exc

        # 3. Start each adapter in deterministic order.
        #    Sort by adapter_id for reproducible startup sequence.
        adapter_ids = sorted(self.adapters.keys())
        total = len(adapter_ids)

        _logger.info(
            "Starting %d adapter(s): %s",
            total,
            ", ".join(adapter_ids) if adapter_ids else "(none)",
        )

        first_error: AdapterStartupError | None = None
        try:
            for adapter_id in adapter_ids:
                adapter = self.adapters[adapter_id]
                transport = getattr(adapter, "platform", "unknown")
                t0 = _monotonic_ms()
                try:
                    from medre.adapters.base import AdapterContext

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
                    self.adapter_start_times[adapter_id] = t0
                    self.started_adapter_ids.append(adapter_id)
                    _logger.info(
                        "Adapter %s.%s started in %.0fms",
                        transport,
                        adapter_id,
                        elapsed,
                    )
                except Exception as exc:
                    elapsed = _monotonic_ms() - t0
                    error = AdapterStartupError(
                        adapter_id,
                        f"failed after {elapsed:.0f}ms: {exc}",
                    )
                    _logger.error(
                        "Adapter %s.%s failed to start (%.0fms): %s",
                        transport,
                        adapter_id,
                        elapsed,
                        exc,
                    )
                    if first_error is None:
                        first_error = error
        except Exception:
            # Catastrophic failure during the loop itself (not an adapter
            # failure).  Clean up already-started adapters in reverse order.
            await self._cleanup_started_adapters()
            raise

        # Summary logging.
        started_count = len(self.started_adapter_ids)
        failed_count = total - started_count
        build_failed = len(self.build_failures)
        if failed_count > 0 or build_failed > 0:
            _logger.info(
                "Runtime started with %d/%d adapter(s)%s (%d start failed, %d build failed)",
                started_count,
                total,
                f" — {', '.join(self.started_adapter_ids)}" if self.started_adapter_ids else "",
                failed_count,
                build_failed,
            )
        else:
            _logger.info(
                "Runtime started with %d/%d adapter(s)",
                started_count,
                total,
            )

        if first_error is not None:
            raise first_error

    async def stop(self) -> None:
        """Stop all subsystems in reverse dependency order.

        Order: adapters → pipeline runner → storage.

        Adapters are stopped in reverse start order.  Individual stop
        failures are logged but do not prevent other subsystems from
        shutting down.

        Raises
        ------
        RuntimeShutdownError
            If one or more subsystems fail to shut down cleanly.
        """
        timeout = self.config.runtime.shutdown_timeout_seconds
        _logger.info(
            "Stopping MEDRE runtime %s (timeout=%ds)",
            self.config.runtime.name,
            timeout,
        )

        # Signal shutdown to adapters and waiters.
        self.shutdown_event.set()

        # 1. Stop adapters in reverse start order for clean teardown.
        errors: list[tuple[str, Exception]] = []
        for adapter_id in reversed(self.started_adapter_ids):
            adapter = self.adapters.get(adapter_id)
            if adapter is None:
                continue
            transport = getattr(adapter, "platform", "unknown")
            _logger.debug("Adapter %s.%s stopping", transport, adapter_id)
            try:
                await adapter.stop(timeout=float(timeout))
                _logger.info(
                    "Adapter %s.%s stopped", transport, adapter_id
                )
            except Exception as exc:
                _logger.error(
                    "Error stopping adapter %s.%s: %s",
                    transport,
                    adapter_id,
                    exc,
                )
                errors.append((adapter_id, exc))

        # Also stop any adapters that were in self.adapters but not in
        # started_adapter_ids (e.g. if start() was never called).
        for adapter_id, adapter in self.adapters.items():
            if adapter_id in self.started_adapter_ids:
                continue
            transport = getattr(adapter, "platform", "unknown")
            _logger.debug(
                "Adapter %s.%s stopping (never started)", transport, adapter_id
            )
            try:
                await adapter.stop(timeout=float(timeout))
            except Exception as exc:
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
            summary = "; ".join(
                f"{name}: {exc}" for name, exc in errors
            )
            raise RuntimeShutdownError(
                f"Errors during shutdown: {summary}"
            )

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
        """
        timeout = self.config.runtime.shutdown_timeout_seconds
        for adapter_id in reversed(self.started_adapter_ids):
            adapter = self.adapters.get(adapter_id)
            if adapter is None:
                continue
            transport = getattr(adapter, "platform", "unknown")
            try:
                await adapter.stop(timeout=float(timeout))
                _logger.info(
                    "Cleaned up adapter %s.%s during failed startup",
                    transport,
                    adapter_id,
                )
            except Exception as exc:
                _logger.error(
                    "Error cleaning up adapter %s.%s during failed startup: %s",
                    transport,
                    adapter_id,
                    exc,
                )
        self.started_adapter_ids.clear()
        self.adapter_start_times.clear()

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
                store = self.paths.adapter_transport_state_dir(adapter_id, "matrix") / "store"
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
