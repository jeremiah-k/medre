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

#: Outbox statuses that are directly claimable by any worker via
#: ``claim_due_outbox_items``.  Items in ``in_progress`` or ``queued``
#: may become reclaimable through lease-expiry or staleness queries but
#: are not directly claimable.
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
#: can transition to ``dead_lettered`` when retries are exhausted.
RECEIPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"sent"}),
    "failed": frozenset({"dead_lettered"}),
    # sent, dead_lettered, suppressed are terminal -- no outgoing transitions.
}

#: Observed outbox transitions.  Terminal statuses (sent, dead_lettered,
#: cancelled, abandoned) have no outgoing entries.
OUTBOX_TRANSITIONS: dict[str, frozenset[str]] = {
    # Lease acquisition paths.
    "pending": frozenset({"in_progress"}),
    "retry_wait": frozenset({"in_progress", "cancelled", "dead_lettered"}),
    "queued": frozenset({"in_progress", "sent"}),
    # Delivery outcome from in_progress.
    "in_progress": frozenset(
        {
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
    """Return ``True`` if *source_status* can transition to ``sent``.

    This is a convenience helper for the common queued->sent receipt
    correlation path.
    """
    return validate_receipt_transition(source_status, "sent")
