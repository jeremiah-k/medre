"""Run CLI command: load config, build runtime, and run until interrupted."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from medre.config.env import apply_env_overrides
from medre.config.errors import ConfigError
from medre.config.loader import load_config
from medre.core.observability.sanitization import sanitize_error, sanitize_for_log
from medre.runtime.summaries import (
    format_duration_ms,
    shutdown_summary,
    startup_route_summary,
    startup_summary,
)

from .exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_STARTUP
from .json import to_json
from .smoke_commands import _setup_logging, _transport_for_adapter

logger = logging.getLogger("medre")

shutdown_requested: bool = False

#: Signal-receipt evidence captured inside the raw handler. The handler must
#: not log or touch streams, so it only records counters and clocks; the run
#: loop reports them once it is back in coroutine context. This quantifies how
#: long the process took to react to the first request when operators report a
#: slow Ctrl+C response.
shutdown_signal_count: int = 0
first_shutdown_request_monotonic: float | None = None
first_shutdown_request_wall: str | None = None

#: Deliveries counted through the wakeup pipe. CPython's C-level signal
#: handler writes one byte per delivery into the ``signal.set_wakeup_fd``
#: pipe at delivery time, while the Python-level handler only runs when the
#: interpreter next services pending signals. Several presses landing inside
#: one un-serviced window (a C call holding the GIL, for example) otherwise
#: coalesce into a single handler invocation, and the receipt would report
#: one request for what were actually two or more presses.
shutdown_delivered_count: int = 0

#: Poll interval only used when a signal or app-side completion cannot wake the loop.
_SHUTDOWN_POLL_SECONDS: float = 1.0

#: Wakeup callback for the run loop, installed with the signal handlers.
#: Invoked at servicing time so teardown starts immediately instead of on
#: the next poll tick.
_shutdown_wake_callback: Callable[[], None] | None = None

#: Ends of the wakeup pipe; ``None`` when the pipe could not be installed.
_wakeup_read_fd: int | None = None
_wakeup_write_fd: int | None = None

#: True while the process-wide wakeup fd is the pipe this module installed.
_wakeup_fd_installed: bool = False


def _reset_shutdown_evidence() -> None:
    """Clear per-run signal evidence before installing handlers."""
    global shutdown_requested, shutdown_signal_count, shutdown_delivered_count
    global first_shutdown_request_monotonic, first_shutdown_request_wall
    shutdown_requested = False
    shutdown_signal_count = 0
    shutdown_delivered_count = 0
    first_shutdown_request_monotonic = None
    first_shutdown_request_wall = None


def _print(line: str = "", *, level: int = logging.INFO) -> None:
    """Emit lifecycle summaries through the configured timestamped logger."""
    logger.log(level, "%s", line.lstrip("\n"))


def _install_wakeup_pipe() -> None:
    """Open the wakeup pipe CPython writes one byte per signal delivery into.

    The write happens in CPython's async-signal-safe C handler, so deliveries
    are counted even when several arrive before the interpreter can run the
    Python-level handler. Failing to install the pipe (exhausted descriptors,
    platforms where ``set_wakeup_fd`` is unsupported) only downgrades the
    receipt to per-servicing counts.
    """
    global _wakeup_read_fd, _wakeup_write_fd, _wakeup_fd_installed
    _close_wakeup_pipe()
    read_fd: int | None = None
    write_fd: int | None = None
    try:
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        previous_fd = signal.set_wakeup_fd(write_fd)
        if previous_fd != -1:
            # A host (including another event-loop integration) already owns
            # signal wakeups. Restore it immediately; stealing the descriptor
            # would strand that owner until MEDRE exits. Fall back to Python
            # handler counts rather than claiming delivery-faithful evidence.
            signal.set_wakeup_fd(previous_fd)
            _forward_probe_wakeups(read_fd, previous_fd)
            for pipe_fd in (read_fd, write_fd):
                os.close(pipe_fd)
            return
    except (OSError, ValueError, NotImplementedError):
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        return
    _wakeup_read_fd = read_fd
    _wakeup_write_fd = write_fd
    _wakeup_fd_installed = True


def _forward_probe_wakeups(read_fd: int, host_fd: int) -> None:
    """Return bytes caught during the ownership probe to the restored host.

    Both descriptors are nonblocking wakeup fds. Capacity and descriptor errors
    retain the same best-effort boundary as CPython's wakeup writes.
    """
    try:
        while chunk := os.read(read_fd, 4096):
            while chunk:
                written = os.write(host_fd, chunk)
                if written <= 0:
                    return
                chunk = chunk[written:]
    except OSError:
        return


def _close_wakeup_pipe() -> None:
    """Detach and close the wakeup pipe if this process installed one."""
    global _wakeup_read_fd, _wakeup_write_fd, _wakeup_fd_installed
    if _wakeup_write_fd is not None:
        if _wakeup_fd_installed:
            # Detach before closing to avoid a signal writing to a closed
            # descriptor. If another integration replaced our descriptor
            # while the command ran, preserve its newer owner instead.
            current_fd = signal.set_wakeup_fd(-1)
            if current_fd >= 0 and current_fd != _wakeup_write_fd:
                signal.set_wakeup_fd(current_fd)
            _wakeup_fd_installed = False
        os.close(_wakeup_write_fd)
        _wakeup_write_fd = None
    if _wakeup_read_fd is not None:
        os.close(_wakeup_read_fd)
        _wakeup_read_fd = None


def _drain_wakeup_pipe() -> None:
    """Count signal deliveries buffered since the last servicing round.

    Each buffered byte was written by the C-level handler for one delivered
    signal; only SIGINT/SIGTERM bytes carry shutdown requests. This is called
    from run-loop context only, not from the Python signal handler, to avoid
    reentrant read-modify-write races under tracing.
    """
    global shutdown_delivered_count
    read_fd = _wakeup_read_fd
    if read_fd is None:
        return
    delivered = 0
    try:
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            delivered += sum(
                1 for byte in chunk if byte in (signal.SIGINT, signal.SIGTERM)
            )
    except OSError:
        # Empty pipe (BlockingIOError; every buffered delivery consumed), or
        # a concurrent close during teardown; whatever was read is already
        # counted.
        pass
    shutdown_delivered_count += delivered


def _wake_run_loop() -> None:
    """Wake the run loop so teardown starts at servicing time.

    ``loop.call_soon_threadsafe`` is safe from a Python-level signal handler:
    it only schedules a callback and writes the loop's self-pipe, so the
    loop's readiness wait returns instead of sleeping out the remainder of
    the poll timeout.
    """
    callback = _shutdown_wake_callback
    if callback is None:
        return
    try:
        callback()
    except RuntimeError:
        # A closed loop means teardown is already over; nothing to wake.
        pass


class _RunSignals:
    """Save and restore signal handlers for the run lifecycle."""

    def __init__(self, wake: Callable[[], None] | None = None) -> None:
        self._prev: dict[int, Any] = {}
        self._wake = wake

    def install(self, handler: Any) -> None:
        global _shutdown_wake_callback
        _install_wakeup_pipe()
        _shutdown_wake_callback = self._wake
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                self._prev[sig] = signal.signal(sig, handler)
        except BaseException:
            self.restore()
            raise

    def restore(self) -> None:
        global _shutdown_wake_callback
        _close_wakeup_pipe()
        _shutdown_wake_callback = None
        for sig, handler in self._prev.items():
            signal.signal(sig, handler)
        self._prev.clear()


def _request_shutdown(signum: int, _frame: object) -> None:
    """Record the request without re-entering logging or stream I/O."""
    global shutdown_requested, shutdown_signal_count
    global first_shutdown_request_monotonic, first_shutdown_request_wall
    shutdown_requested = True
    shutdown_signal_count += 1
    if first_shutdown_request_monotonic is None:
        first_shutdown_request_monotonic = time.monotonic()
        first_shutdown_request_wall = datetime.now(timezone.utc).isoformat()
    # Do not drain in the signal handler: a handler can interrupt another
    # Python operation (including a previous drain under opcode tracing).
    # The run loop drains the C-level delivery bytes once during teardown;
    # keeping one writer for the count prevents lost read-modify-writes.
    # Begin teardown at servicing time, not on the next poll tick.
    _wake_run_loop()


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
                    await asyncio.shield(start_task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                # start() owns rollback, including partially initialized core
                # resources. Calling stop() here would tear them down twice.
                return False
        await start_task
        return True
    except asyncio.CancelledError:
        # Startup owns its resources until rollback finishes. Repeated caller
        # cancellation must not detach that owner or interrupt its cleanup.
        start_task.cancel()
        while not start_task.done():
            try:
                await asyncio.shield(start_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                # Preserve caller cancellation even if startup fails during
                # rollback; awaiting the task has retrieved its exception.
                break
        if not start_task.cancelled():
            start_task.exception()
        raise


async def _wait_for_run_shutdown(app: Any, wake: asyncio.Event) -> None:
    """Own both waiters and surface app failures even beside a shutdown signal."""
    app_wait = asyncio.ensure_future(app.wait_for_shutdown())
    wake_wait = asyncio.create_task(wake.wait())
    try:
        while not shutdown_requested:
            await asyncio.wait(
                {app_wait, wake_wait},
                timeout=_SHUTDOWN_POLL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if app_wait.done():
                await app_wait  # Retrieve failures before honoring a signal wake.
                # A clean completion paces the next rearm on the poll cadence
                # instead of spinning on an app whose shutdown wait returns
                # without a shutdown request.
                if not shutdown_requested and not wake_wait.done():
                    await asyncio.wait({wake_wait}, timeout=_SHUTDOWN_POLL_SECONDS)
                if not shutdown_requested:
                    app_wait = asyncio.ensure_future(app.wait_for_shutdown())
            if wake_wait.done():
                wake.clear()
                wake_wait = asyncio.create_task(wake.wait())
        if app_wait.done() and not app_wait.cancelled():
            await app_wait
    finally:
        app_wait.cancel()
        wake_wait.cancel()
        await asyncio.gather(app_wait, wake_wait, return_exceptions=True)


async def _run(config_path: str | None, snapshot_path: str | None = None) -> None:
    """Load config, build the runtime, and run until interrupted."""
    _reset_shutdown_evidence()

    from medre.runtime.builder import RuntimeBuilder

    try:
        config, source, paths = load_config(config_path)
        # Env overrides participate in the same config-error boundary:
        # identifier-contract violations raised here must surface as an
        # actionable config error, not a traceback.
        config = apply_env_overrides(config, paths)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

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
        print(
            f"Runtime build error: {sanitize_error(str(exc))}",
            file=sys.stderr,
            flush=True,
        )
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
            f"({len(route_list)} configured)"
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

    # Install signal handlers for clean shutdown. The wake callback lets the
    # handler begin teardown at servicing time instead of waiting out the
    # poll tick; the wakeup pipe keeps the receipt faithful when several
    # presses coalesce inside one un-serviced window.
    loop = asyncio.get_running_loop()
    shutdown_wake: asyncio.Event = asyncio.Event()

    def _wake_run() -> None:
        loop.call_soon_threadsafe(shutdown_wake.set)

    signals = _RunSignals(wake=_wake_run)
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
                        sanitize_error(str(exc)),
                    )
                )

            summary = startup_summary(startup_results)
            _print(summary)
            _print(
                f"Runtime startup failed: {sanitize_error(str(exc))}",
                level=logging.ERROR,
            )
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

        # Print current readiness while preserving the historical boot record.
        live_health = app.live_health
        boot = app.boot_summary
        current_health = (
            live_health.runtime_health
            if live_health is not None
            else boot.runtime_health if boot is not None else "healthy"
        )
        summary = startup_summary(startup_results, ready=current_health == "healthy")
        _print(summary)

        route_elig = app.route_eligibility
        if route_elig is not None:
            _print(startup_route_summary(route_elig, app.startup_readiness))

        # Print boot summary diagnostics if available.
        if app.boot_summary is not None:
            bs = app.boot_summary
            if current_health in {"degraded", "failed"}:
                _print(
                    f"  \u26a0 Runtime health is {current_health.upper()}: {bs.adapters_started}/{bs.adapters_total} adapter(s) started",
                    level=logging.WARNING,
                )
                if bs.failed_adapter_ids:
                    _print(f"    Failed adapters: {', '.join(bs.failed_adapter_ids)}")
                degraded = [
                    name
                    for name, state in sorted(app.adapter_states.items())
                    if state.value == "degraded"
                ]
                if degraded:
                    _print(f"    Degraded adapters: {', '.join(degraded)}")
            if bs.persisted_events_count is not None:
                _print(f"  Persisted events: {bs.persisted_events_count}")
            if bs.adapters_disabled > 0:
                _print(f"  Disabled adapters: {bs.adapters_disabled}")
            if bs.storage_path is not None:
                _print(f"  Storage: {bs.storage_backend} ({bs.storage_path})")

        # Log structured startup (stdout summary already printed above)
        logger.debug(
            "Runtime started — %d adapter(s) in %s",
            len(app.started_adapter_ids),
            format_duration_ms(run_start),
            extra=sanitize_for_log({"adapter_count": len(app.started_adapter_ids)}),
        )

        try:
            await _wait_for_run_shutdown(app, shutdown_wake)
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

            # Refresh outbox from storage while it is still open.
            try:
                if app.storage is not None:
                    await app.refresh_outbox_state_from_storage()
            except Exception:
                logger.debug(
                    "Failed to refresh outbox state before shutdown", exc_info=True
                )

            stopping_ids = tuple(app.started_adapter_ids)
            try:
                await app.stop()
            except Exception as exc:
                logger.error("Shutdown error: %s", exc)
                shutdown_errors.append(("runtime", sanitize_error(str(exc))))
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

            # Signal-receipt evidence: how many shutdown requests arrived and
            # how long the process took to move from the first one to here.
            # Handlers record this without logging; this is its loop-context
            # report. It quantifies a slow Ctrl+C response on the next field
            # occurrence instead of leaving the first keypress time unknown.
            # The count prefers wakeup-pipe deliveries so presses that
            # coalesced behind one servicing round are still reported.
            _drain_wakeup_pipe()
            if first_shutdown_request_monotonic is not None:
                observed_requests = max(shutdown_signal_count, shutdown_delivered_count)
                _print(
                    f"  Shutdown requested {observed_requests} time(s) observed so far; "
                    f"first request "
                    f"{time.monotonic() - first_shutdown_request_monotonic:.1f}s "
                    f"before this line (at {first_shutdown_request_wall})"
                )

            summary = shutdown_summary(
                [
                    name
                    for name in stopping_ids
                    if app.adapter_states.get(name) is not None
                    and app.adapter_states[name].value == "stopped"
                ],
                shutdown_errors or None,
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
                    snap_path.write_text(to_json(snap) + "\n")
                    _print(f"  Final snapshot written to: {snapshot_path}")
                except Exception as exc:
                    print(
                        f"  Warning: failed to write snapshot: {sanitize_error(str(exc))}",
                        file=sys.stderr,
                        flush=True,
                    )

            logger.info("MEDRE stopped")
    finally:
        signals.restore()
