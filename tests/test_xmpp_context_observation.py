"""The native witness reads XML without borrowing the production XMPP decoder."""

from copy import deepcopy
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest

from scripts.adapter_campaign.ip_peers import xmpp_bridge_context
from scripts.adapter_campaign.plan import Plan
from scripts.analyze_adapter_campaign import summarize
from tests.helpers.campaign_evidence import baseline_report
from scripts.adapter_campaign.baseline import validate_attributions


def test_legacy_attribution_does_not_invent_a_missing_short_name_expectation():
    """Frozen evidence never declared compact-name coverage, so retain its original bar."""
    data = baseline_report()
    case = next(case for case in data["cases"] if case["source"] == "right")
    case["native_receipts"]["left"]["native_details"]["bridge_context"]["source"][
        "sender_short_name"
    ] = "Actual native short name"
    assert summarize(data)["assessment"] == "passed"


def test_explicit_context_property_requires_independent_short_name_expectations():
    """A new targeted property cannot infer compact names from its native receipt."""
    endpoint = {
        "expect_bridge_context": True,
        "expected_source_attribution": {
            "sender_name": "Peer",
            "origin_label": "Network",
            "verification": "unknown",
        },
    }
    with pytest.raises(ValueError, match="expected_source_attribution"):
        validate_attributions({"source": endpoint})
    endpoint["expected_source_attribution"]["sender_short_name"] = None
    validate_attributions({"source": endpoint})


@pytest.mark.parametrize("expectations", [[], {"right": "yes"}, {"unknown": True}])
def test_corrupted_context_expectations_cannot_downgrade_native_analysis(expectations):
    """Reject malformed or unknown destination requirements instead of guessing intent."""
    data = baseline_report()
    data["bridge_context_expectations"] = expectations
    with pytest.raises(ValueError, match="bridge context expectations"):
        summarize(data)


def test_native_prosody_fixture_preserves_authored_claims_and_exact_scope():
    """Parse a retained independent receiver stanza, including escaped Unicode names."""
    wire = fromstring(
        (Path(__file__).parent / "fixtures/xmpp-relay-context.xml").read_bytes()
    )
    actual = xmpp_bridge_context(wire)
    assert actual["source"]["sender_name"] == "Alice & 🦉"
    assert actual["source"]["sender_short_name"] == "alice"
    assert actual["source"]["origin_label"] == "Origin <Network>"
    assert actual["source"]["canonical_event_id"] == "xmpp-attribution-native-20261025"
    assert actual["source"]["native_ref"] == {
        "adapter": "origin-matrix",
        "native_channel_id": "!origin:origin.test",
        "native_message_id": "$original",
        "native_thread_id": None,
    }
    wire.append(deepcopy(wire.find("{urn:medre:bridge-context:1}context")))
    assert xmpp_bridge_context(wire) is None


def test_targeted_xmpp_property_requires_context_without_recampaigning_baseline():
    """An opt-in expectation fails on prefix-only delivery; legacy evidence still passes."""
    data = baseline_report()
    assert summarize(data)["assessment"] == "passed"
    data["bridge_context_expectations"] = {"right": True}
    assert summarize(data)["assessment"] == "failed"


@pytest.mark.parametrize(
    "field", [None, "sender_name", "origin_label", "canonical_event_id"]
)
def test_targeted_native_claims_are_checked_against_canonical_ingress(field):
    """A valid native observation passes; substituted sender, origin or lineage fails."""
    data = baseline_report()
    data["bridge_context_expectations"] = {"right": True}
    for case in data["cases"]:
        if case["source"] != "left":
            continue
        claim = {
            "adapter": "left",
            "platform": "matrix",
            "canonical_event_id": case["core"]["event"]["event_id"],
            "channel": "group:1",
            "sender_id": "native-peer",
            "sender_name": "Known",
            "origin_label": "Network",
            "sender_verification": "unknown",
            "native_ref": None,
        }
        if field is not None:
            claim[field] = "substituted"
        case["native_receipts"]["right"]["native_details"] = {
            "bridge_context": {"schema_version": 1, "source": claim, "relations": []},
        }
    assert (summarize(data)["assessment"] == "passed") is (field is None)


def test_smoke_targeted_context_cannot_pass_without_native_context():
    """The default smoke suite must enforce explicitly required native metadata too."""
    data = baseline_report()
    data["suite"] = "smoke"
    data["cases"] = [case for case in data["cases"] if case["fixture"] == "unicode"]
    for case in data["cases"]:
        case["fixture"] = "smoke"
    data["bridge_context_expectations"] = {"right": True}
    assert summarize(data)["assessment"] == "failed"


def test_trusted_plan_rejects_downgraded_xmpp_requirement():
    """Compare retained flags with the independent plan, including explicit absence claims."""
    data = baseline_report()
    for attribution in data["expected_source_attribution"].values():
        attribution["sender_short_name"] = None
    endpoints = {}
    for name, identity in data["native_expectations"].items():
        endpoints[name] = {
            "transport": identity["transport"],
            "channel": identity["source_channel"],
            "expected_sender": identity["receipt_sender"],
            "peer": {"channel": identity["receipt_channel"]},
            "expected_source_attribution": data["expected_source_attribution"][name],
            "expect_bridge_context": False,
        }
    trusted = Plan(
        Path("/private/source-plan.yaml"),
        endpoints,
        timeout=30.0,
        pacing=0.0,
        settle=0.0,
    )
    data["bridge_context_expectations"] = {"left": True, "right": False}
    assert summarize(data, trusted_plan=trusted)["assessment"] == "passed"
    trusted.endpoints["right"]["expect_bridge_context"] = True
    with pytest.raises(ValueError, match="trusted plan"):
        summarize(data, trusted_plan=trusted)


def test_authored_short_name_must_match_native_claim():
    """Independently declared compact names must match the destination's native record."""
    data = baseline_report()
    data["bridge_context_expectations"] = {"right": True}
    data["expected_source_attribution"]["left"]["sender_short_name"] = "K"
    for case in data["cases"]:
        if case["source"] != "left":
            continue
        claim = {
            "adapter": "left",
            "platform": "matrix",
            "canonical_event_id": case["core"]["event"]["event_id"],
            "channel": "group:1",
            "sender_id": "native-peer",
            "sender_name": "Known",
            "sender_short_name": "K",
            "origin_label": "Network",
            "sender_verification": "unknown",
            "native_ref": None,
        }
        case["native_receipts"]["right"]["native_details"] = {
            "bridge_context": {"schema_version": 1, "source": claim, "relations": []},
        }
    assert summarize(data)["assessment"] == "passed"
    claim = data["cases"][0]["native_receipts"]["right"]["native_details"][
        "bridge_context"
    ]["source"]
    claim["sender_short_name"] = "not-K"
    assert summarize(data)["assessment"] == "failed"


def test_cli_requires_independent_plan_for_xmpp_report(tmp_path, capsys):
    """A report cannot authorize its own weaker XMPP qualification requirements."""
    from scripts.analyze_adapter_campaign import main
    import json

    (tmp_path / "results.json").write_text(json.dumps(baseline_report()))
    assert main([str(tmp_path)]) == 1
    assert "unreadable" in capsys.readouterr().out
