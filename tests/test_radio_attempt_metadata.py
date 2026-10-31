"""Radio attempt metadata and continuation assessment validate their own shape."""

import pytest

from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.recovery.radio_assessment import assess_radio_continuation
from medre.core.rendering.radio_progress import RadioPartProgress
from medre.core.storage.radio import RadioAttemptMetadata

_FINGERPRINT = "ab" * 32


def _provenance() -> DeliveryAttemptProvenance:
    return DeliveryAttemptProvenance(
        event_id="evt-1",
        delivery_plan_id="plan-1",
        target_adapter="radio_a",
        target_channel="1",
        outbox_id="obox-1",
        attempt_number=1,
        source="live",
        replay_run_id=None,
    )


def test_metadata_rejects_non_contiguous_ordinals() -> None:
    parts = (
        RadioPartProgress(ordinal=1),
        RadioPartProgress(ordinal=3),
    )
    with pytest.raises(ValueError, match="contiguous progress"):
        RadioAttemptMetadata(_provenance(), parts, 0, _FINGERPRINT)


def test_metadata_rejects_revision_contradicting_progress() -> None:
    parts = (
        RadioPartProgress(ordinal=1, state="submitting"),
        RadioPartProgress(ordinal=2),
    )
    with pytest.raises(ValueError, match="revision contradicts progress"):
        RadioAttemptMetadata(_provenance(), parts, 0, _FINGERPRINT)


@pytest.mark.parametrize(
    "fingerprint",
    ["", "ab", "z" * 64, 64, None],
)
def test_metadata_rejects_invalid_plan_fingerprints(fingerprint) -> None:
    with pytest.raises(ValueError, match="fingerprint is invalid"):
        RadioAttemptMetadata(
            _provenance(), (RadioPartProgress(ordinal=1),), 0, fingerprint
        )


def test_metadata_accepts_consistent_evidence_and_reports_handoff_presence() -> None:
    parts = (
        RadioPartProgress(ordinal=1, state="accepted", handoff_id="h-1"),
        RadioPartProgress(ordinal=2),
    )
    metadata = RadioAttemptMetadata(_provenance(), parts, 2, _FINGERPRINT)
    assert metadata.parts[0].state == "accepted"
    report = assess_radio_continuation(metadata)
    assert report["accepted_parts"] == 1


def test_continuation_assessment_rejects_untyped_evidence() -> None:
    with pytest.raises(TypeError, match="typed progress evidence"):
        assess_radio_continuation({"parts": []})  # type: ignore[arg-type]
