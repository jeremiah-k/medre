"""Closed delivery-evidence vocabularies shared across core layers."""

from __future__ import annotations

from typing import Literal, get_args

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
