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
    return RadioAttemptProgress.bind(
        outbox_id="outbox-7", attempt_number=3, plan=plan
    )


def restore(data):
    """Bind retained evidence to the claimed outbox identity, not to itself."""
    return RadioAttemptProgress.from_data(
        data, expected_outbox_id="outbox-7", expected_attempt_number=3
    )


def test_commit_boundary_precedes_each_native_submission():
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
    submitting = progress().begin(1)
    state = submitting.observe(1)
    assert state.parts[0].state == "unknown"
    assert not state.handoff_complete
    with pytest.raises(ValueError, match="handoff"):
        submitting.observe(1, handoff_id="")


def test_snapshot_roundtrip_preserves_wire_and_generation():
    state = progress().begin(1).observe(1, handoff_id="packet-id")
    retained = json.loads(json.dumps(state.to_data()))
    assert restore(retained) == state
    assert (
        state.plan.fingerprint
        == restore(retained).plan.fingerprint
    )
    assert retained["attempt_number"] == 3


@pytest.mark.parametrize("change", [
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
])
def test_snapshot_rejects_corrupted_or_rebound_evidence(change):
    data = copy.deepcopy(progress().to_data())
    change(data)
    with pytest.raises((TypeError, ValueError)):
        restore(data)


def test_out_of_order_and_forged_completion_are_rejected():
    data = progress().to_data()
    data["parts"][1]["state"] = "accepted"
    data["parts"][1]["handoff_id"] = "id2"
    with pytest.raises(ValueError, match="prefix"):
        restore(data)


def test_handoff_id_must_be_nonempty_and_bounded():
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
