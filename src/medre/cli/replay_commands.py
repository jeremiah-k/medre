"""Replay CLI command: execute replay operations via the built (not started) runtime."""

from __future__ import annotations

import json as _json
import sys
import time as _time

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.core.engine.replay.summary import collect_replay_summary
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.runtime.builder import RuntimeBuilder

from .exit_codes import EXIT_BUILD, EXIT_CONFIG

_BEST_EFFORT_WARNING = (
    "WARNING: BEST_EFFORT replay incurs the same duplicate-send risk as "
    "all adapter transports.  Replay receipts are distinguishable from "
    "live records by source='replay' and replay_run_id; however, "
    "traceability is NOT dedupe — duplicate-send risk remains.  "
    "Use --dry-run first to preview."
)


async def _replay(
    config_path: str | None,
    mode: str,
    event_id: str | None,
    json_output: bool,
    target_adapters: list[str] | None,
    route_ids: list[str] | None,
    limit: int,
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
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    config = apply_env_overrides(config, paths)

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
        )

        # Execute replay.
        t0 = _time.monotonic()
        results = app.replay_engine.replay(request)
        summary = await collect_replay_summary(
            results,
            mode=replay_mode,
            elapsed_ms=(_time.monotonic() - t0) * 1000,
        )

        summary_dict = summary.to_dict()
    finally:
        await app.storage.close()

    if json_output:
        print(_json.dumps(summary_dict, sort_keys=True, indent=2))
    else:
        # Human-readable summary.
        print(f"Replay: {mode}")
        print(f"  Events scanned:  {summary.events_scanned}")
        print(f"  Events replayed: {summary.events_replayed}")
        print(f"  Passed:          {summary.by_status.get('passed', 0)}")
        print(f"  Skipped:         {summary.by_status.get('skipped', 0)}")
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
