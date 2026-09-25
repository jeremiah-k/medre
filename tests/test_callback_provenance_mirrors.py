"""Closed adapter delivery contract tests.

The adapter/core boundary carries delivery identity exactly once: asynchronous
feedback owns one immutable :class:`DeliveryAttemptProvenance` envelope, while
transport facts live in the tagged feedback variant.  These tests pin the
closed vocabulary and deep-immutability guarantees that replaced the legacy
scalar-mirror callback records.
"""

from __future__ import annotations

import msgspec
import pytest

from medre.core.contracts.delivery import (
    AdapterHandoffResult,
    DeferredHandoffCompleted,
    DeferredHandoffFailed,
    DeliveryFeedback,
    PostHandoffObservation,
)
from medre.core.events import DeliveryAttemptProvenance


def _provenance(**overrides: object) -> DeliveryAttemptProvenance:
    values: dict[str, object] = {
        "event_id": "evt-contract",
        "delivery_plan_id": "plan-contract",
        "target_adapter": "adapter-contract",
        "target_channel": "room",
        "outbox_id": "outbox-contract",
        "attempt_number": 2,
        "source": "retry",
        "replay_run_id": "run-7",
    }
    values.update(overrides)
    return DeliveryAttemptProvenance(**values)  # type: ignore[arg-type]


def test_handoff_defaults_to_transport_boundary_fact() -> None:
    handoff = AdapterHandoffResult()

    assert handoff.disposition == "transport_handoff"
    assert handoff.confirmation_level == "unknown"
    assert handoff.note == ""


def test_handoff_rejects_unknown_disposition() -> None:
    with pytest.raises(ValueError, match="unknown adapter handoff disposition"):
        AdapterHandoffResult(disposition="sent")  # type: ignore[arg-type]


def test_deferred_handoff_cannot_claim_native_message_id() -> None:
    with pytest.raises(ValueError, match="cannot claim a native_message_id"):
        AdapterHandoffResult(
            disposition="deferred",
            native_message_id="too-early",
        )


def test_deferred_handoff_cannot_claim_transport_confirmation() -> None:
    with pytest.raises(ValueError, match="cannot claim confirmation beyond local queue"):
        AdapterHandoffResult(
            disposition="deferred",
            confirmation_level="local_transport",
        )


def test_feedback_requires_real_attempt_provenance() -> None:
    handoff = AdapterHandoffResult()
    with pytest.raises(TypeError, match="DeferredHandoffCompleted.attempt_provenance"):
        DeferredHandoffCompleted(
            attempt_provenance="not-provenance",  # type: ignore[arg-type]
            handoff=handoff,
        )
    with pytest.raises(TypeError, match="DeferredHandoffFailed.attempt_provenance"):
        DeferredHandoffFailed(
            attempt_provenance="not-provenance",  # type: ignore[arg-type]
            outcome="cancelled",
        )
    with pytest.raises(TypeError, match="PostHandoffObservation.attempt_provenance"):
        PostHandoffObservation(
            attempt_provenance="not-provenance",  # type: ignore[arg-type]
            state="delivered",
        )


def test_deferred_completion_requires_real_handoff_result() -> None:
    with pytest.raises(TypeError, match="handoff must be AdapterHandoffResult"):
        DeferredHandoffCompleted(
            attempt_provenance=_provenance(),
            handoff="not-a-handoff",  # type: ignore[arg-type]
        )


def test_handoff_metadata_is_deeply_immutable() -> None:
    handoff = AdapterHandoffResult(
        metadata={"provider": {"ids": ["one", "two"]}},
    )

    with pytest.raises(TypeError):
        handoff.metadata["other"] = True
    provider = handoff.metadata["provider"]
    assert isinstance(provider, dict)
    with pytest.raises(TypeError):
        provider["state"] = "changed"
    assert provider["ids"] == ("one", "two")


def test_handoff_metadata_rejects_reserved_top_level_keys() -> None:
    with pytest.raises(ValueError, match="reserved by the delivery contract"):
        AdapterHandoffResult(metadata={"status": "sent"})


def test_handoff_metadata_rejects_non_string_keys() -> None:
    with pytest.raises(TypeError, match="metadata keys must be strings"):
        AdapterHandoffResult(
            metadata={"provider": {1: "invalid"}},  # type: ignore[dict-item]
        )


def test_handoff_metadata_rejects_non_finite_numbers() -> None:
    with pytest.raises(TypeError, match="JSON-safe values"):
        AdapterHandoffResult(metadata={"latency": float("nan")})


def test_deferred_completion_requires_transport_handoff_fact() -> None:
    with pytest.raises(ValueError, match="disposition='transport_handoff'"):
        DeferredHandoffCompleted(
            attempt_provenance=_provenance(),
            handoff=AdapterHandoffResult(disposition="deferred"),
        )


def test_deferred_failure_rejects_unknown_outcome() -> None:
    with pytest.raises(ValueError, match="unknown deferred failure outcome"):
        DeferredHandoffFailed(
            attempt_provenance=_provenance(),
            outcome="retrying",  # type: ignore[arg-type]
        )


def test_observation_rejects_unknown_state() -> None:
    with pytest.raises(ValueError, match="unknown delivery observation state"):
        PostHandoffObservation(
            attempt_provenance=_provenance(),
            state="maybe_delivered",  # type: ignore[arg-type]
        )


def test_feedback_variants_carry_only_one_attempt_identity() -> None:
    feedback = DeferredHandoffFailed(
        attempt_provenance=_provenance(),
        outcome="cancelled",
    )

    assert feedback.attempt_provenance.outbox_id == "outbox-contract"
    for legacy_mirror in (
        "event_id",
        "adapter",
        "delivery_plan_id",
        "outbox_id",
        "attempt_number",
    ):
        assert not hasattr(feedback, legacy_mirror)


def test_feedback_tagged_union_round_trips_with_msgspec() -> None:
    feedback: DeliveryFeedback = PostHandoffObservation(
        attempt_provenance=_provenance(),
        state="delivered",
        native_message_id="native-42",
        confirmation_level="remote_service",
        metadata={"provider": {"state": "delivered"}},
    )

    encoded = msgspec.json.encode(feedback)
    decoded = msgspec.json.decode(encoded, type=DeliveryFeedback)

    assert isinstance(decoded, PostHandoffObservation)
    assert decoded.attempt_provenance == feedback.attempt_provenance
    assert decoded.native_message_id == "native-42"
    assert decoded.metadata == feedback.metadata
