"""Tests for degrade_relations_inline covering all relation-type branches."""

from datetime import datetime, timezone

from msgspec.structs import force_setattr

from medre.core.events import CanonicalEvent, EventRelation, NativeRef
from medre.core.rendering.relations import degrade_relations_inline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _native_ref(message_id: str = "nmsg-1") -> NativeRef:
    return NativeRef(
        adapter="test",
        native_channel_id="ch-1",
        native_message_id=message_id,
    )


def _rel(
    relation_type: str = "reply",
    *,
    target_event_id: str | None = "evt-target",
    target_native_ref: NativeRef | None = None,
    key: str | None = None,
    fallback_text: str | None = None,
) -> EventRelation:
    return EventRelation(
        relation_type=relation_type,  # type: ignore[arg-type]
        target_event_id=target_event_id,
        target_native_ref=target_native_ref,
        key=key,
        fallback_text=fallback_text,
    )


def _event(
    relations: tuple[EventRelation, ...] = (),
    payload: dict | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-1",
        event_kind="message",
        schema_version=1,
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        source_adapter="test",
        source_transport_id="transport-1",
        source_channel_id="ch-1",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload or {},
        metadata={},
    )


# ---------------------------------------------------------------------------
# Tests — no relations
# ---------------------------------------------------------------------------


class TestNoRelations:
    def test_returns_text_unchanged(self):
        event = _event()
        assert degrade_relations_inline(event, "hello") == "hello"

    def test_empty_text_no_relations(self):
        event = _event()
        assert degrade_relations_inline(event, "") == ""


# ---------------------------------------------------------------------------
# Tests — reply
# ---------------------------------------------------------------------------


class TestReplyRelation:
    def test_reply_with_fallback_text(self):
        rel = _rel("reply", fallback_text="original msg", target_event_id=None)
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "ok") == "ok [reply to: original msg]"

    def test_reply_with_event_id(self):
        rel = _rel("reply", target_event_id="evt-99")
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "ok") == "ok [reply to: evt-99]"

    def test_reply_with_native_ref_only(self):
        nref = _native_ref("native-42")
        rel = _rel("reply", target_event_id=None, target_native_ref=nref)
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "ok") == "ok [reply to: native-42]"

    def test_reply_with_nothing_falls_to_question_mark(self):
        rel = _rel("reply", target_event_id=None, target_native_ref=None)
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "ok") == "ok [reply to: ?]"


# ---------------------------------------------------------------------------
# Tests — reaction
# ---------------------------------------------------------------------------


class TestReactionRelation:
    def test_reaction_key_from_rel_key(self):
        rel = _rel("reaction", key="👍", target_event_id="t-1")
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "") == "[reaction 👍 to: t-1]"

    def test_reaction_key_from_payload_key(self):
        rel = _rel("reaction", key=None, target_event_id="t-1")
        event = _event(relations=(rel,), payload={"key": "❤️"})
        assert degrade_relations_inline(event, "") == "[reaction ❤️ to: t-1]"

    def test_reaction_key_from_payload_emoji(self):
        rel = _rel("reaction", key=None, target_event_id="t-1")
        event = _event(relations=(rel,), payload={"emoji": "😂"})
        assert degrade_relations_inline(event, "") == "[reaction 😂 to: t-1]"

    def test_reaction_no_key_no_emoji_uses_default(self):
        rel = _rel("reaction", key=None, target_event_id="t-1")
        event = _event(relations=(rel,), payload={})
        assert degrade_relations_inline(event, "") == "[reaction ∟ to: t-1]"

    def test_reaction_rel_key_takes_priority_over_payload(self):
        """rel.key should be preferred over payload['key'] and payload['emoji']."""
        rel = _rel("reaction", key="🔥", target_event_id="t-1")
        event = _event(relations=(rel,), payload={"key": "❤️", "emoji": "😂"})
        assert degrade_relations_inline(event, "") == "[reaction 🔥 to: t-1]"

    def test_reaction_payload_key_preferred_over_emoji(self):
        """payload['key'] should be preferred over payload['emoji']."""
        rel = _rel("reaction", key=None, target_event_id="t-1")
        event = _event(relations=(rel,), payload={"key": "⭐", "emoji": "😂"})
        assert degrade_relations_inline(event, "") == "[reaction ⭐ to: t-1]"


# ---------------------------------------------------------------------------
# Tests — edit, delete, thread
# ---------------------------------------------------------------------------


class TestOtherKnownRelationTypes:
    def test_edit(self):
        rel = _rel("edit", target_event_id="t-edit")
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "fixed") == "fixed [edit of: t-edit]"

    def test_delete(self):
        rel = _rel("delete", target_event_id="t-del")
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "") == "[delete of: t-del]"

    def test_thread(self):
        rel = _rel("thread", target_event_id="t-thread")
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "reply") == "reply [thread on: t-thread]"


# ---------------------------------------------------------------------------
# Tests — unknown relation type (else branch, line 70)
# ---------------------------------------------------------------------------


class TestUnknownRelationType:
    def test_unknown_type_uses_generic_format(self):
        """For an unknown relation_type, the else branch produces [{type}: {target}]."""
        rel = _rel("reply", target_event_id="t-x")
        # Bypass EventRelation validation to set an unknown type
        force_setattr(rel, "relation_type", "pin")
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "") == "[pin: t-x]"


# ---------------------------------------------------------------------------
# Tests — text handling edge cases
# ---------------------------------------------------------------------------


class TestTextHandling:
    def test_empty_text_with_relations_returns_inline_only(self):
        rel = _rel("reply", target_event_id="t-1")
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "") == "[reply to: t-1]"

    def test_nonempty_text_with_relations_joins_with_space(self):
        rel = _rel("reply", target_event_id="t-1")
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "hello") == "hello [reply to: t-1]"

    def test_multiple_relations_are_joined(self):
        r1 = _rel("reply", target_event_id="t-1")
        r2 = _rel("edit", target_event_id="t-2")
        event = _event(relations=(r1, r2))
        result = degrade_relations_inline(event, "msg")
        assert result == "msg [reply to: t-1] [edit of: t-2]"

    def test_target_resolution_prefers_fallback_text(self):
        """fallback_text > target_event_id > native_message_id > '?'"""
        nref = _native_ref("native-1")
        rel = _rel(
            "reply", fallback_text="ft", target_event_id="eid", target_native_ref=nref
        )
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "") == "[reply to: ft]"

    def test_target_resolution_prefers_event_id_over_native_ref(self):
        nref = _native_ref("native-1")
        rel = _rel(
            "reply", fallback_text=None, target_event_id="eid", target_native_ref=nref
        )
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "") == "[reply to: eid]"

    def test_target_resolution_uses_native_ref_when_no_event_id(self):
        nref = _native_ref("native-1")
        rel = _rel(
            "reply", fallback_text=None, target_event_id=None, target_native_ref=nref
        )
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "") == "[reply to: native-1]"

    def test_target_resolution_falls_to_question_mark(self):
        rel = _rel(
            "reply", fallback_text=None, target_event_id=None, target_native_ref=None
        )
        event = _event(relations=(rel,))
        assert degrade_relations_inline(event, "") == "[reply to: ?]"
