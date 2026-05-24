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
from medre.core.rendering.renderer import RenderingResult


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
            renderer.can_render(event, "chat-instance", target_platform="matrix")
            is True
        )

    def test_can_render_non_matrix(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        assert (
            renderer.can_render(event, "fake_presentation", target_platform="fake")
            is False
        )

    def test_can_render_without_platform_returns_false(self) -> None:
        """Without platform info, renderer cannot match (no prefix fallback)."""
        renderer = MatrixRenderer()
        event = _make_event()
        assert renderer.can_render(event, "matrix_instance") is False

    async def test_render_simple_message(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "hello matrix"})
        result = await renderer.render(event, "matrix_instance")
        assert isinstance(result, RenderingResult)
        assert result.payload["msgtype"] == "m.text"
        assert result.payload["body"] == "hello matrix"

    async def test_render_includes_msgtype(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(event, "matrix_instance")
        assert result.payload["msgtype"] == "m.text"

    async def test_render_includes_body(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(event, "matrix_instance")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix_instance")
        assert "medre" in result.payload
        assert "envelope" in result.payload["medre"]

    async def test_render_truncates_very_long_body(self) -> None:
        renderer = MatrixRenderer()
        long_body = "x" * 200_000
        event = _make_event(payload={"body": long_body})
        result = await renderer.render(event, "matrix_instance")
        # Renderer passes body through without truncation
        assert result.payload["body"] == long_body

    async def test_render_returns_rendering_result(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(event, "matrix_instance")
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
        result = await renderer.render(event, "matrix_instance")
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
        result = await renderer.render(event, "matrix_instance")
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
        result = await renderer.render(event, "matrix_instance")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
        body = result.payload["body"]
        assert "> <" not in body

    async def test_reply_body_with_relay_prefix(self) -> None:
        """Reply body includes relay prefix when configured."""
        renderer = MatrixRenderer(
            matrix_relay_prefix="[{longname}] ",
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
        assert result.payload["body"] == "[Alice/AlphaNet]: hello mesh"

    async def test_bravo_source_uses_bravo_prefix(self) -> None:
        """Event from radio-bravo uses bravo's matrix_relay_prefix."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-bravo",
            native_data={"longname": "Bob"},
        )
        result = await renderer.render(event, "matrix-1")
        assert result.payload["body"] == "[Bob/BravoNet]: hello mesh"

    async def test_unknown_source_falls_back_to_defaults(self) -> None:
        """Event from unknown source uses constructor scalar defaults."""
        renderer = MatrixRenderer(
            meshnet_name="DefaultNet",
            matrix_relay_prefix="[{longname}/DefaultNet]: ",
            source_configs=self._source_configs(),
        )
        event = _make_meshtastic_event(
            source_adapter="radio-charlie",
            native_data={"longname": "Charlie"},
        )
        result = await renderer.render(event, "matrix-1")
        assert result.payload["body"] == "[Charlie/DefaultNet]: hello mesh"

    async def test_alpha_mmrelay_compat_enabled(self) -> None:
        """Event from radio-alpha (mmrelay_compat=True) gets mesh metadata."""
        renderer = MatrixRenderer(source_configs=self._source_configs())
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice", "shortname": "A", "packet_id": "99"},
        )
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
        # Bravo has mmrelay_compat=False → reaction fallback emote
        # but KEY_MESHNET should still be BravoNet from source config
        assert result.payload["meshtastic_meshnet"] == "BravoNet"

    async def test_direct_constructor_scalar_defaults(self) -> None:
        """Direct constructor scalar defaults (no source_configs) still works."""
        renderer = MatrixRenderer(
            mmrelay_compat=True,
            meshnet_name="LegacyNet",
            matrix_relay_prefix="[{longname}/LegacyNet]: ",
        )
        event = _make_meshtastic_event(
            source_adapter="anything",
            native_data={"longname": "User"},
        )
        result = await renderer.render(event, "matrix-1")
        assert result.payload["body"] == "[User/LegacyNet]: hello mesh"

    async def test_no_source_configs_event_uses_scalar_defaults(self) -> None:
        """When source_configs is empty, scalar defaults are used for all events."""
        renderer = MatrixRenderer(
            meshnet_name="FallbackNet",
            matrix_relay_prefix="[{longname}/FallbackNet]: ",
        )
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Dave"},
        )
        result = await renderer.render(event, "matrix-1")
        assert result.payload["body"] == "[Dave/FallbackNet]: hello mesh"


# ---------------------------------------------------------------------------
# Runtime assembly tests (source_configs only, no scalar defaults)
# ---------------------------------------------------------------------------


class TestRuntimeAssemblySourceConfig:
    """MatrixRenderer behavior under runtime assembly configuration.

    Runtime assembly passes ``source_configs`` only — no scalar defaults
    from any Meshtastic config.  Unknown / non-Meshtastic sources render
    plain Matrix output without Meshtastic prefix or metadata contamination.
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
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
        result = await renderer.render(event, "matrix-1")
        # No prefix (scalar defaults are empty)
        assert result.payload["body"] == "hello mesh"
        # No mmrelay metadata (scalar default is False)
        assert "meshtastic_id" not in result.payload
        assert "meshtastic_meshnet" not in result.payload
