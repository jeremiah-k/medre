"""Closed delivery-evidence vocabularies shared across core layers."""

from __future__ import annotations

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
