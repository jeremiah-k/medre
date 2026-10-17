"""Author a bridge using production configuration validation and route expansion.

Native fields remain provider-owned. The draft retains the input document's
values and path placeholders; environment overrides affect validation/preview
but are never serialized back into it. No adapters or SDKs are instantiated.
"""

from __future__ import annotations

from dataclasses import dataclass

from medre.adapter_registry import get_adapter_spec
from medre.config._yaml import dump_yaml_config, parse_yaml_config
from medre.config.env import apply_env_overrides
from medre.config.errors import ConfigValidationError
from medre.config.loader import parse_runtime_config
from medre.config.paths import MedrePaths
from medre.config.routes import RouteDirectionality
from medre.core.events.kinds import EventKind
from medre.runtime.adapter_preparation import prepare_adapter_config
from medre.runtime.route_engine import build_runtime_routes
from medre.runtime.route_plan import RoutePlan, RoutePlanLeg, build_route_plan

_TEXT_EVENT_KINDS = (EventKind.MESSAGE_CREATED, EventKind.MESSAGE_TEXT)


@dataclass(frozen=True)
class BridgeRequest:
    """A bounded conversation bridge between two configured adapter instances."""

    source_adapter: str
    dest_adapter: str
    contexts: tuple[tuple[str, str], ...]
    directionality: RouteDirectionality = RouteDirectionality.BIDIRECTIONAL
    route_id: str = "bridge"
    source_origin_label: str | None = None
    dest_origin_label: str | None = None


@dataclass(frozen=True)
class BridgeDraft:
    """Validated YAML and the effective offline plan for the entire document."""

    yaml: str
    plan: RoutePlan


def draft_bridge(
    config_yaml: str, request: BridgeRequest, *, paths: MedrePaths
) -> BridgeDraft:
    """Add a text-message bridge without replacing an existing route.

    Reject unknown/disabled endpoints, unbounded or duplicate source contexts,
    and any blocking error in the complete effective route plan. Bidirectional
    fan-in restrictions and expanded route identities come from the production
    compiler. Loop warnings are retained for review rather than hidden.
    """
    document = parse_yaml_config(config_yaml)
    config = apply_env_overrides(parse_runtime_config(document, paths), paths)
    inventory = {aid: rtc for _, aid, rtc in config.adapters.all_configs()}
    for endpoint in (request.source_adapter, request.dest_adapter):
        if endpoint not in inventory:
            raise ConfigValidationError(f"Unknown adapter instance {endpoint!r}")
        if not inventory[endpoint].enabled:
            raise ConfigValidationError(f"Adapter instance {endpoint!r} is disabled")
    if request.source_adapter == request.dest_adapter:
        raise ConfigValidationError("Choose two different adapter instances")
    if request.route_id in {route.route_id for route in config.routes.routes}:
        raise ConfigValidationError(f"Route {request.route_id!r} already exists")
    if not request.contexts:
        raise ConfigValidationError("At least one explicit context mapping is required")

    context_map = {}
    for source, dest in request.contexts:
        if not all(
            isinstance(value, str) and value.strip() for value in (source, dest)
        ):
            raise ConfigValidationError("Contexts must be non-empty strings")
        if source in context_map:
            raise ConfigValidationError(f"Duplicate source context {source!r}")
        context_map[source] = {"dest_context": dest}
    route = {
        "source_adapters": [request.source_adapter],
        "dest_adapters": [request.dest_adapter],
        "directionality": request.directionality.value,
        "context_map": context_map,
        "policy": {"allowed_event_types": list(_TEXT_EVENT_KINDS)},
    }
    for field in ("source_origin_label", "dest_origin_label"):
        value = getattr(request, field)
        if value is not None:
            route[field] = value
    document["routes"] = {**(document.get("routes") or {}), request.route_id: route}
    text = dump_yaml_config(document)
    effective = apply_env_overrides(
        parse_runtime_config(parse_yaml_config(text), paths), paths
    )
    reference_errors = effective.routes.adapter_reference_errors(
        {aid for _, aid, _ in effective.adapters.all_configs()}
    )
    if reference_errors:
        raise ConfigValidationError(
            "Cannot create bridge: " + "; ".join(reference_errors)
        )
    plan = build_route_plan(effective)
    errors = [
        f"{entry.route_id}: {entry.error}" for entry in plan.routes if entry.error
    ]
    errors.extend(
        f"Exclusive routes {conflict.route_a_id!r} and {conflict.route_b_id!r} overlap"
        for conflict in plan.conflicts
    )
    if errors:
        raise ConfigValidationError("Cannot create bridge: " + "; ".join(errors))
    runtime_routes = tuple(build_runtime_routes(effective.routes))
    for transport, adapter_id, rtc in effective.adapters.all_configs():
        if not rtc.enabled or rtc.config is None:
            continue
        spec = get_adapter_spec(transport)
        if spec is not None:
            # Keep native restrictions in the provider's pure startup hook.
            # Derived values are deliberately not serialized into the draft.
            prepare_adapter_config(
                spec, adapter_id, rtc.config, paths=paths, routes=runtime_routes
            )
    entry = next(entry for entry in plan.routes if entry.route_id == request.route_id)
    if not entry.enabled or not entry.legs:
        raise ConfigValidationError("The new bridge has no active route legs")
    policies = {route.route_id: route.policy for route in effective.routes.routes}
    new_addresses = {
        _leg_address(leg, wildcard_source=wildcard)
        for leg in entry.legs
        for wildcard in (False, True)
    }
    for existing in plan.routes:
        if existing.route_id == request.route_id:
            continue
        policy = policies[existing.route_id]
        if (
            policy
            and policy.allowed_event_types
            and not any(
                kind in policy.allowed_event_types for kind in _TEXT_EVENT_KINDS
            )
        ):
            continue
        if any(_leg_address(old) in new_addresses for old in existing.legs):
            raise ConfigValidationError(
                f"Route {existing.route_id!r} already connects a selected context pair. "
                "Edit that route instead of creating a parallel text bridge."
            )
    return BridgeDraft(text, plan)


def _leg_address(
    leg: RoutePlanLeg, *, wildcard_source: bool = False
) -> tuple[str | None, ...]:
    """Compare targets and source domains without interpreting native addresses."""
    return (
        leg.source_adapter_id,
        None if wildcard_source else leg.source_channel,
        leg.dest_adapter_id,
        leg.dest_channel,
        leg.dest_destination_kind,
        leg.dest_destination_hash,
        leg.dest_destination_name,
    )
