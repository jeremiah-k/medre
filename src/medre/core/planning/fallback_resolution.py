"""Fallback resolution for adapter capability degradation.

When a target adapter does not support a specific event operation
(e.g. reactions, edits), the :class:`FallbackResolver` downgrades
the delivery strategy to the closest supported alternative.

Three-level capability semantics (reactions, edits, deletes, replies):

* ``"native"``      → ``"direct"`` strategy — normal/native rendering.
* ``"fallback"``    → ``"fallback_text"`` strategy — degraded text
  rendering within the target-native format.  The target-native
  renderer produces its native output but embeds relation context as
  inline text.  The adapter receives a payload in its native format,
  not a generic text envelope.
* ``"unsupported"`` → ``"skip"`` strategy — delivery suppressed
  before rendering.  No renderer or adapter invocation.

Fallback rules (Phase 1):

* ``message.reacted`` → check ``caps.reactions`` (native/fallback/unsupported).
* ``message.edited`` → check ``caps.edits`` (native/fallback/unsupported).
* ``message.deleted`` → check ``caps.deletes`` (native/fallback/unsupported).
* ``message.file`` → ``"skip"`` when the target does not support attachments.
* ``message.created`` / ``message.text`` → ``"skip"`` when the adapter
  cannot send text.
* ``presence.changed`` → ``"skip"`` when the adapter does not expose presence.
* ``telemetry.*`` → ``"skip"`` when the adapter does not support metadata fields.
* Reply-carrying events → check ``caps.replies`` (native/fallback/unsupported).
* All other / unknown event kinds → passthrough with ``"direct"``.
"""

from __future__ import annotations

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent
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
        from medre.core.planning.capability_decision import resolver as _resolver

        decision = _resolver.decide(event, caps)
        return DeliveryStrategy(method=decision.delivery_strategy)
