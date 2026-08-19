"""Durable ingress value types shared by core admission and storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

IngressProvenance = Literal["live", "recovered", "history"]
IngressWorkStatus = Literal[
    "pending",
    "processing",
    "suppressed_history",
    "completed",
    "failed",
]

INGRESS_PROVENANCE_VALUES: frozenset[str] = frozenset(get_args(IngressProvenance))
INGRESS_WORK_STATUS_VALUES: frozenset[str] = frozenset(get_args(IngressWorkStatus))


class DurableIngressDeferredError(RuntimeError):
    """Signal that durable ingress must remain pending for a later attempt.

    Raised only after canonical ingress admission has succeeded when routing
    cannot safely transfer responsibility to durable delivery state.  The
    ingress worker releases the work row back to ``pending`` without consuming
    the terminal processing-failure budget.  A deferred row is retried on a later
    poll cycle rather than repeatedly reclaimed in the same cycle.
    """

    def __init__(self, event_id: str, reasons: tuple[str, ...]) -> None:
        self.event_id = event_id
        self.reasons = reasons
        detail = ", ".join(reasons) if reasons else "delivery deferred"
        super().__init__(f"durable ingress deferred for {event_id}: {detail}")


@dataclass(frozen=True)
class AdmissionResult:
    """Result of atomically admitting one canonical inbound event."""

    event_id: str
    created: bool
    provenance: IngressProvenance
    work_status: IngressWorkStatus

    @property
    def duplicate(self) -> bool:
        """Return whether admission resolved to an already-known native event."""
        return not self.created


@dataclass(frozen=True)
class IngressWorkItem:
    """Persisted routing work created by durable ingress admission."""

    event_id: str
    provenance: IngressProvenance
    status: IngressWorkStatus
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str
    locked_at: str | None = None
    lease_until: str | None = None
    worker_id: str | None = None


@dataclass(frozen=True)
class IngressWorkerStopResult:
    """Outcome of requesting durable-ingress worker shutdown.

    ``stopped`` is true only when the worker task has actually terminated.
    A false value means shared pipeline/storage dependencies must remain alive
    until a later stop attempt observes completion.
    """

    stopped: bool
    cancellation_requested: bool
    active_event_id: str | None


@dataclass(frozen=True)
class AdapterCheckpoint:
    """Application-owned cursor for one adapter stream."""

    adapter_id: str
    stream: str
    cursor: str
    metadata_json: str
    updated_at: str
