"""Tests for MatrixRenderer: name, can_render dispatch, rendering output,
relation handling, envelope embedding, and long body handling.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMetadata,
    NativeRef,
)
from medre.core.rendering.renderer import RenderingContext, RenderingResult


def _make_event(
    event_id: str = "evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="transport",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "hello"},
        metadata=EventMetadata(),
    )


class TestMatrixRenderer:
    """MatrixRenderer output and dispatch tests."""

    def test_name_is_matrix(self) -> None:
        renderer = MatrixRenderer()
        assert renderer.name == "matrix"

    def test_can_render_matrix_platform(self) -> None:
        """Renderer matches when target_platform is matrix."""
        renderer = MatrixRenderer()
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="chat-instance",
                    delivery_strategy="direct",
                    target_platform="matrix",
                ),
            )
            is True
        )

    def test_can_render_non_matrix(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="fake_presentation",
                    delivery_strategy="direct",
                    target_platform="fake",
                ),
            )
            is False
        )

    def test_can_render_without_platform_returns_false(self) -> None:
        """Without platform info, renderer cannot match (no prefix fallback)."""
        renderer = MatrixRenderer()
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="matrix_instance", delivery_strategy="direct"
                ),
            )
            is False
        )

    async def test_render_simple_message(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "hello matrix"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert isinstance(result, RenderingResult)
        assert result.payload["msgtype"] == "m.text"
        assert result.payload["body"] == "hello matrix"

    async def test_render_includes_msgtype(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert result.payload["msgtype"] == "m.text"

    async def test_render_includes_body(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert result.payload["body"] == "specific body"

    async def test_render_with_reply_relation(self) -> None:
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text="original text",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "m.relates_to" in result.payload
        relates = result.payload["m.relates_to"]
        assert "m.in_reply_to" in relates
        assert relates["m.in_reply_to"]["event_id"] == "$orig-native"
        # Body is just the relayed body, no manual fallback quoting
        assert result.payload["body"] == "my reply"
        assert "> <" not in result.payload["body"]

    async def test_render_with_reaction_relation(self) -> None:
        """Reaction relations render as native Matrix m.reaction payloads."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key="👍",
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["_matrix_event_type"] == "m.reaction"
        assert result.payload["m.relates_to"] == {
            "rel_type": "m.annotation",
            "event_id": "$orig-native",
            "key": "👍",
        }
        assert "msgtype" not in result.payload
        assert "body" not in result.payload

    async def test_render_with_envelope(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert "medre" in result.payload
        assert "envelope" in result.payload["medre"]

    async def test_render_truncates_very_long_body(self) -> None:
        renderer = MatrixRenderer()
        long_body = "x" * 200_000
        event = _make_event(payload={"body": long_body})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        # Renderer passes body through without truncation
        assert result.payload["body"] == long_body

    async def test_render_returns_rendering_result(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert isinstance(result, RenderingResult)
        assert result.event_id == "evt-1"
        assert result.target_adapter == "matrix_instance"


class TestMatrixRendererForeignRefs:
    """MatrixRenderer must not use native refs from other adapters."""

    async def test_foreign_native_ref_not_used_for_reply(self) -> None:
        """Meshtastic native ref must not produce m.in_reply_to when rendering to Matrix."""
        renderer = MatrixRenderer()
        foreign_ref = NativeRef(
            adapter="mesh-1", native_channel_id="0", native_message_id="123"
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=foreign_ref,
            key=None,
            fallback_text="original",
        )
        event = _make_event(payload={"body": "hello"}, relations=(rel,))
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert "m.relates_to" not in result.payload

    async def test_foreign_native_ref_not_used_for_reaction(self) -> None:
        """Meshtastic native ref must not produce true m.reaction."""
        renderer = MatrixRenderer()
        foreign_ref = NativeRef(
            adapter="mesh-1", native_channel_id="0", native_message_id="123"
        )
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=foreign_ref,
            key="👍",
            fallback_text=None,
        )
        event = _make_event(payload={"body": "👍"}, relations=(rel,))
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert "_matrix_event_type" not in result.payload
        assert result.payload.get("msgtype") == "m.emote"

    async def test_mmrelay_reply_id_preserved_in_fallback(self) -> None:
        """MMRelay meshtastic_replyId from relation metadata preserves KEY_REPLY_ID in fallback."""
        renderer = MatrixRenderer()
        foreign_ref = NativeRef(
            adapter="mesh-1", native_channel_id="0", native_message_id="99"
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=foreign_ref,
            key=None,
            fallback_text="orig",
            metadata={"meshtastic_reply_id": "42"},
        )
        event = _make_event(payload={"body": "hello"}, relations=(rel,))
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert "meshtastic_replyId" in result.payload
        assert result.payload["meshtastic_replyId"] == "42"


class TestMatrixRendererReplySender:
    """Matrix-native reply body must NOT contain manual fallback quoting.

    Matrix handles reply display via m.relates_to.m.in_reply_to natively,
    so the body is just the relayed message text (with any relay prefix).
    """

    async def test_reply_body_equals_relay_body(self) -> None:
        """Matrix-native reply body equals the relayed body exactly."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text="original text",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["body"] == "my reply"

    async def test_reply_body_no_fallback_quote(self) -> None:
        """No Matrix reply body contains '> <' fallback quoting."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text="original text",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "> <" not in body

    async def test_reply_body_with_relay_prefix(self) -> None:
        """Reply body includes relay prefix when configured via source_configs."""
        renderer = MatrixRenderer(
            source_configs={
                "transport": _StubMeshtasticConfig(
                    adapter_id="transport",
                    matrix_relay_prefix="[{longname}] ",
                ),
            },
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text="original text",
        )
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="transport",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(relation,),
            payload={"body": "my reply"},
            metadata=EventMetadata(
                native=NativeMetadata(data={"longname": "TadChilly"})
            ),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert body == "[TadChilly] my reply"
        assert "> <" not in body

    async def test_reply_has_relates_to_with_event_id(self) -> None:
        """Matrix-native reply payload has m.relates_to.m.in_reply_to.event_id."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text="original text",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert (
            result.payload["m.relates_to"]["m.in_reply_to"]["event_id"]
            == "$orig-native"
        )

    async def test_reply_with_mmrelay_id_includes_key_reply_id(self) -> None:
        """Meshtastic->Matrix reply payload still includes KEY_REPLY_ID."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text="original text",
            metadata={"meshtastic_reply_id": "42"},
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "meshtastic_replyId" in result.payload
        assert result.payload["meshtastic_replyId"] == "42"

    async def test_no_fallback_quote_any_sender_style(self) -> None:
        """No Matrix reply body contains '> <matrix>', '> <matrix-1>', or
        '> <Tad Chilly>' style fallback."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text="original text",
            metadata={"displayname": "Tad Chilly"},
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "> <matrix>" not in body
        assert "> <matrix-1>" not in body
        assert "> <Tad Chilly>" not in body


# ---------------------------------------------------------------------------
# Multi-radio source-adapter config resolution tests
# ---------------------------------------------------------------------------


class _StubMeshtasticConfig:
    """Minimal duck-typed MeshtasticConfig for source-config resolution tests."""

    def __init__(
        self,
        adapter_id: str = "radio-alpha",
        meshnet_name: str = "",
        matrix_relay_prefix: str = "",
        mmrelay_compatibility: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.meshnet_name = meshnet_name
        self.matrix_relay_prefix = matrix_relay_prefix
        self.mmrelay_compatibility = mmrelay_compatibility


def _make_meshtastic_event(
    source_adapter: str = "radio-alpha",
    payload: dict | None = None,
    relations: tuple | None = None,
    native_data: dict | None = None,
) -> CanonicalEvent:
    """Build a CanonicalEvent simulating a Meshtastic source."""
    metadata = EventMetadata()
    if native_data:
        metadata = EventMetadata(native=NativeMetadata(data=native_data))
    return CanonicalEvent(
        event_id="evt-mesh-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-42",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "hello mesh"},
        metadata=metadata,
    )


class TestMultiRadioSourceConfig:
    """MatrixRenderer resolves per-source-adapter config for multi-radio setups."""

    @staticmethod
    def _source_configs() -> dict[str, _StubMeshtasticConfig]:
        alpha = _StubMeshtasticConfig(
            adapter_id="radio-alpha",
            meshnet_name="AlphaNet",
            matrix_relay_prefix="[{longname}/AlphaNet]: ",
            mmrelay_compatibility=True,
        )
        bravo = _StubMeshtasticConfig(
            adapter_id="radio-bravo",
            meshnet_name="BravoNet",
            matrix_relay_prefix="[{longname}/BravoNet]: ",
            mmrelay_compatibility=False,
        )
        return {"radio-alpha": alpha, "radio-bravo": bravo}

    async def test_alpha_source_uses_alpha_prefix(self) -> None:
        """Event from radio-alpha uses alpha's matrix_relay_prefix."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["body"] == "[Alice/AlphaNet]: hello mesh"

    async def test_bravo_source_uses_bravo_prefix(self) -> None:
        """Event from radio-bravo uses bravo's matrix_relay_prefix."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-bravo",
            native_data={"longname": "Bob"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["body"] == "[Bob/BravoNet]: hello mesh"

    async def test_unknown_source_renders_plain_output(self) -> None:
        """Event from unknown source renders plain Matrix output (no prefix/metadata)."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-charlie",
            native_data={"longname": "Charlie"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Unknown source → no prefix, plain body
        assert result.payload["body"] == "hello mesh"
        # No mmrelay metadata (no source config match)
        assert "meshtastic_id" not in result.payload

    async def test_alpha_mmrelay_compat_enabled(self) -> None:
        """Event from radio-alpha (mmrelay_compat=True) gets mesh metadata."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice", "shortname": "A", "packet_id": "99"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # mmrelay_compat=True → mesh provenance keys injected
        assert "meshtastic_id" in result.payload
        assert result.payload["meshtastic_id"] == "99"

    async def test_bravo_mmrelay_compat_disabled(self) -> None:
        """Event from radio-bravo (mmrelay_compat=False) omits mesh metadata."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-bravo",
            native_data={"longname": "Bob", "shortname": "B", "packet_id": "88"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # mmrelay_compat=False → no mesh provenance keys
        assert "meshtastic_id" not in result.payload

    async def test_reaction_prefix_resolves_per_source(self) -> None:
        """Reaction emote prefix resolves per source adapter config."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={
                "longname": "Alice",
                "shortname": "A",
                "packet_id": "77",
            },
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        # mmrelay_compat default is True for alpha → emote fallback
        assert "Alice/AlphaNet" in body
        assert result.payload["meshtastic_meshnet"] == "AlphaNet"

    async def test_reaction_meshnet_per_source(self) -> None:
        """Reaction KEY_MESHNET resolves from source adapter config."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_meshtastic_event(
            source_adapter="radio-bravo",
            native_data={
                "longname": "Bob",
                "shortname": "B",
                "packet_id": "55",
            },
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Bravo has mmrelay_compat=False → reaction fallback emote
        # but KEY_MESHNET should still be BravoNet from source config
        assert result.payload["meshtastic_meshnet"] == "BravoNet"

    async def test_non_meshtastic_source_ignores_meshtastic_configs(self) -> None:
        """Non-Meshtastic source with Meshtastic source_configs renders plain output."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_event(
            event_id="evt-plain-1",
            payload={"body": "Hello from elsewhere"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Plain output — no relay prefix from any Meshtastic config
        assert result.payload["body"] == "Hello from elsewhere"
        # No Meshtastic metadata keys
        assert "meshtastic_id" not in result.payload
        assert "meshtastic_meshnet" not in result.payload
        assert "meshtastic_longname" not in result.payload
        # Standard Matrix content
        assert result.payload["msgtype"] == "m.text"


# ---------------------------------------------------------------------------
# Runtime assembly tests (source_configs only)
# ---------------------------------------------------------------------------


class TestRuntimeAssemblySourceConfig:
    """MatrixRenderer behavior under runtime assembly configuration.

    Runtime assembly passes ``source_configs`` only.  Unknown / non-Meshtastic
    sources render plain Matrix output without Meshtastic prefix or metadata
    contamination.
    """

    @staticmethod
    def _source_configs() -> dict[str, _StubMeshtasticConfig]:
        alpha = _StubMeshtasticConfig(
            adapter_id="radio-alpha",
            meshnet_name="AlphaNet",
            matrix_relay_prefix="[{longname}/AlphaNet]: ",
            mmrelay_compatibility=True,
        )
        bravo = _StubMeshtasticConfig(
            adapter_id="radio-bravo",
            meshnet_name="BravoNet",
            matrix_relay_prefix="[{longname}/BravoNet]: ",
            mmrelay_compatibility=False,
        )
        return {"radio-alpha": alpha, "radio-bravo": bravo}

    async def test_runtime_source_a_renders_with_alpha_metadata(self) -> None:
        """Runtime assembly: event from radio-alpha uses alpha's prefix and metadata."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice", "shortname": "A", "packet_id": "42"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["body"] == "[Alice/AlphaNet]: hello mesh"
        # alpha has mmrelay_compat=True → mesh metadata injected
        assert "meshtastic_id" in result.payload
        assert result.payload["meshtastic_id"] == "42"
        assert result.payload["meshtastic_meshnet"] == "AlphaNet"

    async def test_runtime_source_b_renders_with_bravo_metadata(self) -> None:
        """Runtime assembly: event from radio-bravo uses bravo's prefix, no mmrelay metadata."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-bravo",
            native_data={"longname": "Bob", "shortname": "B", "packet_id": "88"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["body"] == "[Bob/BravoNet]: hello mesh"
        # bravo has mmrelay_compat=False → no mesh metadata
        assert "meshtastic_id" not in result.payload
        assert "meshtastic_meshnet" not in result.payload

    async def test_non_meshtastic_source_renders_plain_output(self) -> None:
        """Non-Meshtastic source renders plain Matrix output with no Meshtastic metadata."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_event(
            event_id="evt-matrix-1",
            payload={"body": "Hello from Matrix"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Plain output — no relay prefix
        assert result.payload["body"] == "Hello from Matrix"
        # No Meshtastic metadata keys
        assert "meshtastic_id" not in result.payload
        assert "meshtastic_meshnet" not in result.payload
        assert "meshtastic_longname" not in result.payload
        assert "meshtastic_shortname" not in result.payload
        assert "meshtastic_portnum" not in result.payload
        # Standard Matrix content
        assert result.payload["msgtype"] == "m.text"

    async def test_unknown_meshtastic_source_renders_plain_output(self) -> None:
        """Unknown Meshtastic source (not in source_configs) renders plain output."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-charlie",
            native_data={"longname": "Charlie", "packet_id": "77"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # No prefix (source_configs has no matching entry)
        assert result.payload["body"] == "hello mesh"
        # No mmrelay metadata (no matching source_config)
        assert "meshtastic_id" not in result.payload
        assert "meshtastic_meshnet" not in result.payload


# ---------------------------------------------------------------------------
# Fallback-text strategy tests
# ---------------------------------------------------------------------------


class TestMatrixFallbackText:
    """MatrixRenderer fallback_text strategy: degraded relation text rendering.

    Tests that truncation applies to the final body (including relay prefix),
    original_length metadata is correct, and envelope/mmrelay metadata remain
    intact.
    """

    @staticmethod
    def _make_fallback_event(
        body: str = "hello",
        *,
        source_adapter: str = "transport",
        native_data: dict | None = None,
        fallback_text: str = "original message",
    ) -> CanonicalEvent:
        """Build an event with a reply relation for fallback_text strategy."""
        metadata = EventMetadata()
        if native_data:
            metadata = EventMetadata(native=NativeMetadata(data=native_data))
        rel = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="mesh-1",
                native_channel_id="ch-0",
                native_message_id="mesh-42",
            ),
            key=None,
            fallback_text=fallback_text,
        )
        return CanonicalEvent(
            event_id="evt-fb-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=source_adapter,
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": body},
            metadata=metadata,
        )

    async def test_fallback_text_basic_body(self) -> None:
        """Fallback-text strategy renders degraded text without m.relates_to."""
        renderer = MatrixRenderer()
        event = self._make_fallback_event(body="my reply")
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
            ),
        )
        assert result.payload["msgtype"] == "m.text"
        assert "m.relates_to" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"
        # Body should contain degraded relation info and original text
        assert "my reply" in result.payload["body"]

    async def test_fallback_text_envelope_present(self) -> None:
        """Fallback-text strategy still embeds the MEDRE metadata envelope."""
        renderer = MatrixRenderer()
        event = self._make_fallback_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
            ),
        )
        assert "medre" in result.payload
        assert "envelope" in result.payload["medre"]

    async def test_fallback_text_truncation_respects_max_text_chars(self) -> None:
        """Truncation applies to the final body including relay prefix."""
        renderer = MatrixRenderer(
            source_configs={
                "transport": _StubMeshtasticConfig(
                    adapter_id="transport",
                    matrix_relay_prefix="[Alice]: ",
                ),
            },
        )
        event = self._make_fallback_event(
            body="hello world",
            source_adapter="transport",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
                max_text_chars=10,
            ),
        )
        body: str = result.payload["body"]
        # The final body must respect max_text_chars=10 including prefix
        assert len(body) <= 10
        # Truncation occurred
        assert result.truncated is True

    async def test_fallback_text_truncation_original_length_includes_prefix(
        self,
    ) -> None:
        """original_length metadata reflects the full pre-truncate body (prefix + text)."""
        prefix = "[Alice]: "
        renderer = MatrixRenderer(
            source_configs={
                "transport": _StubMeshtasticConfig(
                    adapter_id="transport",
                    matrix_relay_prefix=prefix,
                ),
            },
        )
        event = self._make_fallback_event(
            body="hello world",
            source_adapter="transport",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
                max_text_chars=5,
            ),
        )
        # original_length should be the full prefixed body length
        # (before truncation, after prefix application)
        assert "original_length" in result.metadata
        # degraded_text from TextRenderer._extract_text includes reply prefix
        # so we check that original_length >= len(prefix + "hello world")
        assert result.metadata["original_length"] >= len(prefix + "hello world")

    async def test_fallback_text_no_truncation_when_within_budget(self) -> None:
        """No truncation when the prefixed body fits within max_text_chars."""
        renderer = MatrixRenderer(
            source_configs={
                "transport": _StubMeshtasticConfig(
                    adapter_id="transport",
                    matrix_relay_prefix="[Al]: ",
                ),
            },
        )
        event = self._make_fallback_event(
            body="hi",
            source_adapter="transport",
            native_data={"longname": "Al"},
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
                max_text_chars=500,
            ),
        )
        assert result.truncated is False
        assert "original_length" not in result.metadata

    async def test_fallback_text_mmrelay_metadata_preserved(self) -> None:
        """mmrelay metadata injection is preserved under fallback_text strategy."""
        renderer = MatrixRenderer(
            source_configs={
                "transport": _StubMeshtasticConfig(
                    adapter_id="transport",
                    mmrelay_compatibility=True,
                ),
            },
        )
        event = self._make_fallback_event(
            source_adapter="transport",
            native_data={
                "longname": "Alice",
                "shortname": "A",
                "packet_id": "99",
            },
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
            ),
        )
        # mmrelay metadata keys should be present
        assert "meshtastic_id" in result.payload
        assert result.payload["meshtastic_id"] == "99"
        assert result.payload["meshtastic_longname"] == "Alice"
        assert result.payload["meshtastic_shortname"] == "A"

    async def test_fallback_without_relations_uses_fallback_path(self) -> None:
        """Fallback_text strategy without relations still uses _render_fallback_text."""
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "plain message"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
            ),
        )
        assert result.fallback_applied == "strategy_fallback_text"
        assert result.payload["msgtype"] == "m.text"
        assert "m.relates_to" not in result.payload

    async def test_fallback_byte_truncation_applies(self) -> None:
        """Byte budget truncation applies to fallback body and reports metadata."""
        renderer = MatrixRenderer()
        # Create event with multi-byte content
        body = "hello" + "é" * 100  # each é is 2 UTF-8 bytes
        event = self._make_fallback_event(body=body)
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
                max_text_bytes=20,
            ),
        )
        rendered_body: str = result.payload["body"]
        assert len(rendered_body.encode("utf-8")) <= 20
        assert result.truncated is True
        assert "original_text_bytes" in result.metadata
        assert "rendered_text_bytes" in result.metadata

    async def test_fallback_byte_truncation_no_truncate_when_within_budget(
        self,
    ) -> None:
        """No byte truncation when body fits within max_text_bytes."""
        renderer = MatrixRenderer()
        event = self._make_fallback_event(body="short")
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
                max_text_bytes=1000,
            ),
        )
        assert result.truncated is False


class TestMatrixReactionEmojiPrecedence:
    """Reaction emoji extraction follows precedence: rel.key, payload['key'],
    payload['emoji'], payload['body'], fallback."""

    async def test_emoji_from_rel_key(self) -> None:
        """rel.key takes highest precedence."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key="❤️",
            fallback_text=None,
        )
        event = _make_event(
            payload={"key": "👍", "emoji": "🎉", "body": "plain"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["m.relates_to"]["key"] == "❤️"

    async def test_emoji_from_payload_key(self) -> None:
        """payload['key'] is used when rel.key is None."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"key": "👍", "emoji": "🎉", "body": "plain"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["m.relates_to"]["key"] == "👍"

    async def test_emoji_from_payload_emoji(self) -> None:
        """payload['emoji'] is used when rel.key and payload['key'] are absent."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"emoji": "🎉", "body": "plain"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["m.relates_to"]["key"] == "🎉"

    async def test_emoji_from_payload_body(self) -> None:
        """payload['body'] is used when all higher-precedence sources are absent."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["m.relates_to"]["key"] == "👍"

    async def test_emoji_fallback_when_all_blank(self) -> None:
        """Falls back to ⚠️ when all sources are blank."""
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": " "},  # whitespace-only body → stripped to blank
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["m.relates_to"]["key"] == "\u26a0\ufe0f"


class TestFallbackAppliedTyping:
    """fallback_applied uses FallbackApplied literals."""

    async def test_fallback_applied_is_strategy_literal(self) -> None:
        """fallback_applied for strategy_fallback_text is the correct literal."""
        from medre.core.rendering.renderer import FallbackApplied

        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="fallback_text",
            ),
        )
        assert result.fallback_applied == "strategy_fallback_text"
        # Type check: the value is one of the FallbackApplied literals
        assert result.fallback_applied in FallbackApplied.__args__

    async def test_direct_strategy_has_no_fallback(self) -> None:
        """Direct strategy yields fallback_applied=None."""
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix-1",
                delivery_strategy="direct",
            ),
        )
        assert result.fallback_applied is None


# ---------------------------------------------------------------------------
# Native-relations-closure: missing-target fallback/suppression tests
# ---------------------------------------------------------------------------


class TestMatrixMissingTargetFallback:
    """When a relation has no resolvable Matrix-native target, the renderer
    must produce an explicit fallback or suppression — never a malformed
    m.relates_to with missing/empty event_id.

    This class covers the gap between "has a valid Matrix native ref" and
    "has a foreign native ref" — specifically the case where
    target_native_ref is None (no native ref at all).
    """

    async def test_reply_no_native_ref_no_relates_to(self) -> None:
        """Reply with target_native_ref=None produces no m.relates_to.

        Without a Matrix-native target, the renderer cannot emit
        m.in_reply_to.  The body is just the relay text — no fallback
        quoting, no malformed m.relates_to with empty event_id.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=None,
            key=None,
            fallback_text="original message text",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # No m.relates_to — cannot target Matrix-native reply without ref
        assert "m.relates_to" not in result.payload
        # Body must be clean relay text, no quoting
        assert result.payload["body"] == "my reply"
        assert "> <" not in result.payload["body"]

    async def test_reaction_no_native_ref_emote_fallback(self) -> None:
        """Reaction with target_native_ref=None produces m.emote fallback.

        Without a Matrix-native target, a true m.reaction (m.annotation)
        cannot be emitted.  The renderer falls back to an m.emote with
        MMRelay-compatible metadata — never a broken m.annotation.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Must NOT produce a true Matrix reaction
        assert "_matrix_event_type" not in result.payload
        assert "m.relates_to" not in result.payload
        # Must produce emote fallback
        assert result.payload["msgtype"] == "m.emote"
        assert "👍" in result.payload["body"]

    async def test_reply_with_resolved_native_ref_correct_structure(self) -> None:
        """Reply with resolved Matrix native ref produces correct m.in_reply_to.

        The m.relates_to structure must be exactly:
        {"m.in_reply_to": {"event_id": "<matrix_event_id>"}}
        using the native_message_id from the target_native_ref, not
        the canonical target_event_id.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="canonical-orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$matrix-evt-abc123",
            ),
            key=None,
            fallback_text="original text",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        relates = result.payload["m.relates_to"]
        # Must use native_message_id, NOT canonical target_event_id
        assert relates == {"m.in_reply_to": {"event_id": "$matrix-evt-abc123"}}
        assert "canonical-orig-001" not in str(relates)

    async def test_reaction_with_resolved_native_ref_correct_structure(self) -> None:
        """Reaction with resolved Matrix native ref produces correct m.annotation.

        The m.relates_to structure must be exactly:
        {"rel_type": "m.annotation", "event_id": "<matrix_event_id>", "key": "<emoji>"}
        using the native_message_id from the target_native_ref.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="canonical-orig-002",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$matrix-evt-def456",
            ),
            key="❤️",
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "❤️"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Must be a true Matrix reaction
        assert result.payload["_matrix_event_type"] == "m.reaction"
        assert result.payload["m.relates_to"] == {
            "rel_type": "m.annotation",
            "event_id": "$matrix-evt-def456",
            "key": "❤️",
        }
        # No body/msgtype on true reactions
        assert "msgtype" not in result.payload
        assert "body" not in result.payload
        # Must NOT use canonical target_event_id
        assert "canonical-orig-002" not in str(result.payload)

    async def test_reaction_no_target_no_broken_annotation(self) -> None:
        """Reaction without any target produces emote fallback, not m.annotation.

        Ensures the renderer never emits a malformed m.annotation with
        empty/missing event_id when no Matrix-native target is available.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="🔥",
            fallback_text="some original text",
        )
        event = _make_event(
            payload={"body": "🔥"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        payload = result.payload
        # Absolutely no m.annotation or _matrix_event_type
        assert payload.get("_matrix_event_type") != "m.reaction"
        if "m.relates_to" in payload:
            rel = payload["m.relates_to"]
            # If present, must NOT be m.annotation
            assert rel.get("rel_type") != "m.annotation"
        # Must be emote fallback
        assert payload["msgtype"] == "m.emote"

    async def test_reply_wrong_adapter_no_relates_to(self) -> None:
        """Reply targeting a different adapter produces no m.in_reply_to.

        A Meshtastic native ref must not produce a Matrix relation.
        The renderer suppresses the relation cleanly.
        """
        renderer = MatrixRenderer()
        relation = EventRelation(
            relation_type="reply",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="meshtastic-1",
                native_channel_id="0",
                native_message_id="12345",
            ),
            key=None,
            fallback_text="original",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "m.relates_to" not in result.payload
        # Body is clean relay text
        assert result.payload["body"] == "my reply"


# ---------------------------------------------------------------------------
# Core attribution integration tests
# ---------------------------------------------------------------------------


def _make_meshcore_event(
    source_adapter: str = "meshcore-1",
    payload: dict | None = None,
    native_data: dict | None = None,
) -> CanonicalEvent:
    """Build a CanonicalEvent simulating a MeshCore source."""
    metadata = EventMetadata()
    if native_data:
        metadata = EventMetadata(native=NativeMetadata(data=native_data))
    return CanonicalEvent(
        event_id="evt-mc-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="mc-node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello meshcore"},
        metadata=metadata,
    )


def _make_lxmf_event(
    source_adapter: str = "lxmf-1",
    payload: dict | None = None,
    native_data: dict | None = None,
) -> CanonicalEvent:
    """Build a CanonicalEvent simulating an LXMF source."""
    metadata = EventMetadata()
    if native_data:
        metadata = EventMetadata(native=NativeMetadata(data=native_data))
    return CanonicalEvent(
        event_id="evt-lxmf-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="lxmf-node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello lxmf"},
        metadata=metadata,
    )


class _StubMeshCoreConfig:
    """Minimal duck-typed config for MeshCore source."""

    def __init__(
        self,
        adapter_id: str = "meshcore-1",
        meshnet_name: str = "",
        matrix_relay_prefix: str = "",
        mmrelay_compatibility: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.meshnet_name = meshnet_name
        self.matrix_relay_prefix = matrix_relay_prefix
        self.mmrelay_compatibility = mmrelay_compatibility


class _StubLXMFConfig:
    """Minimal duck-typed config for LXMF source."""

    def __init__(
        self,
        adapter_id: str = "lxmf-1",
        meshnet_name: str = "",
        matrix_relay_prefix: str = "",
        mmrelay_compatibility: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.meshnet_name = meshnet_name
        self.matrix_relay_prefix = matrix_relay_prefix
        self.mmrelay_compatibility = mmrelay_compatibility


class TestMatrixCoreAttributionIntegration:
    """MatrixRenderer uses core attribution helpers for all relay prefix
    formatting.  Covers missing-variable safety, MeshCore/LXMF prefix
    support, unknown placeholder policy, and reaction prefix integration.
    """

    # -- Missing variables: no 'None' in output --

    async def test_missing_longname_no_none(self) -> None:
        """Missing longname renders as empty, never the literal 'None'."""
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    matrix_relay_prefix="[{longname}]: ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"shortname": "A"},  # no longname
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body: str = result.payload["body"]
        assert "None" not in body
        # Prefix with empty longname → "[]: hello mesh"
        assert body == "[]: hello mesh"

    async def test_missing_shortname_no_none(self) -> None:
        """Missing shortname renders as empty, never 'None'."""
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    matrix_relay_prefix="[{shortname}]: ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},  # no shortname
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "None" not in body
        assert body == "[]: hello mesh"

    async def test_missing_all_vars_no_none(self) -> None:
        """All prefix variables missing: no 'None' anywhere in body."""
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    matrix_relay_prefix="<{longname}/{shortname}/{from_id}> ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={},  # nothing
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "None" not in body
        assert body == "<//> hello mesh"

    # -- MeshCore prefix: uses pubkey/from_id/source_sender_id --

    async def test_meshcore_prefix_uses_pubkey(self) -> None:
        """MeshCore event uses pubkey_prefix as source_sender_id in prefix."""
        renderer = MatrixRenderer(
            source_configs={
                "meshcore-1": _StubMeshCoreConfig(
                    adapter_id="meshcore-1",
                    matrix_relay_prefix="[MC:{from_id}] ",
                ),
            },
        )
        event = _make_meshcore_event(
            native_data={"pubkey_prefix": "a1b2c3"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "[MC:a1b2c3]" in body
        assert "None" not in body

    async def test_meshcore_prefix_source_sender_id_alias(self) -> None:
        """MeshCore event can use source_sender_id canonical name."""
        renderer = MatrixRenderer(
            source_configs={
                "meshcore-1": _StubMeshCoreConfig(
                    adapter_id="meshcore-1",
                    matrix_relay_prefix="[MC:{source_sender_id}] ",
                ),
            },
        )
        event = _make_meshcore_event(
            native_data={"pubkey_prefix": "deadbeef"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "[MC:deadbeef]" in body

    # -- LXMF prefix: falls back to safe sender id --

    async def test_lxmf_prefix_uses_source_hash(self) -> None:
        """LXMF event uses source_hash as from_id in prefix."""
        renderer = MatrixRenderer(
            source_configs={
                "lxmf-1": _StubLXMFConfig(
                    adapter_id="lxmf-1",
                    matrix_relay_prefix="[LXMF:{from_id}] ",
                ),
            },
        )
        event = _make_lxmf_event(
            native_data={"source_hash": "feedface"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "[LXMF:feedface]" in body
        assert "None" not in body

    async def test_lxmf_prefix_no_sender_safe_empty(self) -> None:
        """LXMF event without source_hash renders empty from_id, not None."""
        renderer = MatrixRenderer(
            source_configs={
                "lxmf-1": _StubLXMFConfig(
                    adapter_id="lxmf-1",
                    matrix_relay_prefix="[LXMF:{from_id}] ",
                ),
            },
        )
        event = _make_lxmf_event(native_data={})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "None" not in body
        assert body == "[LXMF:] hello lxmf"

    # -- Unknown placeholder policy: left unchanged --

    async def test_unknown_placeholder_left_unchanged(self) -> None:
        """Unknown template variable {bogus} is left as {bogus} in output."""
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    matrix_relay_prefix="[{bogus}] ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        # Unknown variable left unchanged in prefix
        assert "{bogus}" in body
        assert body == "[{bogus}] hello mesh"
        # Metadata records the unknown variable
        assert "prefix_formatter" in result.metadata
        pf_meta = result.metadata["prefix_formatter"]
        assert "bogus" in pf_meta["unknown_variables"]
        assert pf_meta["formatting_error"] is not None

    async def test_unknown_placeholder_mixed_with_known(self) -> None:
        """Mixed known + unknown variables: known resolved, unknown left."""
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    matrix_relay_prefix="[{longname}/{weird}] ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "[Alice/{weird}]" in body

    # -- Reaction prefix still uses core formatter --

    async def test_reaction_prefix_missing_vars_no_none(self) -> None:
        """Reaction prefix with missing variables never renders 'None'."""
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    matrix_relay_prefix="[{longname}] ",
                    mmrelay_compatibility=True,
                ),
            },
        )
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="orig-001",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"shortname": "A"},  # no longname
            payload={"body": "👍"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = result.payload["body"]
        assert "None" not in body
        # Empty longname → "[]" prefix in reaction emote
        assert "[]" in body

    # -- Meshtastic prefix unchanged (regression guard) --

    async def test_meshtastic_prefix_unchanged(self) -> None:
        """Meshtastic → Matrix prefix output is unchanged after wiring."""
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    meshnet_name="AlphaNet",
                    matrix_relay_prefix="[{longname}/AlphaNet]: ",
                    mmrelay_compatibility=True,
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice", "shortname": "A", "packet_id": "42"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["body"] == "[Alice/AlphaNet]: hello mesh"

    # -- Metadata recorded in rendered result --

    async def test_prefix_formatter_metadata_recorded(self) -> None:
        """PrefixFormatterResult metadata is recorded in RenderingResult.metadata."""
        renderer = MatrixRenderer(
            source_configs={
                "radio-alpha": _StubMeshtasticConfig(
                    adapter_id="radio-alpha",
                    matrix_relay_prefix="[{longname}] ",
                ),
            },
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "prefix_formatter" in result.metadata
        pf = result.metadata["prefix_formatter"]
        assert pf["template_used"] == "[{longname}] "
        assert "longname" in pf["variables_used"]
        assert pf["formatting_error"] is None
        assert pf["unknown_variables"] == ()

    async def test_no_prefix_metadata_when_no_template(self) -> None:
        """No prefix_formatter metadata when no relay prefix template is configured."""
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "hello"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "prefix_formatter" not in result.metadata
