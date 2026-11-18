"""A complete JUnit run alone is not a complete diagnostic inventory."""

import pytest

from scripts.ci.check_deadline_evidence import validate_evidence


@pytest.fixture
def evidence():
    return {
        scenario: {
            "scenario": scenario,
            "sdk_invocations": count,
            "mtjk_version": "fixture-version",
            "provider_provenance": {
                "transport": "meshtastic",
                "revision": "a" * 40,
                "sdk_versions": {"mtjk": "fixture-version"},
            },
        }
        for scenario, count in (
            ("positive_control", 1),
            ("equality_refusal", 0),
            ("earlier_plan_deadline_wins", 0),
            ("radio_prefix_boundary", 1),
        )
    }


def test_complete_evidence_passes(evidence):
    validate_evidence(evidence, expected_revision="a" * 40)


def test_consistent_but_unselected_provider_fails(evidence):
    with pytest.raises(ValueError, match="unexpected provider revision"):
        validate_evidence(evidence, expected_revision="b" * 40)


def test_malformed_revision_fails(evidence):
    evidence["positive_control"]["provider_provenance"]["revision"] = "not-a-revision"
    with pytest.raises(ValueError, match="missing verified provider"):
        validate_evidence(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("scenario", "wrong"),
        ("sdk_invocations", 1),
        ("sdk_invocations", False),
        ("provider_provenance", None),
        ("mtjk_version", "wrong"),
    ],
)
def test_refusal_evidence_mismatch_fails(evidence, field, value):
    evidence["equality_refusal"][field] = value
    with pytest.raises(ValueError):
        validate_evidence(evidence)


def test_missing_case_fails(evidence):
    del evidence["radio_prefix_boundary"]
    with pytest.raises(ValueError, match="four expected"):
        validate_evidence(evidence)


def test_mixed_provider_revisions_fail(evidence):
    evidence["equality_refusal"]["provider_provenance"]["revision"] = "b" * 40
    with pytest.raises(ValueError, match="different provider"):
        validate_evidence(evidence)
