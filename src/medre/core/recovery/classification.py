"""Pure startup reclamation classification helpers.

Maps outbox items (duck-typed) to reclamation categories without I/O,
state mutation, or external dependencies.  Read-only diagnostics
designed for operator-facing evidence, not action.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ._helpers import _get, _parse_as_utc, _to_str

__all__ = ["classify_startup_reclamation"]

# ---------------------------------------------------------------------------
# Status classification sets
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"sent", "dead_lettered", "cancelled", "abandoned"}
)

_NON_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"pending", "retry_wait", "in_progress", "queued"}
)


_ALL_KNOWN_STATUSES: frozenset[str] = _TERMINAL_STATUSES | _NON_TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Classification labels
# ---------------------------------------------------------------------------

CLASS_IMMEDIATELY_CLAIMABLE = "immediately_claimable"
CLASS_RETRY_ELIGIBLE = "retry_eligible"
CLASS_STALE = "stale"
CLASS_ORPHANED = "orphaned"
CLASS_TERMINAL = "terminal"
CLASS_INCONSISTENT = "inconsistent"

_ALL_LABELS: frozenset[str] = frozenset(
    {
        CLASS_IMMEDIATELY_CLAIMABLE,
        CLASS_RETRY_ELIGIBLE,
        CLASS_STALE,
        CLASS_ORPHANED,
        CLASS_TERMINAL,
        CLASS_INCONSISTENT,
    }
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DEFAULT_STALE_QUEUED_GRACE: timedelta = timedelta(minutes=5)


def classify_startup_reclamation(
    outbox_item: Any,
    *,
    known_event_ids: set[str] | frozenset[str] | None = None,
    now: datetime | None = None,
    stale_queued_grace: timedelta | None = None,
) -> tuple[str, str]:
    """Classify a single outbox item for startup reclamation.

    Pure, read-only classification.  Does **not** mutate the outbox
    item or call storage.

    Parameters
    ----------
    outbox_item:
         Duck-typed record with at minimum ``status``, ``event_id``,
         ``outbox_id``, and timestamps (``next_attempt_at``,
         ``lease_until``, ``updated_at``).  Accepts both
         :class:`~medre.core.storage.backend.DeliveryOutboxItem` and
         plain ``dict`` values.

         **Timestamp field note:** Outbox items use ``next_attempt_at``
         for scheduled retry timing (set when status is ``retry_wait``).
         This is distinct from receipt-level ``next_retry_at``, which
         drives receipt-based retry scheduling.  Classification only
          reads the outbox field.
     known_event_ids:
        Known event IDs for orphan detection.  ``None`` skips the
        orphan check.  An empty set flags all non-terminal items as
        orphaned (consistent with
        :func:`~medre.core.diagnostics.convergence.orphans.build_orphan_report`).
    now:
        Reference time for deterministic timestamp comparisons.
        Defaults to ``datetime.now(timezone.utc)`` when ``None``.
    stale_queued_grace:
        Grace period before a ``queued`` item with ``updated_at`` is
        considered stale.  Defaults to 5 minutes when ``None``.

    Returns
    -------
    tuple[str, str]
        ``(classification_label, reason)`` where *classification_label*
        is one of the ``CLASS_*`` constants.
    """
    _now = now if now is not None else datetime.now(timezone.utc)
    _grace = (
        stale_queued_grace
        if stale_queued_grace is not None
        else _DEFAULT_STALE_QUEUED_GRACE
    )

    status = _to_str(_get(outbox_item, "status")).lower()
    event_id = _to_str(_get(outbox_item, "event_id"))
    outbox_id = _to_str(_get(outbox_item, "outbox_id"))
    next_attempt_at = _to_str(_get(outbox_item, "next_attempt_at"))
    lease_until = _to_str(_get(outbox_item, "lease_until"))
    updated_at = _to_str(_get(outbox_item, "updated_at"))

    # -- Terminal: no recovery needed ---------------------------------------
    if status in _TERMINAL_STATUSES:
        return (
            CLASS_TERMINAL,
            f"Outbox item {outbox_id!r} is terminal ({status}) — no recovery required",
        )

    # -- Unrecognised status ------------------------------------------------
    if status not in _ALL_KNOWN_STATUSES:
        return (
            CLASS_INCONSISTENT,
            f"Outbox item {outbox_id!r} has unrecognised status {status!r}",
        )

    # -- Orphan check (non-terminal items) ----------------------------------
    if known_event_ids is not None and event_id not in known_event_ids:
        return (
            CLASS_ORPHANED,
            f"Outbox item {outbox_id!r} (event {event_id!r}) has no known event "
            f"— orphaned work cannot be reclaimed without a valid event catalogue",
        )

    # -- in_progress: lease check -------------------------------------------
    if status == "in_progress":
        if lease_until:
            try:
                lease_dt = _parse_as_utc(lease_until)
                if _now >= lease_dt:
                    return (
                        CLASS_STALE,
                        f"Outbox item {outbox_id!r} lease expired ({lease_until}) "
                        f"— reclaimable by next worker",
                    )
                # Lease still valid — item is actively being worked.
                return (
                    CLASS_RETRY_ELIGIBLE,
                    f"Outbox item {outbox_id!r} is in_progress with active lease "
                    f"(until {lease_until}) — not yet due",
                )
            except (ValueError, TypeError):
                # Unparseable timestamp — treat as stale conservatively.
                pass
        # Stale in_progress with no lease_until (or unparseable).
        return (
            CLASS_STALE,
            f"Outbox item {outbox_id!r} is in_progress with expired or missing "
            f"lease — reclaimable",
        )

    # -- queued: stale grace check ------------------------------------------
    if status == "queued":
        if not updated_at:
            return (
                CLASS_STALE,
                f"Outbox item {outbox_id!r} is queued with no updated_at "
                f"— treat as stale",
            )
        try:
            updated_dt = _parse_as_utc(updated_at)
            age = _now - updated_dt
            if age <= _grace:
                # Recently queued — within grace period, not stale.
                return (
                    CLASS_RETRY_ELIGIBLE,
                    f"Outbox item {outbox_id!r} is queued and recently updated "
                    f"({updated_at}) — within stale grace period",
                )
            # Stale queued — grace period exceeded.
            return (
                CLASS_IMMEDIATELY_CLAIMABLE,
                f"Outbox item {outbox_id!r} is queued and stale "
                f"(updated_at {updated_at} exceeds grace) — claimable",
            )
        except (ValueError, TypeError):
            # Unparseable updated_at — treat as stale conservatively.
            return (
                CLASS_STALE,
                f"Outbox item {outbox_id!r} is queued with unparseable "
                f"updated_at — treat as stale",
            )

    # -- retry_wait with future next_attempt_at -----------------------------
    if status == "retry_wait":
        if next_attempt_at:
            try:
                next_dt = _parse_as_utc(next_attempt_at)
                if _now < next_dt:
                    return (
                        CLASS_RETRY_ELIGIBLE,
                        f"Outbox item {outbox_id!r} is retry_wait with future "
                        f"next_attempt_at ({next_attempt_at}) — not yet due",
                    )
            except (ValueError, TypeError):
                pass
        return (
            CLASS_IMMEDIATELY_CLAIMABLE,
            f"Outbox item {outbox_id!r} is retry_wait and due — immediately claimable",
        )

    # -- pending -----------------------------------------------------------
    if status == "pending":
        return (
            CLASS_IMMEDIATELY_CLAIMABLE,
            f"Outbox item {outbox_id!r} is pending — immediately claimable",
        )

    # -- Fallthrough for any remaining non-terminal ------------------------
    return (
        CLASS_IMMEDIATELY_CLAIMABLE,
        f"Outbox item {outbox_id!r} ({status}) — claimable",
    )
