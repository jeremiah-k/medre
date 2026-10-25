"""Shared relay context retains claims without granting native authority."""

from datetime import datetime, timezone
import json
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
    context = build_bridge_context(event())
    context[field] = value
    assert normalize_bridge_context(context) is None


@pytest.mark.parametrize("length,valid", [(1024, True), (1025, False)])
def test_source_claim_bounds(length, valid):
    context = build_bridge_context(
        event(), RelayAttribution(source_sender_label="x" * length)
    )
    assert (context is not None) is valid


def test_normalization_discards_unknown_data_and_copies_nested_claims():
    context = build_bridge_context(event())
    context["source"]["password"] = "PRIVATE_SENTINEL"
    context["relations"][0]["target_fact"] = {"authorization": "owned"}
    normalized = normalize_bridge_context(context)
    assert normalized is not None
    assert "PRIVATE_SENTINEL" not in json.dumps(normalized)
    assert "target_fact" not in json.dumps(normalized)
    normalized["source"]["native_ref"]["native_message_id"] = "changed"
    assert context["source"]["native_ref"]["native_message_id"] == "native-original"
