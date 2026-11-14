"""Pinned MMRelay retains its declared rendering import and body contract."""

from datetime import UTC, datetime

import pytest

from medre.core.events import CanonicalEvent, EventMetadata, EventRelation
from medre.core.rendering import extract_relation_text
from medre.core.rendering.text_helpers import extract_event_text


@pytest.mark.parametrize(
    ("kind", "relation", "expected"),
    [
        ("message.created", None, "authored"),
        (
            "message.created",
            EventRelation("reply", "parent", None, None, "parent body"),
            "[replying to: parent body] authored",
        ),
        (
            "message.reacted",
            EventRelation("reaction", "parent", None, "👍", "parent body"),
            "source reacted with 👍",
        ),
        (
            "message.deleted",
            EventRelation("delete", "parent", None, None, "parent body"),
            "[deleted: parent body]",
        ),
    ],
)
def test_pinned_mmrelay_extractor_retains_existing_behavior(kind, relation, expected):
    # MMRelay 427cd13f imports this helper and compares it with the authored
    # Matrix body before accepting a proposed canonical outbound operation.
    event = CanonicalEvent(
        event_id="event",
        event_kind=kind,
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="source",
        source_transport_id="sender",
        source_channel_id="channel",
        parent_event_id=None,
        lineage=(),
        relations=(relation,) if relation else (),
        payload={"body": "authored"},
        metadata=EventMetadata(),
    )
    assert extract_relation_text(event) == expected
    assert event.payload == {"body": "authored"}


def test_shared_policy_preserves_ordinary_message_with_secondary_reaction():
    event = CanonicalEvent(
        event_id="ordinary",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="source",
        source_transport_id="sender",
        source_channel_id="channel",
        parent_event_id=None,
        lineage=(),
        relations=(EventRelation("reaction", None, None, "👍", None),),
        payload={"body": "please check this"},
        metadata=EventMetadata(),
    )
    rendered = extract_event_text(event)
    assert rendered == "reacted 👍 to an earlier message please check this"
    assert rendered.count("please check this") == 1
    assert event.event_kind == "message.created"
    assert event.payload == {"body": "please check this"}
