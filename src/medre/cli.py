"""MEDRE command-line interface.

Usage::

    medre run [--config PATH]       Start the MEDRE runtime
    medre smoke [--config] [--message TEXT] [--json]
                                    Run fake bridge smoke test
    medre config check [--config]   Validate config file
    medre config sample             Print a sample TOML config
    medre paths                     Print resolved MEDRE paths
    medre version                   Print MEDRE version
    medre adapters                  List available and configured adapters
    medre diagnostics [--config]    Print runtime snapshot JSON (no server)
    medre diagnostics --refresh-health [--config]  Start runtime, refresh adapter health once, print live snapshot
    medre routes validate [--config]  Validate route configuration
    medre routes topology [--config]  Print route topology preview
    medre routes list [--config]      List configured routes
    medre evidence [--config] [--json]  Collect evidence bundle for support

The module also supports ``python -m medre.cli`` via the ``__main__`` block
at the bottom of this file.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import os
import platform
import signal
import sys
import time
from typing import Any, NoReturn

from medre.config.loader import load_config, ConfigSource
from medre.config.sample import generate_sample_config
from medre.config.paths import resolve, MedrePaths
from medre.config.env import apply_env_overrides
from medre.config.errors import ConfigError, ConfigValidationError
from medre.logging import (
    adapter_logger,
    format_duration_ms,
    sanitize_for_log,
    startup_summary,
    shutdown_summary,
)
from medre.runtime.routes import RouteConfigSet, RouteDirectionality

logger = logging.getLogger("medre")


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
"""Successful exit."""
EXIT_CONFIG = 2
"""Config parse or validation error."""
EXIT_BUILD = 3
"""Runtime build error (missing dependency, bad path, adapter construction failure)."""
EXIT_STARTUP = 4
"""Total startup failure (zero adapters started, core subsystem failure)."""
EXIT_NOT_FOUND = 5
"""Requested entity (event, receipt, native ref) not found in storage."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Transport adapter types that medre supports.
_TRANSPORTS: list[tuple[str, str, tuple[str, ...]]] = [
    ("matrix", "mindroom-nio", ("mindroom_nio", "nio")),
    ("meshtastic", "mtjk", ("mtjk", "meshtastic")),
    ("meshcore", "meshcore", ("meshcore",)),
    ("lxmf", "lxmf", ("lxmf", "RNS")),
]


def _get_version() -> str:
    """Return the MEDRE version string."""
    try:
        return importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _version() -> None:
    """Print version, Python, and platform information."""
    version = _get_version()
    print(f"medre {version}")
    print(f"Python  {platform.python_version()}")
    print(f"Platform {platform.system()} {platform.release()} ({platform.machine()})")


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
        print(f"Mode:     MEDRE_HOME")
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

    # Try to load config for adapter state roots.
    try:
        config, _source, _paths = load_config(None)
        adapter_roots = []
        enabled_count = 0
        disabled_count = 0
        for transport, adapter_id, rtc in config.adapters.all_configs():
            if rtc.enabled:
                enabled_count += 1
                adapter_roots.append(
                    f"{transport}.{adapter_id}: {paths.adapter_state_dir(adapter_id)}"
                )
            else:
                disabled_count += 1
                adapter_roots.append(
                    f"{transport}.{adapter_id}: (disabled)"
                )
        if adapter_roots:
            print()
            print(f"Adapter inventory ({enabled_count} enabled, {disabled_count} disabled):")
            for line in adapter_roots:
                print(f"  {line}")
        # Show storage backend and limits.
        storage_backend = config.storage.backend if config.storage else "none"
        limits = config.limits
        print()
        print(f"Storage backend: {storage_backend}")
        print(f"Runtime limits:")
        print(f"  max_inflight_deliveries = {limits.max_inflight_deliveries}")
        print(f"  max_inflight_replay_events = {limits.max_inflight_replay_events}")
        print(f"  drain_timeout = {limits.shutdown_drain_timeout_seconds}s")
        # Show route count.
        route_list = config.routes.routes if config.routes else []
        if route_list:
            route_enabled = sum(1 for r in route_list if r.enabled)
            print(f"Routes: {route_enabled}/{len(route_list)} active")
    except Exception:
        pass  # No config available — skip adapter roots.


def _config_check(config_path: str | None) -> None:
    """Load and validate the config, printing a rich summary."""
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
            print(f"  {transport}.{name}: {status}  (transport={transport}, adapter_kind={kind})")
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

    # --- Validation errors ---
    if validation_errors:
        print()
        print("Validation errors:")
        for err in validation_errors:
            print(err)

    # --- Adapter state roots ---
    enabled_adapters = config.adapters.all_configs()
    enabled_for_roots = [(t, aid, rtc) for t, aid, rtc in enabled_adapters if rtc.enabled]
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
    print(f"  delivery_acquire_timeout_seconds = {limits.delivery_acquire_timeout_seconds}")

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
            print(f"  {on_off} {route.route_id}: {status}  ({sources} --{direction}--> {dests})")

        route_enabled = sum(1 for r in route_list if r.enabled)
        route_disabled = len(route_list) - route_enabled
        print()
        print(f"  {len(route_list)} route(s) configured ({route_enabled} enabled, {route_disabled} disabled)")

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
            enabled_route_ids = sorted(
                r.route_id for r in route_list if r.enabled
            )
            if enabled_route_ids:
                print(f"  Routes that will activate: {', '.join(enabled_route_ids)}")
        limits = config.limits
        print(
            f"  Limits: max_inflight_deliveries={limits.max_inflight_deliveries}, "
            f"max_inflight_replay_events={limits.max_inflight_replay_events}"
        )


def _adapters() -> None:
    """List available adapter types, SDK availability, and configured adapters."""
    print("Adapter types:")

    # Check SDK availability
    for transport, dist_name, import_names in _TRANSPORTS:
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


# ---------------------------------------------------------------------------
# Routes commands
# ---------------------------------------------------------------------------


def _routes_validate(config_path: str | None) -> None:
    """Load config and validate route definitions, printing a summary."""
    try:
        config, source, paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    routes: RouteConfigSet = config.routes
    route_list = routes.routes
    errors: list[str] = []
    warnings: list[str] = []

    if not route_list:
        print("No routes configured.")
        return

    # Collect adapter IDs from config for reference validation.
    known_adapter_ids: set[str] = set()
    for _transport, adapter_id, rtc in config.adapters.all_configs():
        known_adapter_ids.add(adapter_id)

    # Group issues by route for clearer per-route reporting.
    route_warnings: dict[str, list[str]] = {}
    route_errors: dict[str, list[str]] = {}

    for route in route_list:
        rid = route.route_id
        section = f"routes.{rid}"
        rw: list[str] = []
        re_list: list[str] = []

        # Check source adapters exist in config
        for sa in route.source_adapters:
            if sa not in known_adapter_ids:
                if route.enabled:
                    # Unknown adapter in an enabled route is a config error
                    # (matches runtime RouteValidationError semantics).
                    re_list.append(
                        f"source adapter {sa!r} is not defined in any "
                        f"[adapters.<transport>.{sa}] section. "
                        f"Known adapter IDs: {sorted(known_adapter_ids) or '(none)'}"
                    )
                # Disabled routes with unknown refs are not validated.

        # Check dest adapters exist in config
        for da in route.dest_adapters:
            if da not in known_adapter_ids:
                if route.enabled:
                    re_list.append(
                        f"dest adapter {da!r} is not defined in any "
                        f"[adapters.<transport>.{da}] section. "
                        f"Known adapter IDs: {sorted(known_adapter_ids) or '(none)'}"
                    )

        # Check enabled routes have at least one enabled source and dest.
        # Known-but-disabled adapters are warnings, not errors.
        if route.enabled:
            enabled_ids = {aid for aid, rtc in config.adapters.all_enabled()}
            has_enabled_source = any(a in enabled_ids for a in route.source_adapters)
            has_enabled_dest = any(a in enabled_ids for a in route.dest_adapters)
            if not has_enabled_source:
                rw.append(
                    "no enabled source adapters — all source adapter(s) are disabled"
                )
            if not has_enabled_dest:
                rw.append(
                    "no enabled destination adapters — all dest adapter(s) are disabled"
                )

        if rw:
            route_warnings[rid] = rw
        if re_list:
            route_errors[rid] = re_list

    # Validate route expansion and expanded ID uniqueness (matches startup).
    from medre.runtime.route_engine import (
        build_runtime_routes as _build_runtime_routes,
        RouteValidationError as _RVE,
    )
    try:
        _build_runtime_routes(routes)
    except _RVE as exc:
        errors.append(str(exc))

    # Print route-by-route summary
    for route in route_list:
        rid = route.route_id
        status = "enabled" if route.enabled else "disabled"
        direction = route.directionality.value
        sources = ", ".join(route.source_adapters)
        dests = ", ".join(route.dest_adapters)
        marker = "[ON]" if route.enabled else "[OFF]"
        print(f"  {marker} {rid}: {status}  ({sources} --{direction}--> {dests})")

        # Print per-route warnings grouped under the route
        if rid in route_warnings:
            for w in route_warnings[rid]:
                print(f"       \u26a0 {w}")

        # Print per-route errors grouped under the route
        if rid in route_errors:
            for e in route_errors[rid]:
                print(f"       \u2717 {e}")

    # Print cross-route errors (e.g. expansion failures)
    if errors:
        print()
        print("Errors:")
        for e in errors:
            print(f"  \u2717 {e}")

    all_warnings = [w for ws in route_warnings.values() for w in ws]
    all_route_errors = [e for es in route_errors.values() for e in es]
    total_warnings = len(all_warnings)
    total_errors = len(errors) + len(all_route_errors)

    if total_errors:
        print()
        if total_warnings:
            print(
                f"Routes invalid: {total_errors} error(s), "
                f"{total_warnings} warning(s)"
            )
        else:
            print(f"Routes invalid: {total_errors} error(s)")
        sys.exit(EXIT_CONFIG)
    elif total_warnings:
        print()
        print(f"Routes valid with {total_warnings} warning(s)")
    else:
        print()
        print("Routes valid")


def _routes_topology(config_path: str | None) -> None:
    """Load config and print a deterministic topology preview of routes."""
    try:
        config, source, paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    routes: RouteConfigSet = config.routes
    route_list = routes.routes

    if not route_list:
        print("No routes configured.")
        return

    # Build adapter inventory for context
    adapter_map: dict[str, str] = {}  # adapter_id → transport
    for transport, adapter_id, rtc in config.adapters.all_configs():
        adapter_map[adapter_id] = transport

    print("Route topology:")

    for route in route_list:
        rid = route.route_id
        direction = route.directionality

        # Format source side
        source_labels: list[str] = []
        for sa in route.source_adapters:
            t = adapter_map.get(sa, "?")
            source_labels.append(f"{sa}({t})")
        source_str = ", ".join(source_labels)

        # Format dest side
        dest_labels: list[str] = []
        for da in route.dest_adapters:
            t = adapter_map.get(da, "?")
            dest_labels.append(f"{da}({t})")
        dest_str = ", ".join(dest_labels)

        # Direction arrow
        if direction == RouteDirectionality.SOURCE_TO_DEST:
            arrow = "-->"
        elif direction == RouteDirectionality.DEST_TO_SOURCE:
            arrow = "<--"
        else:
            arrow = "<->"

        # Targeting info
        targets: list[str] = []
        if route.source_room:
            targets.append(f"src_room={route.source_room}")
        elif route.source_channel:
            targets.append(f"src_ch={route.source_channel}")
        if route.dest_room:
            targets.append(f"dst_room={route.dest_room}")
        elif route.dest_channel:
            targets.append(f"dst_ch={route.dest_channel}")
        target_str = f"  [{', '.join(targets)}]" if targets else ""

        on_off = "[ON]" if route.enabled else "[OFF]"

        print(f"  {on_off} {rid}")
        print(f"    {source_str} {arrow} {dest_str}{target_str}")

        # Policy summary
        if route.policy and any(
            (
                route.policy.allowed_event_types,
                route.policy.sender_allowlist,
                route.policy.room_allowlist,
                route.policy.channel_allowlist,
            )
        ):
            policy_parts: list[str] = []
            if route.policy.allowed_event_types:
                policy_parts.append(f"events={','.join(route.policy.allowed_event_types)}")
            if route.policy.sender_allowlist:
                policy_parts.append(f"senders={','.join(route.policy.sender_allowlist)}")
            if route.policy.room_allowlist:
                policy_parts.append(f"rooms={','.join(route.policy.room_allowlist)}")
            if route.policy.channel_allowlist:
                policy_parts.append(f"channels={','.join(route.policy.channel_allowlist)}")
            print(f"    policy: {', '.join(policy_parts)}")

        # Filter hooks
        if route.filter_hooks:
            print(f"    hooks: {', '.join(route.filter_hooks)}")

    # Summary
    enabled_count = sum(1 for r in route_list if r.enabled)
    print()
    print(f"  {enabled_count}/{len(route_list)} route(s) active")


def _routes_list(config_path: str | None) -> None:
    """Load config and list all configured routes with status details."""
    try:
        config, source, paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    routes: RouteConfigSet = config.routes
    route_list = routes.routes

    if not route_list:
        print("No routes configured.")
        return

    # Build adapter inventory for cross-reference
    adapter_map: dict[str, str] = {}
    for transport, adapter_id, rtc in config.adapters.all_configs():
        adapter_map[adapter_id] = transport

    print("Configured routes:")
    for route in route_list:
        status = "enabled" if route.enabled else "disabled"
        direction = route.directionality.value
        sources = ", ".join(route.source_adapters)
        dests = ", ".join(route.dest_adapters)

        print(f"  {route.route_id}:")
        print(f"    status:        {status}")
        print(f"    direction:     {direction}")
        print(f"    sources:       [{sources}]")
        print(f"    destinations:  [{dests}]")

        if route.source_room:
            print(f"    source_room:   {route.source_room}")
        elif route.source_channel:
            print(f"    source_channel:{route.source_channel}")
        if route.dest_room:
            print(f"    dest_room:     {route.dest_room}")
        elif route.dest_channel:
            print(f"    dest_channel:  {route.dest_channel}")

        if route.filter_hooks:
            print(f"    filter_hooks:  [{', '.join(route.filter_hooks)}]")

        if route.policy:
            print(f"    policy:")
            if route.policy.allowed_event_types:
                print(f"      event_types:  [{', '.join(route.policy.allowed_event_types)}]")
            if route.policy.allowed_source_adapters:
                print(f"      src_adapters: [{', '.join(route.policy.allowed_source_adapters)}]")
            if route.policy.allowed_dest_adapters:
                print(f"      dst_adapters: [{', '.join(route.policy.allowed_dest_adapters)}]")
            if route.policy.room_allowlist:
                print(f"      rooms:        [{', '.join(route.policy.room_allowlist)}]")
            if route.policy.channel_allowlist:
                print(f"      channels:     [{', '.join(route.policy.channel_allowlist)}]")
            if route.policy.sender_allowlist:
                print(f"      senders:      [{', '.join(route.policy.sender_allowlist)}]")


# ---------------------------------------------------------------------------
# Diagnostics command
# ---------------------------------------------------------------------------


def _diagnostics(config_path: str | None) -> None:
    """Print runtime snapshot JSON using local config/process construction only.

    This command builds the runtime from configuration but does **not** start
    adapters, storage, or any I/O.  It produces a pre-flight snapshot showing
    what the runtime *would* look like: adapter inventory, route topology,
    limits, and config state.  No server, socket, or API is involved.
    """
    import json
    from datetime import datetime, timezone
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
    import json
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
        snapshot = build_runtime_snapshot(app)
        print(json.dumps(snapshot, sort_keys=True, indent=2))
    finally:
        # Always attempt clean shutdown after a successful start.
        try:
            await app.stop()
        except Exception as exc:
            logger.warning("Error during diagnostics shutdown: %s", exc)


# ---------------------------------------------------------------------------
# Inspect commands (read-only storage queries)
# ---------------------------------------------------------------------------


def _struct_to_json(obj: object) -> str:
    """Serialise a msgspec Struct (or list of Structs) to deterministic JSON."""
    import json
    import msgspec

    raw = msgspec.json.encode(obj)
    return json.dumps(json.loads(raw), sort_keys=True, indent=2)


async def _open_readonly_storage(config_path: str | None) -> Any:
    """Load config, resolve DB path, and open storage for read-only inspection.

    Opens the database in strict read-only mode — no file creation, no DDL,
    no schema writes.  Raises ``SystemExit`` on config or storage errors.
    """
    from medre.config.paths import MedrePathsError
    from medre.core.storage.sqlite import SQLiteStorage

    try:
        config, _source, paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    if config.storage.backend == "memory":
        print(
            "Error: storage backend is 'memory' — no persistent data to inspect.",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    if config.storage.path:
        try:
            db_path = str(paths.expand_placeholder(config.storage.path))
        except MedrePathsError as exc:
            print(f"Invalid storage path: {exc}", file=sys.stderr)
            sys.exit(EXIT_CONFIG)
    else:
        db_path = str(paths.database_path)

    try:
        storage = await SQLiteStorage.open_readonly(db_path)
    except Exception as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)
    return storage


async def _inspect_event(config_path: str | None, event_id: str) -> None:
    """Look up and print a canonical event by its ID."""
    storage = await _open_readonly_storage(config_path)
    try:
        event = await storage.get(event_id)
        if event is None:
            print(
                f"Error: event not found: {event_id}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)
        print(_struct_to_json(event))
    finally:
        await storage.close()


async def _inspect_receipts(
    config_path: str | None,
    event_id: str | None,
    replay_run_id: str | None,
) -> None:
    """List delivery receipts for an event or replay run."""
    storage = await _open_readonly_storage(config_path)
    try:
        if event_id is not None:
            receipts = await storage.list_receipts_for_event(event_id)
        elif replay_run_id is not None:
            receipts = await storage.list_receipts_by_replay_run(replay_run_id)
        else:
            print("Error: specify --event or --replay-run", file=sys.stderr)
            sys.exit(EXIT_CONFIG)
        print(_struct_to_json(receipts))
    finally:
        await storage.close()


async def _inspect_native_ref(
    config_path: str | None,
    adapter: str,
    channel: str | None,
    message: str,
) -> None:
    """Resolve a native message reference to a canonical event."""
    import json as _json
    import msgspec as _msgspec

    storage = await _open_readonly_storage(config_path)
    try:
        event_id = await storage.resolve_native_ref(adapter, channel, message)
        if event_id is None:
            print(
                f"Error: native ref not found: adapter={adapter!r}, "
                f"channel={channel!r}, message={message!r}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)
        # Fetch the full event for richer output.
        event = await storage.get(event_id)
        result: dict[str, object] = {
            "adapter": adapter,
            "native_channel_id": channel,
            "native_message_id": message,
            "event_id": event_id,
        }
        if event is not None:
            result["event"] = _json.loads(_msgspec.json.encode(event))
        print(_json.dumps(result, sort_keys=True, indent=2))
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Trace commands (read-only timeline assembly)
# ---------------------------------------------------------------------------


async def _trace_event(
    config_path: str | None,
    event_id: str,
    json_output: bool,
) -> None:
    """Assemble and print a chronological timeline for a single event."""
    import json as _json

    from medre.runtime.trace import assemble_event_timeline, timeline_to_json

    storage = await _open_readonly_storage(config_path)
    try:
        event = await storage.get(event_id)
        if event is None:
            print(
                f"Error: event not found: {event_id}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)

        receipts = await storage.list_receipts_for_event(event_id)
        native_refs = await storage.list_native_refs_for_event(event_id)
        relations = await storage.list_relations(event_id)

        timeline = assemble_event_timeline(event, receipts, native_refs, relations)

        if json_output:
            print(timeline_to_json(timeline))
        else:
            # Human-readable summary.
            print(f"Event timeline: {event_id}")
            print(f"  Kind:    {event.event_kind}")
            print(f"  Source:  {event.source_adapter}")
            print(f"  Entries: {len(timeline)}")
            print()
            for entry in timeline:
                ts = entry["timestamp"]
                etype = entry["entry_type"]
                data = entry["data"]
                if etype == "relation":
                    print(f"  {ts}  [{etype}] {data.get('relation_type', '')}")
                elif etype == "event":
                    print(f"  {ts}  [{etype}] {data.get('event_kind', '')} from {data.get('source_adapter', '')}")
                elif etype == "native_ref":
                    direction = data.get("direction", "")
                    adapter = data.get("adapter", "")
                    msg_id = data.get("native_message_id", "")
                    print(f"  {ts}  [{etype}] {direction} via {adapter}: {msg_id}")
                elif etype == "receipt":
                    status = data.get("status", "")
                    target = data.get("target_adapter", "")
                    attempt = data.get("attempt_number", 1)
                    print(f"  {ts}  [{etype}] {status} -> {target} (attempt {attempt})")
                else:
                    print(f"  {ts}  [{etype}] {data}")
    finally:
        await storage.close()


async def _trace_replay(
    config_path: str | None,
    run_id: str,
    json_output: bool,
) -> None:
    """Assemble and print a chronological timeline for a replay run."""
    import json as _json

    from medre.runtime.trace import assemble_replay_timeline, timeline_to_json

    storage = await _open_readonly_storage(config_path)
    try:
        receipts = await storage.list_receipts_by_replay_run(run_id)
        if not receipts:
            print(
                f"Error: no receipts found for replay run: {run_id}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NOT_FOUND)

        # Build event cache for all referenced events.
        event_ids = list(dict.fromkeys(r.event_id for r in receipts))
        event_cache = {}
        for eid in event_ids:
            event = await storage.get(eid)
            if event is not None:
                event_cache[eid] = event

        result = assemble_replay_timeline(run_id, receipts, event_cache)

        if json_output:
            print(timeline_to_json(result))
        else:
            # Human-readable summary.
            print(f"Replay timeline: {run_id}")
            print(f"  Status:  {result['status']}")
            print(f"  Receipts: {result['receipt_count']}")
            print(f"  Events:  {len(result['event_ids'])}")
            print()
            for entry in result["timeline"]:
                ts = entry["timestamp"]
                etype = entry["entry_type"]
                data = entry["data"]
                if etype == "receipt":
                    status = data.get("status", "")
                    target = data.get("target_adapter", "")
                    eid = data.get("event_id", "")
                    print(f"  {ts}  [{etype}] {status} -> {target} (event: {eid})")
                elif etype == "event_summary":
                    kind = data.get("event_kind", "")
                    src = data.get("source_adapter", "")
                    print(f"  {ts}  [{etype}] {kind} from {src}")
                else:
                    print(f"  {ts}  [{etype}] {data}")
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Replay command (build runtime, use ReplayEngine, do NOT start app)
# ---------------------------------------------------------------------------


# Radio transports that use fire-and-forget delivery.
_RADIO_TRANSPORTS = frozenset({"meshtastic", "meshcore", "lxmf"})

_BEST_EFFORT_WARNING = (
    "WARNING: BEST_EFFORT replay incurs the same duplicate-send risk as "
    "all adapter transports.  Every BEST_EFFORT delivery creates new "
    "DeliveryReceipt and NativeMessageRef records that are NOT "
    "distinguishable from live records at the storage layer.  "
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
    import json as _json
    import time as _time

    from medre.config.paths import MedrePathsError
    from medre.core.storage.replay import (
        ReplayMode,
        ReplayRequest,
        ReplaySummary,
        collect_replay_summary,
    )
    from medre.runtime.builder import RuntimeBuilder

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


# ---------------------------------------------------------------------------
# Recover command (analyze failed deliveries, generate runbook)
# ---------------------------------------------------------------------------


async def _recover(
    config_path: str | None,
    event_id: str | None,
    failed_only: bool,
    since: str | None,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Analyze failed deliveries and generate a recovery runbook."""
    import json as _json
    import uuid

    from medre.core.storage.replay import ReplayMode, ReplayRequest
    from medre.runtime.trace import assemble_event_timeline

    storage = await _open_readonly_storage(config_path)
    try:
        # Determine scope: single event or broad scan.
        failed_targets: list[dict[str, Any]] = []
        timeline: list[dict[str, Any]] = []
        if event_id is not None:
            # Single-event recovery.
            event = await storage.get(event_id)
            if event is None:
                print(
                    f"Error: event not found: {event_id}",
                    file=sys.stderr,
                )
                sys.exit(EXIT_NOT_FOUND)

            receipts = await storage.list_receipts_for_event(event_id)
            native_refs = await storage.list_native_refs_for_event(event_id)
            relations = await storage.list_relations(event_id)

            # Identify failed targets.
            failed_targets = [
                {
                    "target_adapter": r.target_adapter,
                    "status": r.status,
                    "attempt_number": r.attempt_number,
                    "receipt_id": r.receipt_id,
                }
                for r in receipts
                if r.status in ("failed", "dead_lettered")
            ]

            # Build timeline for runbook.
            timeline = assemble_event_timeline(
                event, receipts, native_refs, relations,
            )

            runbook: dict[str, Any] = {
                "scope": "event",
                "event_id": event_id,
                "event_kind": event.event_kind,
                "source_adapter": event.source_adapter,
                "total_receipts": len(receipts),
                "failed_targets": failed_targets,
                "timeline": timeline,
                "warnings": [],
            }

            # Add duplicate-send risk warnings for radio transports.
            for nref in native_refs:
                if nref.adapter.lower() in _RADIO_TRANSPORTS or any(
                    nref.adapter.lower().startswith(t)
                    for t in _RADIO_TRANSPORTS
                ):
                    runbook["warnings"].append(
                        f"Adapter {nref.adapter} uses a radio transport — "
                        f"recovery may produce duplicate sends. "
                        f"Use --dry-run first to preview."
                    )
                    break

            if runbook["warnings"]:
                runbook["warnings"].append(
                    "Radio transports (Meshtastic, MeshCore, LXMF) use "
                    "fire-and-forget delivery.  Recovery is best-effort "
                    "and duplicates are possible."
                )

        else:
            # Broad scan: list events with failed receipts.
            runbook = {
                "scope": "scan",
                "failed_only": failed_only,
                "since": since,
                "warnings": [
                    "Specify --event <event_id> for a detailed recovery runbook.",
                    "Radio transports (Meshtastic, MeshCore, LXMF) use "
                    "fire-and-forget delivery.  Recovery is best-effort "
                    "and duplicates are possible.",
                ],
                "note": "Use --event <event_id> --dry-run to preview recovery.",
            }

        # If --dry-run, include a replay preview section.
        if dry_run and event_id is not None:
            runbook["dry_run"] = {
                "mode": "dry_run",
                "event_id": event_id,
                "status": "preview",
                "message": (
                    "DRY_RUN replay would re-process this event through "
                    "all pipeline stages except delivery.  Use "
                    "'medre replay --mode dry_run --event <event_id>' "
                    "to execute."
                ),
            }

        if json_output:
            print(_json.dumps(runbook, sort_keys=True, indent=2, default=str))
        else:
            # Human-readable runbook.
            if event_id is not None:
                print(f"Recovery runbook: {event_id}")
                print(f"  Kind:    {runbook['event_kind']}")
                print(f"  Source:  {runbook['source_adapter']}")
                print(f"  Receipts: {runbook['total_receipts']}")
                if failed_targets:
                    print(f"  Failed targets ({len(failed_targets)}):")
                    for ft in failed_targets:
                        print(
                            f"    {ft['target_adapter']}: {ft['status']} "
                            f"(attempt {ft['attempt_number']})"
                        )
                else:
                    print("  Failed targets: none")
                print(f"  Timeline entries: {len(timeline)}")
            else:
                print("Recovery scan")
                print(f"  Failed-only: {failed_only}")
                print(f"  Since: {since or '(all)'}")

            if runbook.get("warnings"):
                print()
                for w in runbook["warnings"]:
                    print(f"  \u26a0 {w}")

            if dry_run and event_id is not None:
                print()
                print("  DRY RUN: No side effects. Preview only.")
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Run command (async)
# ---------------------------------------------------------------------------

shutdown_requested: bool = False


def _request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested  # noqa: PLW0603
    shutdown_requested = True
    logger.info("Received signal %s — requesting shutdown", signal.Signals(signum).name)


async def _run(config_path: str | None) -> None:
    """Load config, build the runtime, and run until interrupted."""
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
        )
        sys.exit(EXIT_CONFIG)

    # Configure logging from the loaded config
    _setup_logging(config)

    adapter_ids = [aid for aid, _rtc in enabled_adapters]
    logger.info("MEDRE starting — config source: %s", source.value)
    logger.info("Config path: %s", paths.config_file)
    logger.info("State dir:   %s", paths.state_dir)

    # Print startup header to console with adapter inventory.
    print(f"Runtime starting with {len(enabled_adapters)} adapter(s): {', '.join(adapter_ids)}")

    # Show disabled adapters for visibility.
    all_cfgs = config.adapters.all_configs()
    disabled = [(t, aid) for t, aid, rtc in all_cfgs if not rtc.enabled]
    if disabled:
        print(f"  Disabled adapters: {', '.join(f'{t}.{aid}' for t, aid in disabled)}")

    builder = RuntimeBuilder(config, paths)
    try:
        app = builder.build()
    except Exception as exc:
        print(f"Runtime build error: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)

    # Report build failures before startup.
    if app.build_failures:
        print(f"  Build failures ({len(app.build_failures)}):")
        for bf in app.build_failures:
            print(f"    \u2717 {bf.transport}.{bf.adapter_id}: {bf.error}")

    # If ALL enabled adapters failed construction there is nothing to start.
    # Exit with EXIT_BUILD (3) — this is a build-phase failure, not startup.
    if not app.adapters:
        print(
            f"\nRuntime build error: all {len(app.build_failures)} enabled "
            "adapter(s) failed to construct",
            file=sys.stderr,
        )
        sys.exit(EXIT_BUILD)

    # Show route inventory.
    route_list = config.routes.routes if config.routes else []
    enabled_routes = [r for r in route_list if r.enabled]
    disabled_routes = [r for r in route_list if not r.enabled]
    if route_list:
        print(
            f"  Routes: {len(enabled_routes)} enabled, "
            f"{len(disabled_routes)} disabled "
            f"({len(route_list)} total)"
        )

    # Show storage backend.
    storage_backend = config.storage.backend if config.storage else "none"
    print(f"  Storage: {storage_backend}")

    # Show runtime limits.
    limits = config.limits
    print(
        f"  Limits: max_inflight_deliveries={limits.max_inflight_deliveries}, "
        f"max_inflight_replay_events={limits.max_inflight_replay_events}, "
        f"drain_timeout={limits.shutdown_drain_timeout_seconds}s"
    )

    # Install signal handlers for clean shutdown
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    # Track per-adapter startup timing
    run_start = time.monotonic()
    startup_results: list[tuple[str, str, bool, float, str | None]] = []

    try:
        await app.start()
    except Exception as exc:
        # RuntimeStartupError means total failure (zero adapters).
        # Print what we know and exit with a startup-specific code.
        from medre.runtime.errors import RuntimeStartupError

        # Report per-adapter results from app state.
        for adapter_id in app.started_adapter_ids:
            dur = time.monotonic() - run_start
            startup_results.append(
                (adapter_id, _transport_for_adapter(adapter_id, config), True, dur, None)
            )
        for adapter_id in app._failed_adapter_ids:
            dur = time.monotonic() - run_start
            startup_results.append(
                (adapter_id, _transport_for_adapter(adapter_id, config), False, dur, str(exc))
            )

        summary = startup_summary(startup_results)
        print(summary)
        if isinstance(exc, RuntimeStartupError):
            print(f"\nRuntime startup failed: {exc}")
            sys.exit(EXIT_STARTUP)
        # Unexpected exception during startup (core subsystem failure).
        print(f"\nRuntime startup failed: {exc}", file=sys.stderr)
        sys.exit(EXIT_STARTUP)

    # Build startup results from app state (supports partial startup).
    for adapter_id in app.started_adapter_ids:
        dur = time.monotonic() - run_start
        startup_results.append(
            (adapter_id, _transport_for_adapter(adapter_id, config), True, dur, None)
        )
    for adapter_id in app._failed_adapter_ids:
        dur = time.monotonic() - run_start
        startup_results.append(
            (adapter_id, _transport_for_adapter(adapter_id, config), False, dur, "failed during startup")
        )

    # Print startup summary
    summary = startup_summary(startup_results)
    print(summary)

    # Print boot summary diagnostics if available.
    if app.boot_summary is not None:
        bs = app.boot_summary
        if bs.runtime_health == "degraded":
            print(f"  \u26a0 Runtime is DEGRADED: {bs.adapters_started}/{bs.adapters_total} adapter(s) started")
            if bs.failed_adapter_ids:
                print(f"    Failed adapters: {', '.join(bs.failed_adapter_ids)}")
        if bs.persisted_events_count is not None:
            print(f"  Persisted events: {bs.persisted_events_count}")
        if bs.adapters_disabled > 0:
            print(f"  Disabled adapters: {bs.adapters_disabled}")

    # Log structured startup
    logger.info(
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
        print("Runtime shutting down")
        logger.info("Runtime shutting down")

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

        # Print drain outcome
        if drain_outcome == "completed":
            print(f"  Drain completed (timeout={drain_timeout}s)")
        else:
            print(
                f"  Drain timed out after {drain_timeout}s "
                f"({abandoned_count} adapter(s) abandoned)"
            )

        # Per-adapter shutdown messages
        for adapter_id in app.adapters:
            alog = adapter_logger("medre.adapters", adapter_id, _transport_for_adapter(adapter_id, config))
            alog.info("Adapter %s stopped", adapter_id)
            print(f"  stopped {adapter_id}")

        summary = shutdown_summary(list(app.adapters.keys()), shutdown_errors or None)
        print(summary)
        logger.info("MEDRE stopped")


def _transport_for_adapter(adapter_id: str, config: object) -> str:
    """Look up the transport type for an adapter_id from config."""
    adapters = getattr(config, "adapters", None)
    if adapters is None:
        return "unknown"
    for transport in ("matrix", "meshtastic", "meshcore", "lxmf"):
        group = getattr(adapters, transport, {})
        for _name, rtc in group.items():
            if rtc.adapter_id == adapter_id:
                return transport
    return "unknown"


def _setup_logging(config: object) -> None:
    """Apply logging configuration from the parsed config."""
    log_cfg = getattr(config, "logging", None)
    if log_cfg is None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        return

    level = getattr(log_cfg, "level", "INFO")
    fmt = getattr(log_cfg, "format", None) or "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt)


# ---------------------------------------------------------------------------
# Smoke command (async)
# ---------------------------------------------------------------------------


async def _smoke(
    config_path: str | None,
    message_text: str,
    json_output: bool,
    storage_path: str | None = None,
    drill_name: str | None = None,
) -> None:
    """Run fake bridge smoke test and print a compact evidence report.

    Builds and starts the runtime with fake adapters, injects one
    ``message.text`` event through the full pipeline, inspects storage
    evidence, and prints a PASS/FAIL report.  Docker-free, network-free.

    Exit codes: 0 on PASS, 1 on FAIL.
    """
    import json as _json

    if drill_name is not None:
        from medre.runtime.drill import run_drill
        report = await run_drill(
            drill_name,
            config_path=config_path,
            storage_path=storage_path,
        )
    else:
        from medre.runtime.smoke import run_fake_bridge_smoke
        report = await run_fake_bridge_smoke(
            config_path,
            message_text=message_text,
            storage_path=storage_path,
        )

    if json_output:
        print(_json.dumps(report, sort_keys=True, indent=2))
    else:
        # Human-readable summary
        status = report["status"]
        event_id = report.get("event_id", "N/A")
        source = report.get("source_adapter", "N/A")
        targets = report.get("target_adapters", [])
        routes = report.get("route_ids", [])
        acc = report.get("accounting", {})
        n_receipts = len(report.get("delivery_receipts", []))
        n_refs = len(report.get("native_refs", []))

        if status == "PASS":
            print(f"Fake bridge smoke: PASS")
        else:
            print(f"Fake bridge smoke: FAIL")
            reasons = report.get("fail_reasons", [])
            for r in reasons:
                print(f"  \u2717 {r}")

        print(f"  Event:       {event_id}")
        print(f"  Source:      {source}")
        print(f"  Targets:     {', '.join(targets) if targets else '(none)'}")
        print(f"  Routes:      {', '.join(routes) if routes else '(none)'}")
        print(f"  Receipts:    {n_receipts}")
        print(f"  Native refs: {n_refs}")
        if acc:
            print(f"  Accounting:  inbound={acc.get('inbound_accepted', 0)} delivered={acc.get('outbound_delivered', 0)} failed={acc.get('outbound_failed', 0)}")

        # Storage info
        sp = report.get("storage_path")
        if sp:
            print(f"  Storage:     {sp}")

        # Print one limitation as a reminder.
        limitations = report.get("limitations", [])
        if limitations:
            print(f"  Note: {limitations[0]}")

    sys.exit(0 if report["status"] == "PASS" else 1)


# ---------------------------------------------------------------------------
# Evidence command (async)
# ---------------------------------------------------------------------------


async def _evidence(
    config_path: str | None,
    json_output: bool,
    event_id: str | None,
    replay_run_id: str | None,
    include_refresh_health: bool,
) -> None:
    """Collect and print an evidence bundle."""
    import json as _json

    from medre.runtime.evidence import collect_evidence_bundle

    report = await collect_evidence_bundle(
        config_path,
        event_id=event_id,
        replay_run_id=replay_run_id,
        include_refresh_health=include_refresh_health,
    )

    if json_output:
        print(_json.dumps(report, sort_keys=True, indent=2))
    else:
        # Human-readable summary.
        status = report["status"]
        if status == "ok":
            print("Evidence: OK")
        elif status == "partial":
            print("Evidence: PARTIAL (some sections incomplete)")
        else:
            print("Evidence: ERROR")

        print(f"  Collected at:  {report['collected_at']}")
        print(f"  Version:       {report['medre_version']}")
        print(f"  Config source: {report.get('config_source', 'N/A')}")
        print(f"  Runtime started: {report['runtime_started']}")

        sections = report.get("sections", {})
        for name, section in sorted(sections.items()):
            sec_status = section.get("status", "unknown")
            marker = {
                "ok": "\u2713",
                "partial": "\u26a0",
                "error": "\u2717",
                "skipped": "-",
            }.get(sec_status, "?")
            print(f"  {marker} {name}: {sec_status}")
            if section.get("error"):
                print(f"      {section['error']}")

        errors = report.get("errors", [])
        if errors:
            print()
            print(f"Errors ({len(errors)}):")
            for err in errors:
                print(f"  \u2717 {err}")

    # Exit 0 for ok/partial, EXIT_CONFIG for config load failure.
    if report["status"] == "error":
        sys.exit(EXIT_CONFIG)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medre",
        description="Modular Event-driven Routing Engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Start the MEDRE runtime")
    run_p.add_argument("--config", default=None, help="Path to config file")

    # config (with sub-subcommands)
    config_p = sub.add_parser("config", help="Config management commands")
    config_sub = config_p.add_subparsers(dest="config_command", required=True)
    check_p = config_sub.add_parser("check", help="Validate config file")
    check_p.add_argument("--config", default=None, help="Path to config file")
    config_sub.add_parser("sample", help="Print sample config")

    # paths
    sub.add_parser("paths", help="Print resolved MEDRE paths")

    # version
    sub.add_parser("version", help="Print MEDRE version")

    # adapters
    sub.add_parser("adapters", help="List available and configured adapters")

    # diagnostics
    diag_p = sub.add_parser("diagnostics", help="Print runtime snapshot JSON (no server)")
    diag_p.add_argument("--config", default=None, help="Path to config file")
    diag_p.add_argument(
        "--refresh-health",
        action="store_true",
        default=False,
        help="Start runtime, refresh adapter health once, print live snapshot",
    )

    # routes (with sub-subcommands)
    routes_p = sub.add_parser("routes", help="Route management commands")
    routes_sub = routes_p.add_subparsers(dest="routes_command", required=True)
    routes_validate_p = routes_sub.add_parser("validate", help="Validate route configuration")
    routes_validate_p.add_argument("--config", default=None, help="Path to config file")
    routes_topology_p = routes_sub.add_parser("topology", help="Print route topology preview")
    routes_topology_p.add_argument("--config", default=None, help="Path to config file")
    routes_list_p = routes_sub.add_parser("list", help="List configured routes")
    routes_list_p.add_argument("--config", default=None, help="Path to config file")

    # smoke
    smoke_p = sub.add_parser("smoke", help="Run fake bridge smoke test")
    smoke_p.add_argument("--config", default=None, help="Path to config file (default: examples/configs/fake-bridge-smoke.toml)")
    smoke_p.add_argument("--message", default="medre smoke test", help="Text for test message")
    smoke_p.add_argument("--storage-path", default=None, metavar="PATH",
        help="Persist smoke evidence to SQLite at this path (default: in-memory)")
    smoke_p.add_argument("--drill", default=None, metavar="NAME",
        help="Run named failure drill instead of normal smoke")
    smoke_p.add_argument("--json", action="store_true", default=False, help="Output JSON report")

    # evidence
    evidence_p = sub.add_parser("evidence", help="Collect evidence bundle for support")
    evidence_p.add_argument("--config", default=None, help="Path to config file")
    evidence_p.add_argument("--json", action="store_true", default=False, help="Output JSON report")
    evidence_p.add_argument("--event", default=None, metavar="EVENT_ID",
        help="Include event and delivery receipts from storage")
    evidence_p.add_argument("--replay-run", default=None, metavar="RUN_ID",
        help="Include delivery receipts for a replay run from storage")
    evidence_p.add_argument("--include-refresh-health", action="store_true", default=False,
        help="Start runtime once to refresh live adapter health")

    # inspect (with sub-subcommands)
    inspect_p = sub.add_parser("inspect", help="Read-only storage inspection")
    inspect_sub = inspect_p.add_subparsers(dest="inspect_command", required=True)

    # inspect event <event_id>
    inspect_evt = inspect_sub.add_parser("event", help="Inspect a canonical event")
    inspect_evt.add_argument("--config", default=None, help="Path to config file")
    inspect_evt.add_argument("event_id", help="Canonical event ID to look up")

    # inspect receipts (--event <id> | --replay-run <run_id>)
    inspect_rcpt = inspect_sub.add_parser("receipts", help="List delivery receipts")
    inspect_rcpt.add_argument("--config", default=None, help="Path to config file")
    inspect_rcpt_group = inspect_rcpt.add_mutually_exclusive_group(required=True)
    inspect_rcpt_group.add_argument(
        "--event", default=None, help="Event ID to query receipts for",
    )
    inspect_rcpt_group.add_argument(
        "--replay-run", default=None, help="Replay run ID to query receipts for",
    )

    # inspect native-ref --adapter A --message M [--channel C]
    inspect_nref = inspect_sub.add_parser("native-ref", help="Resolve native ref to canonical event")
    inspect_nref.add_argument("--config", default=None, help="Path to config file")
    inspect_nref.add_argument("--adapter", required=True, help="Adapter name")
    inspect_nref.add_argument("--channel", default=None, help="Native channel ID (omit for channelless protocols)")
    inspect_nref.add_argument("--message", required=True, help="Native message ID")

    # trace (with sub-subcommands)
    trace_p = sub.add_parser("trace", help="Chronological timeline assembly")
    trace_sub = trace_p.add_subparsers(dest="trace_command", required=True)

    # trace event <event_id>
    trace_evt = trace_sub.add_parser("event", help="Assemble timeline for a canonical event")
    trace_evt.add_argument("--config", default=None, help="Path to config file")
    trace_evt.add_argument("event_id", help="Canonical event ID to trace")
    trace_evt.add_argument("--json", action="store_true", default=False, help="Output JSON timeline")

    # trace replay <run_id>
    trace_rpl = trace_sub.add_parser("replay", help="Assemble timeline for a replay run")
    trace_rpl.add_argument("--config", default=None, help="Path to config file")
    trace_rpl.add_argument("run_id", help="Replay run ID to trace")
    trace_rpl.add_argument("--json", action="store_true", default=False, help="Output JSON timeline")

    # replay
    replay_p = sub.add_parser("replay", help="Execute a replay operation")
    replay_p.add_argument("--config", default=None, help="Path to config file")
    replay_p.add_argument(
        "--mode", required=True,
        choices=["strict", "re_render", "re_route", "best_effort", "dry_run"],
        help="Replay mode",
    )
    replay_p.add_argument("--event", default=None, metavar="EVENT_ID", help="Event ID to replay")
    replay_p.add_argument("--json", action="store_true", default=False, help="Output JSON report")
    replay_p.add_argument(
        "--target-adapters", default=None, nargs="+", metavar="ADAPTER",
        help="Restrict replay to these adapter IDs",
    )
    replay_p.add_argument(
        "--route-ids", default=None, nargs="+", metavar="ROUTE",
        help="Restrict replay to these route IDs",
    )
    replay_p.add_argument("--limit", type=int, default=100, help="Max events to replay (default: 100)")

    # recover
    recover_p = sub.add_parser("recover", help="Analyze failed deliveries and generate recovery runbook")
    recover_p.add_argument("--config", default=None, help="Path to config file")
    recover_p.add_argument("--event", default=None, metavar="EVENT_ID", help="Event ID to analyze")
    recover_p.add_argument(
        "--failed-only", action="store_true", default=False,
        help="Only include events with failed deliveries",
    )
    recover_p.add_argument("--since", default=None, metavar="TIMESTAMP", help="Only consider events after this timestamp")
    recover_p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Preview recovery without side effects",
    )
    recover_p.add_argument("--json", action="store_true", default=False, help="Output JSON runbook")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to the appropriate command handler."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        import asyncio

        try:
            asyncio.run(_run(args.config))
        except KeyboardInterrupt:
            pass
    elif args.command == "config":
        if args.config_command == "check":
            _config_check(args.config)
        elif args.config_command == "sample":
            print(generate_sample_config())
    elif args.command == "paths":
        _paths()
    elif args.command == "version":
        _version()
    elif args.command == "adapters":
        _adapters()
    elif args.command == "diagnostics":
        import asyncio

        if getattr(args, "refresh_health", False):
            asyncio.run(_diagnostics_refresh(args.config))
        else:
            _diagnostics(args.config)
    elif args.command == "routes":
        if args.routes_command == "validate":
            _routes_validate(args.config)
        elif args.routes_command == "topology":
            _routes_topology(args.config)
        elif args.routes_command == "list":
            _routes_list(args.config)
    elif args.command == "smoke":
        import asyncio

        asyncio.run(
            _smoke(args.config, args.message, args.json,
                   storage_path=args.storage_path, drill_name=args.drill)
        )
    elif args.command == "inspect":
        import asyncio

        if args.inspect_command == "event":
            asyncio.run(_inspect_event(args.config, args.event_id))
        elif args.inspect_command == "receipts":
            asyncio.run(
                _inspect_receipts(
                    args.config,
                    event_id=args.event,
                    replay_run_id=args.replay_run,
                )
            )
        elif args.inspect_command == "native-ref":
            asyncio.run(
                _inspect_native_ref(
                    args.config,
                    adapter=args.adapter,
                    channel=args.channel,
                    message=args.message,
                )
            )
    elif args.command == "evidence":
        import asyncio

        asyncio.run(
            _evidence(
                args.config,
                args.json,
                getattr(args, "event", None),
                getattr(args, "replay_run", None),
                args.include_refresh_health,
            )
        )
    elif args.command == "trace":
        import asyncio

        if args.trace_command == "event":
            asyncio.run(_trace_event(args.config, args.event_id, args.json))
        elif args.trace_command == "replay":
            asyncio.run(_trace_replay(args.config, args.run_id, args.json))
    elif args.command == "replay":
        import asyncio

        asyncio.run(
            _replay(
                args.config,
                mode=args.mode,
                event_id=args.event,
                json_output=args.json,
                target_adapters=args.target_adapters,
                route_ids=args.route_ids,
                limit=args.limit,
            )
        )
    elif args.command == "recover":
        import asyncio

        asyncio.run(
            _recover(
                args.config,
                event_id=args.event,
                failed_only=args.failed_only,
                since=args.since,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        )


if __name__ == "__main__":
    main()
