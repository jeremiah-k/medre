"""Real Matrix Operation Alpha runner.

Wires environment-derived configuration into the full MEDRE pipeline:

``MatrixConfig → MatrixAdapter → EventBus → RenderingPipeline → Storage → PipelineRunner``

and runs the lifecycle with signal handling for clean shutdown.

The runner exposes operational state via
:func:`~medre.core.runtime.health.normalize_adapter_health` by extracting
live diagnostics from the :class:`~medre.adapters.matrix.adapter.MatrixAdapter`
and passing them as the ``details`` dict.  This keeps :class:`AdapterInfo`
clean while allowing external monitors to see real operational state:

* ``connected`` — ``True`` when the nio client is instantiated.
* ``logged_in`` — ``True`` when the client reports authenticated state.
* ``sync_task_running`` — ``True`` when the sync task exists and is not done.
* ``last_sync_error`` — string from the last sync failure, or ``None``.

Usage::

    python -m medre.runner

Environment variables
---------------------
MATRIX_HOMESERVER
    Required.  Homeserver URL (``https://…``).
MATRIX_USER_ID
    Required.  Fully-qualified Matrix user ID (``@user:domain``).
MATRIX_ACCESS_TOKEN
    Required.  Access token for authentication.
MATRIX_ROOM_ALLOWLIST
    Optional.  Comma-separated room IDs.  Empty or unset means all rooms.
MATRIX_ADAPTER_ID
    Optional.  Adapter identifier (default ``"matrix-alpha"``).
MATRIX_DEVICE_ID
    Optional.  Device ID for the client session.
MATRIX_STORE_PATH
    Optional.  Filesystem path for nio store directory.
MATRIX_SYNC_TIMEOUT_MS
    Optional.  Sync long-poll timeout in ms (default ``30000``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any

from medre.adapters.base import AdapterContext, AdapterInfo
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.routing.router import Router
from medre.core.runtime.health import normalize_adapter_health
from medre.core.storage.sqlite import SQLiteStorage

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------


def _env(name: str, *, required: bool = True) -> str:
    """Read an environment variable, raising on missing required values."""
    value = os.environ.get(name, "")
    if required and not value:
        raise EnvironmentError(f"Required environment variable {name} is not set")
    return value


def _build_matrix_config() -> MatrixConfig:
    """Construct and validate a :class:`MatrixConfig` from environment."""
    room_allowlist_raw = _env("MATRIX_ROOM_ALLOWLIST", required=False)
    room_allowlist: set[str] | None = None
    if room_allowlist_raw.strip():
        room_allowlist = {
            rid.strip()
            for rid in room_allowlist_raw.split(",")
            if rid.strip()
        }

    sync_timeout_raw = _env("MATRIX_SYNC_TIMEOUT_MS", required=False)
    sync_timeout_ms = 30000
    if sync_timeout_raw.strip():
        sync_timeout_ms = int(sync_timeout_raw)

    config = MatrixConfig(
        adapter_id=_env("MATRIX_ADAPTER_ID", required=False) or "matrix-alpha",
        homeserver=_env("MATRIX_HOMESERVER"),
        user_id=_env("MATRIX_USER_ID"),
        access_token=_env("MATRIX_ACCESS_TOKEN"),
        device_id=_env("MATRIX_DEVICE_ID", required=False) or None,
        room_allowlist=room_allowlist,
        store_path=_env("MATRIX_STORE_PATH", required=False) or None,
        sync_timeout_ms=sync_timeout_ms,
    )
    config.validate()
    return config


# ---------------------------------------------------------------------------
# Operational diagnostics
# ---------------------------------------------------------------------------


def _extract_operational_details(adapter: MatrixAdapter) -> dict[str, Any]:
    """Extract live operational state from a MatrixAdapter for diagnostics.

    Returns a JSON-safe dict with the following keys:

    * ``connected`` — ``True`` when ``_client`` is not ``None``.
    * ``logged_in`` — ``True`` when the client reports ``logged_in``.
    * ``sync_task_running`` — ``True`` when ``_sync_task`` exists and is not done.
    * ``last_sync_error`` — string from ``_sync_failure``, or ``None``.

    No tokens or secrets are included.
    """
    connected: bool = adapter._client is not None
    logged_in: bool = (
        getattr(adapter._client, "logged_in", False) if connected else False
    )
    sync_task_running: bool = (
        adapter._sync_task is not None and not adapter._sync_task.done()
    )
    last_sync_error: str | None = (
        str(adapter._sync_failure) if adapter._sync_failure is not None else None
    )
    return {
        "connected": connected,
        "logged_in": logged_in,
        "sync_task_running": sync_task_running,
        "last_sync_error": last_sync_error,
    }


def collect_diagnostics(
    adapter: MatrixAdapter,
    info: AdapterInfo | None = None,
) -> dict[str, Any]:
    """Collect a full diagnostic snapshot from the adapter.

    Combines :meth:`~MatrixAdapter.health_check` output with live
    operational details extracted from the adapter's internal state via
    :func:`_extract_operational_details`.

    Parameters
    ----------
    adapter:
        The running MatrixAdapter instance.
    info:
        Optional pre-fetched :class:`AdapterInfo`.  When ``None``,
        ``await adapter.health_check()`` should be called first and
        the result passed here.

    Returns
    -------
    dict[str, Any]
        Normalized health dict from :func:`normalize_adapter_health`
        with operational details merged under the ``"details"`` key.
    """
    if info is None:
        raise TypeError(
            "collect_diagnostics requires an AdapterInfo; "
            "call await adapter.health_check() first and pass the result"
        )
    details = _extract_operational_details(adapter)
    return normalize_adapter_health(
        info,
        adapter=adapter,
        details=details,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_alpha_matrix() -> None:
    """Wire and run the full Matrix Operation Alpha pipeline.

    Reads configuration from environment variables, creates all
    subsystems, starts the adapter and pipeline, waits for a shutdown
    signal (SIGINT / SIGTERM), then stops everything cleanly.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        stream=sys.stderr,
    )

    config = _build_matrix_config()
    _logger.info("Matrix Operation Alpha: config loaded for %s", config.user_id)

    # -- Subsystems --------------------------------------------------------
    event_bus = EventBus()
    rendering_pipeline = RenderingPipeline()

    # Register the Matrix-specific renderer.
    rendering_pipeline.register(MatrixRenderer(), priority=10)

    # Storage: SQLite in a temp file or configurable path.
    db_path = os.environ.get("MEDRE_DB_PATH", ":memory:")
    storage = SQLiteStorage(db_path)
    await storage.initialize()

    diagnostician = Diagnostician()
    router = Router()

    adapter = MatrixAdapter(config)

    pipeline_config = PipelineConfig(
        storage=storage,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={config.adapter_id: adapter},
        event_bus=event_bus,
        rendering_pipeline=rendering_pipeline,
        diagnostician=diagnostician,
    )

    pipeline_runner = PipelineRunner(pipeline_config)

    # -- Build AdapterContext ----------------------------------------------
    # AdapterContext.publish_inbound expects Awaitable[None], but
    # PipelineRunner.ingress_handler returns list[DeliveryOutcome].
    # Wrap to satisfy the type contract.
    async def _publish_inbound(event: Any) -> None:
        await pipeline_runner.ingress_handler(event)

    shutdown_event = asyncio.Event()
    ctx = AdapterContext(
        adapter_id=config.adapter_id,
        event_bus=event_bus,
        publish_inbound=_publish_inbound,
        logger=logging.getLogger(f"medre.adapter.{config.adapter_id}"),
        clock=_utc_now,
        shutdown_event=shutdown_event,
    )

    # -- Signal handling ---------------------------------------------------
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    # -- Start sequence ----------------------------------------------------
    await pipeline_runner.start()
    _logger.info("PipelineRunner started")

    await adapter.start(ctx)
    _logger.info("MatrixAdapter %s started", config.adapter_id)

    # -- Log initial diagnostics -------------------------------------------
    info = await adapter.health_check()
    diag = collect_diagnostics(adapter, info=info)
    _logger.info("Initial diagnostics: %s", diag)

    # -- Wait for shutdown -------------------------------------------------
    _logger.info("Matrix Operation Alpha running — awaiting shutdown signal")
    await shutdown_event.wait()

    # -- Stop sequence -----------------------------------------------------
    _logger.info("Shutdown requested — stopping")
    await adapter.stop()
    _logger.info("MatrixAdapter stopped")
    await pipeline_runner.stop()
    _logger.info("PipelineRunner stopped")

    try:
        await storage.close()
    except Exception:
        pass
    _logger.info("Matrix Operation Alpha shut down cleanly")


def _utc_now() -> Any:
    """Return current UTC datetime (isolated for testability)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run_alpha_matrix())
