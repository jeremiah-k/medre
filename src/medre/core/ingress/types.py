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
class AdapterCheckpoint:
    """Application-owned cursor for one adapter stream."""

    adapter_id: str
    stream: str
    cursor: str
    metadata_json: str
    updated_at: str
