"""Route policy evaluator — pure, stateless access-control for event routing.

Provides immutable policy and decision types and a pure evaluator function
with no I/O and no dependency on config, runtime, or adapter packages.

* :class:`RoutePolicy` – immutable allowlist-based policy.
* :class:`RouteDecision` – immutable evaluation result.
* :func:`evaluate_route_policy` – pure evaluator returning a decision.

**Design notes**

* Every allowlist field uses an empty tuple to mean "no restriction".
* Evaluation order is deterministic: source adapter → dest adapter → sender
  → room → channel.  The first denial wins.
* The evaluator imports only :class:`~medre.core.events.canonical.CanonicalEvent`
  and :class:`~medre.core.routing.models.RouteTarget` for type hints; actual
  types are checked at runtime via duck-typing to keep coupling minimal.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "RouteDecision",
    "RoutePolicy",
    "evaluate_route_policy",
]

# ---------------------------------------------------------------------------
# Stable reason codes
# ---------------------------------------------------------------------------

_REASON_SOURCE_ADAPTER = "source_adapter_not_allowed"
_REASON_DEST_ADAPTER = "dest_adapter_not_allowed"
_REASON_SENDER = "sender_not_allowed"
_REASON_ROOM = "room_not_allowed"
_REASON_CHANNEL = "channel_not_allowed"

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

_SUMMARY_CUTOFF = 5


@dataclass(frozen=True)
class RoutePolicy:
    """Immutable allowlist-based route policy.

    Each field is a tuple of strings.  An empty tuple means "no restriction"
    (everything allowed).  A non-empty tuple acts as an allowlist — only
    values present in the tuple are permitted.

    Attributes
    ----------
    allowed_source_adapters:
        Permitted source adapter names.  Empty = any source.
    allowed_dest_adapters:
        Permitted destination adapter names.  Empty = any destination.
    room_allowlist:
        Permitted room identifiers (e.g. Matrix room IDs).  Empty = any room.
    channel_allowlist:
        Permitted channel identifiers.  Empty = any channel.
    sender_allowlist:
        Permitted sender identifiers (``source_transport_id``).  Empty = any
        sender.
    """

    allowed_source_adapters: tuple[str, ...] = ()
    allowed_dest_adapters: tuple[str, ...] = ()
    room_allowlist: tuple[str, ...] = ()
    channel_allowlist: tuple[str, ...] = ()
    sender_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteDecision:
    """Immutable result of evaluating a :class:`RoutePolicy`.

    Attributes
    ----------
    allowed:
        Whether the route is permitted under the policy.
    reason:
        Stable machine-readable reason code (``None`` when allowed).
        One of: ``source_adapter_not_allowed``, ``dest_adapter_not_allowed``,
        ``sender_not_allowed``, ``room_not_allowed``,
        ``channel_not_allowed``.
    blocked_field:
        Name of the policy field that caused denial (``None`` when allowed).
    blocked_value:
        The actual value that was blocked (``None`` when allowed).
    allowed_summary:
        Human-readable summary of what was checked, safe for logging — never
        dumps large lists in full.
    """

    allowed: bool
    reason: str | None
    blocked_field: str | None
    blocked_value: str | None
    allowed_summary: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_summary(field_name: str, values: tuple[str, ...]) -> str:
    """Return a logging-safe summary of an allowlist field."""
    if not values:
        return f"{field_name}: any"
    if len(values) <= _SUMMARY_CUTOFF:
        joined = ", ".join(values)
        return f"{field_name}: [{joined}]"
    preview = ", ".join(values[:_SUMMARY_CUTOFF])
    return f"{field_name}: [{preview}, ... ({len(values)} total)]"


def _deny(
    reason: str,
    blocked_field: str,
    blocked_value: str,
    policy: RoutePolicy,
) -> RouteDecision:
    """Build a denial decision."""
    return RouteDecision(
        allowed=False,
        reason=reason,
        blocked_field=blocked_field,
        blocked_value=blocked_value,
        allowed_summary=f"denied: {reason} ({blocked_field}={blocked_value})",
    )


def _allow(policy: RoutePolicy) -> RouteDecision:
    """Build an allow decision with a safe summary."""
    parts = [
        _safe_summary("source_adapters", policy.allowed_source_adapters),
        _safe_summary("dest_adapters", policy.allowed_dest_adapters),
        _safe_summary("rooms", policy.room_allowlist),
        _safe_summary("channels", policy.channel_allowlist),
        _safe_summary("senders", policy.sender_allowlist),
    ]
    return RouteDecision(
        allowed=True,
        reason=None,
        blocked_field=None,
        blocked_value=None,
        allowed_summary="; ".join(parts),
    )


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def evaluate_route_policy(
    policy: RoutePolicy,
    event: object,
    target: object,
) -> RouteDecision:
    """Evaluate *policy* against *event* and *target*, returning a decision.

    Parameters
    ----------
    policy:
        The :class:`RoutePolicy` to evaluate.
    event:
        A ``CanonicalEvent`` (or duck-type compatible object).  Must have
        ``source_adapter`` (``str``), ``source_transport_id`` (``str``),
        and ``source_channel_id`` (``str | None``) attributes.
    target:
        A ``RouteTarget`` (or duck-type compatible object).  Must have
        ``adapter`` (``str | None``) and ``channel`` (``str | None``)
        attributes.

    Returns
    -------
    RouteDecision
        The evaluation result.  Checks are applied in deterministic order:
        source adapter → dest adapter → sender → room → channel.
        The first denial wins.  If all checks pass, an allow decision is
        returned.

    Notes
    -----
    * An empty allowlist tuple on any policy field means "no restriction".
    * Channel checking prefers ``target.channel``; if that is ``None``,
      it falls back to ``event.source_channel_id``.
    * Room checking uses ``event.source_channel_id`` when present (Matrix
      room identifiers typically appear there).
    """
    # -- Source adapter -------------------------------------------------------
    src_adapter: str = event.source_adapter  # type: ignore[union-attr]
    if (
        policy.allowed_source_adapters
        and src_adapter not in policy.allowed_source_adapters
    ):
        return _deny(
            _REASON_SOURCE_ADAPTER,
            "allowed_source_adapters",
            src_adapter,
            policy,
        )

    # -- Dest adapter ---------------------------------------------------------
    dst_adapter: str | None = target.adapter  # type: ignore[union-attr]
    if policy.allowed_dest_adapters and (
        dst_adapter is None or dst_adapter not in policy.allowed_dest_adapters
    ):
        return _deny(
            _REASON_DEST_ADAPTER,
            "allowed_dest_adapters",
            dst_adapter if dst_adapter is not None else "<missing>",
            policy,
        )

    # -- Sender ---------------------------------------------------------------
    sender: str = event.source_transport_id  # type: ignore[union-attr]
    if policy.sender_allowlist and sender not in policy.sender_allowlist:
        return _deny(
            _REASON_SENDER,
            "sender_allowlist",
            sender,
            policy,
        )

    # -- Room -----------------------------------------------------------------
    source_channel_id: str | None = event.source_channel_id  # type: ignore[union-attr]
    if policy.room_allowlist and (
        source_channel_id is None or source_channel_id not in policy.room_allowlist
    ):
        return _deny(
            _REASON_ROOM,
            "room_allowlist",
            source_channel_id if source_channel_id is not None else "<missing>",
            policy,
        )

    # -- Channel --------------------------------------------------------------
    target_channel: str | None = target.channel  # type: ignore[union-attr]
    effective_channel = (
        target_channel if target_channel is not None else source_channel_id
    )
    if policy.channel_allowlist and (
        effective_channel is None or effective_channel not in policy.channel_allowlist
    ):
        return _deny(
            _REASON_CHANNEL,
            "channel_allowlist",
            effective_channel if effective_channel is not None else "<missing>",
            policy,
        )

    return _allow(policy)
