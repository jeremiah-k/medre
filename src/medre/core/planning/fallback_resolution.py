"""Fallback resolution for adapter capability degradation.

When a target adapter does not support a specific event operation
(e.g. reactions, edits), the :class:`FallbackResolver` downgrades
the delivery strategy to the closest supported alternative.

:class:`FallbackResolver` delegates all capability strategy decisions
to :class:`~medre.core.planning.capability_decision.CapabilityDecisionResolver`.
Detailed relation and event-kind capability mappings live in
:mod:`~medre.core.planning.capability_decision`.
"""

from __future__ import annotations

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent
from medre.core.planning.capability_decision import resolver as _resolver
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
)
from medre.core.routing.models import RouteTarget

# ---------------------------------------------------------------------------
# Fallback resolver
# ---------------------------------------------------------------------------


class FallbackResolver:
    """Resolve delivery plans when adapter capabilities are limited.

    The resolver inspects the event kind and the target adapter's
    :class:`AdapterCapabilities`, then produces a :class:`DeliveryPlan`
    that uses the closest supported strategy.

    Example
    -------
    >>> resolver = FallbackResolver()
    >>> caps = AdapterCapabilities(reactions="unsupported")
    >>> plan = resolver.resolve_fallback(reaction_event, target, caps)
    >>> plan.primary_strategy.method
    'skip'
    """

    def resolve_fallback(
        self,
        event: CanonicalEvent,
        target: RouteTarget,
        capabilities: AdapterCapabilities,
    ) -> DeliveryPlan:
        """Produce a delivery plan, downgrading if the target lacks support.

        Parameters
        ----------
        event:
            The canonical event to deliver.
        target:
            The resolved route target.
        capabilities:
            The adapter's declared :class:`AdapterCapabilities`.

        Returns
        -------
        DeliveryPlan
            A plan whose primary strategy has been adjusted for the
            target's capabilities.
        """
        strategy = self._resolve_strategy(event, capabilities)

        return DeliveryPlan(
            plan_id=f"plan:{event.event_id}:{id(target):x}",
            event_id=event.event_id,
            target=target,
            primary_strategy=strategy,
        )

    # -- Internal ---------------------------------------------------------

    def _resolve_strategy(
        self,
        event: CanonicalEvent,
        caps: AdapterCapabilities,
    ) -> DeliveryStrategy:
        """Determine the effective delivery strategy for *event*.

        Delegates to :class:`CapabilityDecisionResolver` for the actual
        capability resolution, then maps the decision's delivery strategy
        to a :class:`DeliveryStrategy` instance.

        For capability fields that use the three-level string scheme
        (``"native"``, ``"fallback"``, ``"unsupported"``), both
        ``"native"`` and ``"fallback"`` are treated as supported.
        ``"unsupported"`` triggers event-specific behavior (``"skip"``
        for both lifecycle events and hard-incompatible capabilities).
        """
        decision = _resolver.decide(event, caps)
        return DeliveryStrategy(method=decision.delivery_strategy)
