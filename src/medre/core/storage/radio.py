"""Owned radio-attempt checkpoints, separate from delivery lifecycle authority."""

from __future__ import annotations

from dataclasses import dataclass

from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.rendering.radio_progress import RadioAttemptProgress, RadioPartProgress


@dataclass(frozen=True, slots=True)
class StoredRadioAttempt:
    """An immutable checkpoint and its compare-and-swap revision.

    A revision counts retained local part transitions, including inherited
    accepted facts in an explicit continuation. It is not an outbox
    generation, a release version, or evidence of recipient delivery.
    """

    provenance: DeliveryAttemptProvenance
    progress: RadioAttemptProgress
    revision: int

    def __post_init__(self) -> None:
        """Reject inconsistent identity or a revision that contradicts progress."""
        if not isinstance(self.provenance, DeliveryAttemptProvenance) or not isinstance(
            self.progress, RadioAttemptProgress
        ):
            raise ValueError("radio checkpoint requires typed provenance and progress")
        transitions = sum(
            0 if part.state == "unsent" else 1 if part.state == "submitting" else 2
            for part in self.progress.parts
        )
        if (
            self.provenance.outbox_id != self.progress.outbox_id
            or self.provenance.attempt_number != self.progress.attempt_number
            or type(self.revision) is not int
            or self.revision != transitions
        ):
            raise ValueError("radio checkpoint identity or revision is inconsistent")


@dataclass(frozen=True, slots=True)
class RadioAttemptMetadata:
    """Read-only native progress without the retained wire-text plan."""

    provenance: DeliveryAttemptProvenance
    parts: tuple[RadioPartProgress, ...]
    revision: int
    plan_fingerprint: str

    def __post_init__(self) -> None:
        if not self.parts or any(
            part.ordinal != i + 1 for i, part in enumerate(self.parts)
        ):
            raise ValueError("radio metadata ordinals must cover contiguous progress")
        transitions = sum(
            0 if part.state == "unsent" else 1 if part.state == "submitting" else 2
            for part in self.parts
        )
        if type(self.revision) is not int or self.revision != transitions:
            raise ValueError("radio metadata revision contradicts progress")
        if (
            not isinstance(self.plan_fingerprint, str)
            or len(self.plan_fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in self.plan_fingerprint)
        ):
            raise ValueError("radio metadata fingerprint is invalid")

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "outbox_id": self.provenance.outbox_id,
            "attempt_number": self.provenance.attempt_number,
            "plan_fingerprint": self.plan_fingerprint,
            "parts": [
                {
                    "ordinal": part.ordinal,
                    "state": part.state,
                    "handoff_recorded": part.handoff_id is not None,
                }
                for part in self.parts
            ],
            "content_withheld": True,
        }
