"""Tests for text_helpers: _resolve_target_display, _resolve_reaction_key,
extract_relation_text, and truncate_text.

Covers all uncovered branches reported by coverage analysis.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.events import (
    CanonicalEvent,
    EventKind,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from medre.core.rendering.text_helpers import (
    _resolve_reaction_key,
    _resolve_target_display,
    extract_relation_text,
    truncate_text,
    truncate_text_bytes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _evt(
    *,
    event_kind: str = "message.text",
    relations: tuple[EventRelation, ...] = (),
    payload: dict | None = None,
    source_adapter: str = "test-adapter",
) -> CanonicalEvent:
    """Build a minimal valid CanonicalEvent for testing."""
    return CanonicalEvent(
        event_id="evt-1",
        event_kind=event_kind,
        schema_version=1,
        timestamp=_TS,
        source_adapter=source_adapter,
        source_transport_id="t-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload if payload is not None else {},
        metadata=EventMetadata(),
    )


def _rel(
    relation_type: str = "reply",
    *,
    target_event_id: str | None = None,
    target_native_ref: NativeRef | None = None,
    key: str | None = None,
    fallback_text: str | None = None,
    metadata: dict | None = None,
) -> EventRelation:
    """Build an EventRelation with sensible defaults."""
    return EventRelation(
        relation_type=relation_type,  # type: ignore[arg-type]
        target_event_id=target_event_id,
        target_native_ref=target_native_ref,
        key=key,
        fallback_text=fallback_text,
        metadata=metadata or {},
    )


# ===================================================================
# _resolve_target_display (lines 25-35)
# ===================================================================


class TestResolveTargetDisplay:
    """Cover every branch of _resolve_target_display."""

    def test_fallback_text_present(self) -> None:
        """fallback_text is the highest-priority return value."""
        rel = _rel(fallback_text="hello world")
        assert _resolve_target_display(rel) == "hello world"

    def test_no_fallback_short_eid(self) -> None:
        """Short target_event_id (≤8 chars) returned as-is."""
        rel = _rel(target_event_id="abc123")
        assert _resolve_target_display(rel) == "abc123"

    def test_no_fallback_long_eid(self) -> None:
        """Long target_event_id (>8 chars) truncated with ellipsis."""
        rel = _rel(target_event_id="abcdefghijklmnop")
        assert _resolve_target_display(rel) == "abcdefgh…"

    def test_no_fallback_no_eid_but_native_message_id(self) -> None:
        """Falls back to native_message_id when no eid is available."""
        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$msg123",
        )
        rel = _rel(target_native_ref=nref)
        assert _resolve_target_display(rel) == "$msg123"

    def test_nothing_at_all(self) -> None:
        """Returns 'unknown message' when nothing is available."""
        rel = _rel()
        assert _resolve_target_display(rel) == "unknown message"


# ===================================================================
# _resolve_reaction_key (lines 49-60)
# ===================================================================


class TestResolveReactionKey:
    """Cover every branch of _resolve_reaction_key."""

    def test_rel_key_present(self) -> None:
        """rel.key is the highest-priority key source."""
        rel = _rel(relation_type="reaction", key="👍")
        event = _evt(payload={})
        assert _resolve_reaction_key(rel, event) == "👍"

    def test_payload_key_present(self) -> None:
        """Falls back to payload['key'] when rel.key is None."""
        rel = _rel(relation_type="reaction")
        event = _evt(payload={"key": "❤"})
        assert _resolve_reaction_key(rel, event) == "❤"

    def test_payload_emoji_present(self) -> None:
        """Falls back to payload['emoji'] when no key."""
        rel = _rel(relation_type="reaction")
        event = _evt(payload={"emoji": "🔥"})
        assert _resolve_reaction_key(rel, event) == "🔥"

    def test_payload_body_present(self) -> None:
        """Falls back to payload['body'] when nothing else."""
        rel = _rel(relation_type="reaction")
        event = _evt(payload={"body": "+1"})
        assert _resolve_reaction_key(rel, event) == "+1"

    def test_nothing_returns_none(self) -> None:
        """Returns None when no key source is available."""
        rel = _rel(relation_type="reaction")
        event = _evt(payload={})
        assert _resolve_reaction_key(rel, event) is None

    def test_whitespace_stripped_from_rel_key(self) -> None:
        """Whitespace-padded rel.key is stripped."""
        rel = _rel(relation_type="reaction", key="  👍  ")
        event = _evt(payload={})
        assert _resolve_reaction_key(rel, event) == "👍"

    def test_whitespace_stripped_from_payload_key(self) -> None:
        """Whitespace-padded payload['key'] is stripped."""
        rel = _rel(relation_type="reaction")
        event = _evt(payload={"key": "  ❤  "})
        assert _resolve_reaction_key(rel, event) == "❤"

    def test_whitespace_stripped_from_payload_emoji(self) -> None:
        """Whitespace-padded payload['emoji'] is stripped."""
        rel = _rel(relation_type="reaction")
        event = _evt(payload={"emoji": "  🔥  "})
        assert _resolve_reaction_key(rel, event) == "🔥"

    def test_whitespace_only_key_falls_through(self) -> None:
        """A whitespace-only rel.key is stripped to empty and falls through
        to the next resolution source."""
        rel = _rel(relation_type="reaction", key="   ")
        event = _evt(payload={"emoji": "👍"})
        # "   ".strip() → "" (falsy) → falls through to payload["emoji"]
        assert _resolve_reaction_key(rel, event) == "👍"


# ===================================================================
# extract_relation_text — relation branches (lines 96-170)
# ===================================================================


class TestExtractRelationTextReply:
    """Reply relation branches."""

    def test_reply_with_sender_displayname(self) -> None:
        """Reply with sender_displayname in metadata produces full prefix."""
        rel = _rel(
            relation_type="reply",
            target_event_id="short",
            metadata={"sender_displayname": "Alice"},
        )
        event = _evt(relations=(rel,), payload={"text": "hello"})
        result = extract_relation_text(event)
        assert result == "[replying to: short by Alice] hello"

    def test_reply_with_original_sender_displayname(self) -> None:
        """Reply with original_sender_displayname in metadata produces full prefix."""
        rel = _rel(
            relation_type="reply",
            target_event_id="short",
            metadata={"original_sender_displayname": "Alice"},
        )
        event = _evt(relations=(rel,), payload={"text": "hello"})
        result = extract_relation_text(event)
        assert result == "[replying to: short by Alice] hello"

    def test_reply_original_sender_displayname_empty_body(self) -> None:
        """Reply with original_sender_displayname and empty body returns prefix only."""
        rel = _rel(
            relation_type="reply",
            target_event_id="short",
            metadata={"original_sender_displayname": "Bob"},
        )
        event = _evt(relations=(rel,), payload={})
        result = extract_relation_text(event)
        assert result == "[replying to: short by Bob]"

    def test_reply_empty_payload_text(self) -> None:
        """Reply with no payload text returns just the prefix."""
        rel = _rel(
            relation_type="reply",
            target_event_id="short",
            metadata={"sender_displayname": "Bob"},
        )
        event = _evt(relations=(rel,), payload={})
        result = extract_relation_text(event)
        assert result == "[replying to: short by Bob]"


class TestExtractRelationTextReaction:
    """Reaction relation branches."""

    def test_reaction_with_key(self) -> None:
        """Reaction with a resolved key includes it."""
        rel = _rel(relation_type="reaction", key="👍")
        event = _evt(
            event_kind="message.reacted",
            relations=(rel,),
            payload={"displayname": "Alice"},
        )
        result = extract_relation_text(event)
        assert result == "Alice reacted with 👍"

    def test_reaction_without_key(self) -> None:
        """Reaction without a key omits it."""
        rel = _rel(relation_type="reaction")
        event = _evt(
            event_kind="message.reacted",
            relations=(rel,),
            payload={"displayname": "Bob"},
        )
        result = extract_relation_text(event)
        assert result == "Bob reacted"


class TestExtractRelationTextEdit:
    """Edit relation branch."""

    def test_edit_with_text(self) -> None:
        """Edit relation with payload text returns prefixed text."""
        rel = _rel(relation_type="edit")
        event = _evt(relations=(rel,), payload={"text": "corrected"})
        result = extract_relation_text(event)
        assert result == "[edited] corrected"

    def test_edit_without_text(self) -> None:
        """Edit relation with no payload text returns bare prefix."""
        rel = _rel(relation_type="edit")
        event = _evt(relations=(rel,), payload={})
        result = extract_relation_text(event)
        assert result == "[edited]"


class TestExtractRelationTextDelete:
    """Delete relation branch."""

    def test_delete_with_target(self) -> None:
        """Delete with a known target includes it."""
        rel = _rel(relation_type="delete", target_event_id="abc123")
        event = _evt(relations=(rel,), payload={})
        result = extract_relation_text(event)
        assert result == "[deleted: abc123]"

    def test_delete_unknown_target(self) -> None:
        """Delete with no target info returns bare [deleted]."""
        rel = _rel(relation_type="delete")
        event = _evt(relations=(rel,), payload={})
        result = extract_relation_text(event)
        assert result == "[deleted]"


class TestExtractRelationTextThread:
    """Thread relation branch."""

    def test_thread_with_text(self) -> None:
        """Thread with payload text includes both target and text."""
        rel = _rel(relation_type="thread", target_event_id="abc")
        event = _evt(relations=(rel,), payload={"text": "thread msg"})
        result = extract_relation_text(event)
        assert result == "[thread: abc] thread msg"

    def test_thread_without_text(self) -> None:
        """Thread with no payload text returns just target prefix."""
        rel = _rel(relation_type="thread", target_event_id="xyz")
        event = _evt(relations=(rel,), payload={})
        result = extract_relation_text(event)
        assert result == "[thread: xyz]"


# ===================================================================
# extract_relation_text — kind-based branches (no relations)
# ===================================================================


class TestExtractRelationTextKindBranches:
    """Kind-based rendering when event has no relations."""

    def test_message_text_kind(self) -> None:
        """MESSAGE_TEXT returns payload text."""
        event = _evt(event_kind=EventKind.MESSAGE_TEXT, payload={"text": "hi"})
        assert extract_relation_text(event) == "hi"

    def test_message_created_kind(self) -> None:
        """MESSAGE_CREATED returns payload text."""
        event = _evt(event_kind=EventKind.MESSAGE_CREATED, payload={"body": "yo"})
        assert extract_relation_text(event) == "yo"

    def test_message_edited_kind(self) -> None:
        """MESSAGE_EDITED without relations prefixes with [edited]."""
        event = _evt(event_kind=EventKind.MESSAGE_EDITED, payload={"text": "fixed"})
        assert extract_relation_text(event) == "[edited] fixed"

    def test_message_deleted_kind(self) -> None:
        """MESSAGE_DELETED without relations returns [deleted]."""
        event = _evt(event_kind=EventKind.MESSAGE_DELETED, payload={})
        assert extract_relation_text(event) == "[deleted]"

    def test_message_reacted_kind(self) -> None:
        """MESSAGE_REACTED without relations returns payload text."""
        event = _evt(
            event_kind=EventKind.MESSAGE_REACTED, payload={"text": "reacted-text"}
        )
        assert extract_relation_text(event) == "reacted-text"

    def test_presence_changed_kind(self) -> None:
        """PRESENCE_CHANGED renders user status string."""
        event = _evt(
            event_kind=EventKind.PRESENCE_CHANGED,
            payload={"user": "carol", "status": "online"},
        )
        assert extract_relation_text(event) == "carol is now online"

    def test_plugin_custom_kind(self) -> None:
        """PLUGIN_CUSTOM returns payload text."""
        event = _evt(event_kind=EventKind.PLUGIN_CUSTOM, payload={"text": "custom-msg"})
        assert extract_relation_text(event) == "custom-msg"

    def test_unknown_kind_fallback(self) -> None:
        """Unknown kind falls through to payload text."""
        event = _evt(event_kind="system.audit", payload={"text": "audit-entry"})
        assert extract_relation_text(event) == "audit-entry"


# ===================================================================
# truncate_text (lines 196-197)
# ===================================================================


class TestTruncateText:
    """Cover the limit==0 branch of truncate_text."""

    def test_limit_zero_nonempty_text(self) -> None:
        """limit=0 with non-empty text returns ('', True)."""
        result = truncate_text("hello world", max_text_chars=0)
        assert result == ("", True)

    def test_normal_truncation(self) -> None:
        """Text exceeding limit is truncated with True flag."""
        result = truncate_text("abcdefghij", max_text_chars=5)
        assert result == ("abcde", True)

    def test_within_limit(self) -> None:
        """Text within limit is returned unchanged with False flag."""
        result = truncate_text("abc", max_text_chars=10)
        assert result == ("abc", False)

    def test_default_limit(self) -> None:
        """Default limit (500) does not truncate short text."""
        result = truncate_text("short")
        assert result == ("short", False)


# ===================================================================
# truncate_text_bytes
# ===================================================================


class TestTruncateTextBytes:
    """Cover truncate_text_bytes: None budget, empty text, within budget,
    multi-byte boundary, and exceeding budget."""

    def test_none_budget_no_truncation(self) -> None:
        """When max_text_bytes is None, text is returned unchanged."""
        text, truncated, orig, rendered = truncate_text_bytes("hello world", None)
        assert text == "hello world"
        assert truncated is False
        assert orig == rendered

    def test_empty_text(self) -> None:
        """Empty text with any budget returns unchanged."""
        text, truncated, orig, rendered = truncate_text_bytes("", 100)
        assert text == ""
        assert truncated is False
        assert orig == 0
        assert rendered == 0

    def test_within_budget(self) -> None:
        """Text within byte budget returned unchanged."""
        text, truncated, orig, rendered = truncate_text_bytes("hello", 100)
        assert text == "hello"
        assert truncated is False
        assert orig == rendered == 5

    def test_exceeding_budget_ascii(self) -> None:
        """ASCII text exceeding budget is truncated."""
        text, truncated, orig, rendered = truncate_text_bytes("abcdefghij", 5)
        assert text == "abcde"
        assert truncated is True
        assert orig == 10
        assert rendered == 5

    def test_multi_byte_boundary_safe(self) -> None:
        """Truncation at a multi-byte character boundary splits safely
        (never inside a codepoint)."""
        # "ü" is 2 bytes in UTF-8, "a" is 1 byte
        # "aüa" = 1 + 2 + 1 = 4 bytes total
        text, truncated, orig, rendered = truncate_text_bytes("aüa", 3)
        # 3 bytes should allow "aü" (3 bytes) or just "a" (1 byte)
        # The implementation trims one char at a time from the right
        assert truncated is True
        assert rendered <= 3
        # Verify the result is valid UTF-8
        text.encode("utf-8")  # should not raise
