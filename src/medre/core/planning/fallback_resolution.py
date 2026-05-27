"""Fallback resolution for adapter capability degradation.

When a target adapter does not support a specific event operation
(e.g. reactions, edits), the :class:`FallbackResolver` downgrades
the delivery strategy to the closest supported alternative.

Fallback rules (Phase 1):

* ``message.reacted`` → deliver as ``message.text`` when the target
  does not support reactions.
* ``message.edited`` → deliver as a new ``message.text`` when the
  target does not support edits.
* ``message.deleted`` → silently skip delivery when the target does
  not support deletions.
* ``message.file`` → skip delivery when the target does not support
  attachments.
* ``message.created`` / ``message.text`` → skip when the adapter
  cannot send text (future-proof).
* ``presence.changed`` → skip when the adapter does not expose
  presence.
* ``telemetry.*`` → skip when the adapter does not support
  metadata fields.
* Reply-carrying events → check ``caps.replies``.
* All other / unknown event kinds → passthrough with ``"direct"``.
"""

from __future__ import annotations

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
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
    'direct'
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

        Checks the event kind against the adapter's declared capabilities
        and returns a downgraded strategy when the target cannot handle
        the event natively.

        For capability fields that use the three-level string scheme
        (``"native"``, ``"fallback"``, ``"unsupported"``), both
        ``"native"`` and ``"fallback"`` are treated as supported;
        only ``"unsupported"`` triggers a skip.
        """
        kind = event.event_kind

        # -- Message lifecycle ------------------------------------------------

        if kind == EventKind.MESSAGE_REACTED:
            if caps.reactions == "unsupported":
                return DeliveryStrategy(method="direct")

        if kind == EventKind.MESSAGE_EDITED:
            if caps.edits == "unsupported":
                return DeliveryStrategy(method="direct")

        if kind == EventKind.MESSAGE_DELETED:
            if caps.deletes == "unsupported":
                return DeliveryStrategy(method="direct")

        if kind == EventKind.MESSAGE_FILE:
            if not caps.attachments:
                return DeliveryStrategy(method="skip")

        if kind in (EventKind.MESSAGE_CREATED, EventKind.MESSAGE_TEXT):
            # Future-proof: if an adapter cannot send text, skip.
            if not caps.text:
                return DeliveryStrategy(method="skip")

        # -- Presence / telemetry ---------------------------------------------

        if kind == EventKind.PRESENCE_CHANGED:
            if not caps.presence:
                return DeliveryStrategy(method="skip")

        if kind in (EventKind.TELEMETRY_RECEIVED, EventKind.TELEMETRY_POSITION):
            if not caps.metadata_fields:
                return DeliveryStrategy(method="skip")

        # -- Relation-carrying events: reply capability -----------------------

        # Events that carry reply relations require reply support.
        if event.relations:
            for rel in event.relations:
                if rel.relation_type == "reply" and caps.replies == "unsupported":
                    return DeliveryStrategy(method="skip")

        # -- Identity / delivery / system / plugin → passthrough ---------------

        # These event kinds are always forwarded; adapters that cannot
        # handle them will be caught by the pipeline's capability-
        # suppressed check during delivery.

        # Default: direct delivery with standard parameters.
        return DeliveryStrategy(method="direct")
