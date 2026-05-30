"""Replay delivery: envelope wrapping, adapter filtering, and capability filtering."""

from __future__ import annotations

from typing import Any

from medre.core.events import CanonicalEvent
from medre.core.planning.capabilities import resolve_adapter_capabilities
from medre.core.planning.capability_decision import resolver as _resolver

# ---------------------------------------------------------------------------
# Delivery envelope
# ---------------------------------------------------------------------------


def _replay_delivery_envelope(receipts: Any) -> dict[str, Any]:
    """Wrap adapter delivery results in a replay delivery envelope.

    The envelope marks the delivery as originating from replay and
    preserves the adapter's original results without promotion:
    queued/best-effort stays queued/best-effort.  Downstream consumers
    can inspect ``output["replay"]`` to distinguish replay deliveries
    from live ones.

    Parameters
    ----------
    receipts:
        The original adapter delivery results (list of receipts,
        :class:`AdapterDeliveryResult` instances, or any other
        pipeline output).

    Returns
    -------
    dict
        Envelope with ``"replay": True`` and ``"adapter_results"`` key.
    """
    return {
        "replay": True,
        "adapter_results": receipts,
    }


# ---------------------------------------------------------------------------
# Plan filtering
# ---------------------------------------------------------------------------


def _filter_plans_by_adapter(
    plans: list[Any],
    target_adapters: list[str],
) -> list[Any]:
    """Filter delivery plans to those targeting adapters in *target_adapters*.

    Accepts both bare plan lists and ``list[tuple[Route, DeliveryPlan]]``
    (as produced when the real pipeline is in use).  Plans that do not
    expose a ``target`` attribute with an ``adapter`` field are passed
    through (conservative: include rather than exclude when the plan
    structure is opaque).
    """
    allowed = set(target_adapters)
    result: list[Any] = []
    for item in plans:
        # Unwrap tuple (Route, DeliveryPlan) if present.
        if isinstance(item, tuple) and len(item) == 2:
            plan = item[1]
        else:
            plan = item
        target = getattr(plan, "target", None)
        adapter = getattr(target, "adapter", None) if target is not None else None
        if adapter is None:
            # Opaque plan structure -- include conservatively.
            result.append(item)
        elif adapter in allowed:
            result.append(item)
    return result


def _filter_plans_by_capability(
    event: CanonicalEvent,
    plans: list[Any],
    adapters: dict[str, Any] | None = None,
) -> list[Any]:
    """Filter delivery plans to those whose target adapter supports the event.

    For each plan, resolves the target adapter's capabilities and checks
    whether the event kind is supported.  Plans with unsupported event
    kinds are excluded.  When *adapters* is ``None``, plans are included
    conservatively (include rather than exclude).

    Only meaningful for BEST_EFFORT mode; the caller is responsible for
    gating on mode.

    Parameters
    ----------
    event:
        The canonical event being replayed.
    plans:
        Delivery plans to filter.
    adapters:
        Mapping of adapter ID to adapter instance, or ``None`` when
        unavailable (in which case all plans are included).

    Returns
    -------
    list[Any]
        Plans whose target adapters support the event kind.
    """
    if adapters is None:
        return plans

    result: list[Any] = []
    for item in plans:
        # Unwrap tuple (Route, DeliveryPlan) if present.
        if isinstance(item, tuple) and len(item) == 2:
            plan = item[1]
        else:
            plan = item

        target = getattr(plan, "target", None)
        if target is None:
            # Opaque plan structure -- include conservatively.
            result.append(item)
            continue

        caps = resolve_adapter_capabilities(adapters, target)
        if caps is None:
            # Adapter is missing from the registry --- include conservatively
            # rather than suppressing based on default (all-false) caps.
            result.append(item)
            continue

        # Extract target adapter name for traceability in the decision.
        adapter_name = getattr(target, "adapter", None)
        decision = _resolver.decide(event, caps, target_adapter=adapter_name)
        if decision.supported:
            result.append(item)
        # else: capability-suppressed --- exclude from delivery
    return result
