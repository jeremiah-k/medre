"""Typed effective dispatch-start bound observed from storage authority.

A native submission must not be initiated when the final pre-invocation
clock observation is at or after the earliest applicable effective
deadline. This module carries that bound as an immutable typed value so
the pipeline can derive it once from the same storage-authoritative rows
the native-start fence checks, carry it through rendering, and compare it
against a local UTC clock observation immediately before the adapter
invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

__all__ = ["EffectiveDispatchBound", "EffectiveDispatchBoundSource"]


EffectiveDispatchBoundSource = Literal[
    "event_content_ceiling",
    "frozen_plan_deadline",
    "adopted_target_dependency_deadline",
    "delivery_plan_deadline",
]
"""Which authoritative row contributed the earliest applicable bound."""


@dataclass(frozen=True, slots=True)
class EffectiveDispatchBound:
    """Earliest applicable effective deadline for one native dispatch start.

    ``deadline`` is a timezone-aware UTC instant; enforcement refuses a new
    local native submission when the observed local UTC wall clock is at or
    after it. ``source`` names the authoritative bound that is earliest; the
    other applicable bounds may exist but do not bind first. A ``None``
    observation means no bound is installed and the dispatch-start fence
    stays dormant, exactly like the adoption fence.

    This is a local dispatch-start fence, not an atomic transaction with a
    remote submission: it cannot undo a packet already accepted by a
    transport.
    """

    deadline: datetime
    source: EffectiveDispatchBoundSource

    def __post_init__(self) -> None:
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("Effective dispatch bound must be timezone-aware")
        allowed_sources: tuple[str, ...] = (
            "event_content_ceiling",
            "frozen_plan_deadline",
            "adopted_target_dependency_deadline",
            "delivery_plan_deadline",
        )
        if self.source not in allowed_sources:
            raise ValueError(
                "Unknown effective dispatch bound source: "
                f"{self.source!r} (expected one of {list(allowed_sources)})"
            )
