"""Attempt-bound radio progress never turns ambiguous local handoff into delivery."""

import copy
import json

import pytest

from medre.core.rendering.radio_progress import RadioAttemptProgress
from medre.core.rendering.radio_text import plan_radio_text


def progress() -> RadioAttemptProgress:
    """One multi-packet plan reserved under a specific outbox generation."""
    plan = plan_radio_text(
        "alpha " * 60, max_text_bytes=32, prefix="[East] ", max_parts=64
    )
    assert len(plan.parts) > 3
    return RadioAttemptProgress.bind(outbox_id="outbox-7", attempt_number=3, plan=plan)


def restore(data):
    """Bind retained evidence to the claimed outbox identity, not to itself."""
    return RadioAttemptProgress.from_data(
        data,
        expected_outbox_id="outbox-7",
        expected_attempt_number=3,
        expected_plan_fingerprint=progress().plan.fingerprint,
    )


def test_commit_boundary_precedes_each_native_submission():
    """Only ordered, explicitly observed local handoffs unblock the next packet."""
    initial = progress()
    assert initial.next_unsent == 1
    submitting = initial.begin(1)
    assert submitting.next_unsent is None
    assert not submitting.handoff_complete
    assert initial.next_unsent == 1  # immutable snapshot
    with pytest.raises(ValueError, match="not eligible"):
        submitting.begin(2)
    known = submitting.observe(1, handoff_id="radio:east:packet-1")
    assert known.parts[0].state == "accepted"
    assert known.next_unsent == 2
    assert known.plan is initial.plan
    for ordinal in range(2, len(known.parts) + 1):
        known = known.begin(ordinal).observe(ordinal, handoff_id=f"packet:{ordinal}")
    assert known.handoff_complete
    assert known.next_unsent is None
    assert initial.parts[0].state == "unsent"


def test_crash_before_handoff_confirmation_freezes_retry():
    """Crash recovery freezes the interrupted packet instead of guessing acceptance."""
    in_flight = progress().begin(1)
    loaded = restore(json.loads(json.dumps(in_flight.to_data())))
    after_restart = loaded.after_restart()
    assert after_restart.parts[0].state == "unknown"
    assert after_restart.next_unsent is None
    assert not after_restart.handoff_complete
    with pytest.raises(ValueError, match="not eligible"):
        after_restart.begin(1)
    with pytest.raises(ValueError, match="no active"):
        after_restart.observe(1, handoff_id="late-unproven-ref")
    assert after_restart.after_restart() == after_restart


def test_unconfirmed_native_call_is_not_success():
    """A missing handoff observation remains unknown rather than completing work."""
    submitting = progress().begin(1)
    state = submitting.observe(1)
    assert state.parts[0].state == "unknown"
    assert not state.handoff_complete
    with pytest.raises(ValueError, match="handoff"):
        submitting.observe(1, handoff_id="")


def test_snapshot_roundtrip_preserves_wire_and_generation():
    """JSON round trips preserve accepted evidence and exact immutable wire content."""
    state = progress().begin(1).observe(1, handoff_id="packet-id")
    retained = json.loads(json.dumps(state.to_data()))
    assert restore(retained) == state
    assert state.plan.fingerprint == restore(retained).plan.fingerprint
    assert retained["attempt_number"] == 3


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data["plan"].__setitem__("fingerprint", "0" * 64),
        lambda data: data["plan"]["parts"][0].__setitem__("wire_text", "altered"),
        lambda data: data["plan"]["parts"][0].__setitem__("wire_sha256", "0" * 64),
        lambda data: data["plan"].__setitem__("prefix", "[Other] "),
        lambda data: data["parts"].__setitem__(
            0, {"ordinal": 9, "state": "unsent", "handoff_id": None}
        ),
        lambda data: data.__setitem__("attempt_number", 0),
        lambda data: data.__setitem__("attempt_number", 4),
        lambda data: data.__setitem__("outbox_id", "another-outbox"),
        lambda data: data.__setitem__("schema_version", 2),
        lambda data: data["parts"].pop(),
    ],
)
def test_snapshot_rejects_corrupted_or_rebound_evidence(change):
    """Reject modified wire evidence and claims for a different outbox generation."""
    data = copy.deepcopy(progress().to_data())
    change(data)
    with pytest.raises((TypeError, ValueError)):
        restore(data)


def test_out_of_order_and_forged_completion_are_rejected():
    """A later accepted packet cannot conceal an unsent predecessor."""
    data = progress().to_data()
    data["parts"][1]["state"] = "accepted"
    data["parts"][1]["handoff_id"] = "id2"
    with pytest.raises(ValueError, match="prefix"):
        restore(data)


def test_handoff_id_must_be_nonempty_and_bounded():
    """Local acceptance requires a bounded native reference with real content."""
    state = progress().begin(1)
    for bad in ("", " ", "a" * 1025, 19):
        with pytest.raises(ValueError):
            state.observe(1, handoff_id=bad)


def test_loader_rejects_excessive_parts_before_joining_content():
    """Untrusted storage must not allocate arbitrary reconstruction buffers."""
    data = progress().to_data()
    data["plan"]["parts"] *= 1_000
    with pytest.raises(ValueError, match="part count"):
        restore(data)


def test_loader_rejects_invalid_unhashable_status():
    """Malformed JSON progress is refused without throwing an opaque TypeError."""
    data = progress().to_data()
    data["parts"][0]["state"] = []
    with pytest.raises(ValueError, match="part state"):
        restore(data)


def test_accepted_prefix_survives_restart_without_resending():
    state = progress().begin(1).observe(1, handoff_id="first-native-packet")
    state = state.begin(2)
    restored = restore(json.loads(json.dumps(state.to_data()))).after_restart()
    assert restored.parts[0].state == "accepted"
    assert restored.parts[0].handoff_id == "first-native-packet"
    assert restored.parts[1].state == "unknown"
    assert restored.next_unsent is None
    assert not restored.handoff_complete


def test_loading_interrupted_submission_blocks_late_promotion_without_extra_call():
    """A restored object must be safe before any caller invokes another method."""
    restored = restore(progress().begin(1).to_data())
    assert restored.parts[0].state == "unknown"
    assert restored.next_unsent is None
    with pytest.raises(ValueError, match="no active"):
        restored.observe(1, handoff_id="stale-callback")


@pytest.mark.parametrize("state", ["submitting", "unknown"])
def test_snapshot_cannot_skip_an_unsent_predecessor(state):
    """Only the first non-accepted part may own an uncertain native boundary."""
    data = progress().to_data()
    data["parts"][1]["state"] = state
    with pytest.raises(ValueError, match="prefix"):
        restore(data)


def test_constructor_refuses_mutable_progress_and_untyped_records():
    """Frozen state cannot retain a list that a caller can mutate later."""
    from dataclasses import replace
    from types import SimpleNamespace

    state = progress()
    with pytest.raises(ValueError, match="immutable typed"):
        replace(state, parts=list(state.parts))
    with pytest.raises(ValueError, match="immutable typed"):
        replace(state, parts=(SimpleNamespace(ordinal=1, state="accepted"),))


def test_self_consistent_replacement_plan_cannot_rebind_existing_attempt():
    """Matching newly recomputed digests do not authenticate a changed wire plan."""
    original = progress()
    other_plan = plan_radio_text(
        "".join(part.body for part in original.plan.parts),
        max_text_bytes=original.plan.max_text_bytes,
        prefix="[West] ",
        max_parts=128,
    )
    changed = RadioAttemptProgress.bind(
        outbox_id=original.outbox_id,
        attempt_number=original.attempt_number,
        plan=other_plan,
    )
    with pytest.raises(ValueError, match="authoritative claim"):
        restore(changed.to_data())


@pytest.mark.parametrize("field", ["ordinal", "wire_bytes"])
def test_wire_numeric_fields_require_exact_integer_types(field):
    """JSON equality between integer and float/bool must not bypass validation."""
    data = progress().to_data()
    data["plan"]["parts"][0][field] = float(data["plan"]["parts"][0][field])
    with pytest.raises(ValueError, match="part shape"):
        restore(data)


def test_plan_version_boolean_is_not_an_integer_version():
    """The nested plan version uses the same closed integer contract as its owner."""
    data = progress().to_data()
    data["plan"]["schema_version"] = True
    with pytest.raises(ValueError, match="authoritative claim"):
        restore(data)


def test_progress_rows_are_bounded_before_record_construction():
    """An oversized ledger is rejected before walking arbitrary progress rows."""
    data = progress().to_data()
    data["parts"] *= 1000
    with pytest.raises(ValueError, match="every part"):
        restore(data)


@pytest.mark.parametrize("generation", [True, 3.0, 0])
def test_authoritative_claim_generation_is_strict(generation):
    """The independent claim itself must not accept bools or floating generations."""
    state = progress()
    with pytest.raises(ValueError, match="authoritative radio attempt claim"):
        RadioAttemptProgress.from_data(
            state.to_data(),
            expected_outbox_id=state.outbox_id,
            expected_attempt_number=generation,
            expected_plan_fingerprint=state.plan.fingerprint,
        )


def test_published_progress_schema_accepts_all_local_states_and_rejects_bad_shape():
    """The editor schema describes records; runtime retains semantic authority."""
    from pathlib import Path

    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    from medre.core.rendering.radio_text import MAX_RADIO_TEXT_PARTS

    directory = Path(__file__).parents[1] / "docs/schemas"
    schema = json.loads((directory / "radio-attempt-progress.schema.json").read_text())
    plan_schema = json.loads((directory / "radio-text-plan.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    registry = Registry().with_resource(
        plan_schema["$id"], Resource.from_contents(plan_schema)
    )
    validator = Draft202012Validator(schema, registry=registry)
    assert schema["properties"]["parts"]["maxItems"] == MAX_RADIO_TEXT_PARTS
    initial = progress()
    submitting = initial.begin(1)
    for state in (
        initial,
        submitting,
        submitting.observe(1, handoff_id="native:1"),
        submitting.after_restart(),
    ):
        validator.validate(state.to_data())
    malformed = submitting.to_data()
    malformed["parts"][0]["state"] = "accepted"
    assert not validator.is_valid(malformed)
    malformed["parts"][0]["handoff_id"] = "native:1"
    validator.validate(malformed)
    malformed["private"] = True
    assert not validator.is_valid(malformed)
