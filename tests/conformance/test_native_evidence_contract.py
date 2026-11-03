"""Adversarial conformance tests for the typed native-evidence contract.

These tests attack the envelope and its projection constructors from the
failure directions the contract promises to fail closed on: identity and
generation mismatches, reused provider correlation, duplicate evidence,
stale generations, expired content, malformed provider data, unsupported
scopes, secret- or text-shaped inputs, and restart stability. They also
pin the central rule: ``unresolved`` never becomes permission to resend
and absence of evidence never proves rejection — ``not_observed`` stays
``unresolved`` under the remediation projection.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.core.evidence.native_evidence import (
    NATIVE_EVIDENCE_RESULT_VALUES,
    NativeEvidenceObservation,
    NativeEvidenceResult,
    NativeEvidenceScope,
    NativeEvidenceTier,
    native_evidence_from_deferred_completion,
    native_evidence_from_deferred_failure,
    native_evidence_from_post_handoff,
    native_evidence_not_observed,
)
from tests.helpers.delivery_callbacks import (
    make_attempt_provenance,
    make_deferred_completion,
    make_deferred_failure,
    make_post_handoff_observation,
)

_OBSERVED_AT = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
_LATER_OBSERVED_AT = datetime(2026, 11, 3, 18, 0, 0, tzinfo=timezone.utc)

_PROVIDER = "matrix"
_PIN = "7ba5ae7faef9ba53bdd55c54f6c7392b4eba2b69"
_SOURCE = "tests/conformance/test_native_evidence_contract.py"
_ROW_VERSION = "2026-11-03T12:00:00+00:00"

_SECRET_TOKEN = "syt_opaque_access_token_value"
_MESSAGE_TEXT = "hello operator plaintext body"


def _project_delivered(
    *,
    tier: NativeEvidenceTier = "docker",
    observed_at: datetime = _OBSERVED_AT,
) -> NativeEvidenceObservation:
    observation = make_post_handoff_observation(
        event_id="event-1",
        adapter=_PROVIDER,
        state="delivered",
        outbox_id="outbox-1",
        delivery_plan_id="plan-1",
        attempt_number=2,
        native_message_id="$matrix-event-1",
        native_channel_id="!room-1",
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=2,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    return native_evidence_from_post_handoff(
        observation,
        tier=tier,
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=observed_at,
        observed_row_updated_at=_ROW_VERSION,
    )


def _not_observed(
    *,
    scope: NativeEvidenceScope = "provider_acceptance",
    attempt_number: int = 2,
) -> NativeEvidenceObservation:
    return native_evidence_not_observed(
        event_id="event-1",
        delivery_plan_id="plan-1",
        target_adapter=_PROVIDER,
        target_channel="!room-1",
        outbox_id="outbox-1",
        attempt_number=attempt_number,
        observed_row_updated_at=_ROW_VERSION,
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        tier="docker",
        scope=scope,
        observed_at=_OBSERVED_AT,
    )


# ---------------------------------------------------------------------------
# Identity and generation authority
# ---------------------------------------------------------------------------


def test_projection_takes_attempt_identity_from_core_provenance_only() -> None:
    """Identity, outbox, and generation come from the core-issued envelope.

    The projection signatures accept no identity, outbox, generation,
    content, or payload parameter, so a provider payload can never
    re-state the attempt it claims to be about.
    """
    envelope = _project_delivered()
    assert envelope.event_id == "event-1"
    assert envelope.delivery_plan_id == "plan-1"
    assert envelope.target_adapter == _PROVIDER
    assert envelope.target_channel == "!room-1"
    assert envelope.outbox_id == "outbox-1"
    assert envelope.attempt_number == 2
    assert envelope.identity.complete
    assert envelope.identity.target_channel == "!room-1"

    for function in (
        native_evidence_from_post_handoff,
        native_evidence_from_deferred_completion,
        native_evidence_from_deferred_failure,
    ):
        parameters = set(inspect.signature(function).parameters)
        for forbidden in (
            "event_id",
            "delivery_plan_id",
            "target_adapter",
            "target_channel",
            "outbox_id",
            "attempt_number",
            "identity",
            "provenance",
            "metadata",
            "error",
            "payload",
            "content",
        ):
            assert forbidden not in parameters, (function, forbidden)


def test_identity_mismatch_shapes_fail_closed() -> None:
    """Incomplete identities and non-decision channels are rejected."""
    for broken_field in ("event_id", "delivery_plan_id", "target_adapter"):
        fields = _identity_kwargs()
        fields[broken_field] = ""
        with pytest.raises(ValueError, match="non-empty string"):
            native_evidence_not_observed(**fields)
    fields = _identity_kwargs()
    fields["target_channel"] = ""
    with pytest.raises(ValueError, match="channel"):
        native_evidence_not_observed(**fields)


def _identity_kwargs() -> dict[str, Any]:
    """Full valid keyword payload for the absence-record constructor."""
    return dict(
        event_id="event-1",
        delivery_plan_id="plan-1",
        target_adapter=_PROVIDER,
        target_channel="!room-1",
        outbox_id="outbox-1",
        attempt_number=1,
        observed_row_updated_at=_ROW_VERSION,
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        tier="docker",
        scope="provider_acceptance",
        observed_at=_OBSERVED_AT,
    )


def test_empty_string_channel_is_never_a_channel_decision() -> None:
    """The empty channel string cannot silently widen the identity."""
    with pytest.raises(ValueError, match="channel"):
        NativeEvidenceObservation(
            event_id="event-1",
            delivery_plan_id="plan-1",
            target_adapter=_PROVIDER,
            target_channel="",
            outbox_id="outbox-1",
            attempt_number=1,
            observed_row_updated_at=None,
            provider=_PROVIDER,
            provider_repository=None,
            compatibility_pin=_PIN,
            observation_source=_SOURCE,
            tier="docker",
            scope="provider_acceptance",
            observed_at=_OBSERVED_AT,
            result="unresolved",
        )


def test_stale_evidence_is_an_observation_never_a_permission() -> None:
    """Staleness compares raw generations and never flips the result."""
    envelope = _project_delivered()  # generation 2
    assert envelope.is_stale_for_generation(3)
    assert not envelope.is_stale_for_generation(2)
    assert not envelope.is_stale_for_generation(1)
    assert envelope.result == "accepted"
    assert envelope.resolution == "accepted"

    stale_unobserved = _not_observed(attempt_number=1)
    assert stale_unobserved.is_stale_for_generation(2)
    assert stale_unobserved.resolution == "unresolved"

    with pytest.raises(ValueError):
        envelope.is_stale_for_generation(-1)
    with pytest.raises(ValueError):
        # A bool is not an integer counter, even though bool subclasses int.
        envelope.is_stale_for_generation(True)


# ---------------------------------------------------------------------------
# Result taxonomy and the central rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected_result"),
    [
        ("delivered", "accepted"),
        ("rejected", "unresolved"),
        ("failed", "unresolved"),
        ("cancelled", "unresolved"),
    ],
)
def test_post_handoff_state_mapping_table(
    state: str, expected_result: NativeEvidenceResult
) -> None:
    """Each wrapped post-handoff state projects to its typed result."""
    observation = make_post_handoff_observation(
        event_id="event-1",
        adapter=_PROVIDER,
        state=state,
        outbox_id="outbox-1",
        delivery_plan_id="plan-1",
        attempt_number=1,
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=1,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    envelope = native_evidence_from_post_handoff(
        observation,
        tier="docker",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
    )
    assert envelope.result == expected_result
    assert envelope.scope == "provider_acceptance"


@pytest.mark.parametrize(
    "outcome", ["exhausted", "permanent_failed", "cancelled", "abandoned"]
)
def test_every_deferred_failure_outcome_is_never_submitted(
    outcome: str,
) -> None:
    """The wrapped fact's own contract is termination before hand-off."""
    observation = make_deferred_failure(
        event_id="event-1",
        adapter=_PROVIDER,
        outcome=outcome,  # type: ignore[arg-type]
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=1,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    envelope = native_evidence_from_deferred_failure(
        observation,
        tier="local_integration",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
    )
    assert envelope.result == "definitively_not_submitted"
    assert envelope.scope == "native_handoff"
    assert envelope.native_message_id is None


def test_deferred_completion_never_claims_acceptance() -> None:
    """Even the strongest adapter-reported confirmation stays unresolved."""
    observation = make_deferred_completion(
        event_id="event-1",
        adapter=_PROVIDER,
        native_message_id="$native-1",
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=1,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
        confirmation_level="remote_service",
    )
    envelope = native_evidence_from_deferred_completion(
        observation,
        tier="local_integration",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
    )
    assert envelope.result == "unresolved"
    assert envelope.scope == "native_handoff"
    assert envelope.native_message_id == "$native-1"


def test_not_observed_stays_unresolved_and_never_proves_rejection() -> None:
    """Absence of evidence resolves to unresolved, never to a negative."""
    envelope = _not_observed()
    assert envelope.result == "not_observed"
    assert envelope.resolution == "unresolved"
    assert envelope.resolution != "definitively_not_submitted"
    assert envelope.native_message_id is None
    assert envelope.receipt_id is None
    assert envelope.evidence_reference is None

    # An unobserved attempt and a failed-then-unresolved attempt resolve
    # to the same remediation token; neither licenses a resend.
    observation = make_post_handoff_observation(
        event_id="event-1",
        adapter=_PROVIDER,
        state="failed",
        outbox_id="outbox-1",
        delivery_plan_id="plan-1",
        attempt_number=2,
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=2,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    failed = native_evidence_from_post_handoff(
        observation,
        tier="docker",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
    )
    assert failed.resolution == "unresolved"
    assert failed.result != envelope.result


def test_accepted_without_native_reference_stays_accepted() -> None:
    """A missing provider identifier never invents one or flips the result."""
    observation = make_post_handoff_observation(
        event_id="event-1",
        adapter=_PROVIDER,
        state="delivered",
        outbox_id="outbox-1",
        delivery_plan_id="plan-1",
        attempt_number=2,
        native_message_id=None,
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=2,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    envelope = native_evidence_from_post_handoff(
        observation,
        tier="docker",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
        observed_row_updated_at=_ROW_VERSION,
    )
    assert envelope.result == "accepted"
    assert envelope.native_message_id is None


@pytest.mark.parametrize(
    ("result", "scope"),
    [
        ("accepted", "native_handoff"),
        ("definitively_not_submitted", "recipient_observation"),
    ],
)
def test_forbidden_result_scope_pairings_fail_closed(
    result: NativeEvidenceResult, scope: NativeEvidenceScope
) -> None:
    """Hand-off facts cannot claim acceptance; recipient facts cannot
    claim non-submission."""
    with pytest.raises(ValueError, match="scope"):
        NativeEvidenceObservation(
            event_id="event-1",
            delivery_plan_id="plan-1",
            target_adapter=_PROVIDER,
            target_channel=None,
            outbox_id="outbox-1",
            attempt_number=1,
            observed_row_updated_at=None,
            provider=_PROVIDER,
            provider_repository=None,
            compatibility_pin=_PIN,
            observation_source=_SOURCE,
            tier="docker",
            scope=scope,
            observed_at=_OBSERVED_AT,
            result=result,
        )


# ---------------------------------------------------------------------------
# Correlation, duplication, and stability
# ---------------------------------------------------------------------------


def test_reused_correlation_across_profiles_is_not_one_observation() -> None:
    """The same provider id under two identities is reuse, not equality."""
    first = _project_delivered()
    second = native_evidence_from_post_handoff(
        make_post_handoff_observation(
            event_id="event-2",
            adapter=_PROVIDER,
            state="delivered",
            outbox_id="outbox-2",
            delivery_plan_id="plan-2",
            attempt_number=1,
            native_message_id="$matrix-event-1",
            attempt_provenance=make_attempt_provenance(
                event_id="event-2",
                target_adapter=_PROVIDER,
                outbox_id="outbox-2",
                attempt_number=1,
                delivery_plan_id="plan-2",
                target_channel="!room-2",
            ),
        ),
        tier="docker",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
        observed_row_updated_at=_ROW_VERSION,
    )
    assert first.shares_provider_reference(second)
    assert second.shares_provider_reference(first)
    assert first.evidence_fingerprint != second.evidence_fingerprint
    assert first.identity != second.identity
    assert first != second


def test_shares_provider_reference_requires_both_sides_and_provider() -> None:
    """Reference sharing is provider-scoped and needs a present id."""
    envelope = _project_delivered()
    no_reference = native_evidence_from_post_handoff(
        make_post_handoff_observation(
            event_id="event-1",
            adapter=_PROVIDER,
            state="delivered",
            outbox_id="outbox-1",
            delivery_plan_id="plan-1",
            attempt_number=2,
            native_message_id=None,
            attempt_provenance=make_attempt_provenance(
                event_id="event-1",
                target_adapter=_PROVIDER,
                outbox_id="outbox-1",
                attempt_number=2,
                delivery_plan_id="plan-1",
                target_channel="!room-1",
            ),
        ),
        tier="docker",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
        observed_row_updated_at=_ROW_VERSION,
    )
    assert not envelope.shares_provider_reference(no_reference)
    other_provider = dataclasses.replace(envelope, provider="lxmf")
    assert not envelope.shares_provider_reference(other_provider)
    with pytest.raises(TypeError):
        envelope.shares_provider_reference("not-an-envelope")  # type: ignore[arg-type]


def test_duplicated_provider_evidence_has_equal_fingerprints() -> None:
    """The same fact recorded twice dedupes; a different fact does not."""
    duplicate = _project_delivered(observed_at=_LATER_OBSERVED_AT)
    original = _project_delivered()
    assert duplicate.evidence_fingerprint == original.evidence_fingerprint
    assert duplicate != original  # the observation moment stays a field

    different_generation = _not_observed(attempt_number=3)
    assert different_generation.evidence_fingerprint != original.evidence_fingerprint


def test_persistence_restart_observation_stability() -> None:
    """Equal facts rebuilt after a simulated restart fingerprint equal."""
    first = _project_delivered()
    second = _project_delivered()
    third = _project_delivered(observed_at=_LATER_OBSERVED_AT)
    assert first == second
    assert first.evidence_fingerprint == second.evidence_fingerprint
    assert first.evidence_fingerprint == third.evidence_fingerprint
    # The fingerprint is 64 lowercase hex characters, stable and durable.
    assert len(first.evidence_fingerprint) == 64
    assert first.evidence_fingerprint == first.evidence_fingerprint.lower()
    int(first.evidence_fingerprint, 16)


# ---------------------------------------------------------------------------
# Content and secret discipline
# ---------------------------------------------------------------------------


def test_expired_content_leaves_no_content_surface() -> None:
    """Content expiry cannot change the envelope: it has no content input.

    Two projections of the same provider fact while content is retained
    and after it is collected produce identical envelopes; metadata and
    error text carried by the wrapped observation are never projected.
    """
    observation = make_post_handoff_observation(
        event_id="event-1",
        adapter=_PROVIDER,
        state="delivered",
        outbox_id="outbox-1",
        delivery_plan_id="plan-1",
        attempt_number=2,
        native_message_id="$matrix-event-1",
        metadata={"native.matrix.body_preview": _MESSAGE_TEXT},
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=2,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    envelope = native_evidence_from_post_handoff(
        observation,
        tier="docker",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
        observed_row_updated_at=_ROW_VERSION,
    )
    # The same fact with different metadata projects identically: the
    # envelope cannot see content, retained or expired.
    observation_with_other_metadata = make_post_handoff_observation(
        event_id="event-1",
        adapter=_PROVIDER,
        state="delivered",
        outbox_id="outbox-1",
        delivery_plan_id="plan-1",
        attempt_number=2,
        native_message_id="$matrix-event-1",
        metadata={"native.matrix.other": "different"},
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=2,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    envelope_after_expiry = native_evidence_from_post_handoff(
        observation_with_other_metadata,
        tier="docker",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
        observed_row_updated_at=_ROW_VERSION,
    )
    assert envelope == envelope_after_expiry
    assert envelope.evidence_fingerprint == envelope_after_expiry.evidence_fingerprint
    assert _MESSAGE_TEXT not in repr(envelope)


def test_secrets_and_message_text_can_never_appear() -> None:
    """Error text and metadata tokens never surface in any field."""
    observation = make_post_handoff_observation(
        event_id="event-1",
        adapter=_PROVIDER,
        state="failed",
        outbox_id="outbox-1",
        delivery_plan_id="plan-1",
        attempt_number=2,
        native_message_id="$matrix-event-1",
        error=f"send failed for {_SECRET_TOKEN}",
        metadata={"auth": _SECRET_TOKEN, "body": _MESSAGE_TEXT},
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=2,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    envelope = native_evidence_from_post_handoff(
        observation,
        tier="docker",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
        observed_row_updated_at=_ROW_VERSION,
    )
    rendered = repr(envelope) + str(dataclasses.asdict(envelope))
    assert _SECRET_TOKEN not in rendered
    assert _MESSAGE_TEXT not in rendered
    field_names = {field.name for field in dataclasses.fields(envelope)}
    for forbidden in ("metadata", "error", "payload", "secret", "token", "body"):
        assert forbidden not in field_names


# ---------------------------------------------------------------------------
# Fail-closed construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("tier", "production"),
        ("tier", "fake"),
        ("tier", ""),
        ("scope", "queue_depth"),
        ("scope", "authorization"),
        ("scope", ""),
        ("result", "maybe"),
        ("result", ""),
        ("provider", ""),
        ("provider", "   "),
        ("provider", "mat rix"),
        ("compatibility_pin", ""),
        ("compatibility_pin", "ref with space"),
        ("observation_source", ""),
        ("outbox_id", ""),
        ("receipt_id", "   "),
        ("native_message_id", ""),
        ("evidence_reference", ""),
        ("observed_row_updated_at", "  "),
    ],
)
def test_malformed_provider_data_fails_closed(field_name: str, bad_value: str) -> None:
    """Unknown tokens and empty or whitespace values never construct."""
    fields: dict[str, Any] = dict(
        event_id="event-1",
        delivery_plan_id="plan-1",
        target_adapter=_PROVIDER,
        target_channel="!room-1",
        outbox_id="outbox-1",
        attempt_number=1,
        observed_row_updated_at=_ROW_VERSION,
        provider=_PROVIDER,
        provider_repository=None,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        tier="docker",
        scope="provider_acceptance",
        observed_at=_OBSERVED_AT,
        result="unresolved",
    )
    fields[field_name] = bad_value
    with pytest.raises((ValueError, TypeError), match=field_name):
        NativeEvidenceObservation(**fields)


@pytest.mark.parametrize("bad_attempt", [-1, True, False, "1", 1.0, None])
def test_raw_generation_rejects_non_counter_values(bad_attempt: object) -> None:
    """The generation is a raw non-negative integer counter, never a bool."""
    with pytest.raises((ValueError, TypeError)):
        NativeEvidenceObservation(
            event_id="event-1",
            delivery_plan_id="plan-1",
            target_adapter=_PROVIDER,
            target_channel=None,
            outbox_id="outbox-1",
            attempt_number=bad_attempt,  # type: ignore[arg-type]
            observed_row_updated_at=None,
            provider=_PROVIDER,
            provider_repository=None,
            compatibility_pin=_PIN,
            observation_source=_SOURCE,
            tier="docker",
            scope="provider_acceptance",
            observed_at=_OBSERVED_AT,
            result="unresolved",
        )


def test_naive_observation_timestamp_fails_closed() -> None:
    """A naive timestamp is not an observation fact."""
    naive = datetime(2026, 11, 3, 12, 0, 0)
    assert naive.tzinfo is None
    with pytest.raises(ValueError, match="timezone-aware"):
        NativeEvidenceObservation(
            event_id="event-1",
            delivery_plan_id="plan-1",
            target_adapter=_PROVIDER,
            target_channel=None,
            outbox_id="outbox-1",
            attempt_number=1,
            observed_row_updated_at=None,
            provider=_PROVIDER,
            provider_repository=None,
            compatibility_pin=_PIN,
            observation_source=_SOURCE,
            tier="docker",
            scope="provider_acceptance",
            observed_at=naive,
            result="unresolved",
        )
    with pytest.raises(TypeError):
        NativeEvidenceObservation(
            event_id="event-1",
            delivery_plan_id="plan-1",
            target_adapter=_PROVIDER,
            target_channel=None,
            outbox_id="outbox-1",
            attempt_number=1,
            observed_row_updated_at=None,
            provider=_PROVIDER,
            provider_repository=None,
            compatibility_pin=_PIN,
            observation_source=_SOURCE,
            tier="docker",
            scope="provider_acceptance",
            observed_at="2026-11-03T12:00:00+00:00",  # type: ignore[arg-type]
            result="unresolved",
        )


def test_not_observed_cannot_carry_identifiers() -> None:
    """An absence record cannot smuggle an invented identifier."""
    with pytest.raises(ValueError, match="not_observed"):
        NativeEvidenceObservation(
            event_id="event-1",
            delivery_plan_id="plan-1",
            target_adapter=_PROVIDER,
            target_channel=None,
            outbox_id="outbox-1",
            attempt_number=1,
            observed_row_updated_at=None,
            provider=_PROVIDER,
            provider_repository=None,
            compatibility_pin=_PIN,
            observation_source=_SOURCE,
            tier="docker",
            scope="provider_acceptance",
            observed_at=_OBSERVED_AT,
            result="not_observed",
            native_message_id="$invented-1",
        )


# ---------------------------------------------------------------------------
# Evidence tier and post-handoff negative proof boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["fake_pipeline", "sdk_contract"])
@pytest.mark.parametrize("scope", ["provider_acceptance", "recipient_observation"])
def test_lower_tiers_cannot_claim_native_or_recipient_acceptance(
    tier: NativeEvidenceTier, scope: NativeEvidenceScope
) -> None:
    """A synthetic or SDK-contract statement never claims observed acceptance."""
    fields = _identity_kwargs()
    fields.update(provider_repository=None, tier=tier, scope=scope, result="accepted")
    with pytest.raises(ValueError, match="tier"):
        NativeEvidenceObservation(**fields)


@pytest.mark.parametrize("tier", ["fake_pipeline", "sdk_contract"])
def test_lower_tiers_cannot_claim_provider_non_submission(
    tier: NativeEvidenceTier,
) -> None:
    """A lower-tier provider refusal is not definitive negative proof."""
    fields = _identity_kwargs()
    fields.update(
        provider_repository=None,
        tier=tier,
        scope="provider_acceptance",
        result="definitively_not_submitted",
    )
    with pytest.raises(ValueError, match="tier"):
        NativeEvidenceObservation(**fields)


def test_wrapped_post_handoff_acceptance_respects_the_tier_gate() -> None:
    """A provider callback cannot promote synthetic evidence by projection."""
    for tier in ("fake_pipeline", "sdk_contract"):
        with pytest.raises(ValueError, match="tier"):
            _project_delivered(tier=tier)
    assert _project_delivered(tier="local_integration").result == "accepted"


def test_rejected_after_handoff_never_proves_non_submission() -> None:
    """A post-handoff rejection cannot undo an earlier possible acceptance."""
    observation = make_post_handoff_observation(
        event_id="event-1",
        adapter=_PROVIDER,
        state="rejected",
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=1,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    envelope = native_evidence_from_post_handoff(
        observation,
        tier="local_integration",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
    )
    assert envelope.result == "unresolved"
    assert envelope.resolution == "unresolved"


def test_lower_tiers_keep_the_boundary_between_no_fact_and_pre_handoff() -> None:
    """Absence and pre-handoff refusal remain distinguishable core-seam facts."""
    fields = _identity_kwargs()
    fields["tier"] = "fake_pipeline"
    absent = native_evidence_not_observed(**fields)
    assert absent.resolution == "unresolved"
    observation = make_deferred_failure(
        event_id="event-1",
        adapter=_PROVIDER,
        outcome="cancelled",
        attempt_provenance=make_attempt_provenance(
            event_id="event-1",
            target_adapter=_PROVIDER,
            outbox_id="outbox-1",
            attempt_number=1,
            delivery_plan_id="plan-1",
            target_channel="!room-1",
        ),
    )
    handoff = native_evidence_from_deferred_failure(
        observation,
        tier="fake_pipeline",
        provider=_PROVIDER,
        compatibility_pin=_PIN,
        observation_source=_SOURCE,
        observed_at=_OBSERVED_AT,
    )
    assert handoff.scope == "native_handoff"
    assert handoff.result == "definitively_not_submitted"


def test_whitespace_only_channel_cannot_claim_a_distinct_target() -> None:
    """A blank-looking channel is not a valid concrete destination."""
    fields = _identity_kwargs()
    fields["target_channel"] = "   "
    with pytest.raises(ValueError, match="target_channel"):
        native_evidence_not_observed(**fields)


# ---------------------------------------------------------------------------
# Contract surface
# ---------------------------------------------------------------------------


def test_no_execution_authorization_anywhere() -> None:
    """The vocabulary and the envelope expose no authorization concept."""
    assert "execution_authorized" not in NATIVE_EVIDENCE_RESULT_VALUES
    envelope = _project_delivered()
    for name in dir(envelope):
        assert "authoriz" not in name.lower()
        assert "permission" not in name.lower()
        assert "permit" not in name.lower()


def test_vocabulary_is_closed_and_envelope_is_immutable() -> None:
    """The token sets are exactly the specified closed sets."""
    assert NATIVE_EVIDENCE_RESULT_VALUES == frozenset(
        {"accepted", "definitively_not_submitted", "unresolved", "not_observed"}
    )
    envelope = _project_delivered()
    with pytest.raises(dataclasses.FrozenInstanceError):
        envelope.result = "unresolved"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        envelope.attempt_number = 9  # type: ignore[misc]
