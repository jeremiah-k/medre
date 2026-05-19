"""Bridge rendering realism tests.

Verifies that each transport renderer produces correct outbound payloads
when rendering events that originated from a *different* transport.
These tests exercise the rendering pipeline in a cross-transport (bridge)
configuration and assert the shape and content of the rendered output.

Tests are organised by scenario:
1. Matrix-originated event rendered for Meshtastic target
2. Meshtastic-originated event rendered for Matrix target
3. Source adapter display name handling
4. Reply / thread context preservation
5. Empty payload safety (no crash, no malformed output)
6. Special character escaping (HTML, control chars, Unicode)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.rendering.renderer import RenderingPipeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-bridge-001",
    event_kind: str = "message.created",
    source_adapter: str = "matrix-src",
    source_channel_id: str | None = "!room:matrix.org",
    payload: dict | None = None,
    relations: tuple[EventRelation, ...] = (),
) -> CanonicalEvent:
    """Create a CanonicalEvent suitable for bridge rendering tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="transport-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload if payload is not None else {"text": "hello bridge"},
        metadata=EventMetadata(),
    )


def _make_pipeline() -> RenderingPipeline:
    """Create a RenderingPipeline with all four transport renderers registered."""
    pipeline = RenderingPipeline()
    # Register with distinct priorities so ordering is deterministic.
    pipeline.register(MeshtasticRenderer(), priority=10)
    pipeline.register(MatrixRenderer(), priority=20)
    pipeline.register(MeshCoreRenderer(), priority=30)
    pipeline.register(LxmfRenderer(), priority=40)
    return pipeline


async def _render(
    pipeline: RenderingPipeline,
    event: CanonicalEvent,
    target_adapter: str,
    target_platform: str,
    target_channel: str | None = None,
):
    """Render an event through the pipeline for the given target platform."""
    pipeline.register_adapter_platform(target_adapter, target_platform)
    return await pipeline.render(
        event,
        target_adapter,
        target_channel,
    )


# ---------------------------------------------------------------------------
# Test 1: Matrix → Meshtastic rendering
# ---------------------------------------------------------------------------


class TestMatrixRendersForMeshtastic:
    """Matrix-originated events rendered for a Meshtastic target adapter."""

    @pytest.mark.asyncio
    async def test_text_payload_shape(self) -> None:
        """Rendered output is a dict with text, channel_index, meshnet_name."""
        event = _make_event(
            source_adapter="matrix-src",
            payload={"body": "Hello from Matrix", "text": "Hello from Matrix"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "mesh-target", "meshtastic")

        assert result.target_adapter == "mesh-target"
        assert result.event_id == event.event_id
        # MeshtasticRenderer produces: text, channel_index, meshnet_name
        payload = result.payload
        assert "text" in payload
        assert "channel_index" in payload
        assert "meshnet_name" in payload
        assert payload["text"] == "Hello from Matrix"
        assert isinstance(payload["channel_index"], int)

    @pytest.mark.asyncio
    async def test_body_key_preferred_over_text(self) -> None:
        """Meshtastic renderer extracts 'body' key first, then 'text'."""
        event = _make_event(
            source_adapter="matrix-src",
            payload={"body": "body content", "text": "text content"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "mesh-target", "meshtastic")

        assert result.payload["text"] == "body content"

    @pytest.mark.asyncio
    async def test_channel_index_from_target_channel(self) -> None:
        """target_channel is parsed as integer for channel_index."""
        event = _make_event(
            source_adapter="matrix-src",
            payload={"body": "test"},
        )
        pipeline = _make_pipeline()
        result = await _render(
            pipeline, event, "mesh-target", "meshtastic", target_channel="3"
        )

        assert result.payload["channel_index"] == 3

    @pytest.mark.asyncio
    async def test_channel_index_default_zero(self) -> None:
        """When no target_channel, channel_index defaults to 0."""
        event = _make_event(
            source_adapter="matrix-src",
            payload={"body": "test"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "mesh-target", "meshtastic")

        assert result.payload["channel_index"] == 0

    @pytest.mark.asyncio
    async def test_meshnet_name_empty(self) -> None:
        """meshnet_name is an empty string placeholder (tranche 1)."""
        event = _make_event(
            source_adapter="matrix-src",
            payload={"body": "test"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "mesh-target", "meshtastic")

        assert result.payload["meshnet_name"] == ""


# ---------------------------------------------------------------------------
# Test 2: Meshtastic → Matrix rendering
# ---------------------------------------------------------------------------


class TestMeshtasticRendersForMatrix:
    """Meshtastic-originated events rendered for a Matrix target adapter."""

    @pytest.mark.asyncio
    async def test_text_payload_shape(self) -> None:
        """Rendered output has msgtype, body, and medre envelope."""
        event = _make_event(
            source_adapter="mesh-src",
            source_channel_id="0",
            payload={"body": "Hello from mesh", "text": "Hello from mesh"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        assert result.target_adapter == "matrix-target"
        assert result.event_id == event.event_id
        payload = result.payload
        assert payload["msgtype"] == "m.text"
        assert payload["body"] == "Hello from mesh"

    @pytest.mark.asyncio
    async def test_medre_envelope_present(self) -> None:
        """Matrix renderer embeds a medre.envelope in the content dict."""
        event = _make_event(
            source_adapter="mesh-src",
            source_channel_id="0",
            payload={"body": "mesh msg"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        assert "medre" in result.payload
        envelope = result.payload["medre"]["envelope"]
        assert envelope["source_adapter"] == "mesh-src"
        assert envelope["canonical_event_id"] == event.event_id

    @pytest.mark.asyncio
    async def test_body_key_preferred(self) -> None:
        """Matrix renderer extracts 'body' key first."""
        event = _make_event(
            source_adapter="mesh-src",
            payload={"body": "body-val", "text": "text-val"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        assert result.payload["body"] == "body-val"

    @pytest.mark.asyncio
    async def test_metadata_renderer_name(self) -> None:
        """RenderingResult metadata identifies the renderer used."""
        event = _make_event(
            source_adapter="mesh-src",
            payload={"body": "test"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        assert result.metadata["renderer"] == "matrix"


# ---------------------------------------------------------------------------
# Test 3: Source display name handling
# ---------------------------------------------------------------------------


class TestSourceDisplayNameHandling:
    """Verify that renderer handles source_adapter labels correctly."""

    @pytest.mark.asyncio
    async def test_matrix_envelope_captures_source_adapter(self) -> None:
        """Matrix renderer's medre envelope records source_adapter."""
        event = _make_event(
            source_adapter="meshtastic-radio-1",
            source_channel_id="ch-1",
            payload={"body": "radio message"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-out", "matrix")

        envelope = result.payload["medre"]["envelope"]
        assert envelope["source_adapter"] == "meshtastic-radio-1"
        assert envelope["source_channel"] == "ch-1"

    @pytest.mark.asyncio
    async def test_lxmf_renderer_captures_source_adapter(self) -> None:
        """LXMF renderer's fields envelope records source_adapter."""
        event = _make_event(
            source_adapter="matrix-relay",
            source_channel_id="!room:server",
            payload={"body": "relay msg"},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "lxmf-out", "lxmf")

        # LXMF renderer embeds metadata via LxmfFieldsHelper
        payload = result.payload
        assert "content" in payload
        assert "fields" in payload
        fields = payload["fields"]
        # The MEDRE envelope is stored under FIELD_MEDRE_ENVELOPE (0xFD)
        # nested inside {LXMF_NAMESPACE: envelope_dict}
        assert 0xFD in fields
        outer = fields[0xFD]
        assert "medre" in outer
        envelope = outer["medre"]
        assert envelope["source_adapter"] == "matrix-relay"

    @pytest.mark.asyncio
    async def test_various_source_labels(self) -> None:
        """Multiple source adapter labels survive rendering unchanged."""
        labels = [
            "matrix-src",
            "mesh-radio-42",
            "lxmf-node-abcd",
            "meshcore-tcp-localhost",
        ]
        for label in labels:
            event = _make_event(
                source_adapter=label,
                payload={"body": f"from {label}"},
            )
            pipeline = _make_pipeline()
            result = await _render(pipeline, event, "mesh-target", "meshtastic")
            # Meshtastic renderer doesn't embed source_adapter in payload,
            # but the rendering result should still reference the original event
            assert result.event_id == event.event_id


# ---------------------------------------------------------------------------
# Test 4: Reply / thread context
# ---------------------------------------------------------------------------


class TestReplyThreadContext:
    """Verify reply relations are handled in rendered output."""

    @pytest.mark.asyncio
    async def test_matrix_reply_includes_relates_to(self) -> None:
        """Matrix renderer adds m.relates_to when event has a reply relation."""
        native_ref = NativeRef(
            adapter="matrix-target",
            native_channel_id="!room:server",
            native_message_id="$orig-matrix-event",
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id="evt-original-001",
            target_native_ref=native_ref,
            key=None,
            fallback_text="original message text",
            metadata={},
        )
        event = _make_event(
            source_adapter="mesh-src",
            payload={"body": "this is a reply"},
            relations=(relation,),
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        payload = result.payload
        assert "m.relates_to" in payload
        reply_ref = payload["m.relates_to"]["m.in_reply_to"]
        assert reply_ref["event_id"] == "$orig-matrix-event"
        # Body is just the relayed body — no manual fallback quoting
        assert payload["body"] == "this is a reply"
        assert "> <" not in payload["body"]

    @pytest.mark.asyncio
    async def test_matrix_reply_no_native_ref(self) -> None:
        """Matrix reply with no native_ref does not produce m.relates_to.

        Without a target-owned native ref, the Matrix renderer has no
        valid Matrix event ID to use for m.in_reply_to.  The canonical
        target_event_id is never used as a Matrix event ID.
        """
        relation = EventRelation(
            relation_type="reply",
            target_event_id="evt-orig-002",
            target_native_ref=None,
            key=None,
            fallback_text="original",
            metadata={},
        )
        event = _make_event(
            source_adapter="mesh-src",
            payload={"body": "reply body"},
            relations=(relation,),
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        assert "m.relates_to" not in result.payload

    @pytest.mark.asyncio
    async def test_meshtastic_reply_no_special_handling(self) -> None:
        """Meshtastic renderer does not embed reply relations in payload.

        Radio renderers pass text through without relation-aware formatting.
        The relation data remains on the CanonicalEvent for the pipeline to
        use; the renderer itself does not add reply context to the radio
        text payload. Non-native replies use plain text only.
        """
        relation = EventRelation(
            relation_type="reply",
            target_event_id="evt-orig-003",
            target_native_ref=None,
            key=None,
            fallback_text="original text",
            metadata={},
        )
        event = _make_event(
            source_adapter="matrix-src",
            payload={"body": "a reply"},
            relations=(relation,),
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "mesh-target", "meshtastic")

        # Non-native replies use plain text only, no "[replying to: ...]" prefix
        assert result.payload["text"] == "a reply"
        assert "m.relates_to" not in result.payload


# ---------------------------------------------------------------------------
# Test 5: Empty payload handling
# ---------------------------------------------------------------------------


class TestEmptyPayloadHandling:
    """Empty or missing payloads must not crash the renderer."""

    @pytest.mark.asyncio
    async def test_empty_payload_meshtastic(self) -> None:
        """Empty payload produces empty text string, not a crash."""
        event = _make_event(
            source_adapter="matrix-src",
            payload={},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "mesh-target", "meshtastic")

        assert result.payload["text"] == ""
        assert result.payload["channel_index"] == 0

    @pytest.mark.asyncio
    async def test_empty_payload_matrix(self) -> None:
        """Empty payload produces empty body string for Matrix target."""
        event = _make_event(
            source_adapter="mesh-src",
            payload={},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        assert result.payload["body"] == ""
        assert result.payload["msgtype"] == "m.text"

    @pytest.mark.asyncio
    async def test_empty_payload_meshcore(self) -> None:
        """Empty payload produces empty text for MeshCore target."""
        event = _make_event(
            source_adapter="matrix-src",
            payload={},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "meshcore-target", "meshcore")

        assert result.payload["text"] == ""
        assert result.payload["channel_index"] == 0

    @pytest.mark.asyncio
    async def test_empty_payload_lxmf(self) -> None:
        """Empty payload produces empty content for LXMF target."""
        event = _make_event(
            source_adapter="matrix-src",
            payload={},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "lxmf-target", "lxmf")

        assert result.payload["content"] == ""
        assert result.payload["title"] == ""

    @pytest.mark.asyncio
    async def test_no_matching_renderer_raises(self) -> None:
        """Pipeline raises ValueError when no renderer matches platform."""
        event = _make_event(payload={"body": "test"})
        pipeline = _make_pipeline()
        # Register platform that no renderer handles.
        pipeline.register_adapter_platform("unknown-target", "nonexistent")

        with pytest.raises(ValueError, match="No renderer registered"):
            await pipeline.render(event, "unknown-target")


# ---------------------------------------------------------------------------
# Test 6: Escaping safety
# ---------------------------------------------------------------------------


class TestEscapingSafety:
    """Special characters in text must not crash or inject into rendered output."""

    @pytest.mark.asyncio
    async def test_html_chars_meshtastic(self) -> None:
        """HTML characters pass through safely for Meshtastic."""
        text = '<script>alert("xss")</script>'
        event = _make_event(
            source_adapter="matrix-src",
            payload={"body": text},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "mesh-target", "meshtastic")

        # Text is passed through as-is (no HTML escaping needed for radio)
        assert result.payload["text"] == text

    @pytest.mark.asyncio
    async def test_html_chars_matrix(self) -> None:
        """HTML characters pass through for Matrix (body is plain text)."""
        text = '<b>bold</b> & "quotes" &copy;'
        event = _make_event(
            source_adapter="mesh-src",
            payload={"body": text},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        # Matrix body is plain text, not HTML-escaped
        assert result.payload["body"] == text

    @pytest.mark.asyncio
    async def test_control_characters(self) -> None:
        """Control characters in text do not crash rendering."""
        text = "hello\tworld\nnewline\r\nand\ttabs\x00null"
        event = _make_event(
            source_adapter="matrix-src",
            payload={"body": text},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "mesh-target", "meshtastic")

        # Should not crash; text is preserved
        assert isinstance(result.payload["text"], str)
        assert len(result.payload["text"]) > 0

    @pytest.mark.asyncio
    async def test_unicode_multibyte(self) -> None:
        """Multi-byte Unicode characters survive rendering."""
        text = "こんにちは 🌍 Ünïcödé café résumé"
        event = _make_event(
            source_adapter="mesh-src",
            payload={"body": text},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        assert result.payload["body"] == text

    @pytest.mark.asyncio
    async def test_very_long_text_no_truncation_at_renderer(self) -> None:
        """Renderer-level: no truncation in tranche 1 (noted as TODO).

        The TextRenderer truncates at 500 chars, but transport-specific
        renderers (MeshtasticRenderer, etc.) do not enforce truncation.
        This test documents the current behaviour.
        """
        text = "A" * 5000
        event = _make_event(
            source_adapter="matrix-src",
            payload={"body": text},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "mesh-target", "meshtastic")

        # MeshtasticRenderer does NOT truncate in tranche 1
        assert result.payload["text"] == text
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_null_bytes_in_payload(self) -> None:
        """Null bytes in payload value do not crash rendering."""
        text = "before\x00after"
        event = _make_event(
            source_adapter="mesh-src",
            payload={"body": text},
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "meshcore-target", "meshcore")

        assert isinstance(result.payload["text"], str)

    @pytest.mark.asyncio
    async def test_matrix_reply_with_special_chars(self) -> None:
        """Reply body with special chars in fallback_text is safe."""
        relation = EventRelation(
            relation_type="reply",
            target_event_id="evt-orig",
            target_native_ref=None,
            key=None,
            fallback_text="<img src=x onerror=alert(1)>",
            metadata={},
        )
        event = _make_event(
            source_adapter="mesh-src",
            payload={"body": "reply with <special> & chars"},
            relations=(relation,),
        )
        pipeline = _make_pipeline()
        result = await _render(pipeline, event, "matrix-target", "matrix")

        # The reply body includes the fallback text verbatim
        body = result.payload["body"]
        assert isinstance(body, str)
        assert "reply with <special> & chars" in body
