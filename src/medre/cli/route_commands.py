"""Route-related CLI commands: validate, topology, list."""

from __future__ import annotations

import sys

from medre.config.loader import load_config
from medre.runtime.routes import RouteConfigSet, RouteDirectionality

from .exit_codes import EXIT_CONFIG


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

    if not route_list:
        print("No routes configured.")
        return

    # Collect adapter IDs from config for reference validation.
    known_adapter_ids: set[str] = set()
    for _transport, adapter_id, _rtc in config.adapters.all_configs():
        known_adapter_ids.add(adapter_id)

    # Group issues by route for clearer per-route reporting.
    route_warnings: dict[str, list[str]] = {}
    route_errors: dict[str, list[str]] = {}

    for route in route_list:
        rid = route.route_id
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
    from medre.runtime.route_engine import RouteValidationError as _RVE
    from medre.runtime.route_engine import build_runtime_routes as _build_runtime_routes

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
    for transport, adapter_id, _rtc in config.adapters.all_configs():
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
                policy_parts.append(
                    f"events={','.join(route.policy.allowed_event_types)}"
                )
            if route.policy.sender_allowlist:
                policy_parts.append(
                    f"senders={','.join(route.policy.sender_allowlist)}"
                )
            if route.policy.room_allowlist:
                policy_parts.append(f"rooms={','.join(route.policy.room_allowlist)}")
            if route.policy.channel_allowlist:
                policy_parts.append(
                    f"channels={','.join(route.policy.channel_allowlist)}"
                )
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
    for transport, adapter_id, _rtc in config.adapters.all_configs():
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
            print("    policy:")
            if route.policy.allowed_event_types:
                print(
                    f"      event_types:  [{', '.join(route.policy.allowed_event_types)}]"
                )
            if route.policy.allowed_source_adapters:
                print(
                    f"      src_adapters: [{', '.join(route.policy.allowed_source_adapters)}]"
                )
            if route.policy.allowed_dest_adapters:
                print(
                    f"      dst_adapters: [{', '.join(route.policy.allowed_dest_adapters)}]"
                )
            if route.policy.room_allowlist:
                print(f"      rooms:        [{', '.join(route.policy.room_allowlist)}]")
            if route.policy.channel_allowlist:
                print(
                    f"      channels:     [{', '.join(route.policy.channel_allowlist)}]"
                )
            if route.policy.sender_allowlist:
                print(
                    f"      senders:      [{', '.join(route.policy.sender_allowlist)}]"
                )
