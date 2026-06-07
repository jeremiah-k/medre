"""Focused tests for concise relation/conversation/delivery-plan fields
in runtime trace timeline output.

Covers F-1 (relation detail fields), F-2 (event conversation identity),
and F-10 (delivery_plan_id visibility) from the CLI audit findings.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from medre.runtime.trace import assemble_event_timeline, assemble_replay_timeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-1",
    event_kind: str = "message.created",
    source_adapter: str = "fake_transport",
    timestamp: datetime | None = None,
    relations: tuple[EventRelation, ...] | None = None,
    root_event_id: str | None = None,
    conversation_id: str | None = None,
    parent_event_id: str | None = None,
    trace_id: str | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=timestamp or datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=parent_event_id,
        lineage=(),
        relations=relations or (),
        payload={"text": "hello"},
        metadata=EventMetadata(),
        root_event_id=root_event_id,
        conversation_id=conversation_id,
        trace_id=trace_id,
    )


def _make_receipt(
    receipt_id: str = "rcpt-1",
    event_id: str = "evt-1",
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "dest_adapter",
    status: str = "sent",
    source: str = "live",
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        status=status,  # type: ignore[assignment]  # test helper accepts str for convenience
        source=source,
        created_at=created_at or datetime.now(timezone.utc),
    )


# ===================================================================
# F-2: Event conversation identity visibility
# ===================================================================


class TestEventConversationIdentity:
    """Event entries include root_event_id, conversation_id,
    parent_event_id, and trace_id when present."""

    def test_root_event_id_present_when_set(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(
            event_id="evt-root",
            timestamp=ts,
            root_event_id="root-001",
        )

        timeline = assemble_event_timeline(event, [], [], [])
        data = next(e for e in timeline if e["entry_type"] == "event")["data"]

        assert data["root_event_id"] == "root-001"

    def test_conversation_id_present_when_set(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(
            event_id="evt-conv",
            timestamp=ts,
            root_event_id="root-002",
            conversation_id="root-002",
        )

        timeline = assemble_event_timeline(event, [], [], [])
        data = next(e for e in timeline if e["entry_type"] == "event")["data"]

        assert data["conversation_id"] == "root-002"

    def test_parent_event_id_present_when_set(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(
            event_id="evt-child",
            timestamp=ts,
            parent_event_id="parent-001",
        )

        timeline = assemble_event_timeline(event, [], [], [])
        data = next(e for e in timeline if e["entry_type"] == "event")["data"]

        assert data["parent_event_id"] == "parent-001"

    def test_trace_id_present_when_set(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(
            event_id="evt-trace",
            timestamp=ts,
            trace_id="trace-abc-123",
        )

        timeline = assemble_event_timeline(event, [], [], [])
        data = next(e for e in timeline if e["entry_type"] == "event")["data"]

        assert data["trace_id"] == "trace-abc-123"

    def test_identity_fields_absent_when_none(self) -> None:
        """Fields are omitted (not null) when not set — backward compatible."""
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-bare", timestamp=ts)

        timeline = assemble_event_timeline(event, [], [], [])
        data = next(e for e in timeline if e["entry_type"] == "event")["data"]

        assert "root_event_id" not in data
        assert "conversation_id" not in data
        assert "parent_event_id" not in data
        assert "trace_id" not in data

    def test_all_identity_fields_together(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(
            event_id="evt-full",
            timestamp=ts,
            root_event_id="root-full",
            conversation_id="root-full",
            parent_event_id="parent-full",
            trace_id="trace-full",
        )

        timeline = assemble_event_timeline(event, [], [], [])
        data = next(e for e in timeline if e["entry_type"] == "event")["data"]

        assert data["root_event_id"] == "root-full"
        assert data["conversation_id"] == "root-full"
        assert data["parent_event_id"] == "parent-full"
        assert data["trace_id"] == "trace-full"

    def test_identity_fields_json_safe(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(
            event_id="evt-json",
            timestamp=ts,
            root_event_id="root-json",
            conversation_id="root-json",
            trace_id="trace-json",
        )

        timeline = assemble_event_timeline(event, [], [], [])
        serialized = json.dumps(timeline)
        assert isinstance(serialized, str)


# ===================================================================
# F-1: Relation target / native-ref visibility
# ===================================================================


class TestRelationTargetVisibility:
    """Relation entries include target_event_id, key, fallback_text,
    and target_native_ref concise summary."""

    def test_target_event_id_always_present(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-reply-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-rel", timestamp=ts, relations=(relation,))

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        data = next(e for e in timeline if e["entry_type"] == "relation")["data"]

        assert data["target_event_id"] == "target-reply-1"

    def test_target_event_id_null_when_unresolved(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            event_id="evt-rel-unresolved", timestamp=ts, relations=(relation,)
        )

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        data = next(e for e in timeline if e["entry_type"] == "relation")["data"]

        assert data["target_event_id"] is None

    def test_key_present_when_set(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="target-react",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_event(event_id="evt-react", timestamp=ts, relations=(relation,))

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        data = next(e for e in timeline if e["entry_type"] == "relation")["data"]

        assert data["key"] == "👍"

    def test_key_absent_when_none(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-nk", timestamp=ts, relations=(relation,))

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        data = next(e for e in timeline if e["entry_type"] == "relation")["data"]

        assert "key" not in data

    def test_fallback_text_present_when_set(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-ft",
            target_native_ref=None,
            key=None,
            fallback_text="In reply to: Hello world",
        )
        event = _make_event(event_id="evt-ft", timestamp=ts, relations=(relation,))

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        data = next(e for e in timeline if e["entry_type"] == "relation")["data"]

        assert data["fallback_text"] == "In reply to: Hello world"

    def test_fallback_text_absent_when_none(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-nft", timestamp=ts, relations=(relation,))

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        data = next(e for e in timeline if e["entry_type"] == "relation")["data"]

        assert "fallback_text" not in data

    def test_target_native_ref_concise_summary(self) -> None:
        """target_native_ref is a concise dict with adapter, native_channel_id,
        native_message_id — not a full dump."""
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:test",
            native_message_id="$native-msg-42",
            native_thread_id=None,
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text="replied to Alice",
        )
        event = _make_event(
            event_id="evt-nref-rel", timestamp=ts, relations=(relation,)
        )

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        data = next(e for e in timeline if e["entry_type"] == "relation")["data"]

        assert data["target_native_ref"]["adapter"] == "matrix"
        assert data["target_native_ref"]["native_channel_id"] == "!room:test"
        assert data["target_native_ref"]["native_message_id"] == "$native-msg-42"
        # Should NOT include native_thread_id — concise summary only.
        assert "native_thread_id" not in data["target_native_ref"]

    def test_target_native_ref_absent_when_none(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-no-nref", timestamp=ts, relations=(relation,))

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        data = next(e for e in timeline if e["entry_type"] == "relation")["data"]

        assert "target_native_ref" not in data

    def test_multiple_relations_all_enriched(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        rel_reply = EventRelation(
            relation_type="reply",
            target_event_id="target-reply",
            target_native_ref=None,
            key=None,
            fallback_text="reply context",
        )
        rel_reaction = EventRelation(
            relation_type="reaction",
            target_event_id="target-react",
            target_native_ref=None,
            key="❤",
            fallback_text=None,
        )
        event = _make_event(
            event_id="evt-multi-rel",
            timestamp=ts,
            relations=(rel_reply, rel_reaction),
        )

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        rel_entries = [e for e in timeline if e["entry_type"] == "relation"]

        assert len(rel_entries) == 2
        # Both should have target_event_id.
        for entry in rel_entries:
            assert "target_event_id" in entry["data"]

        # Verify specific entries.
        reply_data = next(
            e["data"] for e in rel_entries if e["data"]["relation_type"] == "reply"
        )
        assert reply_data["target_event_id"] == "target-reply"
        assert reply_data["fallback_text"] == "reply context"

        react_data = next(
            e["data"] for e in rel_entries if e["data"]["relation_type"] == "reaction"
        )
        assert react_data["target_event_id"] == "target-react"
        assert react_data["key"] == "❤"

    def test_relation_entries_json_safe(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:test",
            native_message_id="$msg-1",
            native_thread_id=None,
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=nref,
            key="test-key",
            fallback_text="some text",
        )
        event = _make_event(
            event_id="evt-rel-json", timestamp=ts, relations=(relation,)
        )

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        serialized = json.dumps(timeline)
        assert isinstance(serialized, str)


# ===================================================================
# F-10: delivery_plan_id visibility in receipt entries
# ===================================================================


class TestReceiptDeliveryPlanVisibility:
    """Receipt entries include delivery_plan_id from the report dict."""

    def test_delivery_plan_id_in_event_timeline(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-plan", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-plan",
            event_id="evt-plan",
            delivery_plan_id="plan-abc-123",
            created_at=datetime(2026, 3, 1, 10, 0, 1, tzinfo=timezone.utc),
        )

        timeline = assemble_event_timeline(event, [receipt], [], [])
        data = next(e for e in timeline if e["entry_type"] == "receipt")["data"]

        assert data["delivery_plan_id"] == "plan-abc-123"

    def test_delivery_plan_id_in_replay_timeline(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-rplan", timestamp=ts)
        receipt = _make_receipt(
            receipt_id="rcpt-rplan",
            event_id="evt-rplan",
            delivery_plan_id="plan-replay-42",
            source="replay",
            created_at=datetime(2026, 3, 1, 10, 0, 1, tzinfo=timezone.utc),
        )

        result = assemble_replay_timeline(
            "run-plan",
            [receipt],
            {"evt-rplan": event},
        )
        data = next(e for e in result["timeline"] if e["entry_type"] == "receipt")[
            "data"
        ]

        assert data["delivery_plan_id"] == "plan-replay-42"

    def test_delivery_plan_id_empty_string_when_unset(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-no-plan", timestamp=ts)
        receipt = DeliveryReceipt(
            receipt_id="rcpt-no-plan",
            event_id="evt-no-plan",
            delivery_plan_id="",
            target_adapter="dest",
            status="sent",
            created_at=datetime(2026, 3, 1, 10, 0, 1, tzinfo=timezone.utc),
        )

        timeline = assemble_event_timeline(event, [receipt], [], [])
        data = next(e for e in timeline if e["entry_type"] == "receipt")["data"]

        assert data["delivery_plan_id"] == ""


# ===================================================================
# Backward compatibility: existing fields unchanged
# ===================================================================


class TestBackwardCompatibility:
    """New fields are additive — existing output shape is preserved."""

    def test_event_still_has_core_fields(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event(event_id="evt-bc", timestamp=ts)

        timeline = assemble_event_timeline(event, [], [], [])
        data = next(e for e in timeline if e["entry_type"] == "event")["data"]

        # Original fields still present.
        assert data["event_id"] == "evt-bc"
        assert data["event_kind"] == "message.created"
        assert data["source_adapter"] == "fake_transport"
        assert data["source_channel_id"] == "ch-0"

    def test_relation_still_has_relation_type(self) -> None:
        ts = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        relation = EventRelation(
            relation_type="reply",
            target_event_id="t-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-rel-bc", timestamp=ts, relations=(relation,))

        timeline = assemble_event_timeline(event, [], [], list(event.relations))
        data = next(e for e in timeline if e["entry_type"] == "relation")["data"]

        assert data["relation_type"] == "reply"
        # target_event_id is always present now (may be None).
        assert "target_event_id" in data
