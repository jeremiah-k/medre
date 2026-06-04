"""Delivery status vocabularies, transition tables, and validation helpers.

This module is the internal source of truth for delivery status strings
used across the pipeline.  It defines frozenset constants for each status
vocabulary, documents observed transitions as static lookup tables, and
provides pure boolean helpers for status classification and transition
validation.

Design constraints
~~~~~~~~~~~~~~~~~~
- **No enums.**  Statuses are plain strings throughout the codebase.
- **No state-machine engine.**  Transition tables are declarative dicts
  used by ``validate_*_transition`` helpers; they do not drive behaviour.
- **No exceptions.**  Validation helpers return ``bool`` so callers decide
  how to handle invalid states.
- **No external imports.**  This module is leaf-level; it must not import
  from storage, pipeline, or planning layers.

Status vocabularies
~~~~~~~~~~~~~~~~~~~

Receipt statuses (DeliveryReceipt.status)
    ``queued``, ``sent``, ``failed``, ``dead_lettered``, ``suppressed``.

Outbox statuses (DeliveryOutboxItem.status)
    ``pending``, ``in_progress``, ``queued``, ``sent``, ``retry_wait``,
    ``dead_lettered``, ``cancelled``, ``abandoned``.

Outcome statuses (DeliveryOutcome.status)
    ``success``, ``queued``, ``transient_failure``, ``permanent_failure``,
    ``skipped``.

Adapter delivery_status (OutboundResult.delivery_status)
    ``sent``, ``enqueued``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Receipt status vocabulary
# ---------------------------------------------------------------------------

#: All known DeliveryReceipt status values.
RECEIPT_STATUSES: frozenset[str] = frozenset(
    {"queued", "sent", "failed", "dead_lettered", "suppressed"}
)

#: Receipt statuses that are terminal -- once reached, the receipt is never
#: transitioned to a different status.
TERMINAL_RECEIPT_STATUSES: frozenset[str] = frozenset(
    {"sent", "dead_lettered", "suppressed"}
)

#: Receipt statuses that are non-terminal -- the delivery chain may later
#: receive another appended receipt.  Individual receipt rows never
#: transition; the delivery_state ``NON_TERMINAL_RECEIPT_STATUSES`` is
#: derived as ``RECEIPT_STATUSES - TERMINAL_RECEIPT_STATUSES``.
NON_TERMINAL_RECEIPT_STATUSES: frozenset[str] = (
    RECEIPT_STATUSES - TERMINAL_RECEIPT_STATUSES
)

# ---------------------------------------------------------------------------
# Outbox status vocabulary
# ---------------------------------------------------------------------------

#: All known DeliveryOutboxItem status values.
OUTBOX_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "in_progress",
        "queued",
        "sent",
        "retry_wait",
        "dead_lettered",
        "cancelled",
        "abandoned",
    }
)

#: Outbox statuses that are terminal -- once reached, the outbox item is
#: never transitioned to a different status.
TERMINAL_OUTBOX_STATUSES: frozenset[str] = frozenset(
    {"sent", "dead_lettered", "cancelled", "abandoned"}
)

#: Outbox statuses that are non-terminal -- the outbox item may still
#: transition to a different status.  Unlike receipts, outbox rows ARE
#: mutable; the underlying state machine drives ``_update_outbox_status``.
#: Computed as ``OUTBOX_STATUSES - TERMINAL_OUTBOX_STATUSES``.
NON_TERMINAL_OUTBOX_STATUSES: frozenset[str] = (
    OUTBOX_STATUSES - TERMINAL_OUTBOX_STATUSES
)

#: Outbox statuses that are directly claimable by any worker via
#: ``claim_due_outbox_items``.  Items in ``in_progress`` or ``queued``
#: may become reclaimable through lease-expiry or staleness queries but
#: are not directly claimable.
#:
#: **Direct claimability ≠ reclaimability.**  ``queued`` appears in
#: ``OUTBOX_TRANSITIONS`` with ``in_progress`` as a legal target, but
#: that transition is only valid for *stale queued reclaim* after the
#: configured grace period (see ``STALE_QUEUED_GRACE_SECONDS`` in the
#: SQLite storage layer).  ``is_claimable_outbox_status("queued")``
#: returns ``False``.
CLAIMABLE_OUTBOX_STATUSES: frozenset[str] = frozenset({"pending", "retry_wait"})

# ---------------------------------------------------------------------------
# Outcome status vocabulary
# ---------------------------------------------------------------------------

#: All known DeliveryOutcome status values.
OUTCOME_STATUSES: frozenset[str] = frozenset(
    {"success", "queued", "transient_failure", "permanent_failure", "skipped"}
)

#: Outcome statuses that represent accepted (non-failure) deliveries.
ACCEPTED_OUTCOME_STATUSES: frozenset[str] = frozenset({"success", "queued"})

# ---------------------------------------------------------------------------
# Adapter delivery_status vocabulary
# ---------------------------------------------------------------------------

#: All known OutboundResult.delivery_status values returned by adapters.
ADAPTER_DELIVERY_STATUSES: frozenset[str] = frozenset({"sent", "enqueued"})

# ---------------------------------------------------------------------------
# Transition tables
# ---------------------------------------------------------------------------
# These dicts map a source status to the set of statuses that have been
# observed as valid targets.  Terminal statuses have no outgoing entries.
# The tables are declarative documentation -- they do not drive behaviour
# beyond the validate_*_transition helpers below.

#: Observed receipt transitions.  ``failed`` is NOT terminal because it
#: can transition to ``dead_lettered`` when retries are exhausted, or
#: to ``failed`` again when a retry attempt also fails.
RECEIPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"sent"}),
    "failed": frozenset({"dead_lettered", "failed"}),
    # sent, dead_lettered, suppressed are terminal -- no outgoing transitions.
}

#: Observed outbox transitions.  Terminal statuses (sent, dead_lettered,
#: cancelled, abandoned) have no outgoing entries.
#:
#: ``queued`` → ``in_progress`` is legal only for stale queued reclaim
#: after the configured grace period (``STALE_QUEUED_GRACE_SECONDS``).
#: It does **not** make ``queued`` a directly-claimable status; see
#: ``CLAIMABLE_OUTBOX_STATUSES``.
OUTBOX_TRANSITIONS: dict[str, frozenset[str]] = {
    # Lease acquisition paths.
    "pending": frozenset({"in_progress", "cancelled", "abandoned"}),
    "retry_wait": frozenset({"in_progress", "cancelled", "dead_lettered", "abandoned"}),
    "queued": frozenset({"in_progress", "sent", "cancelled", "abandoned"}),
    # Delivery outcome from in_progress.
    "in_progress": frozenset(
        {
            "pending",
            "queued",
            "sent",
            "retry_wait",
            "dead_lettered",
            "cancelled",
            "abandoned",
        }
    ),
    # sent, dead_lettered, cancelled, abandoned are terminal.
}


# ---------------------------------------------------------------------------
# Safe-update guidance
# ---------------------------------------------------------------------------
# When adding or changing statuses, terminal sets, claimable sets, or
# transition tables, the following MUST be updated together:
#
# 1. Status vocabulary frozensets (OUTBOX_STATUSES / RECEIPT_STATUSES / …).
# 2. Terminal sets (TERMINAL_OUTBOX_STATUSES / TERMINAL_RECEIPT_STATUSES).
# 3. Claimable sets (CLAIMABLE_OUTBOX_STATUSES).
# 4. Transition tables (OUTBOX_TRANSITIONS / RECEIPT_TRANSITIONS) — these
#    describe observed legal storage/runtime transitions, not direct
#    claimability alone.
# 5. ``docs/spec/state-machines.md`` §2.3 Legal Transitions.
# 6. ``tests/test_delivery_state.py`` — vocabulary, classification, and
#    transition tests.
#
# Transition tables capture every observed legal status change, including
# reclaim paths (e.g. queued→in_progress via stale queued reclaim) that are
# not direct claims.  Adding a transition here does not make the source
# status directly claimable; that is governed by CLAIMABLE_OUTBOX_STATUSES.


# ---------------------------------------------------------------------------
# Pure helpers -- status validation
# ---------------------------------------------------------------------------


def validate_receipt_status(status: str) -> bool:
    """Return ``True`` if *status* is a known receipt status."""
    return status in RECEIPT_STATUSES


def validate_outbox_status(status: str) -> bool:
    """Return ``True`` if *status* is a known outbox status."""
    return status in OUTBOX_STATUSES


def validate_outcome_status(status: str) -> bool:
    """Return ``True`` if *status* is a known outcome status."""
    return status in OUTCOME_STATUSES


# ---------------------------------------------------------------------------
# Pure helpers -- classification
# ---------------------------------------------------------------------------


def is_terminal_receipt_status(status: str) -> bool:
    """Return ``True`` if *status* is a terminal receipt status."""
    return status in TERMINAL_RECEIPT_STATUSES


def is_terminal_outbox_status(status: str) -> bool:
    """Return ``True`` if *status* is a terminal outbox status.

    Terminal outbox statuses: ``sent``, ``dead_lettered``,
    ``cancelled``, ``abandoned``.
    """
    return status in TERMINAL_OUTBOX_STATUSES


def is_claimable_outbox_status(status: str) -> bool:
    """Return ``True`` if *status* is a directly-claimable outbox status.

    Claimable outbox statuses: ``pending``, ``retry_wait``.
    """
    return status in CLAIMABLE_OUTBOX_STATUSES


def is_accepted_outcome_status(status: str) -> bool:
    """Return ``True`` if *status* is an accepted (non-failure) outcome.

    Accepted outcome statuses: ``success``, ``queued``.
    """
    return status in ACCEPTED_OUTCOME_STATUSES


# ---------------------------------------------------------------------------
# Pure helpers -- transition validation
# ---------------------------------------------------------------------------


def validate_receipt_transition(source: str, target: str) -> bool:
    """Return ``True`` if the transition *source* -> *target* is a known
    receipt transition.

    Unknown source statuses and terminal sources (with no outgoing
    transitions in the table) return ``False``.
    """
    allowed = RECEIPT_TRANSITIONS.get(source)
    return allowed is not None and target in allowed


def validate_outbox_transition(source: str, target: str) -> bool:
    """Return ``True`` if the transition *source* -> *target* is a known
    outbox transition.

    Unknown source statuses and terminal sources (with no outgoing
    transitions in the table) return ``False``.
    """
    allowed = OUTBOX_TRANSITIONS.get(source)
    return allowed is not None and target in allowed


def is_valid_queued_to_sent_transition(source_status: str) -> bool:
    """Return ``True`` if *source_status* may transition to ``sent``.

    Delegates to ``validate_receipt_transition(source_status, "sent")``.
    Under the current :data:`RECEIPT_TRANSITIONS` table, only ``"queued"``
    has ``"sent"`` as a legal target, so this helper effectively answers
    "is *source_status* ``queued``?" — but the check is table-driven so
    it stays correct if future receipt transitions to ``sent`` are added.

    Used by the queued→sent supplemental receipt correlation path in
    :class:`~medre.core.engine.pipeline.delivery_lifecycle.DeliveryLifecycleService`.
    """
    return validate_receipt_transition(source_status, "sent")
