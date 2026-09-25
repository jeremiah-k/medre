"""Attempt-provenance value object and durable-authority validation tests."""

from __future__ import annotations

import pytest

from medre.core.delivery_authority import (
    delivery_attempt_provenance_mismatch,
    delivery_attempt_receipt_provenance_mismatch,
    queued_receipts_for_attempt,
    receipts_for_attempt,
)
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


def _receipt(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "event_id": "evt-1",
        "delivery_plan_id": "plan-1",
        "target_adapter": "mesh-1",
        "target_channel": "0",
        "outbox_id": "outbox-1",
        "attempt_number": 2,
        "status": "queued",
        "source": "retry",
        "replay_run_id": "run-7",
    }
    values.update(overrides)
    return values


def test_attempt_provenance_normalizes_channel_and_replay_run() -> None:
    provenance = _provenance(target_channel="", replay_run_id=" run-7 ")

    assert provenance.target_channel is None
    assert provenance.replay_run_id == "run-7"


@pytest.mark.parametrize(
    "field",
    ["event_id", "delivery_plan_id", "target_adapter", "outbox_id"],
)
def test_attempt_provenance_rejects_blank_identity_fields(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
        _provenance(**{field: "   "})


@pytest.mark.parametrize("bad_attempt", [True, 0, "2", 2.5])
def test_attempt_provenance_rejects_invalid_attempt_number(bad_attempt: object) -> None:
    with pytest.raises(ValueError, match="attempt_number must be an integer >= 1"):
        _provenance(attempt_number=bad_attempt)


def test_attempt_provenance_rejects_non_string_target_channel() -> None:
    with pytest.raises(TypeError, match="target_channel must be a string or None"):
        _provenance(target_channel=7)


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


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"event_id": "evt-2"}, "event_id does not match"),
        ({"target_adapter": "mesh-2"}, "target_adapter does not match"),
        ({"target_channel": "1"}, "target_channel does not match"),
    ],
)
def test_rendering_result_rejects_provenance_field_contradictions(
    kwargs: dict[str, object], fragment: str
) -> None:
    values: dict[str, object] = {
        "event_id": "evt-1",
        "target_adapter": "mesh-1",
        "target_channel": "0",
        "payload": {"text": "hello"},
        "attempt_provenance": _provenance(),
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=fragment):
        RenderingResult(**values)  # type: ignore[arg-type]


def test_attempt_provenance_matches_exact_durable_outbox_generation() -> None:
    assert delivery_attempt_provenance_mismatch(_provenance(), _outbox()) is None


@pytest.mark.parametrize(
    ("row_override", "expected"),
    [
        ({"outbox_id": "other"}, "outbox_id mismatch"),
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


def test_attempt_provenance_matches_exact_immutable_receipt_evidence() -> None:
    assert (
        delivery_attempt_receipt_provenance_mismatch(_provenance(), _receipt()) is None
    )


@pytest.mark.parametrize(
    ("receipt_override", "expected"),
    [
        ({"outbox_id": "other"}, "receipt outbox_id mismatch"),
        ({"target_channel": "1"}, "receipt delivery identity mismatch"),
        ({"attempt_number": 3}, "receipt attempt generation mismatch"),
        ({"source": "live", "replay_run_id": None}, "receipt dispatch source mismatch"),
        ({"replay_run_id": "other-run"}, "receipt replay_run_id mismatch"),
    ],
)
def test_attempt_provenance_rejects_immutable_receipt_contradiction(
    receipt_override: dict[str, object], expected: str
) -> None:
    mismatch = delivery_attempt_receipt_provenance_mismatch(
        _provenance(),
        _receipt(**receipt_override),
    )

    assert mismatch is not None
    assert expected in mismatch


def test_queued_receipt_matching_does_not_hide_identity_corruption() -> None:
    receipt = _receipt(target_channel="wrong-channel")

    matches = queued_receipts_for_attempt(_provenance(), [receipt])

    assert matches == (receipt,)
    mismatch = delivery_attempt_receipt_provenance_mismatch(
        _provenance(),
        matches[0],
    )
    assert mismatch is not None
    assert "receipt delivery identity mismatch" in mismatch


def test_attempt_receipt_matching_includes_nonqueued_evidence() -> None:
    queued = _receipt()
    sent = _receipt(status="sent", target_channel="wrong-channel")
    other = _receipt(outbox_id="other-outbox")

    matches = receipts_for_attempt(_provenance(), [queued, sent, other])

    assert matches == (queued, sent)
    mismatch = delivery_attempt_receipt_provenance_mismatch(
        _provenance(),
        matches[1],
    )
    assert mismatch is not None
    assert "receipt delivery identity mismatch" in mismatch
