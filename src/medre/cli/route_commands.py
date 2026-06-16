"""Route-related CLI commands: validate, topology, list, plan."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

from medre.config.loader import load_config
from medre.config.routes import RouteConfigSet, RouteDirectionality

from .exit_codes import EXIT_CONFIG, EXIT_OK


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

    adapter_platforms: dict[str, str] = {}
    for _transport, _adapter_id, _rtc in config.adapters.all_configs():
        adapter_platforms[_adapter_id] = _transport

    try:
        _build_runtime_routes(routes, adapter_platforms)
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


def _routes_plan(config_path: str | None, as_json: bool = False) -> None:
    """Render the expanded route plan (offline — no adapter I/O).

    Walks every config route, expands enabled ones into per-leg detail
    (including ``channel_room_map`` fan-out, origin-label provenance, and
    fan-in annotations), and reports detected loops.  Disabled routes
    are listed separately.  Exits nonzero on config or expansion errors.
    """
    try:
        config, _source, _paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    from medre.runtime.route_plan import build_route_plan

    try:
        plan = build_route_plan(config)
    except Exception as exc:  # defensive: expansion should be self-contained
        print(f"Route plan error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    if as_json:
        print(json.dumps(asdict(plan), indent=2, sort_keys=True))
        sys.exit(EXIT_CONFIG if _plan_has_errors(plan) else EXIT_OK)

    _render_route_plan(plan)
    sys.exit(EXIT_CONFIG if _plan_has_errors(plan) else EXIT_OK)


def _plan_has_errors(plan) -> bool:
    """True if any route entry carries a blocking error."""
    return any(entry.error is not None for entry in plan.routes)


def _format_adapter_ref(adapter_id: str, platform: str | None) -> str:
    """Render ``platform:adapter_id`` (or just ``adapter_id``)."""
    if platform:
        return f"{platform}:{adapter_id}"
    return adapter_id


def _format_origin_label(value: str | None, source: str) -> str:
    """Render an origin label with its provenance.

    An empty resolved value is an explicit suppression regardless of
    where it came from, so it is shown as ``explicit_empty`` alongside
    the level.
    """
    shown = "unset" if value is None else repr(value)
    if value == "":
        return f"{shown} (explicit_empty, {source})"
    return f"{shown} ({source})"


def _render_route_plan(plan) -> None:
    """Print the human-readable route plan."""
    print("Route Plan (offline — no adapter I/O)")
    print("=" * 38)
    print()

    # -- Adapters ----------------------------------------------------------
    adapters = plan.adapters
    print(f"Adapters ({len(adapters)}):")
    if not adapters:
        print("  (none configured)")
    for a in adapters:
        status = "[ON] " if a.enabled else "[OFF]"
        print(
            f"  {status} {_format_adapter_ref(a.adapter_id, a.transport)}"
            f'   origin_label="{a.origin_label}"'
        )
    print()

    # -- Routes ------------------------------------------------------------
    enabled_entries = [e for e in plan.routes if e.enabled]
    disabled_entries = [e for e in plan.routes if not e.enabled]
    print(f"Routes ({len(plan.routes)} configured, {plan.total_legs} legs):")
    print()

    if not plan.routes:
        print("  (none configured)")
        print()

    for entry in enabled_entries:
        marker = "[ON]" if entry.enabled else "[OFF]"
        leg_count = len(entry.legs)
        print(
            f"  {entry.route_id} [{entry.directionality}] {marker}"
            f" — {leg_count} leg(s)"
        )
        if entry.error is not None:
            print(f"    \u2717 error: {entry.error}")
        for idx, leg in enumerate(entry.legs, start=1):
            src = _format_adapter_ref(leg.source_adapter_id, leg.source_platform)
            dst = _format_adapter_ref(leg.dest_adapter_id, leg.dest_platform)
            print(f"    Leg {idx}: {src} \u2192 {dst}  [{leg.direction}]")
            if leg.channel_room_map_key is not None:
                room = leg.channel_room_map_room or "?"
                print(
                    f"           channel_room_map: ch={leg.channel_room_map_key} \u2192 {room}"
                )
            elif leg.source_channel is not None or leg.dest_channel is not None:
                parts: list[str] = []
                if leg.source_channel is not None:
                    parts.append(f"source_channel={leg.source_channel}")
                if leg.dest_channel is not None:
                    parts.append(f"dest_channel={leg.dest_channel}")
                if parts:
                    print(f"           {', '.join(parts)}")
            print(
                "           origin_label: "
                + _format_origin_label(
                    leg.source_origin_label, leg.source_origin_label_source
                )
            )
            print(f"           expanded_id: {leg.expanded_route_id}")
        for w in entry.warnings:
            print(f"    \u26a0 {w}")
        print()

    # -- Disabled routes ---------------------------------------------------
    if disabled_entries:
        print(f"Disabled routes ({len(disabled_entries)}):")
        for entry in disabled_entries:
            print(f"  {entry.route_id} [{entry.directionality}] [OFF]")
        print()

    # -- Errors summary ----------------------------------------------------
    error_entries = [e for e in plan.routes if e.error is not None]
    if error_entries:
        print(f"Errors ({len(error_entries)}):")
        for entry in error_entries:
            print(f"  \u2717 {entry.route_id}: {entry.error}")
        print()

    # -- Loops -------------------------------------------------------------
    if plan.loops:
        print("Loops:")
        for loop in plan.loops:
            print(f"  \u26a0 {loop}")
    else:
        print("Loops: none detected")
