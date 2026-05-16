"""Config summary and route validation evidence sections."""

from __future__ import annotations

from typing import Any

from medre.config.env import MedreEnvConfig
from medre.config.loader import ConfigSource
from medre.config.paths import MedrePaths
from medre.observability.sanitization import sanitize_error

from ._helpers import _section_ok, _section_partial


# ---------------------------------------------------------------------------
# Config summary
# ---------------------------------------------------------------------------


def _collect_config_summary(
    config: Any,
    source: ConfigSource,
    paths: MedrePaths,
) -> dict[str, Any]:
    """Build a redacted config summary section.

    Exposes only adapter wrapper metadata (transport, adapter_id, enabled,
    adapter_kind) — never adapter config internals (access_token, etc.).
    """
    adapters_summary: list[dict[str, Any]] = []
    for transport, adapter_id, rtc in config.adapters.all_configs():
        adapters_summary.append({
            "adapter_id": adapter_id,
            "adapter_kind": getattr(rtc, "adapter_kind", "real"),
            "enabled": rtc.enabled,
            "transport": transport,
        })

    routes_summary: list[dict[str, Any]] = []
    routes = config.routes
    if routes is not None:
        for route in routes.routes:
            routes_summary.append({
                "dest_adapters": sorted(route.dest_adapters),
                "directionality": route.directionality.value,
                "enabled": route.enabled,
                "route_id": route.route_id,
                "source_adapters": sorted(route.source_adapters),
            })

    # Limits — numeric, no secrets.
    limits = config.limits
    limits_summary: dict[str, Any] = {}
    if hasattr(limits, "max_inflight_deliveries"):
        limits_summary["delivery_acquire_timeout_seconds"] = (
            limits.delivery_acquire_timeout_seconds
        )
        limits_summary["max_inflight_deliveries"] = limits.max_inflight_deliveries
        limits_summary["max_inflight_replay_events"] = limits.max_inflight_replay_events
        limits_summary["shutdown_drain_timeout_seconds"] = (
            limits.shutdown_drain_timeout_seconds
        )

    # Env overrides — list names, redact secret values.
    env = MedreEnvConfig.from_environ()
    env_overrides_applied: list[str] = sorted(
        name for name, _value in env.provenance.redacted_items()
    )

    # Paths — already safe (no secrets).
    paths_summary = paths.to_diagnostics()

    # Storage.
    storage_backend = config.storage.backend if config.storage else "none"
    storage_path: str | None = None
    if config.storage and config.storage.path:
        storage_path = config.storage.path
    elif config.storage:
        storage_path = str(paths.database_path)

    data: dict[str, Any] = {
        "adapters": adapters_summary,
        "env_overrides_applied": env_overrides_applied,
        "limits": limits_summary,
        "logging_level": config.logging.level if config.logging else "INFO",
        "paths": paths_summary,
        "routes": routes_summary,
        "runtime_name": config.runtime.name if config.runtime else "medre",
        "storage_backend": storage_backend,
        "storage_path": storage_path,
    }

    return _section_ok(data)


# ---------------------------------------------------------------------------
# Route validation
# ---------------------------------------------------------------------------


def _collect_route_validation(config: Any) -> dict[str, Any]:
    """Validate route configuration and return results."""
    from medre.runtime.route_engine import (
        build_runtime_routes,
        RouteValidationError,
    )

    routes = config.routes
    route_list = routes.routes if routes is not None else []
    route_count = len(route_list)
    route_enabled = sum(1 for r in route_list if r.enabled)

    errors: list[str] = []
    warnings: list[str] = []

    if routes is not None:
        try:
            build_runtime_routes(routes)
        except RouteValidationError as exc:
            errors.append(str(exc))

    # Check adapter references.
    known_adapter_ids: set[str] = set()
    for _transport, adapter_id, rtc in config.adapters.all_configs():
        known_adapter_ids.add(adapter_id)

    for route in route_list:
        if not route.enabled:
            continue
        for sa in route.source_adapters:
            if sa not in known_adapter_ids:
                errors.append(
                    f"route {route.route_id!r}: source adapter {sa!r} not defined"
                )
        for da in route.dest_adapters:
            if da not in known_adapter_ids:
                errors.append(
                    f"route {route.route_id!r}: dest adapter {da!r} not defined"
                )

    data: dict[str, Any] = {
        "route_count": route_count,
        "route_enabled": route_enabled,
        "route_errors": [sanitize_error(e) for e in errors],
        "route_warnings": [sanitize_error(w) for w in warnings],
        "valid": len(errors) == 0,
    }

    if errors:
        return _section_partial(data, f"{len(errors)} route validation error(s)")
    return _section_ok(data)
