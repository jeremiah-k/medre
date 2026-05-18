"""Diagnostics CLI commands: static snapshot and live health refresh."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config

from .exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_STARTUP

logger = logging.getLogger("medre")


def _diagnostics(config_path: str | None) -> None:
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
        sys.exit(EXIT_BUILD)

    # Use fixed timestamps for deterministic output.
    fixed_now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    fixed_mono = 0.0

    snapshot = build_runtime_snapshot(
        app,
        now_fn=lambda: fixed_now,
        monotonic_fn=lambda: fixed_mono,
    )

    print(json.dumps(snapshot, sort_keys=True, indent=2))


async def _diagnostics_refresh(config_path: str | None) -> None:
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

        # Build snapshot with REAL timestamps (not fixed).
        from medre.runtime.snapshot import build_runtime_snapshot

        snapshot = build_runtime_snapshot(app)
        print(json.dumps(snapshot, sort_keys=True, indent=2))
    finally:
        # Always attempt clean shutdown after a successful start.
        try:
            await app.stop()
        except Exception as exc:
            logger.warning("Error during diagnostics shutdown: %s", exc)
