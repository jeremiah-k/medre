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
from medre.config.model import RuntimeConfig
from medre.config.paths import MedrePaths
from medre.config.routes import RouteConfig, RouteDirectionality
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
    replace_route: bool = False


@dataclass(frozen=True)
class BridgeDraft:
    """Validated YAML and the effective offline plan for the entire document."""

    yaml: str
    plan: RoutePlan


def precheck_bridge(
    config: RuntimeConfig, request: BridgeRequest, *, require_contexts: bool = True
) -> None:
    """Reject request preconditions that are independent of any context value.

    Checks that both endpoints exist and are enabled, that the endpoints
    differ, that the route ID is unused (or exists for explicit replacement),
    and — unless *require_contexts* is
    False — that at least one context mapping is supplied. Importable and
    callable without running the full draft; :func:`draft_bridge` calls it
    before any context-level validation so callers can reject structural
    mistakes before collecting contexts.
    """
    inventory = {aid: rtc for _, aid, rtc in config.adapters.all_configs()}
    for endpoint in (request.source_adapter, request.dest_adapter):
        if endpoint not in inventory:
            raise ConfigValidationError(f"Unknown adapter instance {endpoint!r}")
        if not inventory[endpoint].enabled:
            raise ConfigValidationError(f"Adapter instance {endpoint!r} is disabled")
    if request.source_adapter == request.dest_adapter:
        raise ConfigValidationError("Choose two different adapter instances")
    existing = request.route_id in {route.route_id for route in config.routes.routes}
    if existing and not request.replace_route:
        raise ConfigValidationError(f"Route {request.route_id!r} already exists")
    if request.replace_route and not existing:
        raise ConfigValidationError(
            f"Cannot replace missing route {request.route_id!r}"
        )
    RouteConfig.from_dict(
        request.route_id,
        {
            "source_adapters": [request.source_adapter],
            "dest_adapters": [request.dest_adapter],
            "directionality": request.directionality.value,
        },
    )
    if require_contexts and not request.contexts:
        raise ConfigValidationError("At least one explicit context mapping is required")


def _checked_plan(config: RuntimeConfig, paths: MedrePaths) -> RoutePlan:
    """Validate complete configuration before classifying request failures."""
    reference_errors = config.routes.adapter_reference_errors(
        {aid for _, aid, _ in config.adapters.all_configs()}
    )
    if reference_errors:
        raise ConfigValidationError(
            "Cannot create bridge: " + "; ".join(reference_errors)
        )
    plan = build_route_plan(config)
    errors = [
        f"{entry.route_id}: {entry.error}" for entry in plan.routes if entry.error
    ]
    errors.extend(
        f"Exclusive routes {conflict.route_a_id!r} and {conflict.route_b_id!r} overlap"
        for conflict in plan.conflicts
    )
    if errors:
        raise ConfigValidationError("Cannot create bridge: " + "; ".join(errors))
    runtime_routes = tuple(build_runtime_routes(config.routes))
    for transport, adapter_id, rtc in config.adapters.all_configs():
        if not rtc.enabled or rtc.config is None:
            continue
        spec = get_adapter_spec(transport)
        if spec is not None:
            # Keep native restrictions in the provider's pure startup hook.
            # Derived values are deliberately not serialized into the draft.
            prepare_adapter_config(
                spec, adapter_id, rtc.config, paths=paths, routes=runtime_routes
            )
    return plan


class BridgeAuthoring:
    """Validate a template once, then try bounded bridges without losing answers.

    Construction rejects errors in existing routes and provider settings. A
    subsequent draft failure can therefore be corrected in the new request.
    The raw template is retained separately from effective environment values.
    """

    def __init__(self, config_yaml: str, *, paths: MedrePaths) -> None:
        self._paths = paths
        self._document = parse_yaml_config(config_yaml)
        self.config = apply_env_overrides(
            parse_runtime_config(self._document, paths), paths
        )
        _checked_plan(self.config, paths)

    def precheck(
        self, request: BridgeRequest, *, require_contexts: bool = True
    ) -> dict | None:
        """Validate template-specific replacement constraints before prompting."""
        precheck_bridge(self.config, request, require_contexts=require_contexts)
        original: dict | None = None
        if request.replace_route:
            # Do not flatten arbitrary route policy, structured destinations,
            # retry settings or operator extensions into a text-only bridge.
            original = (self._document.get("routes") or {})[request.route_id]
            accepted = {
                "source_adapters",
                "dest_adapters",
                "directionality",
                "context_map",
                "policy",
                "source_origin_label",
                "dest_origin_label",
            }
            if (
                not isinstance(original, dict)
                or set(original) - accepted
                or not isinstance(original.get("context_map"), dict)
                or any(
                    not isinstance(entry, dict) or set(entry) != {"dest_context"}
                    for entry in original.get("context_map", {}).values()
                )
                or original.get("policy")
                != {"allowed_event_types": list(_TEXT_EVENT_KINDS)}
            ):
                raise ConfigValidationError(
                    f"Route {request.route_id!r} is not a simple guided text "
                    "bridge; edit its advanced settings manually"
                )
            if original.get("source_adapters") != [
                request.source_adapter
            ] or original.get("dest_adapters") != [request.dest_adapter]:
                raise ConfigValidationError(
                    "Cannot change the endpoints of a guided route in place; "
                    "create a new route instead"
                )

        return original

    def draft(self, request: BridgeRequest) -> BridgeDraft:
        """Return complete validated YAML and its plan, preserving the template."""
        document = dict(self._document)
        paths = self._paths
        original = self.precheck(request)

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
            if value is None and request.replace_route:
                # Omission means "keep" when editing, not "erase attribution".
                value = original.get(field) if original is not None else None
            if value is not None:
                route[field] = value
        document["routes"] = {**(document.get("routes") or {}), request.route_id: route}
        text = dump_yaml_config(document)
        effective = apply_env_overrides(
            parse_runtime_config(parse_yaml_config(text), paths), paths
        )
        plan = _checked_plan(effective, paths)
        entry = next(
            entry for entry in plan.routes if entry.route_id == request.route_id
        )
        if not entry.enabled or not entry.legs:
            raise ConfigValidationError("The new bridge has no active route legs")
        policies = {route.route_id: route.policy for route in effective.routes.routes}
        new_addresses = {
            _leg_address(leg, wildcard_source=wildcard, wildcard_dest=wildcard_dest)
            for leg in entry.legs
            for wildcard in (False, True)
            for wildcard_dest in (False, True)
        }
        for existing in plan.routes:
            if not existing.enabled or existing.route_id == request.route_id:
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


def draft_bridge(
    config_yaml: str, request: BridgeRequest, *, paths: MedrePaths
) -> BridgeDraft:
    """Author one bridge through the same session used by the interactive CLI."""
    return BridgeAuthoring(config_yaml, paths=paths).draft(request)


def _leg_address(
    leg: RoutePlanLeg, *, wildcard_source: bool = False, wildcard_dest: bool = False
) -> tuple[str | None, ...]:
    """Compare targets and source domains without interpreting native addresses."""
    return (
        leg.source_adapter_id,
        None if wildcard_source else leg.source_channel,
        leg.dest_adapter_id,
        None if wildcard_dest else leg.dest_channel,
        leg.dest_destination_kind,
        leg.dest_destination_hash,
        leg.dest_destination_name,
    )
