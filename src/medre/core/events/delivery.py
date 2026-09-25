"""Closed delivery-evidence vocabularies shared across core layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast, get_args

DeliveryConfirmationLevel = Literal[
    "unknown",
    "local_queue",
    "local_transport",
    "remote_service",
    "end_to_end",
]
"""Strongest delivery fact proven at adapter hand-off time."""

DELIVERY_CONFIRMATION_LEVEL_VALUES: frozenset[str] = frozenset(
    get_args(DeliveryConfirmationLevel)
)
"""Runtime values accepted for :data:`DeliveryConfirmationLevel`."""


DeliverySource = Literal["live", "retry", "replay"]
"""Dispatch mechanism that produced delivery evidence."""

DELIVERY_SOURCE_VALUES: frozenset[str] = frozenset(get_args(DeliverySource))
"""Runtime values accepted for :data:`DeliverySource`."""


def normalize_replay_run_id(value: str | None) -> str | None:
    """Return canonical replay-run provenance for storage and comparison.

    Replay run IDs are operator identifiers, so surrounding whitespace is not
    semantically meaningful. ``None``, empty strings, and whitespace-only
    strings all represent an unnamed replay.
    """
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def normalize_delivery_provenance(
    source: str,
    replay_run_id: str | None,
) -> tuple[DeliverySource, str | None]:
    """Validate dispatch source and normalize orthogonal replay provenance.

    A named replay may later be dispatched by the retry worker, so
    ``replay_run_id`` is valid for both ``replay`` and ``retry`` evidence. Live
    delivery cannot belong to a replay run.
    """
    if source not in DELIVERY_SOURCE_VALUES:
        raise ValueError(
            f"unknown delivery source {source!r}; "
            f"expected one of {sorted(DELIVERY_SOURCE_VALUES)}"
        )
    normalized_run_id = normalize_replay_run_id(replay_run_id)
    if source == "live" and normalized_run_id is not None:
        raise ValueError("live delivery cannot carry replay_run_id")
    return cast(DeliverySource, source), normalized_run_id


@dataclass(frozen=True, slots=True)
class DeliveryAttemptProvenance:
    """Immutable identity and dispatch provenance for one delivery attempt.

    The delivery pipeline creates this envelope at the point where the exact
    outbox generation and dispatch mechanism are known. Deferred adapters
    carry the same value through asynchronous hand-off and echo it on unified
    delivery feedback. Core validates it against durable outbox authority
    instead of reconstructing lineage from receipt timing.
    """

    event_id: str
    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None
    outbox_id: str
    attempt_number: int
    source: DeliverySource
    replay_run_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "delivery_plan_id",
            "target_adapter",
            "outbox_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number < 1
        ):
            raise ValueError("attempt_number must be an integer >= 1")
        if self.target_channel is not None and not isinstance(self.target_channel, str):
            raise TypeError("target_channel must be a string or None")

        source, replay_run_id = normalize_delivery_provenance(
            self.source, self.replay_run_id
        )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "replay_run_id", replay_run_id)
        object.__setattr__(
            self,
            "target_channel",
            None if self.target_channel in (None, "") else self.target_channel,
        )


DeliveryObservationState = Literal[
    "delivered",
    "failed",
    "rejected",
    "cancelled",
]
"""Terminal transport observation reported after MEDRE hand-off."""

DELIVERY_OBSERVATION_STATE_VALUES: frozenset[str] = frozenset(
    get_args(DeliveryObservationState)
)
"""Runtime values accepted for :data:`DeliveryObservationState`."""
