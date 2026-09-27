"""Failure-kind classification shared between recover and evidence.

The persisted ``failure_kind`` on a receipt is the machine authority; this
module maps it to recovery categories and recommended operator commands.
Nothing reconstructs a classification from error wording.

Public symbols
--------------
* :data:`RETRYABLE_KINDS` — frozenset of retryable failure-kind strings.
* :data:`PERMANENT_KINDS` — frozenset of permanent failure-kind strings.
* :data:`OPERATIONAL_KINDS` — frozenset of operational failure-kind strings.
* :func:`failure_category` — map a failure-kind to a recovery category.
* :func:`recommended_commands` — suggested next commands for a category.
"""

from __future__ import annotations

__all__ = [
    "RETRYABLE_KINDS",
    "PERMANENT_KINDS",
    "OPERATIONAL_KINDS",
    "failure_category",
    "recommended_commands",
]

# ---------------------------------------------------------------------------
# Failure-kind categories for recovery classification.
# ---------------------------------------------------------------------------

RETRYABLE_KINDS: frozenset[str] = frozenset({"adapter_transient", "retry_exhausted"})
"""Failure kinds whose recovery remedy is another attempt.

``adapter_transient`` may still succeed on automatic retry;
``retry_exhausted`` means automatic retries are spent and manual replay
is the remaining remedy — the retryable command set recommends exactly
that. A dead-lettered receipt without a persisted kind stays
``unknown``: ``dead_lettered`` is lifecycle authority, not a
failure-kind signal."""

PERMANENT_KINDS: frozenset[str] = frozenset(
    {
        "adapter_permanent",
        "adapter_missing",
        "renderer_failure",
        "planner_failure",
        "loop_suppressed",
        "policy_suppressed",
        "outbox_not_owned",
    }
)
"""Failure kinds that are permanent and unlikely to succeed on retry."""

OPERATIONAL_KINDS: frozenset[str] = frozenset(
    {
        "capacity_rejection",
        "shutdown_rejection",
        "deadline_exceeded",
    }
)
"""Failure kinds caused by operational conditions (capacity, shutdown, deadline)."""


def failure_category(failure_kind: str | None) -> str:
    """Map a persisted failure-kind string to a recovery category.

    ``None`` maps to ``"unknown"``: receipts without a failure kind carry
    no machine-readable classification and nothing is reconstructed from
    error wording.

    Returns one of: ``"retryable"``, ``"permanent"``, ``"operational"``,
    ``"unknown"``.
    """
    if failure_kind in RETRYABLE_KINDS:
        return "retryable"
    if failure_kind in PERMANENT_KINDS:
        return "permanent"
    if failure_kind in OPERATIONAL_KINDS:
        return "operational"
    return "unknown"


def recommended_commands(
    category: str,
    event_id: str,
    *,
    storage_path: str | None = None,
) -> list[str]:
    """Return recommended next commands for a failure category.

    Generated recommendations prefer ``medre inspect`` commands as the
    primary operator interface.  The ``medre trace event`` command remains
    available as a specialised / lower-level tool but is not the default
    recommendation.

    When *storage_path* is provided, every ``inspect`` and ``recover``
    command includes ``--storage-path {storage_path}`` so the emitted
    commands are valid (argparse enforces ``required=True`` on
    ``--storage-path`` for all read-only subcommands).

    **Replay commands** (``medre replay``) do not include ``--config``
    in the generated string.  Operators may need to append
    ``--config PATH`` depending on deployment layout — the CLI
    auto-discovers the config file via XDG defaults, but explicit
    paths are required when the config lives outside the standard
    search locations.
    """
    sp = f" --storage-path {storage_path}" if storage_path else ""

    if category == "retryable":
        return [
            f"medre inspect event {event_id} --recovery{sp}",
            f"medre replay --mode dry_run --event {event_id}",
            f"medre replay --mode best_effort --event {event_id}",
        ]
    if category == "permanent":
        return [
            f"medre inspect event {event_id} --evidence{sp}",
            f"medre inspect receipts --event {event_id}{sp}",
        ]
    if category == "operational":
        return [
            "medre diagnostics",
            "medre config check",
            f"medre inspect event {event_id} --timeline{sp}",
        ]
    return [f"medre inspect event {event_id} --timeline{sp}"]
