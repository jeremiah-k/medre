"""Opt-in rendering policy can land before its provider migrations."""

from datetime import UTC, datetime

import pytest
from msgspec.structs import replace

from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.canonical import RelationTargetFact
from medre.core.rendering import extract_event_text
from medre.core.rendering.attribution import (
    TRUNCATABLE_RELAY_LABELS,
    RelayAttribution,
    resolve_relay_attribution,
    validate_relay_prefix,
)
from medre.core.rendering.relations import (
    compose_relation_text,
    describe_reaction,
    destination_native_message_id,
)


def _event(*relations: EventRelation, kind: str = "message.created") -> CanonicalEvent:
    return CanonicalEvent(
        event_id="event-1",
        event_kind=kind,
        schema_version=1,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        source_adapter="source",
        source_transport_id="sender",
        source_channel_id="channel",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"body": "authored"},
        metadata=EventMetadata(),
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_composition_preserves_all_context_before_authored_body(reverse: bool) -> None:
    relations = (
        EventRelation("reply", None, None, None, "parent"),
        EventRelation("reaction", None, None, "👍🏽", "target"),
    )
    parts = ["[replying to: parent]", 'reacted 👍🏽 to "target"']
    if reverse:
        relations = relations[::-1]
        parts.reverse()
    event = _event(*relations)
    assert extract_event_text(event) == " ".join([*parts, "authored"])
    assert event.relations == relations
    assert event.payload == {"body": "authored"}


@pytest.mark.parametrize(
    "kind", ["message.created", "message.reacted", "message.deleted"]
)
@pytest.mark.parametrize("represented", [False, True])
def test_represented_relations_never_repeat_and_mutation_payload_is_suppressed(
    kind: str, represented: bool
) -> None:
    relation = EventRelation("reaction", None, None, "👍", None)
    event = _event(relation, kind=kind)
    text = compose_relation_text(
        event, "authored", represented=(relation,) if represented else ()
    )
    expected = [] if represented else ["reacted 👍 to an earlier message"]
    if kind == "message.deleted":
        expected.insert(0, "[deleted]")
    if kind == "message.created":
        expected.append("authored")
    assert text == " ".join(expected)


@pytest.mark.parametrize("key", [None, "", " \t", 42])
def test_reaction_keys_cannot_be_guessed_from_authored_payload(key: object) -> None:
    relation = EventRelation("reaction", None, None, key, None)
    with pytest.raises(ValueError, match="explicit nonempty key"):
        extract_event_text(_event(relation))


def test_reaction_preview_is_normalized_and_limited_without_altering_key() -> None:
    relation = EventRelation("reaction", None, None, " 👍🏽 ", "\t" + "é" * 41)
    assert describe_reaction(relation) == 'reacted  👍🏽  to "' + "é" * 40 + '..."'


@pytest.mark.parametrize(
    "relation_type,expected",
    [
        ("reply", "replying to"),
        ("edit", "edit of"),
        ("delete", "delete of"),
        ("thread", "thread on"),
    ],
)
def test_composition_covers_other_relation_kinds(
    relation_type: str, expected: str
) -> None:
    relation = EventRelation(relation_type, None, None, None, "target")
    assert (
        compose_relation_text(_event(relation), "authored")
        == f"[{expected}: target] authored"
    )


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("message.created", "authored"),
        ("message.edited", "[edited] authored"),
        ("message.deleted", "[deleted]"),
        ("message.reacted", "[reacted]"),
    ],
)
def test_relation_free_event_text(kind: str, expected: str) -> None:
    assert extract_event_text(_event(kind=kind)) == expected


@pytest.mark.parametrize(
    "status", ["ambiguous", "out_of_scope", "binding_unavailable", "unresolved_target"]
)
def test_negative_binding_fact_cannot_be_bypassed_by_native_reference(
    status: str,
) -> None:
    relation = EventRelation(
        "reply",
        None,
        NativeRef("target", "4", "123"),
        None,
        None,
        target_fact=RelationTargetFact(status=status),
    )
    assert destination_native_message_id(relation, "target", "4") is None


@pytest.mark.parametrize(
    "adapter,channel,expected",
    [("target", "4", "123"), ("other", "4", None), ("target", "5", None)],
)
def test_binding_is_scoped_to_adapter_and_native_channel(
    adapter, channel, expected
) -> None:
    relation = EventRelation("reply", None, NativeRef("target", "4", "123"), None, None)
    assert destination_native_message_id(relation, adapter, channel) == expected
    assert (
        destination_native_message_id(relation, adapter, channel, require_fact=True)
        is None
    )


def test_positive_fact_takes_precedence_over_raw_reference() -> None:
    relation = EventRelation(
        "reply",
        None,
        NativeRef("other", "5", "wrong"),
        None,
        None,
        target_fact=RelationTargetFact(
            status="bound",
            adapter="target",
            native_channel_id="4",
            native_message_id="123",
        ),
    )
    assert destination_native_message_id(relation, "target", "4") == "123"
    assert (
        destination_native_message_id(
            replace(relation, target_fact=None), "target", "4"
        )
        is None
    )


@pytest.mark.parametrize(
    "template", [None, "", "{sender}: ", "{sender5}[{origin_label}]: "]
)
def test_prefix_validation_supports_declared_placeholders(template: str | None) -> None:
    validate_relay_prefix(template, truncatable=TRUNCATABLE_RELAY_LABELS)


@pytest.mark.parametrize(
    "template",
    [
        "{unsupported}",
        "{sender:>10}",
        "{sender:{sender}}",
        "{unsupported-name}",
        "{sender!r}",
        "{sender.name}",
        "{sender[0]}",
        "{}",
        "{ sender }",
    ],
)
def test_unsupported_prefix_placeholder_fails_before_delivery(template: str) -> None:
    with pytest.raises(ValueError, match="Invalid relay_prefix"):
        validate_relay_prefix(template)


@pytest.mark.parametrize("template", ["{{sender}}", "{literal", "literal}", "{sender}{"])
def test_prefix_validation_preserves_literal_unmatched_braces(template: str) -> None:
    validate_relay_prefix(template)


@pytest.mark.parametrize(
    "route,snapshot,expected",
    [
        (None, "snapshot", "snapshot"),
        ("", "snapshot", ""),
        ("route", "snapshot", "route"),
        (None, "", ""),
        (None, None, "configured"),
    ],
)
def test_origin_precedence_preserves_empty_overrides(route, snapshot, expected) -> None:
    attr = resolve_relay_attribution(
        _event(),
        source_origin_label=route,
        configured_origin_label="configured",
        source_attribution=RelayAttribution(source_origin_label=snapshot),
    )
    assert attr.source_origin_label == expected


def test_snapshot_labels_cannot_override_event_identity() -> None:
    event = replace(
        _event(), source_native_ref=NativeRef("source", "native-channel", "native-id")
    )
    snapshot = RelayAttribution(
        source_adapter_id="wrong",
        source_transport="wrong",
        source_room_or_channel="wrong",
        source_native_channel_id="wrong",
        source_native_message_id="wrong",
    )
    attr = resolve_relay_attribution(event, source_attribution=snapshot)
    assert (
        attr.source_adapter_id,
        attr.source_transport,
        attr.source_room_or_channel,
    ) == ("source", "sender", "channel")
    assert (attr.source_native_channel_id, attr.source_native_message_id) == (
        "native-channel",
        "native-id",
    )
    assert snapshot.source_adapter_id == "wrong"


@pytest.mark.parametrize("has_native_ref", [False, True])
def test_projected_fields_cannot_override_event_identity(has_native_ref: bool) -> None:
    event = _event()
    if has_native_ref:
        event = replace(
            event, source_native_ref=NativeRef("source", "native-channel", "native-id")
        )
    projected = {
        "source_adapter_id": "wrong",
        "source_transport": "wrong",
        "source_room_or_channel": "wrong",
        "source_native_channel_id": "wrong",
        "source_native_message_id": "wrong",
        "source_origin_label": "wrong",
        "source_sender_label": "display",
    }
    attr = resolve_relay_attribution(event, projected_fields=projected)
    assert (
        attr.source_adapter_id,
        attr.source_transport,
        attr.source_room_or_channel,
    ) == ("source", "sender", "channel")
    assert (attr.source_native_channel_id, attr.source_native_message_id) == (
        ("native-channel", "native-id") if has_native_ref else (None, None)
    )
    assert attr.source_origin_label is None
    assert attr.source_sender_label == "display"
    assert projected["source_adapter_id"] == "wrong"


@pytest.mark.parametrize(
    "kind,marker,body",
    [
        ("message.edited", "[edited]", " authored"),
        ("message.deleted", "[deleted]", ""),
        ("message.reacted", "[reacted]", ""),
    ],
)
def test_kind_marker_survives_unrelated_or_represented_relation(kind, marker, body):
    rel = EventRelation("reply", None, None, None, "parent")
    event = _event(rel, kind=kind)
    assert extract_event_text(event) == marker + " [replying to: parent]" + body
    assert compose_relation_text(event, "authored", represented=(rel,)) == marker + body


@pytest.mark.parametrize(
    "kind,relation_type", [("message.edited", "edit"), ("message.deleted", "delete")]
)
def test_represented_mutation_does_not_gain_kind_marker(kind, relation_type):
    rel = EventRelation(relation_type, None, None, None, "parent")
    event = _event(rel, kind=kind)
    expected = "authored" if kind == "message.edited" else ""
    assert compose_relation_text(event, "authored", represented=(rel,)) == expected
