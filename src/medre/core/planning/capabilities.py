"""Capability checking helpers for adapter event-kind support.

Internal convenience helpers that delegate to
:mod:`~medre.core.planning.capability_decision` for the actual
resolution logic.  :func:`capability_unsupported` is a thin wrapper
around :class:`CapabilityDecisionResolver` that preserves its
return contract (``None`` when deliverable, reason string
when unsupported) while using the resolver's full relation coverage
(reply, reaction, edit, delete).

Capability level semantics (three-level string fields):

* ``"native"``      – first-class support; deliver natively.
* ``"fallback"``    – no native support, but the adapter can receive a
  degraded / textual representation.  The event is **not** suppressed.
* ``"unsupported"`` – the adapter cannot handle this feature at all;
  delivery should be suppressed.

Public symbols
--------------
* :func:`capability_unsupported` – return a reason string when the
  event kind is unsupported by the given capabilities.
* :func:`resolve_adapter_capabilities` – resolve the
  :class:`AdapterCapabilities` for a target from the adapter registry.
"""

from __future__ import annotations

from typing import Any

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent
from medre.core.planning.capability_decision import resolver as _resolver


def capability_unsupported(
    event: CanonicalEvent,
    caps: AdapterCapabilities,
) -> str | None:
    """Return a reason string if the event kind is unsupported by *caps*.

    Returns ``None`` when the event is deliverable — either natively
    (``"native"``) or via fallback text (``"fallback"``).  Only
    ``"unsupported"`` capability values cause a suppression reason to
    be returned.

    This function delegates to
    :meth:`CapabilityDecisionResolver.decide` and returns the decision's
    ``reason`` when unsupported, ``None`` otherwise.  The wrapper
    preserves the original return contract while using the resolver's
    full relation coverage (reply, reaction, edit, delete).

    Parameters
    ----------
    event:
        The canonical event whose kind and relations are being checked.
    caps:
        The target adapter's declared capabilities.

    Returns
    -------
    str | None
        A human-readable reason when the event should be suppressed, or
        ``None`` when the event is deliverable.
    """
    decision = _resolver.decide(event, caps)
    if decision.supported:
        return None
    return decision.reason


def resolve_adapter_capabilities(
    adapters: dict[str, Any],
    target: Any,
) -> AdapterCapabilities | None:
    """Resolve the :class:`AdapterCapabilities` for a target adapter.

    Looks up the target's adapter in the *adapters* registry and
    returns its declared capabilities.  Returns ``None`` when the
    adapter is not in the registry (truly missing).  Returns a default
    :class:`AdapterCapabilities` when the adapter exists but does not
    report capabilities.

    Parameters
    ----------
    adapters:
        Mapping of adapter ID to adapter instance.
    target:
        A :class:`~medre.core.routing.models.RouteTarget` (or any
        object with an ``adapter`` attribute).

    Returns
    -------
    AdapterCapabilities | None
        The resolved capabilities, ``None`` when the adapter is missing
        from the registry, or a default instance when the adapter exists
        but has no ``_capabilities`` attribute.
    """
    adapter_id = getattr(target, "adapter", None)
    if adapter_id is None:
        return None

    adapter = adapters.get(adapter_id)
    if adapter is None:
        return None

    caps = getattr(adapter, "_capabilities", None)
    if isinstance(caps, AdapterCapabilities):
        return caps
    return AdapterCapabilities()
