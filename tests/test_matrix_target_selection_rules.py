"""Target-selection rule tests for MatrixRenderer.

Locks the current target-selection contracts:

* MatrixRenderer uses ``relations[0]`` only — subsequent relations are ignored.
* Reply target: ``_matrix_target_event_id`` returns the native event ID only
  when ``target_native_ref.adapter == target_adapter`` and
  ``native_message_id`` is non-empty.  Cross-adapter refs are rejected.
* Reaction target: true ``m.reaction`` when a Matrix-native target exists and
  mmrelay_compat is off; ``m.emote`` fallback otherwise.
* Missing target (empty relations): renders plain ``m.text`` with no
  ``m.relates_to``.
* Stale / non-existent native target: the renderer emits the ID without
  pre-validation — it trusts the ``target_native_ref`` blindly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from medre.core.planning.delivery_plan import DeliveryStrategyMethod
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TARGET = "chat-instance"


def _ctx(
    target_adapter: str = _TARGET,
    delivery_strategy: DeliveryStrategyMethod = "direct",
) -> RenderingContext:
    return RenderingContext(
        delivery_strategy=delivery_strategy,
        target_adapter=target_adapter,
        target_platform="matrix",
    )


def _event(
    relations: tuple[EventRelation, ...] = (),
    payload: dict[str, object] | None = None,
    source_adapter: str = "transport-1",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-001",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload or {"body": "hello"},
        metadata=EventMetadata(),
    )


def _reply_rel(
    adapter: str = _TARGET,
    native_message_id: str = "$mx-ev-42",
) -> EventRelation:
    return EventRelation(
        relation_type="reply",
        target_event_id="canonical-42",
        target_native_ref=NativeRef(
            adapter=adapter,
            native_channel_id="!room:server",
            native_message_id=native_message_id,
        ),
        key=None,
        fallback_text=None,
    )


def _reaction_rel(
    adapter: str = _TARGET,
    native_message_id: str = "$mx-ev-99",
    key: str = "👍",
) -> EventRelation:
    return EventRelation(
        relation_type="reaction",
        target_event_id="canonical-99",
        target_native_ref=NativeRef(
            adapter=adapter,
            native_channel_id="!room:server",
            native_message_id=native_message_id,
        ),
        key=key,
        fallback_text=None,
    )


# ---------------------------------------------------------------------------
# Reply target selection
# ---------------------------------------------------------------------------


async def test_reply_uses_native_ref_owned_by_target_adapter() -> None:
    """Reply renders m.in_reply_to when native ref belongs to target."""
    renderer = MatrixRenderer()
    event = _event(relations=(_reply_rel(adapter=_TARGET, native_message_id="$mx-1"),))
    result = await renderer.render(event, _ctx())

    relates = result.payload.get("m.relates_to")
    assert relates is not None
    assert relates["m.in_reply_to"]["event_id"] == "$mx-1"  # type: ignore[index]


async def test_reply_ignores_cross_adapter_native_ref() -> None:
    """Reply with a native ref from a different adapter produces no m.in_reply_to."""
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reply_rel(adapter="other-radio", native_message_id="$mx-2"),)
    )
    result = await renderer.render(event, _ctx())

    assert result.payload.get("m.relates_to") is None


async def test_reply_without_native_ref_no_relates_to() -> None:
    """Reply relation with no target_native_ref produces no m.relates_to."""
    renderer = MatrixRenderer()
    rel = EventRelation(
        relation_type="reply",
        target_event_id="canonical-42",
        target_native_ref=None,
        key=None,
        fallback_text=None,
    )
    event = _event(relations=(rel,))
    result = await renderer.render(event, _ctx())

    assert result.payload.get("m.relates_to") is None


async def test_reply_with_empty_native_message_id_no_relates_to() -> None:
    """Reply whose native_message_id is empty produces no m.relates_to."""
    renderer = MatrixRenderer()
    event = _event(relations=(_reply_rel(adapter=_TARGET, native_message_id=""),))
    result = await renderer.render(event, _ctx())

    assert result.payload.get("m.relates_to") is None


# ---------------------------------------------------------------------------
# Reaction target selection
# ---------------------------------------------------------------------------


async def test_reaction_true_m_reaction_when_native_target_exists() -> None:
    """Reaction renders true m.reaction when Matrix-native target exists."""
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reaction_rel(adapter=_TARGET, native_message_id="$mx-r1"),),
        payload={"body": "👍"},
    )
    result = await renderer.render(event, _ctx())

    assert result.payload.get("_matrix_event_type") == "m.reaction"
    relates = result.payload["m.relates_to"]
    assert relates["rel_type"] == "m.annotation"  # type: ignore[index]
    assert relates["event_id"] == "$mx-r1"  # type: ignore[index]


async def test_reaction_emote_fallback_when_no_native_target() -> None:
    """Reaction falls back to m.emote when native ref is from a different adapter."""
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reaction_rel(adapter="other-radio", native_message_id="$mx-r2"),),
        payload={"body": "❤️"},
    )
    result = await renderer.render(event, _ctx())

    # No true m.reaction; should be emote fallback
    assert result.payload.get("_matrix_event_type") is None
    assert result.payload["msgtype"] == "m.emote"


async def test_reaction_emote_fallback_when_no_native_ref_at_all() -> None:
    """Reaction with no target_native_ref falls back to m.emote."""
    renderer = MatrixRenderer()
    rel = EventRelation(
        relation_type="reaction",
        target_event_id="canonical-99",
        target_native_ref=None,
        key="👍",
        fallback_text=None,
    )
    event = _event(relations=(rel,), payload={"body": "👍"})
    result = await renderer.render(event, _ctx())

    assert result.payload["msgtype"] == "m.emote"


# ---------------------------------------------------------------------------
# Missing target (no relations)
# ---------------------------------------------------------------------------


async def test_no_relations_renders_plain_text() -> None:
    """Event with no relations renders as plain m.text."""
    renderer = MatrixRenderer()
    event = _event()
    result = await renderer.render(event, _ctx())

    assert result.payload["msgtype"] == "m.text"
    assert result.payload["body"] == "hello"
    assert "m.relates_to" not in result.payload
    assert "_matrix_event_type" not in result.payload


# ---------------------------------------------------------------------------
# Stale target / no pre-validation
# ---------------------------------------------------------------------------


async def test_stale_native_target_emitted_without_validation() -> None:
    """Renderer emits the native event ID even if it no longer exists on the server.

    The renderer trusts the ``target_native_ref`` blindly — it does not
    validate whether the Matrix event still exists.  This is the current
    contract: pre-validation is the adapter's responsibility, not the
    renderer's.
    """
    renderer = MatrixRenderer()
    event = _event(
        relations=(_reply_rel(adapter=_TARGET, native_message_id="$mx-deleted-event"),),
    )
    result = await renderer.render(event, _ctx())

    relates = result.payload["m.relates_to"]
    assert relates["m.in_reply_to"]["event_id"] == "$mx-deleted-event"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Multiple relations — only relations[0] is used
# ---------------------------------------------------------------------------


async def test_multiple_relations_uses_only_first() -> None:
    """When an event carries multiple relations, only relations[0] is rendered."""
    renderer = MatrixRenderer()
    rel1 = _reply_rel(adapter=_TARGET, native_message_id="$mx-first")
    rel2 = _reaction_rel(adapter=_TARGET, native_message_id="$mx-second")
    event = _event(relations=(rel1, rel2))
    result = await renderer.render(event, _ctx())

    # First relation is a reply — m.relates_to should be a reply, not a reaction
    relates = result.payload["m.relates_to"]
    assert "m.in_reply_to" in relates  # type: ignore[operator]
    assert relates["m.in_reply_to"]["event_id"] == "$mx-first"  # type: ignore[index]
    # No reaction annotation from rel2
    assert result.payload.get("_matrix_event_type") is None


async def test_second_relation_ignored_when_first_is_reaction() -> None:
    """Second relation is ignored; first (reaction) determines the output."""
    renderer = MatrixRenderer()
    rel1 = _reaction_rel(adapter=_TARGET, native_message_id="$mx-r-first", key="🔥")
    rel2 = _reply_rel(adapter=_TARGET, native_message_id="$mx-reply-second")
    event = _event(relations=(rel1, rel2), payload={"body": "🔥"})
    result = await renderer.render(event, _ctx())

    # First relation is a reaction — should render m.reaction
    assert result.payload.get("_matrix_event_type") == "m.reaction"
    relates = result.payload["m.relates_to"]
    assert relates["rel_type"] == "m.annotation"  # type: ignore[index]
    assert relates["key"] == "🔥"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Fallback-text strategy
# ---------------------------------------------------------------------------


async def test_fallback_text_strategy_no_native_relates_to() -> None:
    """Under fallback_text strategy, no m.relates_to or _matrix_event_type."""
    renderer = MatrixRenderer()
    event = _event(relations=(_reply_rel(adapter=_TARGET, native_message_id="$mx-fb"),))
    result = await renderer.render(event, _ctx(delivery_strategy="fallback_text"))

    assert "m.relates_to" not in result.payload
    assert "_matrix_event_type" not in result.payload
    assert result.fallback_applied == "strategy_fallback_text"
