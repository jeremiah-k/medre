"""Callback-record provenance mirror validation at the transport boundary.

The shared mirror rule in ``contracts.adapter`` keeps the legacy scalar
correlation fields consistent with the immutable attempt envelope adapters
echo back: contradictions must fail construction loudly instead of letting a
callback carry two lineages, and absent mirrors populate from the envelope.
"""

from __future__ import annotations

import pytest

from medre.core.contracts.adapter import (
    OutboundDeliveryObservationRecord,
    OutboundNativeRefRecord,
    QueueTerminalRecord,
)
from medre.core.events import DeliveryAttemptProvenance


def _provenance(**overrides: object) -> DeliveryAttemptProvenance:
    values: dict[str, object] = {
        "event_id": "evt-mirror",
        "delivery_plan_id": "plan-mirror",
        "target_adapter": "mesh-mirror",
        "target_channel": "0",
        "outbox_id": "obox-mirror",
        "attempt_number": 2,
        "source": "retry",
        "replay_run_id": "run-7",
    }
    values.update(overrides)
    return DeliveryAttemptProvenance(**values)  # type: ignore[arg-type]


def test_native_ref_record_populates_mirrors_from_provenance() -> None:
    record = OutboundNativeRefRecord(
        event_id="evt-mirror",
        adapter="mesh-mirror",
        native_channel_id="0",
        native_message_id="pkt-1",
        attempt_provenance=_provenance(),
    )

    assert record.delivery_plan_id == "plan-mirror"
    assert record.outbox_id == "obox-mirror"
    assert record.attempt_number == 2
    assert record.attempt_provenance is not None
    assert record.attempt_provenance.replay_run_id == "run-7"


def test_native_ref_record_rejects_event_id_contradiction() -> None:
    with pytest.raises(
        ValueError,
        match="OutboundNativeRefRecord.event_id contradicts attempt_provenance",
    ):
        OutboundNativeRefRecord(
            event_id="evt-other",
            adapter="mesh-mirror",
            native_channel_id="0",
            native_message_id="pkt-1",
            attempt_provenance=_provenance(),
        )


def test_queue_terminal_record_rejects_adapter_contradiction() -> None:
    with pytest.raises(
        ValueError, match="QueueTerminalRecord.adapter contradicts attempt_provenance"
    ):
        QueueTerminalRecord(
            event_id="evt-mirror",
            adapter="mesh-other",
            outcome="exhausted",
            attempt_provenance=_provenance(),
        )


@pytest.mark.parametrize(
    ("mirror_field", "wrong_value"),
    [
        ("delivery_plan_id", "plan-other"),
        ("outbox_id", "obox-other"),
        ("attempt_number", 3),
    ],
)
def test_observation_record_rejects_mirror_field_contradiction(
    mirror_field: str,
    wrong_value: object,
) -> None:
    values: dict[str, object] = {
        "event_id": "evt-mirror",
        "adapter": "mesh-mirror",
        "state": "delivered",
        mirror_field: wrong_value,
        "attempt_provenance": _provenance(),
    }

    with pytest.raises(
        ValueError,
        match=f"OutboundDeliveryObservationRecord.{mirror_field} contradicts attempt_provenance",
    ):
        OutboundDeliveryObservationRecord(**values)  # type: ignore[arg-type]


def test_queue_terminal_record_rejects_non_positive_attempt_mirror() -> None:
    """A non-positive record mirror contradicts the immutable envelope."""
    with pytest.raises(
        ValueError, match="attempt_number contradicts attempt_provenance"
    ):
        QueueTerminalRecord(
            event_id="evt-mirror",
            adapter="mesh-mirror",
            outcome="exhausted",
            attempt_number=0,
            attempt_provenance=_provenance(),
        )


def test_attempt_envelope_rejects_non_positive_generation() -> None:
    """The envelope itself refuses a non-positive attempt generation."""
    with pytest.raises(ValueError, match="attempt_number must be an integer >= 1"):
        _provenance(attempt_number=0)
