"""Failure-kind classification shared between recover and evidence.

Provides :func:`infer_failure_kind` and :func:`failure_category` — reusable
helpers that reconstruct a best-effort failure classification from the error
message patterns produced by the delivery pipeline.

Public symbols
--------------
* :data:`RETRYABLE_KINDS` — frozenset of retryable failure-kind strings.
* :data:`PERMANENT_KINDS` — frozenset of permanent failure-kind strings.
* :data:`OPERATIONAL_KINDS` — frozenset of operational failure-kind strings.
* :func:`infer_failure_kind` — infer failure-kind from receipt error/status.
* :func:`failure_category` — map a failure-kind to a recovery category.
* :func:`recommended_commands` — suggested next commands for a category.
"""

from __future__ import annotations

__all__ = [
    "RETRYABLE_KINDS",
    "PERMANENT_KINDS",
    "OPERATIONAL_KINDS",
    "infer_failure_kind",
    "failure_category",
    "recommended_commands",
]

# ---------------------------------------------------------------------------
# Failure-kind categories for recovery classification.
# ---------------------------------------------------------------------------

RETRYABLE_KINDS: frozenset[str] = frozenset({"adapter_transient"})
"""Failure kinds that are transient and may succeed on retry."""

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


def infer_failure_kind(error: str | None, status: str) -> str:
    """Infer a failure-kind string from receipt error and status fields.

    The ``DeliveryReceipt`` struct does not persist ``failure_kind`` directly;
    this helper reconstructs a best-effort classification from the error
    message patterns produced by the delivery pipeline.
    """
    err = (error or "").lower()
    # Operational: capacity / shutdown / deadline
    if "delivery_capacity_exceeded" in err or "capacity" in err:
        return "capacity_rejection"
    if "delivery_rejected_shutdown" in err or "shutdown" in err:
        return "shutdown_rejection"
    if "deadline_exceeded" in err or "deadline" in err:
        return "deadline_exceeded"
    # Permanent: renderer / adapter-missing
    if "renderer" in err or "no renderer" in err:
        return "renderer_failure"
    if "adapter_missing" in err or "not registered" in err:
        return "adapter_missing"
    if "planner" in err:
        return "planner_failure"
    # Permanent: policy suppression
    if "policy_suppressed" in err or "route policy denied" in err:
        return "policy_suppressed"
    # Retryable: transient signals
    if any(
        s in err
        for s in ("timeout", "connectionerror", "connection reset", "temporary")
    ):
        return "adapter_transient"
    # dead_lettered implies retries exhausted — was transient
    if status == "dead_lettered":
        return "adapter_transient"
    # Outbox ownership skip
    if "outbox_not_owned" in err or "outbox row not owned" in err:
        return "outbox_not_owned"
    # Default: permanent for unclassifiable failures
    if error:
        return "adapter_permanent"
    return "unknown"


def failure_category(failure_kind: str) -> str:
    """Map a failure-kind string to a recovery category.

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
