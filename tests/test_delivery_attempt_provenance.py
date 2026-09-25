"""Attempt-provenance value object and durable-authority validation tests."""

from __future__ import annotations

import pytest

from medre.core.delivery_authority import delivery_attempt_provenance_mismatch
from medre.core.events import DeliveryAttemptProvenance
from medre.core.rendering.renderer import RenderingResult


def _provenance(**overrides: object) -> DeliveryAttemptProvenance:
    values: dict[str, object] = {
        "event_id": "evt-1",
        "delivery_plan_id": "plan-1",
        "target_adapter": "mesh-1",
        "target_channel": "0",
        "outbox_id": "outbox-1",
        "attempt_number": 2,
        "source": "retry",
        "replay_run_id": " run-7 ",
    }
    values.update(overrides)
    return DeliveryAttemptProvenance(**values)  # type: ignore[arg-type]


def _outbox(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "event_id": "evt-1",
        "delivery_plan_id": "plan-1",
        "target_adapter": "mesh-1",
        "target_channel": "0",
        "outbox_id": "outbox-1",
        "attempt_number": 1,
        "active_attempt": 2,
        "dispatch_source": "retry",
        "replay_run_id": "run-7",
    }
    values.update(overrides)
    return values


def test_attempt_provenance_normalizes_channel_and_replay_run() -> None:
    provenance = _provenance(target_channel="", replay_run_id=" run-7 ")

    assert provenance.target_channel is None
    assert provenance.replay_run_id == "run-7"


def test_attempt_provenance_rejects_live_named_replay() -> None:
    with pytest.raises(ValueError, match="live delivery cannot carry replay_run_id"):
        _provenance(source="live", replay_run_id="run-7")


def test_rendering_result_takes_scalar_mirrors_from_provenance() -> None:
    provenance = _provenance()
    result = RenderingResult(
        event_id="evt-1",
        target_adapter="mesh-1",
        target_channel="0",
        payload={"text": "hello"},
        attempt_provenance=provenance,
    )

    assert result.delivery_plan_id == "plan-1"
    assert result.outbox_id == "outbox-1"
    assert result.attempt_number == 2
    assert result.attempt_provenance is provenance


def test_rendering_result_rejects_scalar_contradiction() -> None:
    with pytest.raises(ValueError, match="outbox_id"):
        RenderingResult(
            event_id="evt-1",
            target_adapter="mesh-1",
            target_channel="0",
            payload={"text": "hello"},
            outbox_id="wrong",
            attempt_provenance=_provenance(),
        )


def test_attempt_provenance_matches_exact_durable_outbox_generation() -> None:
    assert delivery_attempt_provenance_mismatch(_provenance(), _outbox()) is None


@pytest.mark.parametrize(
    ("row_override", "expected"),
    [
        ({"active_attempt": 3}, "attempt generation mismatch"),
        ({"dispatch_source": "live"}, "dispatch source mismatch"),
        ({"replay_run_id": "other-run"}, "replay_run_id mismatch"),
        ({"target_channel": "1"}, "delivery identity mismatch"),
    ],
)
def test_attempt_provenance_rejects_durable_outbox_contradiction(
    row_override: dict[str, object], expected: str
) -> None:
    mismatch = delivery_attempt_provenance_mismatch(
        _provenance(), _outbox(**row_override)
    )

    assert mismatch is not None
    assert expected in mismatch
