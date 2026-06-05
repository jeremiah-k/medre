"""Recovery ownership model — statuses, actions, ledger, and summary.

All dataclasses are frozen and JSON-safe.  The model is diagnostic and
accountability-focused; it does **not** introduce fake execution
guarantees.

See :mod:`~medre.core.recovery` for the public package interface.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, fields
from typing import Any

__all__ = [
    "RecoveryOwnershipStatus",
    "RecoveryOwnershipAction",
    "StartupRecoveryLedger",
    "RecoverySummary",
]


# ---------------------------------------------------------------------------
# Recovery ownership status enum
# ---------------------------------------------------------------------------


class RecoveryOwnershipStatus(enum.StrEnum):
    """Canonical recovery ownership statuses.

    Each outbox item is classified into exactly one status during
    startup recovery analysis.
    """

    RECOVERABLE = "recoverable"
    """Outbox item is non-terminal and not yet claimed for recovery."""

    CLAIMED_FOR_RECOVERY = "claimed_for_recovery"
    """Outbox item was moved to ``in_progress`` with a recovery context
    (lease set, worker identity assigned)."""

    RECLAIMED = "reclaimed"
    """Outbox item was previously in a resumable state (``pending`` or
    ``retry_wait``) and has been reclaimed by a worker."""

    ABANDONED = "abandoned"
    """Outbox item was previously ``in_progress`` but recovery was
    abandoned (e.g. drain timeout, ambiguous loss)."""

    UNRECOVERABLE = "unrecoverable"
    """Outbox item is in a terminal status (``sent``, ``dead_lettered``,
    ``cancelled``, ``abandoned``) and does not require recovery."""

    SKIPPED = "skipped"
    """Outbox item is intentionally not recovered during this cycle
    (e.g. retry_eligible with future ``next_attempt_at``, or
    ``in_progress`` with an active lease)."""


# ---------------------------------------------------------------------------
# Recovery ownership action
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryOwnershipAction:
    """A single recovery ownership action recorded in the ledger.

    Every recovery or diagnostic ownership classification is captured with full attribution:
    what was claimed, by whom, from what prior state, and why.
    """

    recovery_run_id: str | None
    """UUID generated at startup that binds this action to a specific
    runtime session.  ``None`` when the startup context is unavailable."""

    startup_timestamp: str | None
    """ISO-8601 timestamp of the runtime startup.  ``None`` when
    the startup context is unavailable (e.g. per-event collection
    without a runtime session)."""

    outbox_id: str
    """The affected outbox item ID."""

    prior_status: str
    """The outbox item's status at the time recovery analysis began."""

    observed_status: str
    """Observed outbox status at analysis time.  In snapshot diagnostics
    this equals ``prior_status`` because no storage mutation occurs."""

    ownership_action: str
    """:class:`RecoveryOwnershipStatus` value describing the action."""

    reason: str
    """Human-readable explanation of why this action was taken."""

    worker_identity: str | None
    """Worker identity (from ``outbox_item.worker_id``), if known."""

    recovery_source: str
    """:class:`RecoverySource` value identifying which subsystem
    reclaimed ownership."""

    timestamp: str
    """ISO-8601 timestamp when this action was recorded."""

    delivery_plan_id: str
    """The delivery plan this outbox item targets."""

    event_id: str
    """The canonical event this outbox item delivers."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict with alphabetically sorted keys."""
        result: dict[str, Any] = {
            name: getattr(self, name) for name in sorted(f.name for f in fields(self))
        }
        return result


# ---------------------------------------------------------------------------
# Startup recovery ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartupRecoveryLedger:
    """Append-only recovery ownership evidence for a startup cycle.

    Contains every recovery/diagnostic ownership classification during startup analysis.
    Actions are deterministically ordered by ``(outbox_id, timestamp)``.
    """

    recovery_run_id: str | None
    """UUID binding all actions to a single runtime startup.
    ``None`` when the startup context is unavailable."""

    startup_timestamp: str | None
    """ISO-8601 timestamp of the runtime startup."""

    actions: tuple[RecoveryOwnershipAction, ...]
    """Deterministically ordered recovery actions."""

    generated_at: str
    """ISO-8601 timestamp when this ledger was assembled."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict with alphabetically sorted keys."""
        result: dict[str, Any] = {
            name: getattr(self, name) for name in sorted(f.name for f in fields(self))
        }
        result["actions"] = [a.to_dict() for a in result["actions"]]
        return result


# ---------------------------------------------------------------------------
# Recovery summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoverySummary:
    """Deterministic recovery summary with consistency validation.

    Aggregates counts across all recovery/diagnostic ownership classifications and validates that
    ``total_items`` equals the sum of all status categories.  The
    ``consistency_valid`` field is ``True`` when the invariant holds.
    """

    recoverable_items: int
    """Count of items classified as :attr:`RecoveryOwnershipStatus.RECOVERABLE`."""

    claimed_items: int
    """Count of items classified as :attr:`RecoveryOwnershipStatus.CLAIMED_FOR_RECOVERY`."""

    reclaimed_items: int
    """Count of items classified as :attr:`RecoveryOwnershipStatus.RECLAIMED`."""

    skipped_items: int
    """Count of items classified as :attr:`RecoveryOwnershipStatus.SKIPPED`."""

    abandoned_items: int
    """Count of items classified as :attr:`RecoveryOwnershipStatus.ABANDONED`."""

    unrecoverable_items: int
    """Count of items classified as :attr:`RecoveryOwnershipStatus.UNRECOVERABLE`."""

    total_items: int
    """Total items examined.  MUST equal ``recoverable + claimed + reclaimed
    + skipped + abandoned + unrecoverable``."""

    consistency_valid: bool
    """``True`` when ``total_items == sum(...)``.  When ``False`` the
    summary contains an accounting inconsistency that operators should
    investigate."""

    by_source: dict[str, int]
    """Count of actions per :class:`RecoverySource` value."""

    recovery_run_id: str | None
    """The recovery run ID this summary is scoped to, or ``None`` when
    unavailable."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict with alphabetically sorted keys.

        ``by_source`` is shallow-copied so that mutating the returned
        dict does not leak back into the frozen model.
        """
        result: dict[str, Any] = {
            name: getattr(self, name) for name in sorted(f.name for f in fields(self))
        }
        result["by_source"] = dict(result["by_source"])
        return result
