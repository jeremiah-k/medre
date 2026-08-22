"""Diagnostics CLI commands: static snapshot, live health refresh, and bundle."""

from __future__ import annotations

import logging
import sys
import zipfile
from datetime import datetime, timezone

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config

from .exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_STARTUP
from .json import to_json

logger = logging.getLogger("medre")


def _metrics_projection(snapshot: dict, runtime_diagnostics: dict) -> dict:
    """Return the bounded, identifier-free subset exported as metrics."""
    accounting = snapshot.get("accounting") or {}
    capacity = snapshot.get("capacity") or {}
    health = snapshot.get("health") or {}
    live_health = health.get("live_health") or {}
    outbox = snapshot.get("outbox") or {}
    replay = snapshot.get("replay") or {}
    replay_counters = replay.get("counters") or {}
    routes = snapshot.get("routes") or {}
    route_stats = routes.get("stats") or {}
    retry = snapshot.get("retry") or {}
    lifecycle = snapshot.get("lifecycle") or {}
    return {
        "schema_version": snapshot.get("schema_version"),
        "accounting": {"counters": accounting.get("counters")},
        "capacity": {"state": capacity.get("state")},
        "health": {"adapter_summary": live_health.get("adapter_summary")},
        "lifecycle": {"uptime_seconds": lifecycle.get("uptime_seconds")},
        "limits": snapshot.get("limits"),
        "outbox": {"counts": outbox.get("counts")},
        "replay": {
            "available": replay.get("available"),
            "counters": {"global": replay_counters.get("global")},
        },
        "retry": retry,
        "routes": {"live_refresh": route_stats.get("live_refresh")},
        "runtime_diagnostics": runtime_diagnostics,
    }


def _diagnostics(config_path: str | None, *, output_format: str = "json") -> None:
    """Print runtime snapshot JSON using local config/process construction only.

    This command builds the runtime from configuration but does **not** start
    adapters, storage, or any I/O.  It produces a pre-flight snapshot showing
    what the runtime *would* look like: adapter inventory, route topology,
    limits, and config state.  No server, socket, or API is involved.
    """
    from medre.runtime.snapshot import build_runtime_snapshot

    try:
        config, source, paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    config = apply_env_overrides(config, paths)

    # Check for enabled adapters *before* building runtime.
    enabled_adapters = config.adapters.all_enabled()
    if not enabled_adapters:
        print(
            "Error: no adapters enabled. Set at least one adapter enabled = true in config.",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    from medre.runtime.builder import RuntimeBuilder

    builder = RuntimeBuilder(config, paths)
    try:
        app = builder.build()
    except Exception as exc:
        print(f"Runtime build error: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)

    # All enabled adapters failed construction — nothing to snapshot.
    if not app.adapters:
        print(
            f"Runtime build error: all {len(app.build_failures)} enabled "
            "adapter(s) failed to construct",
            file=sys.stderr,
        )
        _teardown_unstarted_app(app)
        sys.exit(EXIT_BUILD)

    # Use fixed timestamps for deterministic output.
    fixed_now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    fixed_mono = 0.0

    try:
        snapshot = build_runtime_snapshot(
            app,
            now_fn=lambda: fixed_now,
            monotonic_fn=lambda: fixed_mono,
            snapshot_scope="build",
        )

        if output_format == "prometheus":
            from medre.core.observability.export import snapshot_to_prometheus

            metrics_snapshot = _metrics_projection(snapshot, app.diagnostic_snapshot())
            print(snapshot_to_prometheus(metrics_snapshot), end="")
        else:
            print(to_json(snapshot))
    finally:
        # Diagnostic builds the runtime but never starts it.  The
        # runtime holds open storage handles, the pipeline runner has
        # attached middleware, the capacity controller and replay
        # engine are constructed but never run.  Tear them down
        # deterministically so process exit doesn't race with pending
        # storage executor / asyncio tasks.
        _teardown_unstarted_app(app)


def _teardown_unstarted_app(app: object) -> None:
    """Best-effort cleanup for a built-but-not-started ``MedreApp``.

    The diagnostics CLI builds the runtime purely to introspect its
    snapshot; it does **not** call :meth:`MedreApp.start`.  The
    subsystems constructed during :meth:`RuntimeBuilder.build` still
    hold storage handles, asyncio primitives, and event-bus middleware
    that should be released before the CLI process exits.  Each call
    is guarded so a failure in one subsystem does not leak the others.
    """
    import asyncio

    capacity = getattr(app, "_capacity_controller", None)
    if capacity is not None and hasattr(capacity, "stop_accepting"):
        try:
            capacity.stop_accepting()
        except Exception as exc:
            logger.debug("Diagnostics: error stopping capacity controller: %s", exc)

    replay = getattr(app, "_replay_engine", None)
    if replay is not None and hasattr(replay, "cancel"):
        try:
            replay.cancel()
        except Exception as exc:
            logger.debug("Diagnostics: error cancelling replay engine: %s", exc)

    pipeline = getattr(app, "pipeline_runner", None)
    if pipeline is not None:
        try:
            coro = pipeline.stop()
        except Exception as exc:
            logger.debug("Diagnostics: error scheduling pipeline stop: %s", exc)
            coro = None
        if coro is not None:
            try:
                asyncio.run(coro)
            except Exception as exc:
                logger.debug("Diagnostics: error stopping pipeline runner: %s", exc)

    cleanup_storage = getattr(app, "_cleanup_storage_safely", None)
    if cleanup_storage is not None:
        coro = cleanup_storage()
        try:
            asyncio.run(coro)
        except Exception as exc:
            logger.debug("Diagnostics: error closing storage: %s", exc)


async def _diagnostics_refresh(
    config_path: str | None, *, output_format: str = "json"
) -> None:
    """Start runtime, refresh adapter health once, print live snapshot JSON.

    Builds the runtime via the same :class:`RuntimeBuilder` path as
    :func:`_diagnostics`, then starts the runtime, calls
    :meth:`~medre.runtime.app.MedreApp.refresh_live_health`, prints a
    snapshot with ``health.live_health`` populated, and stops the runtime
    cleanly.

    The snapshot is built after the health refresh but **before**
    ``app.stop()``, so ``lifecycle.runtime_state`` reflects ``"running"``
    when printed.  ``app.stop()`` is called in a ``finally`` block to
    ensure clean shutdown regardless of snapshot or print errors.

    Uses real timestamps (not fixed) so operators can see when the
    health refresh occurred.

    Exit codes mirror ``medre run`` semantics:
    ``EXIT_CONFIG`` (2), ``EXIT_BUILD`` (3), ``EXIT_STARTUP`` (4).
    Exits 0 on success regardless of runtime health classification
    (operators read the JSON).
    """
    try:
        config, source, paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    config = apply_env_overrides(config, paths)

    # Check for enabled adapters *before* building runtime.
    enabled_adapters = config.adapters.all_enabled()
    if not enabled_adapters:
        print(
            "Error: no adapters enabled. Set at least one adapter enabled = true in config.",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    from medre.runtime.builder import RuntimeBuilder

    builder = RuntimeBuilder(config, paths)
    try:
        app = builder.build()
    except Exception as exc:
        print(f"Runtime build error: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)

    # All enabled adapters failed construction — nothing to start.
    if not app.adapters:
        print(
            f"Runtime build error: all {len(app.build_failures)} enabled "
            "adapter(s) failed to construct",
            file=sys.stderr,
        )
        _teardown_unstarted_app(app)
        sys.exit(EXIT_BUILD)

    # Start the runtime.  On failure, start() cleans up core resources
    # internally (callers do NOT need to call stop() after start() raises
    # RuntimeStartupError).
    try:
        await app.start()
    except Exception as exc:
        from medre.runtime.errors import RuntimeStartupError

        if isinstance(exc, RuntimeStartupError):
            print(f"\nRuntime startup failed: {exc}", file=sys.stderr)
        else:
            print(f"\nRuntime startup failed: {exc}", file=sys.stderr)
        sys.exit(EXIT_STARTUP)

    try:
        # Refresh live health — refreshes each adapter's health_check() once.
        await app.refresh_live_health()

        # Refresh outbox counts from storage before snapshot.
        await app.refresh_outbox_state_from_storage()

        # Build snapshot with REAL timestamps (not fixed).
        from medre.runtime.snapshot import build_runtime_snapshot

        snapshot = build_runtime_snapshot(app, snapshot_scope="live")
        if output_format == "prometheus":
            from medre.core.observability.export import snapshot_to_prometheus

            metrics_snapshot = _metrics_projection(snapshot, app.diagnostic_snapshot())
            print(snapshot_to_prometheus(metrics_snapshot), end="")
        else:
            print(to_json(snapshot))
    finally:
        # Always attempt clean shutdown after a successful start.
        try:
            await app.stop()
        except Exception as exc:
            logger.warning("Error during diagnostics shutdown: %s", exc)


def _support_bundle(config_path: str | None, output_path: str | None) -> None:
    """Write a redacted, offline support bundle ZIP.

    Delegates to :func:`medre.runtime.support_bundle.create_support_bundle`.
    The bundle loads config, builds a route plan, and redacts every
    secret-named field; it never starts adapters or performs network /
    hardware I/O.

    Exit codes: ``0`` when the ZIP was written (including the partial
    case where config load failed but the bundle still contains
    manifest / environment / config_check / config_source). Non-zero
    only if the ZIP itself could not be written or an unexpected error
    escaped the collector.
    """
    from medre.core.observability.sanitization import sanitize_error
    from medre.runtime.support_bundle import create_support_bundle

    try:
        written = create_support_bundle(config_path, output_path)
    except Exception as exc:
        print(f"Support bundle error: {sanitize_error(str(exc))}", file=sys.stderr)
        sys.exit(EXIT_BUILD)

    try:
        with zipfile.ZipFile(written, "r") as zf:
            member_count = len(zf.namelist())
    except Exception:
        member_count = 0

    print(
        f"Support bundle written to {written}. "
        f"{member_count} files, secrets redacted."
    )
