"""Validation and immutability of post-handoff observation evidence."""

from __future__ import annotations

import pytest

from medre.core.events import DeliveryObservation


def _observation(**overrides: object) -> DeliveryObservation:
    values: dict[str, object] = {
        "observation_id": "obs-1",
        "event_id": "event-1",
        "delivery_plan_id": "plan-1",
        "target_adapter": "lxmf-main",
        "outbox_id": "outbox-1",
        "attempt_number": 1,
        "state": "delivered",
        "confirmation_level": "unknown",
    }
    values.update(overrides)
    return DeliveryObservation(**values)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("state", "invented", "invalid delivery observation state"),
        ("confirmation_level", "invented", "invalid delivery confirmation level"),
        ("attempt_number", 0, "attempt_number must be >= 1"),
        ("observation_id", "", "observation_id must be non-empty"),
        ("event_id", "", "requires event_id, delivery_plan_id"),
        ("delivery_plan_id", "", "requires event_id, delivery_plan_id"),
        ("target_adapter", "", "requires event_id, delivery_plan_id"),
        ("outbox_id", "", "outbox_id must be non-empty"),
    ],
)
def test_observation_rejects_invalid_evidence(
    field: str, value: object, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        _observation(**{field: value})


def test_observation_freezes_nested_metadata() -> None:
    metadata = {"provider": {"hops": ["first"]}}
    observation = _observation(metadata=metadata)

    metadata["provider"]["hops"].append("later")
    assert observation.metadata["provider"]["hops"] == ("first",)
    with pytest.raises(TypeError, match="immutable mapping"):
        observation.metadata["provider"]["other"] = "changed"
