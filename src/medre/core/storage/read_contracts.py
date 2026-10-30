"""Typed results for scoped canonical content reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent


@dataclass(frozen=True, slots=True)
class ContentBodyRead:
    """One authorized result; denied results never expose presence or bytes."""

    status: Literal["retained", "unavailable", "denied"]
    body: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"retained", "unavailable", "denied"}:
            raise ValueError("Unknown content body read status")
        if (self.status == "retained" and not isinstance(self.body, str)) or (
            self.status != "retained" and self.body is not None
        ):
            raise ValueError("Only retained reads contain a string body")


@dataclass(frozen=True, slots=True)
class AuthorizedEventRead:
    """Closed response; a denied caller receives no event object."""

    status: Literal["retained", "denied"]
    event: "CanonicalEvent | None" = None

    def __post_init__(self) -> None:
        if self.status not in {"retained", "denied"}:
            raise ValueError("Unknown authorized event read status")
        if (self.status == "retained") != (self.event is not None):
            raise ValueError("Only retained authorized reads return an event")


@dataclass(frozen=True, slots=True)
class ReplyContextRead:
    """Authorized quote plus sender context; denial exposes no target bytes."""

    status: Literal["retained", "unavailable", "denied"]
    event: CanonicalEvent | None = None

    def __post_init__(self) -> None:
        if self.status not in {"retained", "unavailable", "denied"}:
            raise ValueError("Unknown reply context read status")
        if (self.status == "denied") != (self.event is None):
            raise ValueError("Only authorized reply reads contain sender context")
