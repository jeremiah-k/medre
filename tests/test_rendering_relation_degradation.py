"""Relation fallback degradation, multi-relation determinism, and thread
relation capability tests for renderers.

Covers:
- All relation types (reply, reaction, edit, delete, thread) degrade
  deterministically under fallback_text across all renderers.
- Multi-relation events process only the first relation deterministically.
- Thread relation: native platforms get direct handling; degraded platforms
  get text-only thread representation.
"""

from __future__ import annotations

from typing import Literal, cast

import pytest

from medre.core.events import (
    EventRelation,
)
from tests.helpers.rendering_evidence import (
    make_context,
    make_event,
    make_lxmf_renderer,
    make_matrix_renderer,
    make_meshcore_renderer,
    make_meshtastic_renderer,
)

# ---------------------------------------------------------------------------
# Relation-specific helper
# ---------------------------------------------------------------------------


def _make_relation_event(
    relation_type: str,
    *,
    payload: dict | None = None,
    fallback_text: str | None = None,
    key: str | None = None,
):
    """Create an event with a specific relation type."""
    rel = EventRelation(
        relation_type=cast(
            Literal["reply", "reaction", "edit", "delete", "thread"],
            relation_type,
        ),
        target_event_id="evt-target-abc",
        target_native_ref=None,
        key=key,
        fallback_text=fallback_text,
    )
    return make_event(
        payload=payload or {"text": "test message"},
        relations=(rel,),
    )


# ===================================================================
# Fallback/degradation for all relation types (item C)
# ===================================================================


class TestRelationFallbackDegradation:
    """All relation types degrade deterministically under fallback_text."""

    # -- TextRenderer (shared text fallback) --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_text_renderer_relation_fallback_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """TextRenderer produces deterministic degraded text for each relation."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "hello"},
            key=key,
        )
        ctx = make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_text_renderer_relation_deterministic_across_calls(
        self,
        relation_type: str,
    ) -> None:
        """Same event produces identical text on repeated renders."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "hello"},
            key=key,
        )
        ctx = make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result1 = await renderer.render(event, ctx)
        result2 = await renderer.render(event, ctx)
        assert result1.payload["text"] == result2.payload["text"]

    # -- Meshtastic renderer fallback for each relation type --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "edit", "delete", "thread"],
        ids=["reply", "edit", "delete", "thread"],
    )
    async def test_meshtastic_fallback_relation_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """Meshtastic fallback_text produces deterministic degraded text."""
        renderer = make_meshtastic_renderer()
        event = _make_relation_event(
            relation_type,
            payload={"text": "test body"},
        )
        ctx = make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0

    # -- MeshCore renderer fallback for each relation type --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_meshcore_fallback_relation_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """MeshCore fallback_text produces deterministic degraded text."""
        renderer = make_meshcore_renderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "test body"},
            key=key,
        )
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0

    # -- LXMF renderer fallback for each relation type --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_lxmf_fallback_relation_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """LXMF fallback_text produces deterministic degraded content."""
        renderer = make_lxmf_renderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "test body"},
            key=key,
        )
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        content = result.payload.get("content", "")
        assert isinstance(content, str)
        assert len(content) > 0

    # -- Matrix renderer fallback for each relation type --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_matrix_fallback_relation_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """Matrix fallback_text produces deterministic degraded body."""
        renderer = make_matrix_renderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "test body"},
            key=key,
        )
        ctx = make_context(
            target_adapter="matrix-target",
            target_platform="matrix",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        assert "m.relates_to" not in result.payload
        body = result.payload.get("body", "")
        assert isinstance(body, str)
        assert len(body) > 0

    # -- Specific degradation format checks --

    async def test_text_renderer_reply_fallback_format(self) -> None:
        """TextRenderer reply fallback includes '[replying to:]' prefix."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-123456789",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "reply body"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "[replying to:" in text

    async def test_text_renderer_delete_fallback_format(self) -> None:
        """TextRenderer delete fallback produces '[deleted]' text."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="delete",
            target_event_id="evt-del-123",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": ""},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "[deleted" in text

    async def test_text_renderer_edit_fallback_format(self) -> None:
        """TextRenderer edit fallback includes '[edited]' prefix."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="edit",
            target_event_id="evt-edit-123",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "edited text"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert text.startswith("[edited]")

    async def test_text_renderer_reaction_fallback_format(self) -> None:
        """TextRenderer reaction fallback includes 'reacted with' text."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="reaction",
            target_event_id="evt-react-123",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "", "user": "Alice"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "reacted with" in text
        assert "👍" in text

    async def test_text_renderer_thread_fallback_format(self) -> None:
        """TextRenderer thread fallback includes '[thread:]' prefix."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-123",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "thread reply"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "[thread:" in text


# ===================================================================
# Multi-relation behavior determinism (item D)
# ===================================================================


class TestMultiRelationDeterminism:
    """When an event carries multiple relations, only the first is processed
    and the result is deterministic."""

    async def test_text_renderer_uses_first_relation_only(self) -> None:
        """TextRenderer processes only the first relation when multiple exist."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel1 = EventRelation(
            relation_type="reply",
            target_event_id="evt-first",
            target_native_ref=None,
            key=None,
            fallback_text="first target",
        )
        rel2 = EventRelation(
            relation_type="reaction",
            target_event_id="evt-second",
            target_native_ref=None,
            key="❤️",
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "hello"},
            relations=(rel1, rel2),
        )
        ctx = make_context(target_adapter="text-target")
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        # First relation is reply, so text should contain reply formatting
        assert "replying to" in text or "first target" in text
        # Second relation (reaction) should NOT appear
        assert "❤️" not in text and "reacted with" not in text

    async def test_meshcore_uses_first_relation_only(self) -> None:
        """MeshCore processes only the first relation."""
        renderer = make_meshcore_renderer()
        rel1 = EventRelation(
            relation_type="edit",
            target_event_id="evt-edit-first",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        rel2 = EventRelation(
            relation_type="delete",
            target_event_id="evt-del-second",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "multi-rel"},
            relations=(rel1, rel2),
        )
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        # First relation is edit → degrade_relations_inline appends [edit of: ...]
        assert "edit" in text.lower()
        # Second relation (delete) must NOT be processed
        assert "deleted" not in text.lower()

    async def test_multi_relation_deterministic_across_calls(self) -> None:
        """Same multi-relation event produces identical output every time."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel1 = EventRelation(
            relation_type="reply",
            target_event_id="evt-reply-001",
            target_native_ref=None,
            key=None,
            fallback_text="target msg",
        )
        rel2 = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-002",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "content"},
            relations=(rel1, rel2),
        )
        ctx = make_context(target_adapter="text-target")

        results = [await renderer.render(event, ctx) for _ in range(5)]
        texts = [str(r.payload["text"]) for r in results]
        # All renders produce identical text
        assert len(set(texts)) == 1

    async def test_relation_order_affects_output(self) -> None:
        """Swapping relation order produces different output."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel_reply = EventRelation(
            relation_type="reply",
            target_event_id="evt-001",
            target_native_ref=None,
            key=None,
            fallback_text="target",
        )
        rel_edit = EventRelation(
            relation_type="edit",
            target_event_id="evt-002",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event_reply_first = make_event(
            payload={"text": "content"},
            relations=(rel_reply, rel_edit),
        )
        event_edit_first = make_event(
            payload={"text": "content"},
            relations=(rel_edit, rel_reply),
        )
        ctx = make_context(target_adapter="text-target")

        result1 = await renderer.render(event_reply_first, ctx)
        result2 = await renderer.render(event_edit_first, ctx)

        assert str(result1.payload["text"]) != str(result2.payload["text"])


# ===================================================================
# Thread relation capability — native/direct vs degraded (item F)
# ===================================================================


class TestThreadRelationCapability:
    """Thread relation: native platforms get direct thread handling;
    degraded platforms get text-only thread representation."""

    async def test_matrix_fallback_thread_no_m_relates_to(self) -> None:
        """Matrix fallback_text thread omits m.relates_to."""
        renderer = make_matrix_renderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "thread reply"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="matrix-target",
            target_platform="matrix",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert "m.relates_to" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"
        assert "thread" in str(result.payload.get("body", "")).lower()

    async def test_meshcore_fallback_thread_degrades_text(self) -> None:
        """MeshCore fallback_text thread degrades to inline text."""
        renderer = make_meshcore_renderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-002",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "thread msg"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = str(result.payload["text"])
        assert "thread" in text.lower()
        # Byte budget is still enforced
        assert isinstance(result.metadata["rendered_text_bytes"], int)

    async def test_meshtastic_fallback_thread_degrades_text(self) -> None:
        """Meshtastic fallback_text thread degrades to readable text."""
        renderer = make_meshtastic_renderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-003",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "thread reply"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = str(result.payload["text"])
        assert "thread" in text.lower()

    async def test_lxmf_fallback_thread_degrades_text(self) -> None:
        """LXMF fallback_text thread degrades to inline text."""
        renderer = make_lxmf_renderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-004",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "thread content"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        content = str(result.payload["content"])
        assert "thread" in content.lower()

    async def test_text_renderer_thread_degrades_text(self) -> None:
        """TextRenderer thread relation produces deterministic degraded text."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-005",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "thread body"},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="text-target",
            delivery_strategy="direct",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "[thread:" in text
        assert "thread body" in text

    async def test_meshcore_thread_text_truncated_by_byte_budget(self) -> None:
        """Thread degraded text is still subject to byte budget truncation."""
        renderer = make_meshcore_renderer(max_text_bytes=20)
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-006",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "A" * 200},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.truncated is True
        assert len(str(result.payload["text"]).encode("utf-8")) <= 20

    async def test_thread_relation_deterministic_repeated_renders(self) -> None:
        """Thread degradation text is identical across repeated renders."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-007",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = make_event(
            payload={"text": "deterministic"},
            relations=(rel,),
        )
        ctx = make_context(target_adapter="text-target")

        results = [await renderer.render(event, ctx) for _ in range(3)]
        texts = [str(r.payload["text"]) for r in results]
        assert len(set(texts)) == 1
