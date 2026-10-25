"""Uniform native text contracts have complete, independent fixture coverage."""

from copy import deepcopy

import pytest

from scripts.adapter_campaign.baseline import (
    FIXTURES,
    SUITE,
    text,
    turns,
    validate_attributions,
)
from scripts.adapter_campaign.plan import TRANSPORTS
from scripts.analyze_adapter_campaign import summarize
from tests.helpers.campaign_evidence import report


def test_every_transport_has_every_fixed_text_variant():
    """Seven sources fan out four fixtures without confusing case and pair counts."""
    selected = turns(sorted(TRANSPORTS), SUITE)
    assert len(selected) == len(set(selected)) == 28
    assert set(selected) == {
        (source, name) for source in TRANSPORTS for name in FIXTURES
    }
    assert len(turns(sorted(TRANSPORTS), "smoke")) == 7
    with pytest.raises(ValueError, match="suite"):
        turns(TRANSPORTS, "invented")


def test_baseline_words_and_whitespace_are_authored_not_renderer_predictions():
    """Literal HTML and interior whitespace must survive plain-text relay intact."""
    assert text("unicode", "MR ", "matrix") == "MR café ✓ 雪 🦉"
    assert text("lines", "MR ", "matrix") == "MR café ✓\n  second line\tend"
    assert text("literal", "MR ", "xmpp") == "MR café ✓ <b>literal</b> & [text]"


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_budget_fixture_never_generates_oversized_radio_ingress(transport):
    """The same named case can qualify radio truncation through IP-origin traffic."""
    body = text("budget", "MR ", transport)
    assert (len(body.encode()) < 100) is (
        transport in {"meshtastic", "meshcore", "lxmf"}
    )
    assert "café ✓" in body


def baseline_report():
    """Create native/durable observations without calling a production renderer."""
    data = report()
    originals = data["cases"]
    data["cases"] = []
    data["suite"] = SUITE
    data["expected_source_attribution"] = {
        name: {
            "sender_name": "Known",
            "origin_label": "Network",
            "verification": "unknown",
        }
        for name in ("left", "right")
    }
    data["presentation_expectations"] = {
        target: {
            "prefixes": {
                source: "[Known/Network]: "
                for source in ("left", "right")
                if source != target
            },
            "max_text_bytes": None,
        }
        for target in ("left", "right")
    }
    for original in originals:
        source = original["source"]
        target = next(iter(original["native_receipts"]))
        for index, name in enumerate(FIXTURES):
            case = deepcopy(original)
            event_id = f"event-{source}-{index}"
            case["core"]["event"]["event_id"] = event_id
            for row in case["core"]["receipts"] + case["core"]["outbox"]:
                row.update(
                    event_id=event_id,
                    outbox_id=f"outbox-{source}-{index}",
                    delivery_plan_id=f"plan-{source}-{index}",
                )
            case["fixture"] = name
            case["marker"] = original["marker"] + f"-{index} "
            case["sent_text"] = text(
                name, case["marker"], data["native_expectations"][source]["transport"]
            )
            case["core"]["event"]["payload"]["body"] = case["sent_text"]
            case["native_receipts"][target]["text"] = (
                "[Known/Network]: " + case["sent_text"]
            )
            case["core"]["event"].update(
                source_transport_id="native-peer", source_native_ref=None
            )
            if target == "left":
                case["native_receipts"][target]["native_details"] = {
                    "bridge_context": {
                        "schema_version": 1,
                        "source": {
                            "adapter": source,
                            "platform": "xmpp",
                            "canonical_event_id": event_id,
                            "channel": "group:1",
                            "sender_id": "native-peer",
                            "sender_name": "Known",
                            "origin_label": "Network",
                            "sender_verification": "unknown",
                            "native_ref": None,
                        },
                        "relations": [],
                    }
                }
            data["cases"].append(case)
    return data


def test_baseline_analyzer_checks_every_fixture_and_native_presentation():
    """A baseline result has eight source cases and eight independent receipts."""
    result = summarize(baseline_report())
    assert result["assessment"] == "passed"
    assert result["expected_directions"] == 2
    assert result["independent_receipts"] == result["durable_source_cases"] == 8


def test_baseline_requires_source_claims_but_preserves_explicit_absence():
    """Missing fixture declarations fail before starting peers; absent names stay absent."""
    with pytest.raises(ValueError, match="expected_source_attribution"):
        validate_attributions({"source": {}})
    validate_attributions(
        {
            "source": {
                "expected_source_attribution": {
                    "sender_name": None,
                    "origin_label": "",
                    "verification": "unknown",
                }
            }
        }
    )


@pytest.mark.parametrize(
    "field",
    [
        "adapter",
        "platform",
        "sender_name",
        "sender_verification",
        "origin_label",
        "canonical_event_id",
        "sender_id",
        "native_ref",
    ],
)
def test_matrix_source_context_cannot_pass_using_visible_prefix_only(field):
    """Correct visible text cannot hide substituted identity or verification hints."""
    data = baseline_report()
    case = next(case for case in data["cases"] if case["source"] == "right")
    context = case["native_receipts"]["left"]["native_details"]["bridge_context"]
    context["source"][field] = "different"
    assert summarize(data)["assessment"] == "failed"


def test_plain_matrix_context_rejects_invented_relations():
    """Embedded peer claims cannot manufacture replies on plain fixture messages."""
    data = baseline_report()
    case = next(case for case in data["cases"] if case["source"] == "right")
    case["native_receipts"]["left"]["native_details"]["bridge_context"]["relations"] = [
        {"relation_type": "reply"}
    ]
    assert summarize(data)["assessment"] == "failed"


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "body",
        "prefix",
        "text",
        "contract",
        "fixture",
        "event_id",
    ],
)
def test_baseline_analyzer_rejects_delivery_only_or_incomplete_evidence(damage):
    """Markers and terminal receipts cannot hide absent or misrendered fixtures."""
    data = baseline_report()
    case = data["cases"][0]
    target = next(iter(case["native_receipts"]))
    if damage == "missing":
        data["cases"].pop()
    elif damage == "duplicate":
        data["cases"][-1] = deepcopy(case)
    elif damage == "body":
        case["core"]["event"]["payload"]["body"] += " changed"
    elif damage == "prefix":
        case["native_receipts"][target]["text"] = case["sent_text"]
    elif damage == "text":
        case["native_receipts"][target]["text"] += " changed"
    elif damage == "contract":
        del data["presentation_expectations"][target]
    elif damage == "event_id":
        data["cases"][-1]["core"]["event"]["event_id"] = case["core"]["event"][
            "event_id"
        ]
    else:
        case["fixture"] = "invented"
    assert summarize(data)["assessment"] == "failed"
