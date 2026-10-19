"""Independent visible-message oracles reject delivery-only false positives."""

from types import SimpleNamespace

import pytest
import yaml

from scripts.adapter_campaign.plan import load_plan
from scripts.adapter_campaign.run import native_receipt


def receipt(text, *, transport="matrix", html=None):
    record = {"kind": "received", "sender": "bot", "channel": "3", "text": text}
    if html is not None:
        record["formatted_body"] = html
    peer = SimpleNamespace(name="target", records=[record])
    endpoint = {
        "transport": transport,
        "channel": "3",
        "expected_sender": "bot",
        "peer": {},
        "expected_prefixes": {"source": "[Alice/Forx]: "},
    }
    return peer, endpoint


@pytest.mark.parametrize(
    "prefix", ["", "Alice: ", "[Bob/Forx]: ", "[Alice/Wrong]: ", "[Alice/Forx] "]
)
def test_native_nonce_delivery_does_not_hide_missing_or_wrong_attribution(prefix):
    message = "MR-proof café ✓"
    peer, endpoint = receipt(prefix + message)
    with pytest.raises(AssertionError, match="presentation contract"):
        native_receipt(peer, endpoint, "MR-proof", source="source", message=message)


def test_matrix_body_and_html_must_agree():
    message = "MR-proof café ✓"
    peer, endpoint = receipt("[Alice/Forx]: " + message, html="<p>" + message + "</p>")
    with pytest.raises(AssertionError, match="HTML attribution"):
        native_receipt(peer, endpoint, "MR-proof", source="source", message=message)
    peer.records[0]["formatted_body"] = "<p>[Alice/Forx]: " + message + "</p>"
    assert native_receipt(peer, endpoint, "MR-proof", source="source", message=message)


def test_matrix_html_validation_cannot_be_bypassed_without_prefix_expectation():
    message = "MR-proof café ✓"
    peer, endpoint = receipt(message, html="<p>MR-proof</p><p> café ✓</p>")
    endpoint["expected_prefixes"] = {}
    with pytest.raises(AssertionError, match="HTML attribution"):
        native_receipt(peer, endpoint, "MR-proof", source=None)


def test_matrix_html_distinguishes_visible_breaks_from_inline_formatting():
    message = "MR-proof café ✓"
    peer, endpoint = receipt(message, html="<p>MR-proof</p><p> café ✓</p>")
    endpoint["expected_prefixes"]["source"] = ""
    with pytest.raises(AssertionError, match="HTML attribution"):
        native_receipt(peer, endpoint, "MR-proof", source="source", message=message)
    peer.records[0]["formatted_body"] = "<p>MR-proof<br>café ✓</p>"
    with pytest.raises(AssertionError, match="HTML attribution"):
        native_receipt(peer, endpoint, "MR-proof", source="source", message=message)
    peer.records[0]["formatted_body"] = "<p>MR-proof <strong>café</strong> ✓</p>"
    assert native_receipt(peer, endpoint, "MR-proof", source="source", message=message)


def test_radio_prefix_consumes_wire_budget_and_unicode_is_not_split():
    message = "MR-proof café ✓ " + "🌍" * 80
    expected = (
        ("[Alice/Forx]: " + message)
        .encode("utf-8")[:47]
        .decode("utf-8", errors="ignore")
    )
    peer, endpoint = receipt(expected, transport="meshtastic")
    assert native_receipt(
        peer, endpoint, "MR-proof", source="source", message=message, max_text_bytes=47
    )
    peer.records[0]["text"] += "🌍"
    with pytest.raises(AssertionError, match="byte budget exceeded"):
        native_receipt(
            peer,
            endpoint,
            "MR-proof",
            source="source",
            message=message,
            max_text_bytes=47,
        )


def test_explicitly_unprefixed_expectation_is_verified():
    peer, endpoint = receipt("MR-proof café ✓")
    endpoint["expected_prefixes"]["source"] = ""
    assert native_receipt(
        peer, endpoint, "MR-proof", source="source", message="MR-proof café ✓"
    )


def test_native_plan_requires_both_reference_bridge_presentation_expectations(tmp_path):
    data = {
        "version": 1,
        "runtime_config": "runtime.yaml",
        "endpoints": {
            "chat": {
                "transport": "matrix",
                "channel": "!room:example.org",
                "expected_sender": "@bot:example.org",
                "peer": {},
            },
            "radio": {
                "transport": "meshtastic",
                "channel": "3",
                "expected_sender": "42",
                "peer": {},
            },
        },
    }
    path = tmp_path / "campaign.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="expected_prefixes"):
        load_plan(path, {"matrix", "meshtastic"})
    data["endpoints"]["chat"]["expected_prefixes"] = {"radio": "[Alice/Forx]: "}
    data["endpoints"]["radio"]["expected_prefixes"] = {"chat": "Alice[M]: "}
    path.write_text(yaml.safe_dump(data))
    assert len(load_plan(path, {"matrix", "meshtastic"}).pairs) == 2


def test_meshcore_body_budget_excludes_its_checked_firmware_sender_header():
    """Keep native sender evidence separate from the renderer's text budget."""
    message = "MR-proof café ✓ " + "🌍" * 80
    body = ("[Alice/Forx]: " + message).encode()[:47].decode(errors="ignore")
    peer, endpoint = receipt("bot: " + body, transport="meshcore")
    assert native_receipt(
        peer, endpoint, "MR-proof", source="source", message=message, max_text_bytes=47
    )
    peer.records[0]["text"] = "other: " + body
    with pytest.raises(AssertionError, match="native sender header"):
        native_receipt(
            peer,
            endpoint,
            "MR-proof",
            source="source",
            message=message,
            max_text_bytes=47,
        )


def test_non_reference_pair_requires_a_presentation_expectation(tmp_path):
    """Every selected pair needs an independent visible-prefix oracle."""
    data = {
        "version": 1,
        "runtime_config": "runtime.yaml",
        "endpoints": {
            name: {
                "transport": name,
                "channel": "room",
                "expected_sender": "bot",
                "peer": {},
            }
            for name in ("xmpp", "chatmail")
        },
    }
    path = tmp_path / "campaign.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="expected_prefixes"):
        load_plan(path, {"xmpp", "chatmail"})
