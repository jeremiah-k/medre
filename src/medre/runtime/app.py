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

__all__ = ["MedreApp"]

_logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(timezone.utc)


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

    # -- Lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Start all subsystems in dependency order.

        Order: storage → pipeline runner → adapters.

        Raises
        ------
        RuntimeStartupError
            If a core subsystem fails to start.
        AdapterStartupError
            If an individual adapter fails to start.
        """
        _logger.info("Starting MEDRE runtime %s", self.config.runtime.name)

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

        # 3. Start each adapter with a properly wired AdapterContext.
        for adapter_id, adapter in self.adapters.items():
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
                _logger.info("Adapter %r started", adapter_id)
            except Exception as exc:
                raise AdapterStartupError(adapter_id, str(exc)) from exc

        _logger.info(
            "MEDRE runtime %s started (%d adapter(s))",
            self.config.runtime.name,
            len(self.adapters),
        )

    async def stop(self) -> None:
        """Stop all subsystems in reverse dependency order.

        Order: adapters → pipeline runner → storage.

        Errors in individual subsystems are logged but do not prevent
        other subsystems from shutting down.

        Raises
        ------
        RuntimeShutdownError
            If a core subsystem fails to shut down.
        """
        timeout = self.config.runtime.shutdown_timeout_seconds
        _logger.info(
            "Stopping MEDRE runtime %s (timeout=%ds)",
            self.config.runtime.name,
            timeout,
        )

        # Signal shutdown to adapters and waiters.
        self.shutdown_event.set()

        # 1. Stop adapters (reverse order for clean teardown).
        errors: list[tuple[str, Exception]] = []
        for adapter_id in reversed(list(self.adapters.keys())):
            adapter = self.adapters[adapter_id]
            try:
                await adapter.stop(timeout=float(timeout))
                _logger.info("Adapter %r stopped", adapter_id)
            except Exception as exc:
                _logger.error("Error stopping adapter %r: %s", adapter_id, exc)
                errors.append((adapter_id, exc))

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

        _logger.info("MEDRE runtime %s stopped", self.config.runtime.name)

    async def wait_for_shutdown(self) -> None:
        """Wait until the shutdown signal is set."""
        await self.shutdown_event.wait()

    # -- Helpers -----------------------------------------------------------------

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
