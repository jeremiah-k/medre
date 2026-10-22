"""Transient typing values, separate from canonical messages and presence facts.

These values confer no routing or identity authority. Runtime code must bind
source adapter identity and authorize concrete destinations before admission.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import unicodedata

MAX_TYPING_SECONDS = 30.0
MAX_TYPING_IDENTIFIER_CHARS = 4096


def _identifier(value: str, name: str) -> None:
    """Require a bounded opaque identifier without interpreting native syntax."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_TYPING_IDENTIFIER_CHARS
        or any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in value)
    ):
        raise ValueError(
            f"{name} must be a nonempty bounded opaque identifier without controls"
        )


@dataclass(frozen=True, slots=True)
class TypingActivity:
    """One native actor's short-lived composing state in a concrete conversation.

    Sender identity is a transport claim, not a reconciled or verified actor.
    A false update releases only this sender's lease. Duration is a local
    maximum lifetime, not a server timestamp, delivery guarantee, or presence
    assertion. The adapter may shorten the 30-second host maximum.
    """

    channel_id: str
    sender_id: str
    typing: bool
    duration_seconds: float = MAX_TYPING_SECONDS

    def __post_init__(self) -> None:
        """Reject ambiguous booleans, invalid lifetimes, and unbounded identities."""
        _identifier(self.channel_id, "channel_id")
        _identifier(self.sender_id, "sender_id")
        if type(self.typing) is not bool:
            raise ValueError("typing must be a boolean")
        seconds = self.duration_seconds
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not 0 < seconds <= MAX_TYPING_SECONDS
        ):
            raise ValueError("duration_seconds must be finite and within (0, 30]")


@dataclass(frozen=True, slots=True)
class TypingTarget:
    """One operator-authorized adapter/conversation, never a wildcard or actor."""

    adapter_id: str
    channel_id: str

    def __post_init__(self) -> None:
        """Keep destination identifiers concrete, opaque, and bounded."""
        _identifier(self.adapter_id, "adapter_id")
        _identifier(self.channel_id, "channel_id")


@dataclass(frozen=True, slots=True)
class TypingUpdate:
    """Aggregate relay-identity activity desired at one native destination.

    A positive timeout is a receiver lifetime bound, not evidence of a send.
    A zero timeout clears the relay's indicator. This value never identifies
    the original actors as the native sending account and cannot puppet them.
    """

    target: TypingTarget
    timeout_seconds: float

    def __post_init__(self) -> None:
        """Reject invalid targets and nonfinite, negative, or excessive timeouts."""
        if not isinstance(self.target, TypingTarget):
            raise ValueError("target must be a TypingTarget")
        seconds = self.timeout_seconds
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not 0 <= seconds <= MAX_TYPING_SECONDS
        ):
            raise ValueError("timeout_seconds must be finite and within [0, 30]")

    @property
    def typing(self) -> bool:
        """Return whether the desired destination indicator is active."""
        return self.timeout_seconds > 0
