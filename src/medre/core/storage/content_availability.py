"""Versioned, non-mutating content availability and hold policy.

This is an opt-in policy *preview*. No runtime storage or provider reads are
replaced by these decisions, and no retained bytes are deleted by this module.
A future migration must persist these facts transactionally with admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

Availability = Literal["retained", "unavailable", "denied"]
Scope = Literal["body", "reply_excerpt", "attachment"]


@dataclass(frozen=True, slots=True)
class ContentHold:
    """One bounded consumer intent, not permission to extend a hard ceiling."""

    consumer: str
    scope: Scope
    expires_at: datetime
    mandatory: bool = False
    issued_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.consumer, str)
            or not self.consumer.strip()
            or len(self.consumer) > 128
        ):
            raise ValueError("A bounded consumer identity is required")
        if type(self.mandatory) is not bool:
            raise ValueError("Mandatory must be a boolean")
        if self.scope not in {"body", "reply_excerpt", "attachment"}:
            raise ValueError("Unsupported content scope")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("Hold expiration must be timezone aware")
        if self.issued_at is not None and (
            self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.issued_at >= self.expires_at
        ):
            raise ValueError("Hold issue time must precede expiry and have a timezone")


@dataclass(frozen=True, slots=True)
class AvailabilityPolicy:
    """Explicit versioned policy. Disabled by default in the live runtime."""

    hard_ceiling: datetime
    max_optional_hold: timedelta
    allowed_optional_consumers: frozenset[str] = frozenset()
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unknown availability policy version")
        if self.hard_ceiling.tzinfo is None or self.hard_ceiling.utcoffset() is None:
            raise ValueError("Hard ceiling must be timezone aware")
        if not isinstance(self.allowed_optional_consumers, (set, frozenset)) or any(
            not isinstance(consumer, str) or not consumer.strip() or len(consumer) > 128
            for consumer in self.allowed_optional_consumers
        ):
            raise ValueError("Optional consumers must be bounded non-empty identities")
        object.__setattr__(
            self,
            "allowed_optional_consumers",
            frozenset(self.allowed_optional_consumers),
        )
        if not timedelta(0) < self.max_optional_hold <= timedelta(days=30):
            raise ValueError("Optional hold limit must be in (0,30 days]")


@dataclass(frozen=True, slots=True)
class AvailabilityDecision:
    version: int
    result: Availability
    reason: str
    held_until: datetime | None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unknown availability decision version")
        reasons = {
            "retained": {"bounded_hold"},
            "unavailable": {"bytes_absent", "hard_ceiling_elapsed"},
            "denied": {"consumer_not_authorized", "no_live_hold"},
        }
        if not isinstance(self.result, str) or self.result not in reasons:
            raise ValueError("Unsupported availability decision result")
        if not isinstance(self.reason, str) or self.reason not in reasons[self.result]:
            raise ValueError("Decision reason does not match its result")
        if self.result == "retained":
            if (
                not isinstance(self.held_until, datetime)
                or self.held_until.utcoffset() is None
            ):
                raise ValueError(
                    "A retained decision requires a timezone-aware held_until"
                )
        elif self.held_until is not None:
            raise ValueError("Only a retained decision carries held_until")

    def to_metadata(self) -> dict[str, str | int | None]:
        return {
            "version": self.version,
            "result": self.result,
            "reason": self.reason,
            "held_until": (
                self.held_until.astimezone(timezone.utc).isoformat()
                if self.held_until is not None
                else None
            ),
        }


def preview_content_access(
    *,
    now: datetime,
    scope: Scope,
    available: bool,
    policy: AvailabilityPolicy,
    holds: tuple[ContentHold, ...] = (),
    requester: str | None,
) -> AvailabilityDecision:
    """Assess access without reading bytes, extending holds or making changes.

    A hold is not a read grant: optional consumers require an explicit allowlist,
    while a future runtime must still authenticate the requester at the read.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Observation time must be timezone aware")
    if scope not in {"body", "reply_excerpt", "attachment"}:
        raise ValueError("Unsupported content scope")
    if type(available) is not bool:
        raise ValueError("Content presence must be a boolean")
    if requester is not None and (
        not isinstance(requester, str) or not requester.strip() or len(requester) > 128
    ):
        raise ValueError("Requester must be a bounded non-empty identity")
    # Optional callers need their own live optional hold before learning whether
    # bytes exist or the privacy ceiling has elapsed. Mandatory dependency holds
    # are trusted internal inputs and never grant an optional consumer access.
    if requester is not None and requester not in policy.allowed_optional_consumers:
        return AvailabilityDecision(1, "denied", "consumer_not_authorized", None)
    valid = [
        hold
        for hold in holds
        if hold.scope == scope
        and hold.expires_at > now
        and (hold.issued_at is None or hold.issued_at <= now)
        and (
            (hold.mandatory and requester is None)
            or (
                not hold.mandatory
                and hold.consumer in policy.allowed_optional_consumers
                and hold.issued_at is not None
                and hold.expires_at - hold.issued_at <= policy.max_optional_hold
            )
        )
        and (requester is None or hold.consumer == requester)
    ]
    if requester is not None and not valid:
        return AvailabilityDecision(1, "denied", "no_live_hold", None)
    if not available:
        return AvailabilityDecision(1, "unavailable", "bytes_absent", None)
    if now >= policy.hard_ceiling:
        return AvailabilityDecision(1, "unavailable", "hard_ceiling_elapsed", None)
    if not valid:
        return AvailabilityDecision(1, "denied", "no_live_hold", None)
    until = min(policy.hard_ceiling, max(hold.expires_at for hold in valid))
    return AvailabilityDecision(1, "retained", "bounded_hold", until)


def preview_release_blockers(
    *,
    delivery_hold_candidate: bool,
    incoming_relations: int,
    provider_coverage_known: bool,
) -> tuple[str, ...]:
    """Conservative dependencies; no positive result authorizes deletion."""
    if type(incoming_relations) is not int or incoming_relations < 0:
        raise ValueError("Incoming relation count must be a nonnegative integer")
    if (
        type(delivery_hold_candidate) is not bool
        or type(provider_coverage_known) is not bool
    ):
        raise ValueError("Dependency observations must be booleans")
    blockers: list[str] = []
    if delivery_hold_candidate:
        blockers.append("outstanding_core_delivery")
    if incoming_relations:
        blockers.append("dependent_reply_references")
    if not provider_coverage_known:
        blockers.append("unmeasured_provider_copies")
    return tuple(blockers)
