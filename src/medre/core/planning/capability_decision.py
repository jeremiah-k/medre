"""Capability decision model — single source of truth for capability resolution.

This module provides the :class:`CapabilityDecision` frozen dataclass and the
:class:`CapabilityDecisionResolver` stateless resolver that together form the
operational decision model for capability-driven delivery.

Every capability check in the system — live delivery (Phase 2.5),
FallbackResolver strategy resolution, plan-level skip (Phase 2.75),
replay BEST_EFFORT filtering, rendering evidence, and diagnostics —
delegates to the resolver so that capability semantics are consistent
across live and replay delivery paths.

Capability level semantics (three-level string values):

* ``"native"``      – first-class support; deliver natively via ``"direct"``.
* ``"fallback"``    – no native support, but the adapter can receive a
  degraded / textual representation via ``"fallback_text"``.  The event
  is **not** suppressed.
* ``"unsupported"`` – the adapter cannot handle this feature at all;
  delivery is suppressed via ``"skip"``.

Boolean capability fields (``text``, ``attachments``, ``presence``,
``metadata_fields``) are mapped to native/unsupported: ``True`` → native,
``False`` → unsupported.

Three-level string fields (``reactions``, ``edits``, ``deletes``,
``replies``) map directly: ``"native"`` → native, ``"fallback"`` →
fallback, ``"unsupported"`` → unsupported.

Event-kind to capability field mapping
--------------------------------------
* ``message.reacted``       → ``reactions``
* ``message.edited``        → ``edits``
* ``message.deleted``       → ``deletes``
* ``message.file``          → ``attachments`` (boolean)
* ``message.created``       → ``text`` (boolean)
* ``message.text``          → ``text`` (boolean)
* ``presence.changed``      → ``presence`` (boolean)
* ``telemetry.received``    → ``metadata_fields`` (boolean)
* ``telemetry.position``    → ``metadata_fields`` (boolean)

Relation to capability field mapping
------------------------------------
* ``reply``    → ``replies``
* ``reaction`` → ``reactions``
* ``edit``     → ``edits``
* ``delete``   → ``deletes``
* ``thread``   → **DEFERRED** — thread capability is not yet modelled.
  Thread relations do not produce a capability candidate; current
  behaviour (native / direct, no ``capability_field``) is preserved.

Multiple-relation precedence
-----------------------------
When an event has both event-kind and relation candidates, the resolver
picks the **most severe** decision.  Severity ordering:

* ``unsupported`` (severity 2) > ``fallback`` (severity 1) > ``native`` (severity 0).

At the same severity level, the **first candidate in evaluation order**
breaks ties.  Evaluation order is:

1. Event-kind candidate (if the event kind maps to a capability field).
2. Relation candidates in ``event.relations`` order.

This ensures that ``unsupported`` always wins over ``fallback`` or
``native``, regardless of ordering, while preserving deterministic
tie-breaking for equal-severity candidates.

Thread capability deferral
--------------------------
``AdapterCapabilities.threads`` does not exist.  Thread relations are
not mapped to a capability field.  The resolver preserves current
behaviour: thread-carrying events receive native / direct delivery
with ``capability_field=None``, and the deferral is documented
explicitly in the decision's ``reason`` field when the only candidate
is a thread relation.

Public symbols
--------------
* :class:`CapabilityDecision` – frozen decision record.
* :class:`CapabilityDecisionResolver` – stateless resolver.
"""

from __future__ import annotations

from dataclasses import dataclass

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind

# ---------------------------------------------------------------------------
# Event-kind → capability field mapping
# ---------------------------------------------------------------------------

#: Maps event kind strings to the corresponding AdapterCapabilities field
#: name for capability resolution.
_EVENT_KIND_FIELDS: dict[str, str] = {
    EventKind.MESSAGE_REACTED: "reactions",
    EventKind.MESSAGE_EDITED: "edits",
    EventKind.MESSAGE_DELETED: "deletes",
    EventKind.MESSAGE_FILE: "attachments",
    EventKind.MESSAGE_CREATED: "text",
    EventKind.MESSAGE_TEXT: "text",
    EventKind.PRESENCE_CHANGED: "presence",
    EventKind.TELEMETRY_RECEIVED: "metadata_fields",
    EventKind.TELEMETRY_POSITION: "metadata_fields",
}

#: Boolean capability fields: ``True`` → native/direct, ``False`` → unsupported/skip.
_BOOLEAN_FIELDS: frozenset[str] = frozenset(
    {"text", "attachments", "presence", "metadata_fields"}
)

#: Three-level string capability fields: ``"native"``, ``"fallback"``, ``"unsupported"``.
_STRING_FIELDS: frozenset[str] = frozenset({"reactions", "edits", "deletes", "replies"})

# ---------------------------------------------------------------------------
# Relation → capability field mapping
# ---------------------------------------------------------------------------

#: Maps relation type strings to the corresponding AdapterCapabilities field.
#: ``"thread"`` is intentionally absent (capability deferred).
_RELATION_FIELDS: dict[str, str] = {
    "reply": "replies",
    "reaction": "reactions",
    "edit": "edits",
    "delete": "deletes",
    # thread: DEFERRED — no AdapterCapabilities.threads field.
}

# ---------------------------------------------------------------------------
# Severity ordering for precedence resolution
# ---------------------------------------------------------------------------

#: Numeric severity for precedence: higher value = more restrictive.
_SEVERITY: dict[str, int] = {
    "unsupported": 2,
    "fallback": 1,
    "native": 0,
}

# ---------------------------------------------------------------------------
# Strategy mapping: capability_level → delivery_strategy
# ---------------------------------------------------------------------------

_STRATEGY_FOR_LEVEL: dict[str, str] = {
    "native": "direct",
    "fallback": "fallback_text",
    "unsupported": "skip",
}


# ---------------------------------------------------------------------------
# Internal candidate tuple (used during resolution)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """Internal resolution candidate produced for one check point."""

    capability_level: str  # "native" | "fallback" | "unsupported"
    capability_field: str
    reason: str | None


def _resolve_field_level(
    field_name: str,
    caps: AdapterCapabilities,
) -> tuple[str, str | None]:
    """Resolve a single capability field to (level, reason_fragment).

    Returns ``(capability_level, None)`` for native; ``(level, fragment)``
    for fallback or unsupported.  The reason fragment is the field name
    (e.g. ``"reactions"``) and the caller assembles the full reason string.
    """
    raw_value = getattr(caps, field_name, None)
    if raw_value is None:
        # Field not found — treat as native (defensive).
        return ("native", None)

    if field_name in _BOOLEAN_FIELDS:
        if raw_value:
            return ("native", None)
        return ("unsupported", field_name)

    if field_name in _STRING_FIELDS:
        if raw_value == "native":
            return ("native", None)
        if raw_value == "fallback":
            return ("fallback", field_name)
        # "unsupported" or any other value.
        return ("unsupported", field_name)

    # Unknown field type — treat as native.
    return ("native", None)


def _make_event_kind_reason(
    capability_level: str,
    field_name: str,
    event_kind: str,
) -> str | None:
    """Build a reason string for an event-kind candidate."""
    if capability_level == "native":
        return None
    if capability_level == "fallback":
        return f"{field_name} fallback for adapter (event_kind={event_kind})"
    # unsupported
    return f"{field_name} unsupported by adapter (event_kind={event_kind})"


def _make_relation_reason(
    capability_level: str,
    field_name: str,
    relation_type: str,
) -> str | None:
    """Build a reason string for a relation candidate."""
    if capability_level == "native":
        return None
    if capability_level == "fallback":
        return (
            f"{field_name} fallback for adapter "
            f"(event has {relation_type} relation)"
        )
    # unsupported
    return (
        f"{field_name} unsupported by adapter " f"(event has {relation_type} relation)"
    )


# ---------------------------------------------------------------------------
# CapabilityDecision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityDecision:
    """Immutable capability decision for one event × adapter pair.

    Produced by :meth:`CapabilityDecisionResolver.decide`.  Carries all
    information needed by the pipeline (delivery strategy), replay
    (filtering), rendering (evidence), and diagnostics (logging).

    Attributes
    ----------
    target_adapter:
        Name of the target adapter, or ``None`` when not provided.
    event_kind:
        The event kind string from the canonical event.
    capability_level:
        The resolved capability level: ``"native"``, ``"fallback"``,
        or ``"unsupported"``.
    delivery_strategy:
        The delivery strategy derived from the capability level:
        ``"direct"``, ``"fallback_text"``, or ``"skip"``.
    supported:
        ``True`` when the event is deliverable (native or fallback).
        ``False`` when unsupported (should be suppressed).
    capability_field:
        The AdapterCapabilities field name that determined the decision,
        or ``None`` for passthrough event kinds (no capability mapping).
    reason:
        Human-readable reason string for fallback and unsupported
        decisions, or ``None`` for native / passthrough.
    """

    target_adapter: str | None
    event_kind: str
    capability_level: str
    delivery_strategy: str
    supported: bool
    capability_field: str | None
    reason: str | None


# ---------------------------------------------------------------------------
# CapabilityDecisionResolver
# ---------------------------------------------------------------------------


class CapabilityDecisionResolver:
    """Stateless resolver that produces :class:`CapabilityDecision` instances.

    Synchronous, stateless, no caching, no framework dependency.  Instantiate
    once (or use the module-level ``resolver`` singleton) and call
    :meth:`decide` for each event × capabilities pair.

    Example
    -------
    >>> resolver = CapabilityDecisionResolver()
    >>> caps = AdapterCapabilities(reactions="unsupported")
    >>> decision = resolver.decide(reaction_event, caps)
    >>> decision.delivery_strategy
    'skip'
    >>> decision.supported
    False
    """

    def decide(
        self,
        event: CanonicalEvent,
        caps: AdapterCapabilities,
        *,
        target_adapter: str | None = None,
    ) -> CapabilityDecision:
        """Resolve the capability decision for *event* against *caps*.

        Parameters
        ----------
        event:
            The canonical event whose kind and relations are checked.
        caps:
            The target adapter's declared capabilities.
        target_adapter:
            Optional adapter name for diagnostics / traceability.

        Returns
        -------
        CapabilityDecision
            The resolved decision with capability level, delivery strategy,
            support flag, and reason string.
        """
        kind = event.event_kind
        candidates: list[_Candidate] = []

        # 1. Event-kind candidate.
        ek_field = _EVENT_KIND_FIELDS.get(kind)
        if ek_field is not None:
            level, _fragment = _resolve_field_level(ek_field, caps)
            reason = _make_event_kind_reason(level, ek_field, kind)
            candidates.append(
                _Candidate(
                    capability_level=level,
                    capability_field=ek_field,
                    reason=reason,
                )
            )

        # 2. Relation candidates (in event.relations order).
        if event.relations:
            for rel in event.relations:
                rel_field = _RELATION_FIELDS.get(rel.relation_type)
                if rel_field is None:
                    # thread or unknown relation type — deferred / skip.
                    continue
                level, _fragment = _resolve_field_level(rel_field, caps)
                reason = _make_relation_reason(level, rel_field, rel.relation_type)
                candidates.append(
                    _Candidate(
                        capability_level=level,
                        capability_field=rel_field,
                        reason=reason,
                    )
                )

        # 3. No candidates → passthrough (native / direct).
        if not candidates:
            return CapabilityDecision(
                target_adapter=target_adapter,
                event_kind=kind,
                capability_level="native",
                delivery_strategy="direct",
                supported=True,
                capability_field=None,
                reason=None,
            )

        # 4. Pick the most severe candidate; first-in-order breaks ties.
        winner = candidates[0]
        winner_severity = _SEVERITY.get(winner.capability_level, 0)
        for candidate in candidates[1:]:
            c_severity = _SEVERITY.get(candidate.capability_level, 0)
            if c_severity > winner_severity:
                winner = candidate
                winner_severity = c_severity

        level = winner.capability_level
        strategy = _STRATEGY_FOR_LEVEL.get(level, "direct")
        supported = level != "unsupported"

        return CapabilityDecision(
            target_adapter=target_adapter,
            event_kind=kind,
            capability_level=level,
            delivery_strategy=strategy,
            supported=supported,
            capability_field=winner.capability_field,
            reason=winner.reason,
        )


#: Module-level singleton resolver for convenience.
resolver = CapabilityDecisionResolver()
