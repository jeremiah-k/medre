"""Fallback-text scenario tests: reaction, reply, evidence, and edit/delete/thread.

Verifies:

* **Fallback reaction text** — reaction events produce meaningful text
  when emoji present but no relation target is available.
* **Fallback reply sender metadata** — reply fallback uses enriched
  sender/displayname metadata from the relation.
* **Fallback text evidence recording** — ``fallback_applied`` field is set
  to ``"strategy_fallback_text"`` when the delivery strategy is
  fallback_text, regardless of which renderer handles the event.
* **TextRenderer edit/delete/thread** — dedicated degraded-text branches
  for edit, delete, and thread relation types.

All renderers and mock objects use the strict :class:`RenderingContext`
protocol — no legacy positional-arg signatures.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.canonical import EventRelation, NativeRef
from medre.core.rendering.renderer import (
    CapabilityLevel,
    DeliveryStrategyMethod,
    RenderingContext,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ===================================================================
# Helpers
# ===================================================================


def _ctx(
    *,
    delivery_strategy: DeliveryStrategyMethod = "direct",
    target_adapter: str = "dest",
    target_channel: str | None = None,
    target_platform: str | None = None,
    max_text_chars: int | None = None,
    max_text_bytes: int | None = None,
    capability_level: CapabilityLevel = "native",
) -> RenderingContext:
    """Build a frozen RenderingContext for unit tests."""
    return RenderingContext(
        delivery_strategy=delivery_strategy,
        target_adapter=target_adapter,
        target_channel=target_channel,
        target_platform=target_platform,
        max_text_chars=max_text_chars,
        max_text_bytes=max_text_bytes,
        capability_level=capability_level,
    )


def _make_reaction_event(
    event_id: str = "react-001",
    source_adapter: str = "src",
    emoji: str = "\U0001f44d",
    fallback_text: str | None = None,
    target_event_id: str | None = None,
    payload: dict | None = None,
) -> CanonicalEvent:
    """Build a message.reacted event with a reaction relation."""
    rel = EventRelation(
        relation_type="reaction",
        target_event_id=target_event_id,
        target_native_ref=None,
        key=emoji if emoji else None,
        fallback_text=fallback_text,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.reacted",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload=payload or {"emoji": emoji},
        metadata=EventMetadata(),
    )


def _make_reply_event(
    event_id: str = "reply-001",
    source_adapter: str = "src",
    body: str = "a reply",
    fallback_text: str = "original message",
    sender_displayname: str | None = None,
    sender: str | None = None,
) -> CanonicalEvent:
    """Build a message.text event with a reply relation."""
    meta: dict[str, object] = {}
    if sender_displayname:
        meta["sender_displayname"] = sender_displayname
    if sender:
        meta["sender"] = sender

    rel = EventRelation(
        relation_type="reply",
        target_event_id="evt-parent",
        target_native_ref=NativeRef(
            adapter="dest",
            native_channel_id="ch-0",
            native_message_id="native-001",
        ),
        key=None,
        fallback_text=fallback_text,
        metadata=meta,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.text",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"text": body},
        metadata=EventMetadata(),
    )


def _make_text_event(
    event_id: str = "evt-001",
    source_adapter: str = "src",
    body: str = "hello",
    event_kind: str = "message.text",
    relations: tuple | None = None,
) -> CanonicalEvent:
    """Build a simple text event."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload={"body": body},
        metadata=EventMetadata(),
    )


def _make_edit_event(
    event_id: str = "edit-001",
    source_adapter: str = "src",
    body: str = "",
    fallback_text: str | None = None,
    target_event_id: str | None = "evt-original",
) -> CanonicalEvent:
    """Build a message.edited event with an edit relation."""
    rel = EventRelation(
        relation_type="edit",
        target_event_id=target_event_id,
        target_native_ref=None,
        key=None,
        fallback_text=fallback_text,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.edited",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": body},
        metadata=EventMetadata(),
    )


def _make_delete_event(
    event_id: str = "del-001",
    source_adapter: str = "src",
    fallback_text: str | None = None,
    target_event_id: str | None = None,
    target_native_ref: NativeRef | None = None,
) -> CanonicalEvent:
    """Build a message.deleted event with a delete relation."""
    rel = EventRelation(
        relation_type="delete",
        target_event_id=target_event_id,
        target_native_ref=target_native_ref,
        key=None,
        fallback_text=fallback_text,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.deleted",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={},
        metadata=EventMetadata(),
    )


def _make_thread_event(
    event_id: str = "thread-001",
    source_adapter: str = "src",
    body: str = "thread reply",
    fallback_text: str | None = "thread root msg",
    target_event_id: str | None = "evt-thread-root",
) -> CanonicalEvent:
    """Build a message.text event with a thread relation."""
    rel = EventRelation(
        relation_type="thread",
        target_event_id=target_event_id,
        target_native_ref=None,
        key=None,
        fallback_text=fallback_text,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.text",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": body},
        metadata=EventMetadata(),
    )


# ===================================================================
# TestFallbackReactionText
# ===================================================================


class TestFallbackReactionText:
    """Verify reaction fallback produces meaningful text when emoji present
    but no relation target is available.

    When a reaction event has an emoji/key but no target_native_ref
    (no relation target), the fallback text renderer still produces
    meaningful output — not empty or ambiguous text.
    """

    @pytest.mark.asyncio
    async def test_reaction_with_emoji_no_target_yields_meaningful_text(
        self,
    ) -> None:
        """Reaction with emoji but no native target: TextRenderer produces
        '{actor} reacted with {emoji}'."""
        renderer = TextRenderer()
        event = _make_reaction_event(
            emoji="\U0001f44d",
            target_event_id=None,
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0
        # Contains the emoji.
        assert "\U0001f44d" in text
        # Contains reaction indicator.
        assert "reacted" in text.lower()

    @pytest.mark.asyncio
    async def test_reaction_no_emoji_no_target_yields_meaningful_text(
        self,
    ) -> None:
        """Reaction with no emoji and no target: still produces meaningful
        text, not empty."""
        renderer = TextRenderer()
        event = _make_text_event(
            event_kind="message.reacted",
            body="",
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id=None,
                    target_native_ref=None,
                    key=None,
                    fallback_text=None,
                ),
            ),
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0
        # Still says "reacted".
        assert "reacted" in text.lower()

    @pytest.mark.asyncio
    async def test_reaction_with_emoji_no_target_matrix_fallback(
        self,
    ) -> None:
        """Matrix fallback_text for reaction with no target: body contains
        reaction info, no m.relates_to."""
        renderer = MatrixRenderer()
        event = _make_reaction_event(
            emoji="\U0001f44d",
            target_event_id=None,
        )
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="matrix")

        result = await renderer.render(event, ctx)

        body = result.payload.get("body", "")
        assert isinstance(body, str)
        assert "\U0001f44d" in body
        assert "m.relates_to" not in result.payload
        assert "_matrix_event_type" not in result.payload


# ===================================================================
# TestFallbackReplySenderMetadata
# ===================================================================


class TestFallbackReplySenderMetadata:
    """Verify reply fallback uses enriched sender/displayname metadata
    from the relation.

    When the pipeline enriches relations with sender_displayname and
    sender metadata, the text fallback for replies includes the sender
    information.
    """

    @pytest.mark.asyncio
    async def test_reply_fallback_uses_sender_displayname(self) -> None:
        """Reply with sender_displayname in metadata: fallback text includes
        'by {displayname}'."""
        renderer = TextRenderer()
        event = _make_reply_event(
            body="my reply",
            fallback_text="original message",
            sender_displayname="Alice",
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        # Sender displayname used in reply context.
        assert "Alice" in text

    @pytest.mark.asyncio
    async def test_reply_fallback_uses_sender_fallback(self) -> None:
        """Reply with sender (no displayname): fallback uses sender field."""
        renderer = TextRenderer()
        event = _make_reply_event(
            body="my reply",
            fallback_text="original message",
            sender="@user:matrix.org",
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        # Sender ID used when no displayname available.
        assert "@user:matrix.org" in text

    @pytest.mark.asyncio
    async def test_reply_fallback_without_sender_no_crash(self) -> None:
        """Reply without sender metadata: no crash, meaningful text still
        produced."""
        renderer = TextRenderer()
        event = _make_reply_event(
            body="my reply",
            fallback_text="original message",
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0
        # Should still contain the reply target.
        assert "original message" in text


# ===================================================================
# TestFallbackTextEvidenceRecording
# ===================================================================


class TestFallbackTextEvidenceRecording:
    """Verify fallback_text records evidence it was fallback, not native
    relation delivery.

    The RenderingResult.fallback_applied field is set to
    ``"strategy_fallback_text"`` when the delivery strategy is
    fallback_text, regardless of which renderer handles the event.
    """

    @pytest.mark.asyncio
    async def test_matrix_fallback_records_strategy_marker(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Matrix adapter with reactions='fallback' records
        fallback_applied='strategy_fallback_text' on the rendered result."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="fallback",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="fallback-evidence-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="fallback-evidence-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            rendered = adapter.delivered_payloads[0]
            # Evidence marker: strategy_fallback_text, not a native relation.
            assert rendered.fallback_applied == "strategy_fallback_text"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_direct_reaction_no_fallback_marker(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with reactions='native': no fallback_applied marker."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="direct-no-fallback-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="direct-no-fallback-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            rendered = adapter.delivered_payloads[0]
            assert rendered.fallback_applied != "strategy_fallback_text"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_text_renderer_fallback_records_strategy_marker(
        self,
    ) -> None:
        """TextRenderer with fallback_text strategy records
        fallback_applied='strategy_fallback_text'."""
        renderer = TextRenderer()
        event = _make_reaction_event(emoji="\U0001f44d")
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.asyncio
    async def test_text_renderer_direct_no_strategy_marker(
        self,
    ) -> None:
        """TextRenderer with direct strategy: no strategy_fallback_text."""
        renderer = TextRenderer()
        event = _make_text_event(body="hello")
        ctx = _ctx(delivery_strategy="direct")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied is None

    @pytest.mark.asyncio
    async def test_lxmf_fallback_records_strategy_marker(self) -> None:
        """LxmfRenderer with fallback_text records fallback_applied."""
        renderer = LxmfRenderer()
        event = _make_reaction_event(emoji="\U0001f44d")
        ctx = _ctx(delivery_strategy="fallback_text", target_platform="lxmf")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"


# ===================================================================
# TestTextRendererEditDeleteThread
# ===================================================================


class TestTextRendererEditDeleteThread:
    """Verify TextRenderer edit, delete, and thread relation fallback paths.

    These relation types have dedicated degraded-text branches in
    :meth:`TextRenderer._extract_text`.  The tests cover each branch
    including edge cases: empty edit body, delete without target context,
    and thread with/without payload text.
    """

    # -- Edit relation fallback ------------------------------------------

    @pytest.mark.asyncio
    async def test_edit_with_body_produces_edited_prefix(self) -> None:
        """Edit relation with body: '[edited] {body}'."""
        renderer = TextRenderer()
        event = _make_edit_event(body="corrected text")
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[edited] corrected text"
        assert result.fallback_applied == "relation_edit"

    @pytest.mark.asyncio
    async def test_edit_empty_body_produces_bare_edited(self) -> None:
        """Edit relation with empty body: '[edited]' (no trailing space)."""
        renderer = TextRenderer()
        event = _make_edit_event(body="")
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[edited]"
        assert result.fallback_applied == "relation_edit"

    @pytest.mark.asyncio
    async def test_edit_strategy_fallback_preserves_relation_in_metadata(
        self,
    ) -> None:
        """Edit relation under fallback_text strategy: strategy marker
        overrides, but relation type preserved in metadata."""
        renderer = TextRenderer()
        event = _make_edit_event(body="fixed")
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"
        assert result.metadata.get("strategy_relation_type") == "edit"
        assert "[edited]" in result.payload["text"]

    # -- Delete relation fallback ----------------------------------------

    @pytest.mark.asyncio
    async def test_delete_with_target_context_includes_target(self) -> None:
        """Delete relation with fallback_text target: '[deleted: {target}]'."""
        renderer = TextRenderer()
        event = _make_delete_event(
            fallback_text="the original message",
            target_event_id="evt-abc123",
        )
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[deleted: the original message]"
        assert result.fallback_applied == "relation_delete"

    @pytest.mark.asyncio
    async def test_delete_with_abbreviated_event_id_target(self) -> None:
        """Delete relation with no fallback_text but target_event_id:
        uses abbreviated event ID."""
        renderer = TextRenderer()
        event = _make_delete_event(
            target_event_id="evt-abcdefghijklmnop",
        )
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        # target_event_id abbreviated to first 8 chars + ellipsis.
        assert "evt-abc" in text
        assert result.fallback_applied == "relation_delete"

    @pytest.mark.asyncio
    async def test_delete_without_target_yields_bare_deleted(self) -> None:
        """Delete relation with no target context at all: '[deleted]'."""
        renderer = TextRenderer()
        event = _make_delete_event(
            target_event_id=None,
            target_native_ref=None,
        )
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[deleted]"
        assert result.fallback_applied == "relation_delete"

    # -- Thread relation fallback ----------------------------------------

    @pytest.mark.asyncio
    async def test_thread_with_body_includes_target_and_text(self) -> None:
        """Thread relation with body: '[thread: {target}] {body}'."""
        renderer = TextRenderer()
        event = _make_thread_event(body="a thread reply")
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert "[thread: thread root msg]" in text
        assert "a thread reply" in text
        assert result.fallback_applied == "relation_thread"

    @pytest.mark.asyncio
    async def test_thread_empty_body_includes_target_only(self) -> None:
        """Thread relation with empty body: '[thread: {target}]' (no trailing
        space)."""
        renderer = TextRenderer()
        event = _make_thread_event(body="")
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert text == "[thread: thread root msg]"
        assert result.fallback_applied == "relation_thread"

    @pytest.mark.asyncio
    async def test_thread_strategy_fallback_preserves_relation_in_metadata(
        self,
    ) -> None:
        """Thread relation under fallback_text strategy: strategy marker
        overrides, but relation type preserved in metadata."""
        renderer = TextRenderer()
        event = _make_thread_event(body="in-thread msg")
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"
        assert result.metadata.get("strategy_relation_type") == "thread"
        assert "[thread:" in result.payload["text"]


# ===================================================================
# TestReactionKeyFromPayload
# ===================================================================


class TestReactionKeyFromPayload:
    """Verify _resolve_reaction_key falls back to payload["key"] when
    rel.key is None.

    The resolution order is: rel.key → payload["key"] → payload["emoji"]
    → payload["body"].  When rel.key is absent but the payload carries a
    ``"key"`` field, the renderer must pick it up.
    """

    @pytest.mark.asyncio
    async def test_payload_key_used_when_rel_key_is_none(self) -> None:
        """Reaction with rel.key=None and payload={"key": "👍"}: rendered
        text contains 👍."""
        renderer = TextRenderer()
        event = _make_reaction_event(
            emoji="",
            target_event_id=None,
            payload={"key": "👍"},
        )
        # Override the relation key to None — _make_reaction_event sets
        # key=emoji by default but we passed empty string.
        # Build a fresh event with key=None explicitly.
        from medre.core.events.canonical import EventRelation as ER

        rel = ER(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="rxn-pkey-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"key": "👍"},
            metadata=EventMetadata(),
        )
        ctx = _ctx(delivery_strategy="fallback_text")

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert isinstance(text, str)
        assert "👍" in text
        assert "reacted" in text.lower()

    @pytest.mark.asyncio
    async def test_rel_key_takes_precedence_over_payload_key(self) -> None:
        """When both rel.key and payload["key"] exist, rel.key wins."""
        renderer = TextRenderer()
        from medre.core.events.canonical import EventRelation as ER

        rel = ER(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="❤️",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="rxn-precedence-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"key": "👍"},
            metadata=EventMetadata(),
        )
        ctx = _ctx()

        result = await renderer.render(event, ctx)

        text = result.payload["text"]
        assert "❤️" in text
        assert "👍" not in text


# ===================================================================
# TestRenderingContextStrategyValidation
# ===================================================================


class TestRenderingContextStrategyValidation:
    """Verify RenderingContext rejects unknown delivery_strategy values
    at runtime with a ValueError."""

    def test_bogus_strategy_raises_value_error(self) -> None:
        """Passing 'bogus' as delivery_strategy raises ValueError."""
        with pytest.raises(ValueError, match="Unknown delivery_strategy"):
            RenderingContext(
                delivery_strategy="bogus",  # type: ignore[arg-type]
                target_adapter="dest",
            )

    def test_valid_strategies_accepted(self) -> None:
        """All well-known strategies are accepted without error."""
        for strategy in (
            "direct",
            "fallback_text",
            "skip",
            "propagated",
            "opportunistic",
            "paper",
        ):
            ctx = RenderingContext(
                delivery_strategy=strategy,  # type: ignore[arg-type]
                target_adapter="dest",
            )
            assert ctx.delivery_strategy == strategy

    def test_empty_string_rejected(self) -> None:
        """Passing '' (empty string) as delivery_strategy raises ValueError.

        The pipeline normalisation uses ``is None`` (not falsy) to
        default to "direct", so an empty string must be caught by
        RenderingContext validation rather than silently converted.
        """
        with pytest.raises(ValueError, match="Unknown delivery_strategy"):
            RenderingContext(
                delivery_strategy="",  # type: ignore[arg-type]
                target_adapter="dest",
            )


# ===================================================================
# TestSkipDeliveryGuard
# ===================================================================


class TestSkipDeliveryGuard:
    """Verify RenderingPipeline.render() rejects delivery_strategy='skip'.

    The skip strategy must be handled upstream (by the delivery plan /
    pipeline runner) before any rendering attempt.  Calling render() with
    ``"skip"`` raises ValueError and never invokes a registered renderer.
    """

    @pytest.mark.asyncio
    async def test_skip_strategy_raises_value_error(self) -> None:
        """RenderingPipeline.render(..., delivery_strategy='skip') raises
        ValueError and no renderer is called."""
        call_count = 0

        class SpyRenderer:
            name = "spy"

            def can_render(self, event, ctx):
                return True

            async def render(self, event, ctx):
                nonlocal call_count
                call_count += 1
                return RenderingResult(
                    event_id=event.event_id,
                    target_adapter=ctx.target_adapter,
                    target_channel=ctx.target_channel,
                    payload={},
                )

        pipeline = RenderingPipeline()
        pipeline.register(SpyRenderer(), priority=1)

        event = _make_text_event(event_id="skip-guard-001", body="hello")

        with pytest.raises(
            ValueError,
            match="delivery_strategy='skip' must be handled before rendering",
        ):
            await pipeline.render(
                event,
                target_adapter="dest",
                delivery_strategy="skip",
            )

        assert call_count == 0, "No renderer should have been called"
