"""Strict versioned operator retention policy and activation epochs.

This module is the declared contract for the opt-in ``relay-only``
content-retention policy: a closed, versioned operator declaration, the
durable activation epoch that bounds every sweep to events admitted after
activation, and the closed outcome vocabulary of the bounded aggregate
preview. Declaring a policy collects nothing by itself: the retained mode
never collects, and even ``relay-only`` collection remains governed by the
guarded per-event collector and its ``BEGIN IMMEDIATE`` revalidation.

The preview never authorizes deletion, never selects content values, and
never claims physical erasure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from medre.core.storage.content import (
    UNMEASURED_CONTENT_SCOPES as COLLECTION_UNMEASURED_SCOPES,
)

__all__ = [
    "COLLECTION_UNMEASURED_SCOPES",
    "CandidatePage",
    "CollectionCandidateSource",
    "ContentRetentionPolicy",
    "RETENTION_EPOCH_ACTIVATORS",
    "RETENTION_POLICY_MODES",
    "RETENTION_PREVIEW_OUTCOMES",
    "RetentionEpoch",
    "RETENTION_POLICY_VERSION",
    "RETENTION_INTERVAL_MIN",
    "RETENTION_INTERVAL_MAX",
    "RETENTION_BATCH_MIN",
    "RETENTION_BATCH_MAX",
    "RETENTION_DEFAULT_INTERVAL",
    "RETENTION_DEFAULT_BATCH",
    "normalize_admission_watermark",
    "retention_epoch_grace_seconds",
]

#: Closed mode vocabulary of the operator declaration. ``retain`` is the
#: default and authorizes no collection; ``relay-only`` is the explicit
#: opt-in that the guarded collector acts under.
RETENTION_POLICY_MODES: frozenset[str] = frozenset({"retain", "relay-only"})

#: Closed provenance vocabulary for the durable activation record: the
#: runtime's first start under an enabled relay-only policy, or an explicit
#: operator action.
RETENTION_EPOCH_ACTIVATORS: frozenset[str] = frozenset(
    {"runtime-first-start", "operator"}
)

#: Closed per-event outcome vocabulary of the aggregate preview. The labels
#: deliberately reuse the guarded collector's statuses — a dry-run decision
#: is reported as ``eligible`` — so the preview and the mutation never speak
#: two dialects. ``inconsistent`` covers the collector's fail-closed
#: ``StorageError`` refusals (missing or corrupted admission identity).
RETENTION_PREVIEW_OUTCOMES: tuple[str, ...] = (
    "eligible",
    "blocked",
    "empty",
    "already_collected",
    "inconsistent",
)


RETENTION_POLICY_VERSION = 1
RETENTION_INTERVAL_MIN = 1
RETENTION_INTERVAL_MAX = 86_400
RETENTION_BATCH_MIN = 1
RETENTION_BATCH_MAX = 100
RETENTION_DEFAULT_INTERVAL = 300
RETENTION_DEFAULT_BATCH = 25


def retention_epoch_grace_seconds(interval_seconds: int) -> int:
    """Freshness grace for an already validated retention policy interval."""
    return max(3600, 10 * interval_seconds)


def normalize_admission_watermark(value: str | None) -> str | None:
    """Upper spelling of an instant in the admission index's UTC ISO format.

    Zero microseconds can be stored with or without the fraction. Using the
    full fraction excludes both spellings of the boundary instant without
    applying a function to indexed admission columns or rewriting facts.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("The admission watermark must be a timestamp or absent")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("The admission watermark must be an aware timestamp") from exc
    if instant.utcoffset() is None:
        raise ValueError("The admission watermark must be an aware timestamp")
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class ContentRetentionPolicy:
    """Explicit versioned operator declaration. Disabled by default.

    A ``retain`` mode policy is a declared, observing policy that never
    collects. A ``relay-only`` policy opts the operator in to bounded,
    guarded collection; the cadence knobs bound the runtime worker that a
    separate change installs and do not authorize anything by themselves.
    """

    version: int = RETENTION_POLICY_VERSION
    mode: str = "retain"
    interval_seconds: int = RETENTION_DEFAULT_INTERVAL
    batch_limit: int = RETENTION_DEFAULT_BATCH

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != RETENTION_POLICY_VERSION:
            raise ValueError(
                f"version must be the integer {RETENTION_POLICY_VERSION}, got {self.version!r}"
            )
        if not isinstance(self.mode, str) or self.mode not in RETENTION_POLICY_MODES:
            raise ValueError(
                f"mode must be one of {sorted(RETENTION_POLICY_MODES)!r}, got {self.mode!r}"
            )
        if type(self.interval_seconds) is not int or not (
            RETENTION_INTERVAL_MIN <= self.interval_seconds <= RETENTION_INTERVAL_MAX
        ):
            raise ValueError(
                f"interval_seconds must be an integer in [{RETENTION_INTERVAL_MIN}, {RETENTION_INTERVAL_MAX}], got {self.interval_seconds!r}"
            )
        if (
            type(self.batch_limit) is not int
            or not RETENTION_BATCH_MIN <= self.batch_limit <= RETENTION_BATCH_MAX
        ):
            raise ValueError(
                f"batch_limit must be an integer in [{RETENTION_BATCH_MIN}, {RETENTION_BATCH_MAX}], got {self.batch_limit!r}"
            )

    @property
    def retained(self) -> bool:
        """Whether the policy authorizes no collection at all."""
        return self.mode == "retain"


@dataclass(frozen=True, slots=True)
class RetentionEpoch:
    """One immutable durable activation record.

    ``admission_watermark`` is the store's maximum local admission
    ``created_at`` observed inside the activation transaction; ``None``
    records that the store held no events at activation. Candidates are
    events admitted strictly after the watermark, so pre-activation history
    is never swept. The epoch number is assigned by the durable
    AUTOINCREMENT key and never reused.
    """

    epoch: int
    mode: str
    activated_at: str
    admission_watermark: str | None
    last_observed_active_at: str | None
    activated_by: str

    def __post_init__(self) -> None:
        if type(self.epoch) is not int or self.epoch < 1:
            raise ValueError("A retention epoch number must be a positive integer")
        if self.mode != "relay-only":
            raise ValueError("A retention epoch records the relay-only mode")
        if not isinstance(self.activated_at, str) or not self.activated_at:
            raise ValueError("A retention epoch requires an activation timestamp")
        if self.admission_watermark is not None and (
            not isinstance(self.admission_watermark, str)
            or not self.admission_watermark
        ):
            raise ValueError("The admission watermark must be a timestamp or absent")
        if self.last_observed_active_at is not None and (
            not isinstance(self.last_observed_active_at, str)
            or not self.last_observed_active_at
        ):
            raise ValueError("The heartbeat timestamp must be a timestamp or absent")
        if self.activated_by not in RETENTION_EPOCH_ACTIVATORS:
            raise ValueError("Unknown retention epoch activator")


@dataclass(frozen=True, slots=True)
class CandidatePage:
    """One bounded page of collection candidate event IDs.

    ``next_after`` is the opaque ``(created_at, event_id)`` keyset cursor for
    the next page; ``has_more`` reports whether another page exists.
    """

    event_ids: list[str]
    next_after: tuple[str, str] | None
    has_more: bool


class CollectionCandidateSource(Protocol):
    """Read-only bounded candidate discovery seam.

    The storage implementation pages epoch-scoped candidate event IDs in a
    stable ``(admission created_at, event_id)`` ordering; the runtime
    retention worker consumes this protocol without depending on SQLite.
    """

    async def page_collection_candidates(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
        watermark: str | None,
    ) -> CandidatePage: ...
