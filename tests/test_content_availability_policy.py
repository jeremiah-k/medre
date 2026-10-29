"""Policy preview is bounded, explicit and never a permission to erase bytes."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.storage.content_availability import (
    AvailabilityDecision,
    AvailabilityPolicy,
    ContentHold,
    preview_content_access,
    preview_release_blockers,
)

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def policy():
    return AvailabilityPolicy(
        hard_ceiling=NOW + timedelta(days=1),
        max_optional_hold=timedelta(hours=2),
        allowed_optional_consumers=frozenset({"plugin-a"}),
    )


def hold(
    *,
    consumer="plugin-a",
    scope="reply_excerpt",
    expiry=timedelta(hours=1),
    mandatory=False,
):
    issued = NOW if expiry > timedelta(0) else NOW + expiry - timedelta(hours=1)
    return ContentHold(consumer, scope, NOW + expiry, mandatory, issued)


def test_bound_hold_returns_retained_metadata_without_bytes():
    decision = preview_content_access(
        now=NOW,
        scope="reply_excerpt",
        available=True,
        policy=policy(),
        holds=(hold(),),
        requester="plugin-a",
    )
    assert decision.result == "retained"
    assert decision.held_until == NOW + timedelta(hours=1)
    assert set(decision.to_metadata()) == {"version", "result", "reason", "held_until"}
    assert not any("content" in str(x) for x in decision.to_metadata().values())


@pytest.mark.parametrize(
    "requester,available,result",
    [
        ("untrusted", True, "denied"),
        ("plugin-a", False, "unavailable"),
        ("plugin-a", True, "retained"),
    ],
)
def test_scope_and_consumer_are_explicit(requester, available, result):
    decision = preview_content_access(
        now=NOW,
        scope="reply_excerpt",
        available=available,
        policy=policy(),
        holds=(hold(),),
        requester=requester,
    )
    assert decision.result == result


def test_hard_ceiling_stops_mandatory_and_optional_holds():
    decision = preview_content_access(
        now=NOW + timedelta(days=1),
        scope="reply_excerpt",
        available=True,
        policy=policy(),
        holds=(hold(expiry=timedelta(days=4), mandatory=True),),
        requester=None,
    )
    assert decision.result == "unavailable"


def test_expired_or_excessive_optional_holds_are_not_extended():
    for expiry in (timedelta(seconds=-1), timedelta(hours=3)):
        decision = preview_content_access(
            now=NOW,
            scope="reply_excerpt",
            available=True,
            policy=policy(),
            holds=(hold(expiry=expiry),),
            requester="plugin-a",
        )
        assert decision.result == "denied"


def test_unauthorized_requester_cannot_borrow_another_consumer_hold():
    decision = preview_content_access(
        now=NOW,
        scope="body",
        available=True,
        policy=policy(),
        holds=(hold(scope="body", mandatory=True),),
        requester="plugin-b",
    )
    assert decision.result == "denied"


def test_release_preview_is_fail_closed_for_provider_copies():
    assert preview_release_blockers(
        delivery_hold_candidate=False,
        incoming_relations=0,
        provider_coverage_known=False,
    ) == ("unmeasured_provider_copies",)
    assert (
        len(
            preview_release_blockers(
                delivery_hold_candidate=True,
                incoming_relations=2,
                provider_coverage_known=False,
            )
        )
        == 3
    )


@pytest.mark.parametrize("time", [NOW.replace(tzinfo=None)])
def test_naive_time_is_rejected(time):
    with pytest.raises(ValueError):
        preview_content_access(
            now=time, scope="body", available=True, policy=policy(), requester=None
        )


def test_unauthorized_absence_is_not_disclosed():
    decision = preview_content_access(
        now=NOW,
        scope="body",
        available=False,
        policy=policy(),
        holds=(),
        requester="untrusted",
    )
    assert decision.result == "denied"
    assert decision.reason == "consumer_not_authorized"


def test_optional_holds_cannot_exceed_issue_to_expiry_limit():
    long_hold = ContentHold(
        consumer="plugin-a",
        scope="body",
        expires_at=NOW + timedelta(minutes=5),
        issued_at=NOW - timedelta(hours=3),
    )
    decision = preview_content_access(
        now=NOW,
        scope="body",
        available=True,
        policy=policy(),
        holds=(long_hold,),
        requester="plugin-a",
    )
    assert decision.result == "denied"


@pytest.mark.parametrize("available", [True, False])
def test_allowlisted_requester_without_hold_cannot_probe_presence(available):
    decision = preview_content_access(
        now=NOW,
        scope="body",
        available=available,
        policy=policy(),
        requester="plugin-a",
    )
    assert decision.to_metadata() == {
        "version": 1,
        "result": "denied",
        "reason": "no_live_hold",
        "held_until": None,
    }


def test_mandatory_future_hold_is_not_active():
    future = ContentHold(
        "delivery", "body", NOW + timedelta(hours=2), True, NOW + timedelta(hours=1)
    )
    result = preview_content_access(
        now=NOW,
        scope="body",
        available=True,
        policy=policy(),
        holds=(future,),
        requester=None,
    )
    assert result.result == "denied"


def test_optional_caller_cannot_convert_mandatory_hold_into_read_grant():
    mandatory = hold(scope="body", mandatory=True, expiry=timedelta(hours=4))
    result = preview_content_access(
        now=NOW,
        scope="body",
        available=True,
        policy=policy(),
        holds=(mandatory,),
        requester="plugin-a",
    )
    assert result.result == "denied"
    internal = preview_content_access(
        now=NOW,
        scope="body",
        available=True,
        policy=policy(),
        holds=(mandatory,),
        requester=None,
    )
    assert internal.result == "retained"


def test_version_boolean_is_not_policy_version_one():
    with pytest.raises(ValueError, match="version"):
        AvailabilityPolicy(NOW, timedelta(hours=1), version=True)


def test_negative_dependency_counts_are_not_valid_observations():
    with pytest.raises(ValueError, match="nonnegative"):
        preview_release_blockers(
            delivery_hold_candidate=False,
            incoming_relations=-1,
            provider_coverage_known=True,
        )


@pytest.mark.parametrize(
    "available,requester,holds,observed_at,ceiling",
    [
        (True, "plugin-a", (hold(),), NOW, NOW + timedelta(days=1)),
        (False, "plugin-a", (hold(),), NOW, NOW + timedelta(days=1)),
        (True, "untrusted", (), NOW, NOW + timedelta(days=1)),
        (False, "plugin-a", (), NOW, NOW + timedelta(days=1)),
        (True, "plugin-a", (hold(),), NOW, NOW + timedelta(minutes=30)),
        (
            True,
            "plugin-a",
            (hold(),),
            NOW + timedelta(minutes=30),
            NOW + timedelta(minutes=30),
        ),
    ],
)
def test_availability_metadata_matches_its_schema(
    available, requester, holds, observed_at, ceiling
):
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "docs/schemas/content-availability-decision.schema.json"
        ).read_text()
    )
    result = preview_content_access(
        now=observed_at,
        scope="reply_excerpt",
        available=available,
        policy=replace(policy(), hard_ceiling=ceiling),
        holds=holds,
        requester=requester,
    )
    metadata = result.to_metadata()
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(metadata)
    # jsonschema's RFC 3339 format support is optional in a clean environment.
    # The public serializer must always emit an aware UTC datetime regardless.
    if metadata["held_until"] is not None:
        decoded = datetime.fromisoformat(metadata["held_until"])
        assert decoded.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "updates",
    [
        {"version": True},
        {"version": 2},
        {"result": "unknown"},
        {"reason": "bytes_absent"},
        {"held_until": None},
        {"held_until": NOW.replace(tzinfo=None)},
        {"held_until": "not-a-date"},
        {"result": "denied", "reason": "no_live_hold", "held_until": NOW},
        {"result": "denied", "reason": "bounded_hold", "held_until": None},
    ],
)
def test_inconsistent_decisions_are_refused_before_serialization(updates):
    values = {
        "version": 1,
        "result": "retained",
        "reason": "bounded_hold",
        "held_until": NOW,
    }
    values.update(updates)
    with pytest.raises(ValueError):
        AvailabilityDecision(**values)


def test_omitted_requester_cannot_select_internal_inspection():
    with pytest.raises(TypeError, match="requester"):
        preview_content_access(now=NOW, scope="body", available=False, policy=policy())


def test_optional_hold_is_clamped_at_privacy_ceiling_and_refused_afterward():
    bounded = replace(policy(), hard_ceiling=NOW + timedelta(minutes=30))
    before = preview_content_access(
        now=NOW,
        scope="reply_excerpt",
        available=True,
        policy=bounded,
        holds=(hold(),),
        requester="plugin-a",
    )
    assert before.result == "retained"
    assert before.held_until == bounded.hard_ceiling
    after = preview_content_access(
        now=bounded.hard_ceiling,
        scope="reply_excerpt",
        available=True,
        policy=bounded,
        holds=(hold(),),
        requester="plugin-a",
    )
    assert after.result == "unavailable"
    assert after.reason == "hard_ceiling_elapsed"
    assert after.held_until is None
