"""Diagnostics snapshot and live health evidence sections."""

from __future__ import annotations

import logging
from typing import Any

from medre.config.paths import MedrePaths

from ._helpers import (
    _fixed_mono,
    _fixed_now,
    _section_error,
    _section_ok,
    _section_partial,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Diagnostics snapshot
# ---------------------------------------------------------------------------


async def _collect_diagnostics_snapshot(
    config: Any,
    paths: MedrePaths,
) -> dict[str, Any]:
    """Build diagnostics snapshot section (no runtime start, no I/O)."""
    from medre.runtime.snapshot import build_runtime_snapshot

    enabled_adapters = config.adapters.all_enabled()
    if not enabled_adapters:
        return _section_error("No adapters enabled in configuration")

    from medre.runtime.builder import RuntimeBuilder

    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
    except Exception as exc:
        return _section_error(f"Runtime build error: {exc}")

    if not app.adapters:
        return _section_error(
            f"All {len(app.build_failures)} enabled adapter(s) failed to construct"
        )

    snapshot = build_runtime_snapshot(
        app,
        now_fn=_fixed_now,
        monotonic_fn=_fixed_mono,
    )
    return _section_ok(snapshot)


# ---------------------------------------------------------------------------
# Live health
# ---------------------------------------------------------------------------


async def _collect_live_health(
    config: Any,
    paths: MedrePaths,
) -> dict[str, Any]:
    """Start runtime, refresh health once, capture snapshot, stop cleanly.

    The caller is responsible for setting ``runtime_started`` in the
    top-level report based on whether this section succeeds.
    """
    from medre.runtime.snapshot import build_runtime_snapshot

    enabled_adapters = config.adapters.all_enabled()
    if not enabled_adapters:
        return _section_error("No adapters enabled — cannot start for health check")

    from medre.runtime.builder import RuntimeBuilder

    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
    except Exception as exc:
        return _section_error(f"Runtime build error: {exc}")

    if not app.adapters:
        return _section_error(
            f"All {len(app.build_failures)} enabled adapter(s) failed to construct"
        )

    try:
        await app.start()
    except Exception as exc:
        return _section_error(f"Runtime startup failed: {exc}")

    try:
        await app.refresh_live_health()
        await app.refresh_outbox_state_from_storage()
        snapshot = build_runtime_snapshot(app)
        return _section_ok(snapshot)
    except Exception as exc:
        return _section_partial(None, f"Health refresh error: {exc}")
    finally:
        try:
            await app.stop()
        except Exception as exc:
            _logger.warning("Error during evidence live-health shutdown: %s", exc)
