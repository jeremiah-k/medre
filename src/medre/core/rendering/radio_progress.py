"""Pure attempt-bound radio handoff progress; never native delivery authority.

The caller must persist each returned snapshot atomically with its attempt
identity, before it submits the next packet. This module does not send, lease,
pace, or commit outbox work and does not authorize retry of uncertain packets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Literal, Mapping

from medre.core.rendering.radio_text import (
    MAX_RADIO_SOURCE_BYTES,
    MAX_RADIO_TEXT_PARTS,
    RadioTextPlan,
    plan_radio_text,
)

RadioPartState = Literal["unsent", "submitting", "accepted", "unknown"]
_STATES: frozenset[str] = frozenset({"unsent", "submitting", "accepted", "unknown"})


@dataclass(frozen=True, slots=True)
class RadioPartProgress:
    """Evidence for a single numbered packet within an outbox attempt.

    ``accepted`` means the native send boundary returned a retained local
    handoff identifier. It does not prove RF receipt at a remote device.
    """

    ordinal: int
    state: RadioPartState = "unsent"
    handoff_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.ordinal) is not int
            or not 1 <= self.ordinal <= MAX_RADIO_TEXT_PARTS
        ):
            raise ValueError("invalid radio part ordinal")
        if not isinstance(self.state, str) or self.state not in _STATES:
            raise ValueError("invalid radio part state")
        if (self.state == "accepted") != (self.handoff_id is not None):
            raise ValueError("accepted radio part requires an exclusive handoff ID")
        if self.handoff_id is not None and (
            not isinstance(self.handoff_id, str)
            or not self.handoff_id.strip()
            or len(self.handoff_id) > 1024
        ):
            raise ValueError("invalid radio native handoff reference")


@dataclass(frozen=True, slots=True)
class RadioAttemptProgress:
    """Exact immutable progress over one already-reserved outbox attempt.

    All accepted parts must form a prefix; only the next part may be in flight.
    An unknown part blocks every subsequent packet, including after restart.
    The caller must never infer acceptance from a native reference alone.
    """

    outbox_id: str
    attempt_number: int
    plan: RadioTextPlan
    parts: tuple[RadioPartProgress, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.outbox_id, str)
            or not self.outbox_id
            or len(self.outbox_id) > 1024
        ):
            raise ValueError("outbox identity is required")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt generation must be positive")
        if not isinstance(self.plan, RadioTextPlan):
            raise ValueError("radio attempt requires a valid plan")
        # Verify that the loaded plan is exactly reproducible. No changed
        # budget, prefix, text, marker or digest can silently be adopted.
        if (
            not 1 <= len(self.plan.parts) <= MAX_RADIO_TEXT_PARTS
            or any(not isinstance(part.body, str) for part in self.plan.parts)
            or sum(len(part.body) for part in self.plan.parts) > MAX_RADIO_SOURCE_BYTES
        ):
            raise ValueError("radio attempt plan exceeds bounded source limits")
        original_text = "".join(part.body for part in self.plan.parts)
        expected = plan_radio_text(
            original_text, max_text_bytes=self.plan.max_text_bytes,
            prefix=self.plan.prefix, max_parts=MAX_RADIO_TEXT_PARTS,
        )
        if self.plan != expected:
            raise ValueError(
                "radio attempt plan does not match source and wire digests"
            )
        if len(self.parts) != len(self.plan.parts):
            raise ValueError("radio attempt progress does not cover every part")
        if any(part.ordinal != i for i, part in enumerate(self.parts, 1)):
            raise ValueError("radio attempt ordinals are not contiguous")
        blocked = False
        for part in self.parts:
            if part.state == "accepted":
                if blocked:
                    raise ValueError("accepted radio parts must form a prefix")
            else:
                if part.state in {"submitting", "unknown"}:
                    if any(
                        after.state != "unsent" for after in self.parts[part.ordinal :]
                    ):
                        raise ValueError(
                            "work cannot advance past an uncertain handoff"
                        )
                blocked = True
        if sum(part.state in {"submitting", "unknown"} for part in self.parts) > 1:
            raise ValueError("only one radio part may own the native handoff")

    @classmethod
    def bind(
        cls, *, outbox_id: str, attempt_number: int, plan: RadioTextPlan
    ) -> RadioAttemptProgress:
        """Build the snapshot to commit *before* any native side effect."""
        return cls(
            outbox_id, attempt_number, plan,
            tuple(RadioPartProgress(part.ordinal) for part in plan.parts),
        )

    @property
    def next_unsent(self) -> int | None:
        """Next permissible packet, or None when blocked or fully accepted."""
        for part in self.parts:
            if part.state == "unsent":
                return part.ordinal
            if part.state != "accepted":
                return None
        return None

    @property
    def handoff_complete(self) -> bool:
        """Only all accepted local handoffs can complete this local boundary."""
        return all(part.state == "accepted" for part in self.parts)

    def begin(self, ordinal: int) -> RadioAttemptProgress:
        """Reserve the next packet; persist the result before native send."""
        if type(ordinal) is not int or ordinal != self.next_unsent:
            raise ValueError("radio part not eligible for native submission")
        return self._replace_part(ordinal, RadioPartProgress(ordinal, "submitting"))

    def observe(
        self, ordinal: int, *, handoff_id: str | None = None
    ) -> RadioAttemptProgress:
        """Record a known handoff, or explicitly retain ambiguous acceptance."""
        if type(ordinal) is not int or not 1 <= ordinal <= len(self.parts):
            raise ValueError("radio part ordinal does not belong to this attempt")
        if self.parts[ordinal - 1].state != "submitting":
            raise ValueError("radio part has no active native submission")
        next_part = (
            RadioPartProgress(ordinal, "accepted", handoff_id)
            if handoff_id is not None
            else RadioPartProgress(ordinal, "unknown")
        )
        return self._replace_part(ordinal, next_part)

    def after_restart(self) -> RadioAttemptProgress:
        """A crash during a native call is unknown, never safe to resend."""
        for part in self.parts:
            if part.state == "submitting":
                return self._replace_part(
                    part.ordinal, RadioPartProgress(part.ordinal, "unknown")
                )
        return self

    def _replace_part(
        self, ordinal: int, progress: RadioPartProgress
    ) -> RadioAttemptProgress:
        parts = list(self.parts)
        parts[ordinal - 1] = progress
        return replace(self, parts=tuple(parts))

    def to_data(self) -> dict[str, object]:
        """Produce a closed, JSON-serializable snapshot for an atomic store CAS."""
        plan_data = asdict(self.plan)
        plan_data["parts"] = list(plan_data["parts"])
        return {
            "schema_version": 1,
            "outbox_id": self.outbox_id,
            "attempt_number": self.attempt_number,
            "plan": plan_data,
            "parts": [asdict(part) for part in self.parts],
        }

    @classmethod
    def from_data(
        cls, data: Mapping[str, object], *,
        expected_outbox_id: str, expected_attempt_number: int,
    ) -> RadioAttemptProgress:
        """Reload under independently held attempt identity, refusing rebinding."""
        if not isinstance(data, Mapping) or set(data) != {
            "schema_version", "outbox_id", "attempt_number", "plan", "parts"
        } or type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("unknown radio progress record")
        raw_plan = data["plan"]
        raw_parts = data["parts"]
        if not isinstance(raw_plan, Mapping) or set(raw_plan) != {
            "max_text_bytes", "prefix", "source_sha256", "parts",
            "fingerprint", "schema_version",
        } or not isinstance(raw_parts, list):
            raise ValueError("invalid radio plan or progress record")
        # Reconstruct from the original body fragments, and compare every
        # persisted field to the computed plan to detect changed snapshots.
        stored_wire_parts = raw_plan["parts"]
        if (
            not isinstance(stored_wire_parts, (list, tuple))
            or not 1 <= len(stored_wire_parts) <= MAX_RADIO_TEXT_PARTS
        ):
            raise ValueError("invalid retained radio part count")
        if any(
            not isinstance(part, Mapping) or set(part) != {
                "ordinal", "body", "wire_text", "wire_bytes", "wire_sha256"
            } for part in stored_wire_parts
        ):
            raise ValueError("invalid retained radio part shape")
        if (
            any(not isinstance(part["body"], str) for part in stored_wire_parts)
            or sum(len(part["body"]) for part in stored_wire_parts)
            > MAX_RADIO_SOURCE_BYTES
        ):
            raise ValueError("invalid retained radio body")
        if (
            type(raw_plan["max_text_bytes"]) is not int
            or not isinstance(raw_plan["prefix"], str)
        ):
            raise ValueError("invalid retained radio budget or prefix")
        computed = plan_radio_text(
            "".join(part["body"] for part in stored_wire_parts),
            max_text_bytes=raw_plan["max_text_bytes"],
            prefix=raw_plan["prefix"],
            max_parts=MAX_RADIO_TEXT_PARTS,
        )
        serialized_plan = asdict(computed)
        serialized_plan["parts"] = list(serialized_plan["parts"])
        if dict(raw_plan) != serialized_plan:
            raise ValueError(
                "radio plan content or fingerprint differs from retained evidence"
            )
        if any(
            not isinstance(part, Mapping)
            or set(part) != {"ordinal", "state", "handoff_id"}
            for part in raw_parts
        ):
            raise ValueError("invalid radio progress row")
        progress = tuple(RadioPartProgress(**part) for part in raw_parts)
        outbox_id = data["outbox_id"]
        attempt_number = data["attempt_number"]
        if not isinstance(outbox_id, str) or type(attempt_number) is not int:
            raise ValueError("invalid retained attempt identity")
        if (
            outbox_id != expected_outbox_id
            or attempt_number != expected_attempt_number
        ):
            raise ValueError("retained radio evidence belongs to another attempt")
        return cls(outbox_id, attempt_number, computed, progress)
