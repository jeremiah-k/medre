"""Targeted coverage for uncovered branches in TextRenderer.

Covers:
  - Line 153: relation_type tuple check in render() for all five types
  - Lines 167-170: fallback_text strategy with relation present
  - Lines 246-250: _resolve_reaction_key body fallback and None return
  - Lines 301-303: _extract_text reply branch with empty payload_text
  - Line 331: _extract_text thread branch with/without body
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.events import CanonicalEvent, EventKind, EventRelation, NativeRef
from medre.core.events.metadata import EventMetadata
from medre.core.rendering.renderer import RenderingContext
from medre.core.rendering.text import TextRenderer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_renderer = TextRenderer()


def _ctx(
    strategy: str = "direct",
    max_chars: int | None = None,
) -> RenderingContext:
    return RenderingContext(
        delivery_strategy=strategy,  # type: ignore[arg-type]
        target_adapter="test-adapter",
        target_channel="test-channel",
        target_platform=None,
        max_text_chars=max_chars,
    )


def _event(
    *,
    kind: str = EventKind.MESSAGE_TEXT,
    relations: tuple[EventRelation, ...] = (),
    payload: dict[str, object] | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-001",
        event_kind=kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="src",
        source_transport_id="transport-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload or {},
        metadata=EventMetadata(),
    )


def _rel(
    rtype: str,
    *,
    key: str | None = None,
    fallback_text: str | None = None,
    target_event_id: str | None = None,
    target_native_ref: NativeRef | None = None,
    metadata: dict[str, object] | None = None,
) -> EventRelation:
    return EventRelation(
        relation_type=rtype,  # type: ignore[arg-type]
        target_event_id=target_event_id,
        target_native_ref=target_native_ref,
        key=key,
        fallback_text=fallback_text,
        metadata=metadata or {},
    )


# ===================================================================
# Line 153 — relation_type tuple check + fallback_text strategy
# ===================================================================


@pytest.mark.parametrize("rtype", ["reply", "reaction", "edit", "delete", "thread"])
@pytest.mark.asyncio
async def test_render_sets_fallback_applied_for_all_relation_types(rtype: str) -> None:
    """Line 153: each relation type sets fallback_applied = f"relation_{rtype}"."""
    rel = _rel(
        rtype, fallback_text="some target", key="👍" if rtype == "reaction" else None
    )
    event = _event(
        kind=EventKind.MESSAGE_TEXT, relations=(rel,), payload={"text": "hi"}
    )
    result = await _renderer.render(event, _ctx())
    assert result.fallback_applied == f"relation_{rtype}"


@pytest.mark.asyncio
async def test_render_fallback_text_strategy_with_reply_preserves_relation_type() -> (
    None
):
    """Lines 167-170: fallback_text strategy with reply relation stores strategy_relation_type."""
    rel = _rel("reply", fallback_text="original msg")
    event = _event(
        kind=EventKind.MESSAGE_TEXT,
        relations=(rel,),
        payload={"text": "my reply"},
    )
    result = await _renderer.render(event, _ctx(strategy="fallback_text"))
    assert result.fallback_applied == "strategy_fallback_text"
    assert result.metadata["strategy_relation_type"] == "reply"


@pytest.mark.asyncio
async def test_render_fallback_text_strategy_with_reaction_preserves_relation_type() -> (
    None
):
    """Lines 167-170: fallback_text strategy with reaction relation stores strategy_relation_type."""
    rel = _rel("reaction", key="👍")
    event = _event(
        kind=EventKind.MESSAGE_TEXT,
        relations=(rel,),
        payload={"text": ""},
    )
    result = await _renderer.render(event, _ctx(strategy="fallback_text"))
    assert result.fallback_applied == "strategy_fallback_text"
    assert result.metadata["strategy_relation_type"] == "reaction"


# ===================================================================
# Lines 246-250 — _resolve_reaction_key fallback order
# ===================================================================


@pytest.mark.parametrize(
    "payload", [{"body": "thumbsup"}, {"key": "👍"}, {"emoji": "👍"}, {}]
)
def test_missing_explicit_reaction_key_is_not_inferred(payload):
    event = _event(relations=(_rel("reaction"),), payload=payload)
    with pytest.raises(ValueError, match="explicit nonempty key"):
        TextRenderer._extract_text(event)


# ===================================================================
# Lines 301-303 — _extract_text reply with empty payload_text
# ===================================================================


def test_extract_text_reply_empty_payload_returns_prefix_only() -> None:
    """Lines 301-303: empty payload_text → returns just "[replying to: {target}]"."""
    rel = _rel("reply", fallback_text="the original")
    event = _event(
        kind=EventKind.MESSAGE_TEXT,
        relations=(rel,),
        payload={"text": ""},
    )
    text = TextRenderer._extract_text(event)
    assert text == "[replying to: the original]"


# ===================================================================
# Line 331 — _extract_text thread branch
# ===================================================================


def test_extract_text_thread_with_body() -> None:
    """Line 331-337: thread relation with body text → "[thread on: {target}] {body}"."""
    rel = _rel("thread", fallback_text="thread-root")
    event = _event(
        kind=EventKind.MESSAGE_TEXT,
        relations=(rel,),
        payload={"text": "thread message"},
    )
    text = TextRenderer._extract_text(event)
    assert text == "[thread on: thread-root] thread message"


def test_extract_text_thread_without_body() -> None:
    """Line 338: thread relation with no body → "[thread on: {target}]"."""
    rel = _rel("thread", fallback_text="thread-root")
    event = _event(
        kind=EventKind.MESSAGE_TEXT,
        relations=(rel,),
        payload={"text": ""},
    )
    text = TextRenderer._extract_text(event)
    assert text == "[thread on: thread-root]"


@pytest.mark.parametrize(
    ("text", "bytes_limit", "expected"),
    [
        ("🦉🦉🦉", 9, "🦉🦉"),
        ("🦉🦉", 0, ""),
        ("café", 4, "caf"),
        ("short", 100, "short"),
    ],
)
async def test_generic_text_renderer_enforces_utf8_budget(text, bytes_limit, expected):
    from dataclasses import replace

    context = replace(_ctx(), max_text_bytes=bytes_limit)
    result = await _renderer.render(_event(payload={"text": text}), context)
    assert result.payload["text"] == expected
    assert result.truncated is (expected != text)
    assert len(expected.encode("utf-8")) <= bytes_limit
