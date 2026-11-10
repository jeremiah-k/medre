"""Shared relay context retains claims without granting native authority."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.rendering.attribution import RelayAttribution
from medre.core.rendering.bridge_context import (
    build_bridge_context,
    normalize_bridge_context,
)


def event() -> CanonicalEvent:
    """Build a reply with exact source and target references plus private metadata."""
    return CanonicalEvent(
        event_id="canonical-original",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2026, 10, 25, tzinfo=timezone.utc),
        source_adapter="radio-east",
        source_transport_id="native-sender",
        source_channel_id="3",
        parent_event_id=None,
        lineage=(),
        relations=(
            EventRelation(
                relation_type="reply",
                key=None,
                fallback_text=None,
                target_event_id="canonical-target",
                target_native_ref=NativeRef(
                    adapter="radio-east",
                    native_channel_id="3",
                    native_message_id="native-target",
                    native_thread_id="thread",
                ),
            ),
        ),
        payload={"body": "hello"},
        metadata=EventMetadata(custom={"password": "PRIVATE_SENTINEL"}),
        source_native_ref=NativeRef(
            adapter="radio-east",
            native_channel_id="3",
            native_message_id="native-original",
        ),
    )


@pytest.mark.parametrize("raw", [None, "", "Peer"])
def test_context_preserves_raw_names_and_exact_source(raw):
    """Keep raw sender claims and scoped IDs while excluding private event metadata."""
    source = event()
    context = build_bridge_context(
        source,
        RelayAttribution(
            source_sender_label="Peer (unverified)",
            source_sender_label_raw=raw,
            source_sender_verification="unverified",
            source_origin_label="East",
        ),
    )
    assert context is not None
    assert context["source"]["sender_name"] == (
        "Peer (unverified)" if raw is None else raw
    )
    assert context["source"]["sender_id"] == "native-sender"
    assert context["source"]["sender_verification"] == "unverified"
    assert context["source"]["origin_label"] == "East"
    assert context["source"]["native_ref"]["native_message_id"] == "native-original"
    assert context["relations"][0]["target_native_ref"]["native_thread_id"] == "thread"
    assert "PRIVATE_SENTINEL" not in json.dumps(context)
    schema = json.loads(
        (
            Path(__file__).parents[1] / "docs/schemas/bridge-context.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(context)


def test_text_fallback_omits_relation_hints_without_mutating_event():
    """Suppress structured reply hints without changing the canonical source relation."""
    source = event()
    context = build_bridge_context(source, include_relations=False)
    assert context is not None
    assert context["relations"] == []
    assert source.relations[0].target_event_id == "canonical-target"
    assert context["source"]["sender_verification"] == "unknown"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("relations", [{}] * 33),
        ("source", []),
    ],
)
def test_malformed_context_is_ignored(field, value):
    """Discard unsupported versions and invalid context containers instead of trusting them."""
    context = build_bridge_context(event())
    context[field] = value
    assert normalize_bridge_context(context) is None


@pytest.mark.parametrize(
    "ref",
    [
        "not-a-dict",
        {"adapter": "", "native_message_id": "m"},
        {"adapter": "a", "native_message_id": ""},
        {"adapter": 42, "native_message_id": "m"},
        {"adapter": "a", "native_message_id": "m", "native_thread_id": 1},
        {"adapter": "a", "native_message_id": "m" * 1025},
        {"adapter": "a", "native_message_id": "m", "native_channel_id": "c" * 1025},
    ],
)
@pytest.mark.parametrize("location", ["source", "relation"])
def test_malformed_native_reference_is_rejected(ref, location):
    """Reject malformed or unscoped references in both source and relation-target hints."""
    context = build_bridge_context(event())
    assert context is not None
    if location == "source":
        context["source"]["native_ref"] = ref
    else:
        context["relations"][0]["target_native_ref"] = ref
    assert normalize_bridge_context(context) is None


@pytest.mark.parametrize("length,valid", [(1024, True), (1025, False)])
def test_source_claim_bounds(length, valid):
    """Accept the maximum name length and omit context when a claim exceeds it."""
    context = build_bridge_context(
        event(), RelayAttribution(source_sender_label="x" * length)
    )
    assert (context is not None) is valid


def test_normalization_discards_unknown_data_and_copies_nested_claims():
    """Strip unknown claims and detach nested references from the caller’s input."""
    context = build_bridge_context(event())
    context["source"]["password"] = "PRIVATE_SENTINEL"
    context["relations"][0]["target_fact"] = {"authorization": "owned"}
    normalized = normalize_bridge_context(context)
    assert normalized is not None
    assert "PRIVATE_SENTINEL" not in json.dumps(normalized)
    assert "target_fact" not in json.dumps(normalized)
    normalized["source"]["native_ref"]["native_message_id"] = "changed"
    assert context["source"]["native_ref"]["native_message_id"] == "native-original"


def test_normalization_drops_unknown_native_ref_fields_on_both_paths():
    """Drop private source and target reference fields without mutating either input."""
    context = build_bridge_context(event())
    assert context is not None
    source_ref = context["source"]["native_ref"]
    target_ref = context["relations"][0]["target_native_ref"]
    source_ref["private_key"] = "PRIVATE_SENTINEL"
    target_ref["access_token"] = "PRIVATE_SENTINEL"

    normalized = normalize_bridge_context(context)
    assert normalized is not None
    assert "PRIVATE_SENTINEL" not in json.dumps(normalized)
    assert source_ref["private_key"] == "PRIVATE_SENTINEL"
    assert target_ref["access_token"] == "PRIVATE_SENTINEL"


@pytest.mark.parametrize("transport", ["meshtastic", "lxmf", "xmpp"])
def test_canonical_reaction_context_is_transport_neutral(transport: str) -> None:
    """Exact reaction key, packet ID zero and origin claims fit every v1 transport."""
    from msgspec.structs import replace

    original = event()
    reference = NativeRef(
        adapter=transport,
        native_channel_id="channel-0",
        native_message_id="0",
    )
    reaction = replace(
        original,
        event_kind="message.reacted",
        source_adapter=transport,
        source_transport_id="native-node",
        source_channel_id="channel-0",
        source_native_ref=reference,
        relations=(
            EventRelation(
                relation_type="reaction",
                target_event_id=None,
                target_native_ref=reference,
                key="🔥",
                fallback_text="visible original",
            ),
        ),
        payload={"body": "reacted 🔥", "msgtype": "m.emote"},
    )
    context = build_bridge_context(
        reaction,
        RelayAttribution(
            source_platform=transport,
            source_sender_label_raw="Full node name",
            source_sender_short_label_raw="FN",
            source_origin_label="Network",
        ),
    )
    assert context is not None
    assert context["source"]["adapter"] == transport
    assert context["source"]["native_ref"]["native_message_id"] == "0"
    assert context["source"]["sender_name"] == "Full node name"
    assert context["source"]["sender_short_name"] == "FN"
    assert context["relations"][0]["key"] == "🔥"
    assert context["relations"][0]["target_native_ref"]["native_message_id"] == "0"
    assert "meshtastic_replyId" not in json.dumps(context)
    schema = json.loads(
        (
            Path(__file__).parents[1] / "docs/schemas/bridge-context.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(context)
    assert normalize_bridge_context(context) == context
