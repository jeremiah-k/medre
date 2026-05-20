"""Run CLI command: load config, build runtime, and run until interrupted."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from medre.config.env import apply_env_overrides
from medre.config.errors import ConfigError
from medre.config.loader import load_config
from medre.core.observability.sanitization import sanitize_for_log
from medre.observability import (
    format_duration_ms,
    shutdown_summary,
    startup_summary,
)

from .exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_STARTUP
from .smoke_commands import _setup_logging, _transport_for_adapter

logger = logging.getLogger("medre")

shutdown_requested: bool = False


def _print(line: str = "") -> None:
    """Print a console status line immediately, even when output is captured."""
    print(line, flush=True)


class _RunSignals:
    """Save and restore signal handlers for the run lifecycle."""

    def __init__(self) -> None:
        self._prev: dict[int, Any] = {}

    def install(self, handler: Any) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._prev[sig] = signal.signal(sig, handler)

    def restore(self) -> None:
        for sig, handler in self._prev.items():
            signal.signal(sig, handler)
        self._prev.clear()


def _request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested  # noqa: PLW0603
    shutdown_requested = True
    logger.info("Received signal %s — requesting shutdown", signal.Signals(signum).name)


async def _start_interruptibly(app: Any) -> bool:
    """Start *app*, allowing SIGINT/SIGTERM to cancel startup cleanly."""
    start_task = asyncio.create_task(app.start())
    try:
        while not start_task.done():
            done, _pending = await asyncio.wait({start_task}, timeout=0.1)
            if done:
                break
            if shutdown_requested:
                logger.info("Shutdown requested during startup — cancelling startup")
                start_task.cancel()
                try:
                    await start_task
                except asyncio.CancelledError:
                    pass
                try:
                    await app.stop()
                except Exception as exc:
                    logger.error("Error stopping after interrupted startup: %s", exc)
                return False
        await start_task
        return True
    except asyncio.CancelledError:
        start_task.cancel()
        raise


async def _run(config_path: str | None, snapshot_path: str | None = None) -> None:
    """Load config, build the runtime, and run until interrupted."""
    global shutdown_requested  # noqa: PLW0603
    shutdown_requested = False

    from medre.runtime.builder import RuntimeBuilder

    try:
        config, source, paths = load_config(config_path)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)
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
            flush=True,
        )
        sys.exit(EXIT_CONFIG)

    # Configure logging from the loaded config
    _setup_logging(config)

    adapter_ids = [aid for aid, _rtc in enabled_adapters]
    logger.debug("Config source: %s", source.value)
    logger.debug("Config path: %s", paths.config_file)
    logger.debug("State dir:   %s", paths.state_dir)

    # Print one concise console header; detailed construction/start timing
    # belongs in the logger and the post-start summary below.
    _print(
        f"MEDRE starting ({config.runtime.name}) with "
        f"{len(enabled_adapters)} adapter(s): {', '.join(adapter_ids)}"
    )
    _print(f"  Config:  {paths.config_file}")
    _print(f"  State:   {paths.state_dir}")

    # Show disabled adapters for visibility.
    all_cfgs = config.adapters.all_configs()
    disabled = [(t, aid) for t, aid, rtc in all_cfgs if not rtc.enabled]
    if disabled:
        _print(f"  Disabled adapters: {', '.join(f'{t}.{aid}' for t, aid in disabled)}")

    builder = RuntimeBuilder(config, paths)
    try:
        app = builder.build()
    except Exception as exc:
        print(f"Runtime build error: {exc}", file=sys.stderr, flush=True)
        sys.exit(EXIT_BUILD)

    # Report build failures before startup.
    if app.build_failures:
        _print(f"  Build failures ({len(app.build_failures)}):")
        for bf in app.build_failures:
            _print(f"    \u2717 {bf.transport}.{bf.adapter_id}: {bf.error}")

    # If ALL enabled adapters failed construction there is nothing to start.
    # Exit with EXIT_BUILD (3) — this is a build-phase failure, not startup.
    if not app.adapters:
        print(
            f"\nRuntime build error: all {len(app.build_failures)} enabled "
            "adapter(s) failed to construct",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(EXIT_BUILD)

    # Show route inventory.
    route_list = config.routes.routes if config.routes else []
    enabled_routes = [r for r in route_list if r.enabled]
    disabled_routes = [r for r in route_list if not r.enabled]
    if route_list:
        _print(
            f"  Routes: {len(enabled_routes)} enabled, "
            f"{len(disabled_routes)} disabled "
            f"({len(route_list)} total)"
        )

    # Show storage backend.
    storage_backend = config.storage.backend if config.storage else "none"
    _print(f"  Storage: {storage_backend}")

    # Show runtime limits.
    limits = config.limits
    _print(
        f"  Limits: max_inflight_deliveries={limits.max_inflight_deliveries}, "
        f"max_inflight_replay_events={limits.max_inflight_replay_events}, "
        f"drain_timeout={limits.shutdown_drain_timeout_seconds}s"
    )

    # Install signal handlers for clean shutdown
    signals = _RunSignals()
    signals.install(_request_shutdown)

    # Track per-adapter startup timing
    run_start = time.monotonic()
    startup_results: list[tuple[str, str, bool, float, str | None]] = []

    try:
        try:
            started = await _start_interruptibly(app)
            if not started:
                _print("Runtime startup interrupted")
                return
        except Exception as exc:
            # RuntimeStartupError means total failure (zero adapters).
            # Print what we know and exit with a startup-specific code.
            from medre.runtime.errors import RuntimeStartupError

            # Report per-adapter results from app state.
            for adapter_id in app.started_adapter_ids:
                dur = time.monotonic() - run_start
                startup_results.append(
                    (
                        adapter_id,
                        _transport_for_adapter(adapter_id, config),
                        True,
                        dur,
                        None,
                    )
                )
            for adapter_id in app._failed_adapter_ids:
                dur = time.monotonic() - run_start
                startup_results.append(
                    (
                        adapter_id,
                        _transport_for_adapter(adapter_id, config),
                        False,
                        dur,
                        str(exc),
                    )
                )

            summary = startup_summary(startup_results)
            _print(summary)
            if isinstance(exc, RuntimeStartupError):
                _print(f"\nRuntime startup failed: {exc}")
                sys.exit(EXIT_STARTUP)
            # Unexpected exception during startup (core subsystem failure).
            print(f"\nRuntime startup failed: {exc}", file=sys.stderr, flush=True)
            sys.exit(EXIT_STARTUP)

        # Build startup results from app state (supports partial startup).
        for adapter_id in app.started_adapter_ids:
            dur = (
                getattr(app, "adapter_start_duration_ms", {}).get(
                    adapter_id,
                    (time.monotonic() - run_start) * 1000,
                )
                / 1000.0
            )
            startup_results.append(
                (
                    adapter_id,
                    _transport_for_adapter(adapter_id, config),
                    True,
                    dur,
                    None,
                )
            )
        for adapter_id in app._failed_adapter_ids:
            dur = time.monotonic() - run_start
            startup_results.append(
                (
                    adapter_id,
                    _transport_for_adapter(adapter_id, config),
                    False,
                    dur,
                    "failed during startup",
                )
            )

        # Print startup summary
        summary = startup_summary(startup_results)
        _print(summary)

        # Route eligibility summary (build-time classification).
        route_elig = getattr(app, "route_eligibility", None)
        if route_elig is not None:
            n_registered = len(route_elig.registered)
            n_degraded = len(route_elig.degraded)
            n_skipped = len(route_elig.skipped)
            parts = [f"{n_registered} enabled"]
            if n_degraded:
                parts.append(f"{n_degraded} degraded")
            if n_skipped:
                parts.append(f"{n_skipped} skipped")
            _print(f"  Route eligibility: {', '.join(parts)}")

        _print("  Run `medre diagnostics --refresh-health` for live adapter health")

        # Print boot summary diagnostics if available.
        if app.boot_summary is not None:
            bs = app.boot_summary
            if bs.runtime_health == "degraded":
                _print(
                    f"  \u26a0 Runtime is DEGRADED: {bs.adapters_started}/{bs.adapters_total} adapter(s) started"
                )
                if bs.failed_adapter_ids:
                    _print(f"    Failed adapters: {', '.join(bs.failed_adapter_ids)}")
            if bs.persisted_events_count is not None:
                _print(f"  Persisted events: {bs.persisted_events_count}")
            if bs.adapters_disabled > 0:
                _print(f"  Disabled adapters: {bs.adapters_disabled}")

        # Log structured startup (stdout summary already printed above)
        logger.debug(
            "Runtime started — %d adapter(s) in %s",
            len(app.started_adapter_ids),
            format_duration_ms(run_start),
            extra=sanitize_for_log({"adapter_count": len(app.started_adapter_ids)}),
        )

        try:
            while not shutdown_requested:
                try:
                    await asyncio.wait_for(app.wait_for_shutdown(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass  # expected — poll loop
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down")
        finally:
            # Shutdown
            _print("Runtime shutting down")
            logger.info("Runtime shutdown requested")

            # Capture final accounting counters before stop.
            final_accounting: dict[str, int] | None = None
            accounting_obj = getattr(app, "_runtime_accounting", None)
            if accounting_obj is not None and hasattr(accounting_obj, "snapshot"):
                try:
                    final_accounting = accounting_obj.snapshot()
                except Exception:
                    pass  # best-effort

            limits = config.limits
            drain_timeout = limits.shutdown_drain_timeout_seconds

            shutdown_errors: list[tuple[str, str]] = []
            drain_outcome = "completed"
            abandoned_count = 0
            try:
                await app.stop()
            except Exception as exc:
                logger.error("Shutdown error: %s", exc)
                shutdown_errors.append(("runtime", str(exc)))
                drain_outcome = "timed_out"
                # Count adapters that were still running as abandoned work.
                abandoned_count = len(getattr(app, "started_adapter_ids", []))

            # Print drain outcome once. Adapter stop details are already
            # logged by the runtime in deterministic order.
            if drain_outcome == "completed":
                _print(f"  drain completed (timeout={drain_timeout}s)")
            else:
                _print(
                    f"  drain timed out after {drain_timeout}s "
                    f"({abandoned_count} adapter(s) abandoned)"
                )

            summary = shutdown_summary(
                list(app.adapters.keys()), shutdown_errors or None
            )
            _print(summary)

            # Print final accounting counters.
            if final_accounting is not None:
                _print(
                    f"  Accounting: inbound={final_accounting.get('inbound_accepted', 0)} "
                    f"outbound_delivered={final_accounting.get('outbound_delivered', 0)} "
                    f"outbound_failed={final_accounting.get('outbound_failed', 0)} "
                    f"loop_prevented={final_accounting.get('loop_prevented', 0)} "
                    f"capacity_rejections={final_accounting.get('capacity_rejections', 0)}"
                )

            # Write final snapshot if requested.
            if snapshot_path is not None:
                try:
                    from medre.runtime.snapshot import build_runtime_snapshot

                    snap = build_runtime_snapshot(app)
                    snap_path = Path(snapshot_path)
                    snap_path.parent.mkdir(parents=True, exist_ok=True)
                    snap_path.write_text(
                        json.dumps(snap, indent=2, sort_keys=True) + "\n"
                    )
                    _print(f"  Final snapshot written to: {snapshot_path}")
                except Exception as exc:
                    print(
                        f"  Warning: failed to write snapshot: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )

            logger.info("MEDRE stopped")
    finally:
        signals.restore()
