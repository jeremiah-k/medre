"""Replay CLI command: execute replay operations via the built runtime.

Modes without delivery side effects (everything except ``best_effort``)
run against the built-but-not-started runtime with read-only storage
access.  ``best_effort`` re-delivers through real adapters, so it starts
the runtime in the replay-delivery scope first (storage → pipeline →
adapters; the durable-ingress and retry workers stay stopped so the
replay never dispatches unrelated pending/live work) and stops it
afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time as _time
from typing import Any

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.core.engine.replay.summary import collect_replay_summary
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.observability.sanitization import sanitize_error
from medre.core.supervision.diagnostic_contract import (
    PENDING_DELIVERY_COUNT,
    QUEUE_PENDING,
)
from medre.runtime.app import StartupScope
from medre.runtime.builder import RuntimeBuilder

from .exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_STARTUP
from .json import to_json

_logger = logging.getLogger(__name__)

_BEST_EFFORT_WARNING = (
    "WARNING: BEST_EFFORT replay incurs the same duplicate-send risk as "
    "all adapter transports.  Replay receipts are distinguishable from "
    "live records by source='replay' and replay_run_id; however, "
    "traceability is NOT dedupe — duplicate-send risk remains.  "
    "Use --mode dry_run first to preview."
)


async def _drain_inflight_deliveries(app: Any, timeout: float) -> None:
    """Wait for adapters' in-flight outbound deliveries to go terminal.

    Reads each started adapter's own ``diagnostics()`` report and waits
    while an adapter still reports unflushed outbound work under the
    shared keys from :mod:`medre.core.supervision.diagnostic_contract`
    (``session.pending_delivery_count`` for async-transfer adapters such
    as LXMF; ``queue_pending`` for queue-backed adapters such as
    Meshtastic).  Returns as soon as every exposed count is zero, or
    after *timeout* seconds.  Purely observational: no adapter state is
    mutated, and the lifecycle stop remains the teardown authority.
    """
    deadline = _time.monotonic() + timeout
    while True:
        any_exposed = False
        all_terminal = True
        for adapter_id in getattr(app, "started_adapter_ids", []):
            adapter = app.adapters.get(adapter_id)
            diag_fn = getattr(adapter, "diagnostics", None)
            if diag_fn is None:
                continue
            diag = diag_fn() or {}
            session = diag.get("session") or {}
            if PENDING_DELIVERY_COUNT in session:
                any_exposed = True
                if session[PENDING_DELIVERY_COUNT] > 0:
                    all_terminal = False
                    break
            queue_pending = diag.get(QUEUE_PENDING)
            if queue_pending is not None:
                any_exposed = True
                if queue_pending > 0:
                    all_terminal = False
                    break
        if not any_exposed or all_terminal:
            return
        if _time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.2)


async def _teardown_replay_runtime(app: Any, drain_timeout: float) -> None:
    """Teardown after a ``best_effort`` replay body (runs from ``finally``).

    Preserves the replay body's own failure or cancellation as the
    operator-facing error: a secondary teardown failure (the bounded
    drain, ``app.stop()``) is logged and never allowed to replace the
    in-flight exception.  When the body succeeded, teardown failures
    still propagate so a broken shutdown is visible.  The drain and
    ``stop()`` share one ``shutdown_drain_timeout_seconds`` deadline —
    congestion cannot spend the documented drain budget twice
    (durable-ingress.md "Capacity and shutdown handoff").
    """
    primary_error = sys.exc_info()[1]
    drain_deadline = _time.monotonic() + drain_timeout
    drain_error: BaseException | None = None
    stop_error: BaseException | None = None
    # Give adapters' in-flight outbound deliveries a bounded window to
    # reach terminal state before teardown — an immediate stop would abort
    # asynchronous transfers (e.g. LXMF DIRECT link delivery) right after
    # acceptance.  Bounded by the documented shutdown drain limit; purely
    # observational: delivery truth is recorded only by the real queue
    # terminal callbacks through the lifecycle authority, never from
    # aggregate drain state.
    try:
        await _drain_inflight_deliveries(
            app, max(0.0, drain_deadline - _time.monotonic())
        )
    except BaseException as exc:
        # The drain is observational only.  It must never prevent lifecycle
        # teardown, including when it is itself cancelled.  Preserve a replay
        # body's already-in-flight error; otherwise surface the drain failure
        # only after ``app.stop()`` has had its chance to close resources.
        if primary_error is None:
            drain_error = exc
            _logger.error(
                "Replay pre-stop drain failed: %s", sanitize_error(str(exc))
            )
        else:
            _logger.warning(
                "Replay pre-stop drain failed (replay error preserved): %s",
                sanitize_error(str(exc)),
            )
    # Full lifecycle teardown (stops adapters, closes storage), bounded by
    # the SAME deadline the drain just consumed from.
    try:
        await app.stop(drain_deadline=drain_deadline)
    except BaseException as exc:
        if primary_error is None:
            stop_error = exc
        else:
            _logger.error(
                "Runtime stop after replay failure also failed "
                "(primary replay error preserved): %s",
                sanitize_error(str(exc)),
            )

    # When the replay body itself succeeded, teardown failures are fatal —
    # but only after the full lifecycle stop has been attempted.  Prefer the
    # stop failure because it describes the authoritative teardown operation;
    # chain an earlier observational drain failure for diagnosis.
    if primary_error is None and stop_error is not None:
        if drain_error is not None:
            raise stop_error from drain_error
        raise stop_error
    if primary_error is None and drain_error is not None:
        raise drain_error


async def _replay(
    config_path: str | None,
    mode: str,
    event_id: str | None,
    json_output: bool,
    target_adapters: list[str] | None,
    route_ids: list[str] | None,
    limit: int,
    run_id: str = "",
) -> None:
    """Execute a replay operation via the built (not started) runtime."""
    # Validate mode.
    mode_map = {m.value: m for m in ReplayMode}
    if mode not in mode_map:
        print(
            f"Error: invalid mode {mode!r}. "
            f"Valid modes: {', '.join(sorted(mode_map.keys()))}",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    replay_mode = mode_map[mode]

    # Warn for BEST_EFFORT.
    if replay_mode == ReplayMode.BEST_EFFORT and not json_output:
        print(_BEST_EFFORT_WARNING, file=sys.stderr)
        print(file=sys.stderr)

    # Load config and build runtime (but do NOT start it).
    try:
        config, _source, paths = load_config(config_path)
        # Env overrides share the config-error boundary (see run command).
        config = apply_env_overrides(config, paths)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    builder = RuntimeBuilder(config, paths)
    try:
        app = builder.build()
    except Exception as exc:
        print(f"Runtime build error: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)

    if app.replay_engine is None:
        print(
            "Error: replay engine not available — runtime was built without one.",
            file=sys.stderr,
        )
        sys.exit(EXIT_BUILD)

    if app.storage is None:
        print(
            "Error: storage not available — runtime was built without one.",
            file=sys.stderr,
        )
        sys.exit(EXIT_BUILD)

    # Side-effect modes (best_effort) re-deliver through real adapters, so
    # the built runtime must be STARTED — the deliver stage requires live
    # adapters and fails with ``AdapterPermanentError("Adapter not
    # started")`` otherwise (observed on real hardware recovery).  Modes
    # without delivery side effects keep the read-only storage-only path.
    needs_runtime = replay_mode == ReplayMode.BEST_EFFORT
    if needs_runtime:
        try:
            # REPLAY scope: adapters start so the selected replay can be
            # delivered through them, but the durable-ingress and retry
            # workers stay stopped — the replay must not dispatch unrelated
            # pending/retryable work or route live ingress outside the
            # selected replay.  Ingress received while scoped crosses the
            # durable admission boundary and is processed by the next live
            # start (see StartupScope.REPLAY).
            await app.start(scope=StartupScope.REPLAY)
        except Exception as exc:
            print(
                f"\nRuntime startup failed: {sanitize_error(str(exc))}",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(EXIT_STARTUP)
    else:
        # Initialize storage for read access without starting the runtime.
        await app.storage.initialize()
    try:
        # Build replay request.
        request = ReplayRequest(
            mode=replay_mode,
            correlation_ids=[event_id] if event_id else None,
            target_adapters=target_adapters,
            route_ids=tuple(route_ids) if route_ids else (),
            limit=limit,
            run_id=run_id,
        )

        # Execute replay.
        t0 = _time.monotonic()
        results = app.replay_engine.replay(request)
        summary = await collect_replay_summary(
            results,
            mode=replay_mode,
            elapsed_ms=(_time.monotonic() - t0) * 1000,
            run_id=run_id,
        )

        summary_dict = summary.to_dict()
    finally:
        if needs_runtime:
            # Preserve the replay body's failure/cancellation as the
            # operator-facing error; secondary drain/stop failures are
            # surfaced without replacing it.  Drain and stop share one
            # documented shutdown-drain deadline.
            await _teardown_replay_runtime(
                app, config.limits.shutdown_drain_timeout_seconds
            )
        else:
            await app.storage.close()

    if json_output:
        print(to_json(summary_dict))
    else:
        # Human-readable summary.
        print(f"Replay: {mode}")
        if run_id:
            print(f"  Run ID:        {run_id}")
        print(f"  Events scanned:  {summary.events_scanned}")
        print(f"  Events replayed: {summary.events_replayed}")
        print(f"  Passed:          {summary.by_status.get('passed', 0)}")
        print(f"  Skipped:         {summary.by_status.get('skipped', 0)}")
        if summary.skip_reasons:
            for reason, count in sorted(summary.skip_reasons.items()):
                print(f"    {reason}: {count}")
        print(f"  Failed:          {summary.by_status.get('failed', 0)}")
        print(f"  Errors:          {summary.by_status.get('error', 0)}")
        print(f"  Elapsed:         {summary.elapsed_ms:.1f}ms")
        if summary.errors:
            print(f"  Errors ({len(summary.errors)}):")
            for err in summary.errors[:10]:
                print(f"    {err[:120]}")
        if summary.by_route:
            print("  Per-route:")
            for rid, counts in sorted(summary.by_route.items()):
                print(
                    f"    {rid}: {counts['succeeded']} succeeded, "
                    f"{counts['failed']} failed"
                )
