"""Reply fallbacks quote the same canonical text selected for transport rendering."""

from __future__ import annotations

from datetime import datetime, timezone

import msgspec
import pytest

from medre.core.events.canonical import CanonicalEvent, EventRelation
from medre.core.events.metadata import EventMetadata
from medre.core.planning.relation_enricher import RelationEnricher
from medre.core.rendering.relations import degrade_relations_inline


def _event(event_id, payload, relations=()):
    """Create independent stored and replying messages without native SDK state."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2026, 10, 28, tzinfo=timezone.utc),
        source_adapter="source",
        source_transport_id="sender",
        source_channel_id="room",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload,
        metadata=EventMetadata(),
    )


class StoredEvents:
    """Return authoritative canonical targets without inventing native references."""

    def __init__(self, event):
        self.event = event

    async def get(self, event_id):
        return self.event if self.event.event_id == event_id else None

    async def list_native_refs_for_event(self, _event_id):
        return []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"body": "stale alias", "text": "text authoritative"},
            "text authoritative",
        ),
        (
            {
                "body": "styled authoritative",
                "text": "stale alias",
                "formatting": {"version": 1, "spans": []},
            },
            "styled authoritative",
        ),
        ({"body": "stale alias", "text": ""}, None),
        ({"body": None, "text": "available text"}, "available text"),
        ({"text": "plain text"}, "plain text"),
        ({"body": "plain body"}, "plain body"),
    ],
)
async def test_reply_quote_uses_canonical_text_precedence(payload, expected):
    """Fallback and original_text agree with the target's authoritative content."""
    target = _event("target", payload)
    retained = msgspec.json.encode(target)
    relation = EventRelation(
        relation_type="reply",
        target_event_id="target",
        target_native_ref=None,
        key=None,
        fallback_text=None,
        metadata={},
    )
    reply = _event("reply", {"text": "response"}, (relation,))
    result = await RelationEnricher(StoredEvents(target)).enrich_for_target(
        reply, target_adapter="destination", target_channel="other-room"
    )
    enriched = result.relations[0]
    assert enriched.fallback_text == expected
    assert enriched.metadata.get("original_text") == expected
    assert reply.relations[0].fallback_text is None
    assert enriched.target_native_ref is None
    rendered = degrade_relations_inline(result, "response")
    assert f"[reply to: {expected or 'target'}]" in rendered
    assert "stale alias" not in rendered
    assert msgspec.json.encode(target) == retained


async def test_supplied_fallback_is_preserved_beside_canonical_original_text():
    """An explicit quote stays intact while missing original_text uses canonical selection."""
    target = _event("target", {"body": "stale alias", "text": "canonical"})
    relation = EventRelation(
        relation_type="reply",
        target_event_id="target",
        target_native_ref=None,
        key=None,
        fallback_text="supplied quote",
        metadata={},
    )
    reply = _event("reply", {"text": "response"}, (relation,))
    result = await RelationEnricher(StoredEvents(target)).enrich_for_target(
        reply, target_adapter="destination"
    )
    assert result.relations[0].fallback_text == "supplied quote"
    assert result.relations[0].metadata["original_text"] == "canonical"
