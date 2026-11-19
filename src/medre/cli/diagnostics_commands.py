"""Diagnostics CLI commands: static snapshot, live health refresh, and bundle."""

from __future__ import annotations

import logging
import sys
import zipfile
from datetime import datetime, timezone
from typing import Any

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config

from .exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_STARTUP
from .json import to_json

logger = logging.getLogger("medre")


def _metrics_projection(
    snapshot: dict[str, Any], runtime_diagnostics: dict[str, Any]
) -> dict[str, Any]:
    """Return the bounded, identifier-free subset exported as metrics."""
    accounting = snapshot.get("accounting") or {}
    capacity = snapshot.get("capacity") or {}
    capacity_state = dict(capacity.get("state") or {})
    # Per-source admission pressure is keyed by adapter id; metric names
    # must stay identifier-free, so the detail stays in JSON/evidence only.
    capacity_state.pop("inbound_sources", None)
    runtime_diag = dict(runtime_diagnostics or {})
    runtime_capacity = runtime_diag.get("capacity")
    if isinstance(runtime_capacity, dict):
        runtime_diag["capacity"] = {
            key: value
            for key, value in runtime_capacity.items()
            if key != "inbound_sources"
        }
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
        "capacity": {"state": capacity_state},
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
        "runtime_diagnostics": runtime_diag,
    }


def _human_lifecycle_line(lifecycle: dict[str, Any] | None) -> str:
    state = (
        str(lifecycle.get("runtime_state", "unknown"))
        if isinstance(lifecycle, dict)
        else "unknown"
    )
    uptime = lifecycle.get("uptime_seconds") if isinstance(lifecycle, dict) else None
    if isinstance(uptime, (int, float)) and not isinstance(uptime, bool):
        return f"Runtime state: {state} (uptime {uptime:g}s)"
    return f"Runtime state: {state} (not started)"


def _human_adapter_line(row: dict[str, Any], state: object = None) -> str:
    platform = str(row.get("platform", "unknown"))
    role = str(row.get("role", "unknown"))
    header = f"  {row.get('adapter_id', '?')} ({platform}, {role})"
    lifecycle = f"state {state}, " if isinstance(state, str) else ""
    return f"{header}: {lifecycle}health {row.get('health', 'unknown')}"


def _human_routes_line(routes: object) -> str:
    eligibility = routes.get("eligibility") if isinstance(routes, dict) else None
    if not isinstance(eligibility, dict):
        return "Routes (build eligibility): not reported"

    def count(key: str) -> int:
        value = eligibility.get(key)
        return len(value) if isinstance(value, list) else 0

    configured = count("configured")
    facts: list[str] = []
    for key in ("degraded", "disabled", "unavailable", "skipped"):
        total = count(key)
        if total:
            facts.append(f"{total} {key}")
    detail = ", ".join(facts) if facts else "all eligible"
    return f"Routes (build eligibility): {configured} configured ({detail})"


def _human_startup_routes_line(routes: object) -> str | None:
    readiness = routes.get("startup_readiness") if isinstance(routes, dict) else None
    if not isinstance(readiness, dict):
        return None
    states = readiness.get("readiness")
    if not isinstance(states, dict):
        return "Routes (startup readiness): not reported"
    counts: dict[str, int] = {}
    for state in states.values():
        if isinstance(state, str):
            counts[state] = counts.get(state, 0) + 1
    detail = ", ".join(f"{total} {state}" for state, total in sorted(counts.items()))
    return f"Routes (startup readiness): {detail or 'no states reported'}"


def _human_capacity_line(state: object) -> str:
    if not isinstance(state, dict):
        return "Capacity: state unavailable"

    def pair(current_key: str, limit_key: str) -> str:
        current = state.get(current_key)
        limit = state.get(limit_key)
        current_text = str(current) if isinstance(current, int) else "?"
        limit_text = str(limit) if isinstance(limit, int) else "?"
        return f"{current_text}/{limit_text}"

    accepting = state.get("accepting_work")
    suffix = "" if accepting is not False else ", not accepting work"
    return (
        f"Capacity: inbound {pair('inbound_current', 'inbound_limit')}, "
        f"delivery {pair('delivery_current', 'delivery_limit')}{suffix}"
    )


def _human_outbox_line(outbox: object) -> str:
    counts = outbox.get("counts") if isinstance(outbox, dict) else None
    if not isinstance(counts, dict):
        return "Outbox: counts unavailable"
    if not counts:
        return "Outbox: no counts reported"
    detail = ", ".join(
        f"{status}={total}"
        for status, total in sorted(counts.items())
        if isinstance(total, int) and not isinstance(total, bool)
    )
    return f"Outbox: {detail}" if detail else "Outbox: counts unavailable"


def _human_replay_line(replay: object) -> str:
    available = replay.get("available") if isinstance(replay, dict) else None
    if available is True:
        return "Replay: available"
    if available is False:
        return "Replay: unavailable"
    return "Replay: state unavailable"


def _human_retry_line(retry: object) -> str:
    if not isinstance(retry, dict):
        return "Retry: state unavailable"
    enabled = retry.get("enabled")
    if enabled is False:
        return "Retry: disabled"
    if enabled is not True:
        return "Retry: state unavailable"
    facts: list[str] = []
    for key in ("failed", "dead_lettered"):
        total = retry.get(key)
        if isinstance(total, int) and total:
            facts.append(f"{total} {key}")
    return "Retry: enabled" + (f" ({', '.join(facts)})" if facts else "")


def _render_human(snapshot: dict[str, Any]) -> None:
    """Print the short, path-free operator view of a runtime snapshot.

    Mirrors the readiness report's human style: one line per section,
    typed statuses instead of error text, and ordered next steps. The
    full machine contract stays available through ``--format json`` and
    ``--format prometheus``.
    """
    scope = snapshot.get("snapshot_scope", "unknown")
    print(f"Runtime diagnostics snapshot ({scope})")
    lifecycle = snapshot.get("lifecycle")
    print(_human_lifecycle_line(lifecycle if isinstance(lifecycle, dict) else None))

    adapters_raw = snapshot.get("adapters")
    adapter_rows = (
        {aid: row for aid, row in adapters_raw.items() if isinstance(row, dict)}
        if isinstance(adapters_raw, dict)
        else {}
    )
    print(f"Adapters: {len(adapter_rows)} built")
    states = lifecycle.get("adapters") if isinstance(lifecycle, dict) else None
    for adapter_id in sorted(adapter_rows):
        state = states.get(adapter_id) if isinstance(states, dict) else None
        print(_human_adapter_line(adapter_rows[adapter_id], state))

    startup = snapshot.get("startup")
    failures = startup.get("build_failures") if isinstance(startup, dict) else None
    if isinstance(failures, list) and failures:
        print(f"Build failures: {len(failures)} adapter(s) failed to construct")
        for failure in failures:
            if isinstance(failure, dict):
                print(f"  {failure.get('adapter_id', '?')}: failed to construct")
    else:
        print("Build failures: none")

    boot = startup.get("boot_summary") if isinstance(startup, dict) else None
    if isinstance(boot, dict):
        print(f"Startup outcome: {boot.get('startup_outcome', 'unknown')}")
        failed_ids = boot.get("failed_adapter_ids")
        if isinstance(failed_ids, list):
            for adapter_id in failed_ids:
                print(f"  {adapter_id}: failed to start")

    print(_human_routes_line(snapshot.get("routes")))
    startup_routes = _human_startup_routes_line(snapshot.get("routes"))
    if startup_routes is not None:
        print(startup_routes)
    capacity = snapshot.get("capacity")
    capacity_state = capacity.get("state") if isinstance(capacity, dict) else None
    print(_human_capacity_line(capacity_state))
    print(_human_outbox_line(snapshot.get("outbox")))
    print(_human_replay_line(snapshot.get("replay")))
    print(_human_retry_line(snapshot.get("retry")))

    health = snapshot.get("health")
    live = health.get("live_health") if isinstance(health, dict) else None
    if isinstance(live, dict) and live:
        runtime_health = live.get("runtime_health", "unknown")
        summary = live.get("adapter_summary")
        if isinstance(summary, dict):
            counts = ", ".join(
                f"{category}={total}"
                for category, total in sorted(summary.items())
                if isinstance(total, int)
            )
            print(f"Live health: {runtime_health} ({counts})")
        else:
            print(f"Live health: {runtime_health}")
        adapters_live = live.get("adapters")
        if isinstance(adapters_live, dict):
            for adapter_id in sorted(adapters_live):
                entry = adapters_live[adapter_id]
                if isinstance(entry, dict):
                    print(f"  {adapter_id}: health {entry.get('health', 'unknown')}")

    _render_human_next_steps(
        adapter_rows, failures, live, snapshot.get("routes"), capacity_state
    )


def _render_human_next_steps(
    adapter_rows: dict[str, dict[str, Any]],
    failures: object,
    live: object,
    routes: object,
    capacity: object,
) -> None:
    """Print ordered, path-free next steps plus the closing observation rule."""
    steps: list[str] = []
    if isinstance(failures, list) and failures:
        steps.append(
            "Adapter build failures present; see startup.build_failures in "
            "'medre diagnostics --format json'."
        )
    # A current observation supersedes startup health for that adapter only.
    effective_health = {aid: row.get("health") for aid, row in adapter_rows.items()}
    live_rows = live.get("adapters") if isinstance(live, dict) else None
    if isinstance(live_rows, dict):
        for aid, entry in live_rows.items():
            if isinstance(entry, dict) and isinstance(entry.get("health"), str):
                effective_health[aid] = entry["health"]
    unhealthy = any(
        value in ("degraded", "failed") for value in effective_health.values()
    )
    if unhealthy:
        steps.append(
            "Degraded or failed adapter health reported; "
            "'medre diagnostics --refresh-health' refreshes live health "
            "(starts the runtime) before relying on delivery."
        )
    if isinstance(routes, dict):
        eligibility = routes.get("eligibility")
        if isinstance(eligibility, dict) and any(
            isinstance(eligibility.get(key), list) and eligibility[key]
            for key in ("degraded", "unavailable", "skipped")
        ):
            steps.append(
                "Build route eligibility has restrictions; 'medre routes plan' shows the "
                "per-route detail."
            )
        readiness = routes.get("startup_readiness")
        if isinstance(readiness, dict) and any(
            isinstance(readiness.get(key), list) and readiness[key]
            for key in ("degraded", "skipped")
        ):
            steps.append(
                "Startup route readiness has restrictions; see routes.startup_readiness "
                "in 'medre diagnostics --refresh-health --format json' "
                "(starts the runtime)."
            )
    if isinstance(capacity, dict) and capacity.get("accepting_work") is False:
        steps.append(
            "Capacity is not accepting work; inspect capacity.state in "
            "'medre diagnostics --format json'."
        )
    if steps:
        steps.append(
            "Reuse the original configuration selection for follow-up commands "
            "(including --config if supplied)."
        )
    steps.append(
        "Snapshots are point-in-time observations; they do not establish "
        "delivery success or native acceptance."
    )
    print("Next steps:")
    for step in steps:
        print(f"  - {step}")


def _diagnostics(config_path: str | None, *, output_format: str = "human") -> None:
    """Print runtime snapshot using local config/process construction only.

    This command builds the runtime from configuration but does **not** start
    adapters, storage, or any I/O.  It produces a pre-flight snapshot showing
    what the runtime *would* look like: adapter inventory, route topology,
    limits, and config state.  No server, socket, or API is involved.

    The default output is the human-readable summary; ``--format json``
    prints the full machine snapshot and ``--format prometheus`` the
    numeric exposition.
    """
    from medre.runtime.snapshot import build_runtime_snapshot

    try:
        config, source, paths = load_config(config_path)
        # Env overrides share the config-error boundary (see run command).
        config = apply_env_overrides(config, paths)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

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
        elif output_format == "json":
            print(to_json(snapshot))
        else:
            _render_human(snapshot)
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
    config_path: str | None, *, output_format: str = "human"
) -> None:
    """Start runtime, refresh adapter health once, print the live snapshot.

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
    (operators read the rendered report; ``--format json`` for the full
    machine snapshot).
    """
    try:
        config, source, paths = load_config(config_path)
        # Env overrides share the config-error boundary (see run command).
        config = apply_env_overrides(config, paths)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

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
        elif output_format == "json":
            print(to_json(snapshot))
        else:
            _render_human(snapshot)
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
