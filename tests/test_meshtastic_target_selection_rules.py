"""Target-selection rule tests for MeshtasticRenderer.

Locks the current target-selection contracts:

* MeshtasticRenderer uses ``relations[0]`` only — subsequent relations ignored.
* Reply target: ``_meshtastic_reply_id_from_relation`` returns a numeric ID
  when ``target_native_ref.adapter == target_adapter`` and the native_message_id
  is numeric.  Cross-adapter refs are rejected.
* Metadata fallback: when no owned native ref exists, the renderer falls back
  to ``relation.metadata["meshtastic_reply_id"]``.
* Reaction: native Meshtastic tapback (``emoji=1`` + ``reply_id``) when
  ``event.source_adapter == target_adapter`` and a numeric ref exists.
  Cross-platform reactions use MMRelay-style descriptive text.
* Missing target (empty relations): renders plain text.
* Stale native target: no pre-validation — renderer trusts the ref blindly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
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

_TARGET = "mesh-1"


def _config(
    adapter_id: str = _TARGET,
    max_text_bytes: int = 227,
) -> MeshtasticConfig:
    return MeshtasticConfig(
        adapter_id=adapter_id,
        radio_relay_prefix="",
        meshnet_name="",
        max_text_bytes=max_text_bytes,
    )


def _renderer(target_adapter: str = _TARGET) -> MeshtasticRenderer:
    return MeshtasticRenderer(configs={target_adapter: _config(target_adapter)})


def _ctx(
    target_adapter: str = _TARGET,
    delivery_strategy: DeliveryStrategyMethod = "direct",
) -> RenderingContext:
    return RenderingContext(
        delivery_strategy=delivery_strategy,
        target_adapter=target_adapter,
        target_platform="meshtastic",
    )


def _event(
    relations: tuple[EventRelation, ...] = (),
    payload: dict[str, object] | None = None,
    source_adapter: str = _TARGET,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-001",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="!node1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload or {"body": "hello mesh"},
        metadata=EventMetadata(),
    )


def _reply_rel(
    adapter: str = _TARGET,
    native_message_id: str = "42",
    metadata: dict[str, object] | None = None,
) -> EventRelation:
    native_ref = None
    if native_message_id is not None:
        native_ref = NativeRef(
            adapter=adapter,
            native_channel_id="0",
            native_message_id=native_message_id,
        )
    return EventRelation(
        relation_type="reply",
        target_event_id="canonical-42",
        target_native_ref=native_ref,
        key=None,
        fallback_text=None,
        metadata=metadata or {},
    )


def _reaction_rel(
    adapter: str = _TARGET,
    native_message_id: str = "99",
    key: str = "👍",
) -> EventRelation:
    return EventRelation(
        relation_type="reaction",
        target_event_id="canonical-99",
        target_native_ref=NativeRef(
            adapter=adapter,
            native_channel_id="0",
            native_message_id=native_message_id,
        ),
        key=key,
        fallback_text=None,
    )


# ---------------------------------------------------------------------------
# Reply target selection — native ref ownership
# ---------------------------------------------------------------------------


async def test_reply_sets_reply_id_when_ref_owned_by_target() -> None:
    """Reply sets reply_id when native ref belongs to target adapter."""
    renderer = _renderer()
    event = _event(relations=(_reply_rel(adapter=_TARGET, native_message_id="42"),))
    result = await renderer.render(event, _ctx())

    assert result.payload.get("reply_id") == 42


async def test_reply_no_reply_id_when_ref_from_different_adapter() -> None:
    """Reply with a cross-adapter native ref does not set reply_id."""
    renderer = _renderer()
    event = _event(
        relations=(_reply_rel(adapter="other-radio", native_message_id="42"),)
    )
    result = await renderer.render(event, _ctx())

    assert "reply_id" not in result.payload


async def test_reply_no_reply_id_when_ref_is_non_numeric() -> None:
    """Reply with a non-numeric native_message_id does not set reply_id."""
    renderer = _renderer()
    event = _event(
        relations=(_reply_rel(adapter=_TARGET, native_message_id="not-a-number"),)
    )
    result = await renderer.render(event, _ctx())

    assert "reply_id" not in result.payload


# ---------------------------------------------------------------------------
# Metadata fallback for meshtastic_reply_id
# ---------------------------------------------------------------------------


async def test_metadata_fallback_for_meshtastic_reply_id() -> None:
    """When no owned native ref, falls back to metadata meshtastic_reply_id."""
    renderer = _renderer()
    rel = _reply_rel(
        adapter="other-radio",
        native_message_id="42",
        metadata={"meshtastic_reply_id": "77"},
    )
    event = _event(relations=(rel,))
    result = await renderer.render(event, _ctx())

    assert result.payload.get("reply_id") == 77


async def test_metadata_fallback_ignored_when_owned_ref_exists() -> None:
    """Owned native ref takes precedence over metadata fallback."""
    renderer = _renderer()
    rel = _reply_rel(
        adapter=_TARGET,
        native_message_id="42",
        metadata={"meshtastic_reply_id": "77"},
    )
    event = _event(relations=(rel,))
    result = await renderer.render(event, _ctx())

    # Owned ref (42) wins over metadata (77)
    assert result.payload.get("reply_id") == 42


# ---------------------------------------------------------------------------
# Reaction target selection — native vs cross-platform
# ---------------------------------------------------------------------------


async def test_native_reaction_sets_emoji_and_reply_id() -> None:
    """Native Meshtastic reaction sets emoji=1 and reply_id."""
    renderer = _renderer()
    # source_adapter == target_adapter → native reaction
    event = _event(
        relations=(_reaction_rel(adapter=_TARGET, native_message_id="99"),),
        payload={"body": "👍"},
        source_adapter=_TARGET,
    )
    result = await renderer.render(event, _ctx())

    assert result.payload.get("emoji") == 1
    assert result.payload.get("reply_id") == 99


async def test_cross_platform_reaction_uses_descriptive_text() -> None:
    """Cross-platform reaction uses MMRelay descriptive text, not emoji=1."""
    renderer = _renderer()
    # source_adapter != target_adapter → cross-platform
    event = _event(
        relations=(_reaction_rel(adapter=_TARGET, native_message_id="99"),),
        payload={"body": "👍"},
        source_adapter="matrix-1",
    )
    result = await renderer.render(event, _ctx())

    assert result.payload.get("emoji") is None
    assert "reacted" in str(result.payload.get("text", ""))


async def test_native_reaction_without_numeric_ref_no_emoji_field() -> None:
    """Native reaction without a numeric ref emits [reacted: …] text."""
    renderer = _renderer()
    rel = EventRelation(
        relation_type="reaction",
        target_event_id="canonical-99",
        target_native_ref=None,
        key="👍",
        fallback_text=None,
    )
    event = _event(
        relations=(rel,),
        payload={"body": "👍"},
        source_adapter=_TARGET,
    )
    result = await renderer.render(event, _ctx())

    # No emoji=1, no reply_id — just readable text
    assert result.payload.get("emoji") is None
    assert "reacted" in str(result.payload.get("text", ""))


# ---------------------------------------------------------------------------
# Missing target (no relations)
# ---------------------------------------------------------------------------


async def test_no_relations_renders_plain_text() -> None:
    """Event with no relations renders as plain text."""
    renderer = _renderer()
    event = _event()
    result = await renderer.render(event, _ctx())

    assert result.payload.get("text") == "hello mesh"
    assert "reply_id" not in result.payload
    assert "emoji" not in result.payload


# ---------------------------------------------------------------------------
# Stale native target — no pre-validation
# ---------------------------------------------------------------------------


async def test_stale_native_reply_id_emitted_without_validation() -> None:
    """Renderer emits reply_id even if the packet no longer exists on the radio.

    The renderer trusts the ``target_native_ref`` blindly — it does not
    validate whether the Meshtastic packet still exists.  Pre-validation
    is the adapter/session's responsibility, not the renderer's.
    """
    renderer = _renderer()
    event = _event(
        relations=(_reply_rel(adapter=_TARGET, native_message_id="9999999"),),
    )
    result = await renderer.render(event, _ctx())

    assert result.payload.get("reply_id") == 9999999


# ---------------------------------------------------------------------------
# Multiple relations — only relations[0] is used
# ---------------------------------------------------------------------------


async def test_multiple_relations_uses_only_first() -> None:
    """When multiple relations exist, only relations[0] determines output."""
    renderer = _renderer()
    rel1 = _reply_rel(adapter=_TARGET, native_message_id="10")
    rel2 = _reaction_rel(adapter=_TARGET, native_message_id="20")
    event = _event(relations=(rel1, rel2))
    result = await renderer.render(event, _ctx())

    # First relation is a reply — reply_id from rel1, no emoji from rel2
    assert result.payload.get("reply_id") == 10
    assert "emoji" not in result.payload


async def test_second_relation_ignored_when_first_is_reaction() -> None:
    """Second relation ignored; first (reaction) determines output."""
    renderer = _renderer()
    rel1 = _reaction_rel(adapter=_TARGET, native_message_id="30", key="🔥")
    rel2 = _reply_rel(adapter=_TARGET, native_message_id="40")
    event = _event(
        relations=(rel1, rel2),
        payload={"body": "🔥"},
        source_adapter=_TARGET,
    )
    result = await renderer.render(event, _ctx())

    # First relation is reaction → emoji=1, reply_id=30
    assert result.payload.get("emoji") == 1
    assert result.payload.get("reply_id") == 30


# ---------------------------------------------------------------------------
# Fallback-text strategy
# ---------------------------------------------------------------------------


async def test_fallback_text_no_reply_id_or_emoji() -> None:
    """Under fallback_text, native relation fields (reply_id, emoji) suppressed."""
    renderer = _renderer()
    event = _event(
        relations=(_reply_rel(adapter=_TARGET, native_message_id="42"),),
    )
    result = await renderer.render(event, _ctx(delivery_strategy="fallback_text"))

    assert "reply_id" not in result.payload
    assert "emoji" not in result.payload
    assert result.fallback_applied == "strategy_fallback_text"
