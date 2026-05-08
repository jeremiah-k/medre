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
* All other event kinds → no fallback needed; use ``"direct"``.
"""

from __future__ import annotations

from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
)
from medre.core.routing.models import RouteTarget


# ---------------------------------------------------------------------------
# Capability helpers
# ---------------------------------------------------------------------------

# Well-known capability keys that adapters may report.
_CAP_REACTIONS = "supports_reactions"
_CAP_EDITS = "supports_edits"
_CAP_DELETES = "supports_deletes"


def _adapter_supports(capabilities: dict, capability: str) -> bool:
    """Return ``True`` if the capability dict explicitly reports support."""
    return bool(capabilities.get(capability, False))


# ---------------------------------------------------------------------------
# Fallback resolver
# ---------------------------------------------------------------------------


class FallbackResolver:
    """Resolve delivery plans when adapter capabilities are limited.

    The resolver inspects the event kind and the target adapter's
    reported capabilities, then produces a :class:`DeliveryPlan` that
    uses the closest supported strategy.

    Example
    -------
    >>> resolver = FallbackResolver()
    >>> caps = {"supports_reactions": False}
    >>> plan = resolver.resolve_fallback(reaction_event, target, caps)
    >>> plan.primary_strategy.method
    'direct'
    """

    def resolve_fallback(
        self,
        event: CanonicalEvent,
        target: RouteTarget,
        capabilities: dict,
    ) -> DeliveryPlan:
        """Produce a delivery plan, downgrading if the target lacks support.

        Parameters
        ----------
        event:
            The canonical event to deliver.
        target:
            The resolved route target.
        capabilities:
            Adapter capability dictionary.  Expected keys include
            ``"supports_reactions"``, ``"supports_edits"``, and
            ``"supports_deletes"``.

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
        capabilities: dict,
    ) -> DeliveryStrategy:
        """Determine the effective delivery strategy for *event*.

        Checks the event kind against the adapter's reported capabilities
        and returns a downgraded strategy when the target cannot handle
        the event natively.
        """
        kind = event.event_kind

        if kind == EventKind.MESSAGE_REACTED:
            if not _adapter_supports(capabilities, _CAP_REACTIONS):
                return DeliveryStrategy(method="direct")

        if kind == EventKind.MESSAGE_EDITED:
            if not _adapter_supports(capabilities, _CAP_EDITS):
                return DeliveryStrategy(method="direct")

        if kind == EventKind.MESSAGE_DELETED:
            if not _adapter_supports(capabilities, _CAP_DELETES):
                return DeliveryStrategy(method="direct")

        # Default: direct delivery with standard parameters.
        return DeliveryStrategy(method="direct")
