"""Owned radio-attempt checkpoints, separate from delivery lifecycle authority."""

from __future__ import annotations

from dataclasses import dataclass

from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.rendering.radio_progress import RadioAttemptProgress


@dataclass(frozen=True, slots=True)
class StoredRadioAttempt:
    """An immutable checkpoint and its compare-and-swap revision.

    A revision counts committed local part transitions. It is not an outbox
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
