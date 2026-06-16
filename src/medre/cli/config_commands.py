"""Config-related CLI commands: paths, check, adapters, version."""

from __future__ import annotations

import importlib
import os
import sys

from medre.config.paths import resolve

from .exit_codes import EXIT_CONFIG
from .transports import TRANSPORTS


def _dir_status(p: object) -> str:
    """Return a status indicator for a path — exists / will be created."""
    path = p if isinstance(p, str) else str(p)
    if os.path.isdir(path):
        return "exists"
    return "will be created"


def _paths() -> None:
    """Resolve and print all MEDRE paths with status indicators."""
    paths = resolve()
    medre_home = os.environ.get("MEDRE_HOME", "").strip()

    if medre_home:
        print("Mode:     MEDRE_HOME")
        print(f"MEDRE_HOME: {medre_home}")
    else:
        print("Mode:     XDG")
        xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
        xdg_state = os.environ.get("XDG_STATE_HOME", "")
        xdg_data = os.environ.get("XDG_DATA_HOME", "")
        xdg_cache = os.environ.get("XDG_CACHE_HOME", "")
        if xdg_config:
            print(f"XDG_CONFIG_HOME:  {xdg_config}")
        if xdg_state:
            print(f"XDG_STATE_HOME:   {xdg_state}")
        if xdg_data:
            print(f"XDG_DATA_HOME:    {xdg_data}")
        if xdg_cache:
            print(f"XDG_CACHE_HOME:   {xdg_cache}")

    print()
    print(f"Config file:  {paths.config_file}")
    print(f"State dir:    {paths.state_dir}  [{_dir_status(paths.state_dir)}]")
    print(f"Data dir:     {paths.data_dir}  [{_dir_status(paths.data_dir)}]")
    print(f"Cache dir:    {paths.cache_dir}  [{_dir_status(paths.cache_dir)}]")
    print(f"Log dir:      {paths.log_dir}  [{_dir_status(paths.log_dir)}]")
    print(f"Global DB:    {paths.database_path}")


def _config_check(config_path: str | None) -> None:
    """Load and validate the config, printing a rich summary."""
    from medre.config.errors import ConfigValidationError
    from medre.config.loader import load_config

    try:
        config, source, paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    # --- Config file info ---
    print(f"Config file: {paths.config_file}")
    print(f"Source:      {source.value}")

    # --- Resolved paths ---
    print()
    print("Resolved paths:")
    print(f"  State dir:  {paths.state_dir}")
    print(f"  Data dir:   {paths.data_dir}")
    print(f"  Cache dir:  {paths.cache_dir}")
    print(f"  Log dir:    {paths.log_dir}")
    print(f"  Global DB:  {paths.database_path}")

    # --- Adapter inventory ---
    print()
    print("Adapter inventory:")
    total = 0
    enabled_count = 0
    validation_errors: list[str] = []

    for transport, adapters in [
        ("matrix", config.adapters.matrix),
        ("meshtastic", config.adapters.meshtastic),
        ("meshcore", config.adapters.meshcore),
        ("lxmf", config.adapters.lxmf),
    ]:
        for name, ac in adapters.items():
            total += 1
            status = "enabled" if ac.enabled else "disabled"
            kind = getattr(ac, "adapter_kind", "real")
            print(
                f"  {transport}.{name}: {status}  (transport={transport}, adapter_kind={kind})"
            )
            if ac.enabled:
                enabled_count += 1
            # Try adapter-specific validation.
            adapter_conf = getattr(ac, "config", None)
            if adapter_conf is not None and hasattr(adapter_conf, "validate"):
                try:
                    adapter_conf.validate()
                except Exception as exc:
                    msg = f"  \u26a0 {transport}.{name}: validation error — {exc}"
                    validation_errors.append(msg)

    if total == 0:
        print("  (no adapters configured)")

    # --- Route adapter-reference validation (F-016) ---
    # Cross-check every route's source/dest refs against the configured
    # adapter IDs so ``config check`` is a complete pre-flight gate.  Done
    # inline (no runtime import) — the CLI already has both the adapter set
    # and the route set.  Checks all routes so latent refs in disabled
    # routes surface now, not when the operator enables them.
    known_adapter_ids = {aid for _t, aid, _rtc in config.adapters.all_configs()}
    for route in config.routes.routes:
        for aid in route.source_adapters:
            if aid not in known_adapter_ids:
                validation_errors.append(
                    f"  \u26a0 route {route.route_id}: references unknown "
                    f"source adapter {aid!r}"
                )
        for aid in route.dest_adapters:
            if aid not in known_adapter_ids:
                validation_errors.append(
                    f"  \u26a0 route {route.route_id}: references unknown "
                    f"dest adapter {aid!r}"
                )

    # --- Validation errors ---
    if validation_errors:
        print()
        print("Validation errors:")
        for err in validation_errors:
            print(err)

    # --- Adapter state roots ---
    enabled_adapters = config.adapters.all_configs()
    enabled_for_roots = [
        (t, aid, rtc) for t, aid, rtc in enabled_adapters if rtc.enabled
    ]
    if enabled_for_roots:
        print()
        print("Adapter state roots:")
        for transport, adapter_id, _rtc in enabled_for_roots:
            root = paths.adapter_state_dir(adapter_id)
            print(f"  {transport}.{adapter_id}: {root}")

    # --- Global DB ---
    print()
    print(f"Global DB: {paths.database_path}")

    # --- Storage backend ---
    storage_backend = config.storage.backend if config.storage else "none"
    print(f"Storage backend: {storage_backend}")

    # --- Runtime limits ---
    limits = config.limits
    print()
    print("Runtime limits:")
    print(f"  max_inflight_deliveries = {limits.max_inflight_deliveries}")
    print(f"  max_inflight_replay_events = {limits.max_inflight_replay_events}")
    print(f"  shutdown_drain_timeout_seconds = {limits.shutdown_drain_timeout_seconds}")
    print(
        f"  delivery_acquire_timeout_seconds = {limits.delivery_acquire_timeout_seconds}"
    )

    # Validate limits and append any errors
    try:
        limits.validate()
    except ConfigValidationError as exc:
        validation_errors.append(f"  \u26a0 runtime.limits: {exc}")

    # --- Route inventory ---
    routes = config.routes
    route_list = routes.routes
    print()
    print("Route inventory:")
    if not route_list:
        print("  (no routes configured)")
    else:
        for route in route_list:
            status = "enabled" if route.enabled else "disabled"
            direction = route.directionality.value
            sources = ", ".join(route.source_adapters)
            dests = ", ".join(route.dest_adapters)
            on_off = "[ON]" if route.enabled else "[OFF]"
            print(
                f"  {on_off} {route.route_id}: {status}  ({sources} --{direction}--> {dests})"
            )

        route_enabled = sum(1 for r in route_list if r.enabled)
        route_disabled = len(route_list) - route_enabled
        print()
        print(
            f"  {len(route_list)} route(s) configured ({route_enabled} enabled, {route_disabled} disabled)"
        )

    # --- Summary ---
    print()
    if validation_errors:
        print(f"Config has {len(validation_errors)} error(s)")
        sys.exit(EXIT_CONFIG)
    else:
        print("Config valid")
    print(f"  {enabled_count}/{total} adapter(s) enabled")
    if route_list:
        route_enabled = sum(1 for r in route_list if r.enabled)
        print(f"  {route_enabled}/{len(route_list)} route(s) active")
    print(f"  Storage: {storage_backend}")

    # --- Startup topology preview ---
    if enabled_count > 0:
        print()
        print("Startup preview:")
        enabled_ids = sorted(
            aid for _t, aid, rtc in config.adapters.all_configs() if rtc.enabled
        )
        print(f"  Adapters that will start: {', '.join(enabled_ids)}")
        if route_list:
            enabled_route_ids = sorted(r.route_id for r in route_list if r.enabled)
            if enabled_route_ids:
                print(f"  Routes that will activate: {', '.join(enabled_route_ids)}")
        limits = config.limits
        print(
            f"  Limits: max_inflight_deliveries={limits.max_inflight_deliveries}, "
            f"max_inflight_replay_events={limits.max_inflight_replay_events}"
        )


def _adapters() -> None:
    """List available adapter types, SDK availability, and configured adapters."""
    from medre.config.loader import load_config

    print("Adapter types:")

    # Check SDK availability
    for transport, dist_name, import_names in TRANSPORTS:
        installed = False
        for mod_name in import_names:
            try:
                importlib.import_module(mod_name)
                installed = True
                break
            except ImportError:
                pass
        status = "installed" if installed else "not installed"
        print(f"  {transport:14s} SDK ({dist_name}): {status}")

    # Configured adapters from config
    print()
    try:
        config, _source, _paths = load_config(None)
        configured = config.adapters.all_configs()
        if not configured:
            print("No adapters configured.")
            return
        print("Configured adapters:")
        for transport, adapter_id, rtc in configured:
            status = "enabled" if rtc.enabled else "disabled"
            kind = getattr(rtc, "adapter_kind", "real")
            print(f"  {transport}.{adapter_id}: {status} (kind={kind})")
    except Exception:
        print("No config found — run 'medre config sample' to generate one.")
