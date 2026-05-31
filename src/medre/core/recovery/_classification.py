"""Pure startup reclamation classification helpers.

Maps outbox items (duck-typed) to reclamation categories without I/O,
state mutation, or external dependencies.  Read-only diagnostics
designed for operator-facing evidence, not action.
"""

from __future__ import annotations

from typing import Any

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

_RECLAIMABLE_STATUSES: frozenset[str] = frozenset({"pending", "retry_wait"})

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
# Duck-typed field access (parallel to convergence helpers)
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Duck-typed field access — ``dict.get`` or ``getattr``."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_str(val: Any) -> str:
    """Coerce to string safely."""
    if val is None:
        return ""
    return str(val)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_startup_reclamation(
    outbox_item: Any,
    *,
    startup_timestamp: str | None = None,
    known_event_ids: set[str] | frozenset[str] | None = None,
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
    startup_timestamp:
        ISO-8601 startup timestamp for source inference.  ``None``
        means the startup context is unavailable.
    known_event_ids:
        Known event IDs for orphan detection.  ``None`` skips the
        orphan check.  An empty set flags all non-terminal items as
        orphaned (consistent with
        :func:`~medre.core.diagnostics.convergence.orphans.build_orphan_report`).

    Returns
    -------
    tuple[str, str]
        ``(classification_label, reason)`` where *classification_label*
        is one of the ``CLASS_*`` constants.
    """
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

    # -- Stale / expired in_progress ----------------------------------------
    if status == "in_progress":
        if lease_until:
            from datetime import datetime, timezone

            try:
                lease_dt = datetime.fromisoformat(lease_until)
                if datetime.now(timezone.utc) >= lease_dt:
                    return (
                        CLASS_STALE,
                        f"Outbox item {outbox_id!r} lease expired ({lease_until}) "
                        f"— reclaimable by next worker",
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

    # -- Stale queued -------------------------------------------------------
    if status == "queued":
        # A queued item with no updated_at or very old updated_at
        # is stale.  The exact grace period is storage-level policy,
        # so we classify conservatively: if updated_at is empty or
        # clearly old, treat as stale.
        if not updated_at:
            return (
                CLASS_STALE,
                f"Outbox item {outbox_id!r} is queued with no updated_at "
                f"— treat as stale",
            )
        return (
            (
                CLASS_ORPHANED
                if known_event_ids is not None and event_id not in known_event_ids
                else CLASS_IMMEDIATELY_CLAIMABLE
            ),
            f"Outbox item {outbox_id!r} is queued — claimable if stale",
        )

    # -- retry_wait with future next_attempt_at -----------------------------
    if status == "retry_wait":
        if next_attempt_at:
            from datetime import datetime, timezone

            try:
                next_dt = datetime.fromisoformat(next_attempt_at)
                if datetime.now(timezone.utc) < next_dt:
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
