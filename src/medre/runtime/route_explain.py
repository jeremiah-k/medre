"""Explain configured route filters through the production source matcher.

Purely offline: no native transport, runtime startup, or live capability probe.
Known route-policy inputs are evaluated, but no delivery outcome is promised.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace

from medre.config.errors import ConfigValidationError
from medre.config.model import RuntimeConfig
from medre.core.events.kinds import KNOWN_KINDS
from medre.core.policies.route_policy import evaluate_route_policy
from medre.core.routing.router import Router
from medre.runtime.route_engine import build_runtime_routes


@dataclass(frozen=True)
class ExplainedLeg:
    """An expanded route's source-match result and candidate destinations."""

    route_id: str
    result: str
    reason: str
    targets: tuple[dict[str, str | None], ...]


@dataclass(frozen=True)
class RouteExplanation:
    """Structured, explicitly scoped offline route explanation."""

    source_adapter: str
    source_context: str
    event_kind: str
    sender_id: str | None
    matches: tuple[ExplainedLeg, ...]
    excluded: tuple[ExplainedLeg, ...]
    scope: str = "offline_source_matching_only"
    caveat: str = (
        "Source matching and configured policy checks do not prove delivery: "
        "destination capabilities, runtime readiness, identity, duplicate "
        "suppression, and native transport delivery require separate evidence."
    )

    def to_dict(self) -> dict:
        """Return a stable JSON-safe report without runtime objects."""
        return asdict(self)


def explain_routes(
    config: RuntimeConfig,
    *,
    source_adapter: str,
    source_context: str,
    event_kind: str,
    sender_id: str | None = None,
) -> RouteExplanation:
    """Classify expanded routes with the actual runtime source matcher."""
    inventory = {aid: rtc for _, aid, rtc in config.adapters.all_configs()}
    if source_adapter not in inventory:
        raise ConfigValidationError(f"Unknown adapter instance {source_adapter!r}")
    if not inventory[source_adapter].enabled:
        raise ConfigValidationError(f"Adapter {source_adapter!r} is disabled")

    event = SimpleNamespace(
        source_adapter=source_adapter,
        source_channel_id=source_context,
        event_kind=event_kind,
        source_transport_id=sender_id if sender_id is not None else "",
    )
    routes = build_runtime_routes(config.routes)
    matches = {route.id for route in Router(routes).match(event)}
    allowed: list[ExplainedLeg] = []
    excluded: list[ExplainedLeg] = []
    for route in sorted(routes, key=lambda item: (item.priority, item.id)):
        reason = "source filter matched"
        if route.source.adapter is not None and route.source.adapter != source_adapter:
            reason = "different source adapter"
        elif (
            route.source.channel is not None and route.source.channel != source_context
        ):
            reason = "different source context"
        elif route.source.event_kinds and event_kind not in route.source.event_kinds:
            reason = "event kind excluded by route source filter"
        result = "candidate" if route.id in matches else "excluded"
        if result == "candidate":
            reason = "source filter matched"
        elif reason == "source filter matched":
            reason = "excluded by production source matcher"
        targets = (
            tuple(
                _explain_target(route, event, target, sender_id=sender_id)
                for target in route.targets
            )
            if result == "candidate"
            else ()
        )
        item = ExplainedLeg(route.id, result, reason, targets)
        (allowed if result == "candidate" else excluded).append(item)
    for disabled in config.routes.routes:
        if not disabled.enabled:
            excluded.append(
                ExplainedLeg(disabled.route_id, "excluded", "route disabled", ())
            )
    return RouteExplanation(
        source_adapter,
        source_context,
        event_kind,
        sender_id,
        tuple(allowed),
        tuple(excluded),
    )


def _explain_target(route, event, target, *, sender_id: str | None) -> dict:
    """Reuse the pure production allowlist evaluator when inputs are known."""
    outcome = "policy_allowed"
    reason = None
    if event.event_kind not in KNOWN_KINDS:
        outcome = "unsupported_kind"
        reason = "unregistered event kind is suppressed by capability planning"
    elif route.policy is not None:
        # Evaluate known filters before asking for the missing sender. A denied
        # destination remains denied regardless of an unspecified identity.
        policy = route.policy
        needs_sender = bool(policy.sender_allowlist) and sender_id is None
        if needs_sender:
            policy = replace(policy, sender_allowlist=())
        decision = evaluate_route_policy(policy, event, target)
        if not decision.allowed:
            outcome = "policy_denied"
            reason = decision.reason
        elif needs_sender:
            outcome = "sender_required"
            reason = "provide --sender-id to evaluate the sender allowlist"
    return {
        "adapter": target.adapter,
        "context": target.channel,
        "destination_kind": target.destination.kind if target.destination else None,
        "policy": outcome,
        "reason": reason,
    }
