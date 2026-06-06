"""Target-selection rule tests for fallback-only transports (MeshCore, LXMF).

Locks the current target-selection contracts for transports that do not
perform native relation handling:

* MeshCore and LXMF renderers delegate relation degradation to the shared
  ``degrade_relations_inline()`` helper under ``fallback_text`` strategy.
  This helper iterates **all** relations — not just ``relations[0]``.
* MeshCore direct mode: does not inspect relations for native fields
  (no ``reply_id``, no ``emoji``).  It simply extracts text.
* LXMF direct mode: embeds the full ``event.relations`` tuple in the
  MEDRE fields envelope (all relations, not just ``relations[0]``).
* Missing target (empty relations): both render plain text with no inline
  relation text.
* Fallback-text strategy on MeshCore: all relations are degraded into
  inline text in the payload body.
* Fallback-text strategy on LXMF: all relations degraded inline AND
  envelope carries empty relations list (no duplication).
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
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


def _meshcore_renderer(adapter_id: str = "mc-node") -> MeshCoreRenderer:
    cfg = MeshCoreConfig(adapter_id=adapter_id, max_text_bytes=512)
    return MeshCoreRenderer(configs={adapter_id: cfg})


def _meshcore_ctx(
    target_adapter: str = "mc-node",
    delivery_strategy: DeliveryStrategyMethod = "direct",
) -> RenderingContext:
    return RenderingContext(
        delivery_strategy=delivery_strategy,
        target_adapter=target_adapter,
        target_platform="meshcore",
    )


def _lxmf_ctx(
    target_adapter: str = "lxmf-node",
    delivery_strategy: DeliveryStrategyMethod = "direct",
) -> RenderingContext:
    return RenderingContext(
        delivery_strategy=delivery_strategy,
        target_adapter=target_adapter,
        target_platform="lxmf",
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
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload or {"body": "hello"},
        metadata=EventMetadata(),
    )


def _reply_rel(
    adapter: str = "mc-node",
    native_message_id: str = "42",
    fallback_text: str | None = "original msg",
) -> EventRelation:
    return EventRelation(
        relation_type="reply",
        target_event_id="canonical-42",
        target_native_ref=NativeRef(
            adapter=adapter,
            native_channel_id="0",
            native_message_id=native_message_id,
        ),
        key=None,
        fallback_text=fallback_text,
    )


def _reaction_rel(
    adapter: str = "mc-node",
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


# ===========================================================================
# MeshCore
# ===========================================================================


# ---------------------------------------------------------------------------
# Direct mode — no native relation fields
# ---------------------------------------------------------------------------


async def test_meshcore_direct_no_reply_id() -> None:
    """MeshCore direct mode does not set reply_id from relations."""
    renderer = _meshcore_renderer()
    event = _event(relations=(_reply_rel(),))
    result = await renderer.render(event, _meshcore_ctx())

    assert "reply_id" not in result.payload
    assert result.payload.get("text") == "hello"


async def test_meshcore_direct_no_emoji() -> None:
    """MeshCore direct mode does not set emoji from reaction relations."""
    renderer = _meshcore_renderer()
    event = _event(relations=(_reaction_rel(),), payload={"body": "👍"})
    result = await renderer.render(event, _meshcore_ctx())

    assert "emoji" not in result.payload


# ---------------------------------------------------------------------------
# Fallback-text mode — degrade_relations_inline iterates ALL relations
# ---------------------------------------------------------------------------


async def test_meshcore_fallback_all_relations_inlined() -> None:
    """Under fallback_text, ALL relations are degraded into inline text."""
    renderer = _meshcore_renderer()
    rel1 = _reply_rel(fallback_text="msg A")
    rel2 = _reaction_rel(key="🔥")
    event = _event(relations=(rel1, rel2), payload={"body": "hello"})
    result = await renderer.render(
        event, _meshcore_ctx(delivery_strategy="fallback_text")
    )

    text = str(result.payload.get("text", ""))
    # Both relations should appear in the text
    assert "[reply to:" in text
    assert "[reaction" in text
    assert result.fallback_applied == "strategy_fallback_text"


async def test_meshcore_fallback_first_relation_inlined() -> None:
    """Single relation is degraded into inline text under fallback_text."""
    renderer = _meshcore_renderer()
    event = _event(
        relations=(_reply_rel(fallback_text="msg B"),),
        payload={"body": "reply here"},
    )
    result = await renderer.render(
        event, _meshcore_ctx(delivery_strategy="fallback_text")
    )

    text = str(result.payload.get("text", ""))
    assert "[reply to: msg B]" in text


# ---------------------------------------------------------------------------
# Missing target (no relations)
# ---------------------------------------------------------------------------


async def test_meshcore_no_relations_plain_text() -> None:
    """No relations renders plain text with no inline relation text."""
    renderer = _meshcore_renderer()
    event = _event()
    result = await renderer.render(event, _meshcore_ctx())

    assert result.payload.get("text") == "hello"


# ===========================================================================
# LXMF
# ===========================================================================


# ---------------------------------------------------------------------------
# Direct mode — full relations embedded in envelope
# ---------------------------------------------------------------------------


async def test_lxmf_direct_embeds_all_relations_in_envelope() -> None:
    """LXMF direct mode embeds all relations (not just relations[0]) in fields."""
    renderer = LxmfRenderer(metadata_embedding=True)
    rel1 = _reply_rel(fallback_text="msg X")
    rel2 = _reaction_rel(key="❤️")
    event = _event(relations=(rel1, rel2), payload={"body": "hello"})
    result = await renderer.render(event, _lxmf_ctx())

    fields = result.payload.get("fields", {})
    assert isinstance(fields, dict)
    # The envelope should contain both relations
    # Look for the envelope dict — it's nested under the MEDRE key
    found_rels: list[dict] = []
    for _key, val in fields.items():
        if isinstance(val, dict):
            env = val.get("medre")
            if isinstance(env, dict):
                rels = env.get("relations", [])
                found_rels = rels
                break
    assert len(found_rels) == 2
    assert found_rels[0].get("relation_type") == "reply"
    assert found_rels[1].get("relation_type") == "reaction"


async def test_lxmf_direct_no_inline_relation_text() -> None:
    """LXMF direct mode does NOT degrade relations into inline text."""
    renderer = LxmfRenderer(metadata_embedding=True)
    event = _event(
        relations=(_reply_rel(fallback_text="msg X"),),
        payload={"body": "hello"},
    )
    result = await renderer.render(event, _lxmf_ctx())

    content = str(result.payload.get("content", ""))
    assert "[reply to:" not in content
    assert content == "hello"


# ---------------------------------------------------------------------------
# Fallback-text mode — relations degraded inline, envelope empty
# ---------------------------------------------------------------------------


async def test_lxmf_fallback_all_relations_inlined() -> None:
    """Under fallback_text, ALL relations are degraded into inline text."""
    renderer = LxmfRenderer(metadata_embedding=True)
    rel1 = _reply_rel(fallback_text="msg A")
    rel2 = _reaction_rel(key="🔥")
    event = _event(relations=(rel1, rel2), payload={"body": "hello"})
    result = await renderer.render(event, _lxmf_ctx(delivery_strategy="fallback_text"))

    content = str(result.payload.get("content", ""))
    assert "[reply to:" in content
    assert "[reaction" in content
    assert result.fallback_applied == "strategy_fallback_text"


async def test_lxmf_fallback_envelope_relations_empty() -> None:
    """Under fallback_text, the envelope carries empty relations to avoid duplication."""
    renderer = LxmfRenderer(metadata_embedding=True)
    event = _event(
        relations=(_reply_rel(fallback_text="msg A"),),
        payload={"body": "hello"},
    )
    result = await renderer.render(event, _lxmf_ctx(delivery_strategy="fallback_text"))

    fields = result.payload.get("fields", {})
    assert isinstance(fields, dict)
    for _key, val in fields.items():
        if isinstance(val, dict):
            env = val.get("medre")
            if isinstance(env, dict):
                assert env.get("relations") == []
                break


# ---------------------------------------------------------------------------
# Missing target (no relations)
# ---------------------------------------------------------------------------


async def test_lxmf_no_relations_plain_text() -> None:
    """No relations renders plain content with no inline relation text."""
    renderer = LxmfRenderer(metadata_embedding=True)
    event = _event()
    result = await renderer.render(event, _lxmf_ctx())

    assert result.payload.get("content") == "hello"


async def test_lxmf_no_relations_envelope_empty_relations() -> None:
    """No relations produces envelope with empty relations list."""
    renderer = LxmfRenderer(metadata_embedding=True)
    event = _event()
    result = await renderer.render(event, _lxmf_ctx())

    fields = result.payload.get("fields", {})
    assert isinstance(fields, dict)
    for _key, val in fields.items():
        if isinstance(val, dict):
            env = val.get("medre")
            if isinstance(env, dict):
                assert env.get("relations") == []
                break


# ---------------------------------------------------------------------------
# Metadata embedding disabled
# ---------------------------------------------------------------------------


async def test_lxmf_no_metadata_embedding_no_fields() -> None:
    """With metadata_embedding=False, no envelope is embedded."""
    renderer = LxmfRenderer(metadata_embedding=False)
    event = _event(relations=(_reply_rel(fallback_text="msg A"),))
    result = await renderer.render(event, _lxmf_ctx())

    fields = result.payload.get("fields", {})
    assert fields == {}
