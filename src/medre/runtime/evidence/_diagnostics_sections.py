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
# Adapter status and shutdown evidence derivation from snapshot
# ---------------------------------------------------------------------------


def _derive_adapter_status_from_snapshot(
    snapshot: dict[str, Any],
    config: Any,
) -> list[dict[str, Any]]:
    """Derive per-adapter status evidence from snapshot and config.

    Uses :func:`build_adapter_status_evidence` without SDK imports.
    Derives from snapshot lifecycle adapters, adapter metadata, and
    config adapter summaries.
    """
    from medre.core.evidence.adapter_status import build_adapter_status_evidence

    _MISSING = object()  # sentinel for "adapter not found in config"

    # Gather adapter metadata from snapshot.
    adapters_meta = snapshot.get("adapters", {})
    lifecycle_adapters = snapshot.get("lifecycle", {}).get("adapters", {})

    results: list[dict[str, Any]] = []
    for adapter_id in sorted(
        set(adapters_meta.keys()) | set(lifecycle_adapters.keys())
    ):
        meta = adapters_meta.get(adapter_id, {})
        state_str = lifecycle_adapters.get(adapter_id)
        transport = meta.get("platform")

        # Derive config from the runtime config object.
        adapter_kind = None
        enabled = None
        adapter_config = _MISSING  # sentinel: distinguish "not found" from None

        if config is not None:
            adapters_cfg = getattr(config, "adapters", None)
            if adapters_cfg is not None:
                all_configs = adapters_cfg.all_configs()
                for _transport, _aid, _rtc in all_configs:
                    if _aid == adapter_id:
                        enabled = getattr(_rtc, "enabled", None)
                        adapter_kind = getattr(_rtc, "adapter_kind", None)
                        adapter_config = getattr(_rtc, "config", None)
                        if transport is None:
                            transport = _transport
                        break

        # Build the config dict for build_adapter_status_evidence.
        # When adapter_config is still _MISSING the adapter was not found in
        # the runtime config (e.g. snapshot-only adapter).  Omit the
        # "config" key entirely so _resolve_configured() returns None
        # instead of forcing configured=False.
        if adapter_config is not _MISSING:
            cfg_dict: dict[str, Any] = {
                "enabled": enabled,
                "adapter_kind": adapter_kind,
                "config": adapter_config,
            }
        else:
            cfg_dict = {"enabled": enabled, "adapter_kind": adapter_kind}

        evidence = build_adapter_status_evidence(
            adapter_id,
            config=cfg_dict,
            lifecycle_state=state_str,
            transport=transport,
        )
        results.append(evidence.to_dict())

    return results


def _derive_shutdown_evidence_from_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Derive shutdown evidence from runtime snapshot data.

    Uses :func:`build_shutdown_evidence` with snapshot-derived inputs.
    """
    from medre.core.evidence.shutdown import build_shutdown_evidence

    lifecycle = snapshot.get("lifecycle", {})
    outbox = snapshot.get("outbox", {})
    retry = snapshot.get("retry", {})
    capacity = snapshot.get("capacity", {})
    snapshot.get("diagnostics", {})

    # Runtime state.
    runtime_state = lifecycle.get("runtime_state")

    # Outbox counts.
    outbox_counts = outbox.get("counts")

    # Retry state as dict.
    retry_state: dict[str, Any] | None = None
    if retry:
        retry_state = {
            "enabled": retry.get("enabled", False),
            "running": retry.get("running", False),
            "processed": retry.get("processed", 0),
            "succeeded": retry.get("succeeded", 0),
            "failed": retry.get("failed", 0),
            "dead_lettered": retry.get("dead_lettered", 0),
        }

    # Capacity state.
    capacity_state = capacity.get("state")

    # Runtime events.

    evidence = build_shutdown_evidence(
        runtime_state=runtime_state,
        outbox_counts=outbox_counts,
        retry_state=retry_state,
        events=[],  # Events not easily derivable from snapshot dict
        capacity_state=capacity_state,
    )
    return evidence.to_dict()


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

    # Derive adapter status evidence from snapshot + config.
    snapshot["adapter_status"] = _derive_adapter_status_from_snapshot(snapshot, config)

    # Derive shutdown evidence from snapshot.
    snapshot["shutdown_evidence"] = _derive_shutdown_evidence_from_snapshot(snapshot)

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

        # Derive adapter status evidence from snapshot + config.
        snapshot["adapter_status"] = _derive_adapter_status_from_snapshot(
            snapshot,
            config,
        )

        # Derive shutdown evidence from snapshot.
        snapshot["shutdown_evidence"] = _derive_shutdown_evidence_from_snapshot(
            snapshot
        )

        return _section_ok(snapshot)
    except Exception as exc:
        return _section_partial(None, f"Live refresh error (health/outbox): {exc}")
    finally:
        try:
            await app.stop()
        except Exception as exc:
            _logger.warning("Error during evidence live-health shutdown: %s", exc)
