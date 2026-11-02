"""The operator hard content privacy ceiling as a core policy value.

The runtime builder converts the ``content_ceiling`` configuration
declaration into this policy at startup; configuration is startup-compiled
and a changed ceiling applies to later admissions only. Every consumer of
the ceiling — the admission producer today, the fenced cleanup transition in
later work — derives its windows from this object and may only end earlier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Self

#: Upper bound for one declared ceiling window: 365 days expressed in
#: seconds. Mirrors the load-time bound in :mod:`medre.config.model`.
CONTENT_CEILING_MAX_AGE_SECONDS = 31_536_000


@dataclass(frozen=True)
class ContentCeilingPolicy:
    """One bounded hard content privacy ceiling.

    ``max_age_seconds`` is the window between the trusted local admission
    time and the absolute UTC ceiling instant. It is not a per-message TTL
    negotiated with providers; it bounds how long the relay's own retained
    content may be used at all, including for delivery.
    """

    max_age_seconds: int

    def validate(self) -> Self:
        """Reject an unbounded or ill-typed window before it reaches runtime.

        Raises
        ------
        ValueError
            If ``max_age_seconds`` is not an ``int`` (a boolean is not an
            integer) or lies outside the closed 1..31536000 range.
        """
        if type(self.max_age_seconds) is not int:
            raise ValueError(
                "Content ceiling window must be an integer, got "
                f"{type(self.max_age_seconds).__name__}"
            )
        if not 1 <= self.max_age_seconds <= CONTENT_CEILING_MAX_AGE_SECONDS:
            raise ValueError(
                "Content ceiling window must be between 1 and "
                f"{CONTENT_CEILING_MAX_AGE_SECONDS} seconds, got "
                f"{self.max_age_seconds}"
            )
        return self

    def ceiling_for(self, admitted_at: datetime) -> datetime:
        """Return the absolute UTC ceiling instant for one admission.

        The admission time is a trusted local sample: remote timestamps or
        provider TTL hints never create local authority.
        """
        if admitted_at.tzinfo is None or admitted_at.utcoffset() is None:
            raise ValueError(
                "The content ceiling requires a timezone-aware admission time"
            )
        return admitted_at.astimezone(timezone.utc) + timedelta(
            seconds=self.max_age_seconds
        )
