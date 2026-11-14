# medre-test-tier: compatibility
"""Tests for MatrixRenderer: name, can_render dispatch, rendering output,
relation handling, envelope embedding, and long body handling.
"""

from __future__ import annotations

from datetime import datetime, timezone

from msgspec.structs import replace

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMetadata,
    NativeRef,
)
from medre.core.rendering.renderer import RenderingContext, RenderingResult
from tests.helpers.matrix import matrix_payload_content as _payload_content
from tests.helpers.matrix_events import make_matrix_event, make_meshtastic_event
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig
from tests.helpers.matrix_stubs import StubSourceAttribution as _StubSourceAttribution
from tests.helpers.native_metadata import meshtastic_native_data

# Module-level aliases so existing test call-sites stay concise.
_make_event = make_matrix_event


def _make_meshtastic_event(
    *,
    source_adapter: str = "radio-alpha",
    payload: dict | None = None,
    relations: tuple | None = None,
    native_data: dict | None = None,
) -> CanonicalEvent:
    """Build a Meshtastic event from current-version transport fields."""
    inner = native_data or {}
    # The meshtastic adapter persists transport-native identity under the
    # versioned ``native.meshtastic`` namespace.
    nd = meshtastic_native_data(inner)
    return make_meshtastic_event(
        source_adapter=source_adapter,
        payload=payload,
        relations=relations,
        native_data=nd,
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
        assert _payload_content(result)["msgtype"] == "m.text"
        assert _payload_content(result)["body"] == "hello matrix"

    async def test_render_includes_msgtype(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert _payload_content(result)["msgtype"] == "m.text"

    async def test_render_includes_body(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert _payload_content(result)["body"] == "specific body"

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
        assert "m.relates_to" in _payload_content(result)
        relates = _payload_content(result)["m.relates_to"]
        assert "m.in_reply_to" in relates
        assert relates["m.in_reply_to"]["event_id"] == "$orig-native"
        # Body is just the relayed body, no manual fallback quoting
        assert _payload_content(result)["body"] == "my reply"
        assert "> <" not in _payload_content(result)["body"]

    async def test_render_with_reaction_relation(self) -> None:
        """Reaction events render as described m.emote messages."""
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
        event = replace(
            _make_event(payload={"body": "👍"}, relations=(relation,)),
            event_kind="message.reacted",
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"
        content = _payload_content(result)
        assert content["msgtype"] == "m.emote"
        assert "reacted 👍 to" in content["body"]
        assert "m.relates_to" not in content

    async def test_render_with_envelope(self) -> None:
        renderer = MatrixRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert "medre" in _payload_content(result)
        assert "envelope" in _payload_content(result)["medre"]

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
        assert _payload_content(result)["body"] == long_body

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
        assert "m.relates_to" not in _payload_content(result)

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
        event = replace(
            _make_event(payload={"body": "👍"}, relations=(rel,)),
            event_kind="message.reacted",
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"
        assert _payload_content(result).get("msgtype") == "m.emote"


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
        assert _payload_content(result)["body"] == "my reply"

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
        body = _payload_content(result)["body"]
        assert "> <" not in body

    async def test_reply_body_with_relay_prefix(self) -> None:
        """Reply body includes relay prefix when configured via target-local configs."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
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
                native=NativeMetadata(
                    data=meshtastic_native_data({"longname": "BobSmith"})
                )
            ),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = _payload_content(result)["body"]
        assert body == "[BobSmith] my reply"
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
            _payload_content(result)["m.relates_to"]["m.in_reply_to"]["event_id"]
            == "$orig-native"
        )

    async def test_no_fallback_quote_any_sender_style(self) -> None:
        """No Matrix reply body contains '> <matrix>', '> <matrix-1>', or
        '> <Bob Smith>' style fallback."""
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
            metadata={"displayname": "Bob Smith"},
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(relation,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = _payload_content(result)["body"]
        assert "> <matrix>" not in body
        assert "> <matrix-1>" not in body
        assert "> <Bob Smith>" not in body


# ---------------------------------------------------------------------------
# Multi-radio source-adapter config resolution tests
# ---------------------------------------------------------------------------


class TestMultiRadioSourceConfig:
    """MatrixRenderer resolves per-source attribution for multi-radio setups."""

    @staticmethod
    def _configs() -> dict[str, _StubMatrixConfig]:
        """Target-local configs with per-origin prefix via origin_label variable."""
        return {
            "matrix-1": _StubMatrixConfig(
                adapter_id="matrix-1",
                relay_prefix="[{sender}/{origin_label}]: ",
            ),
        }

    @staticmethod
    def _source_attribution() -> dict[str, _StubSourceAttribution]:
        """Per-source adapter registry carrying each radio's origin label."""
        return {
            "radio-alpha": _StubSourceAttribution(
                adapter_id="radio-alpha",
                origin_label="AlphaNet",
            ),
            "radio-bravo": _StubSourceAttribution(
                adapter_id="radio-bravo",
                origin_label="BravoNet",
            ),
        }

    def _make_renderer(self, **kwargs: object) -> MatrixRenderer:
        return MatrixRenderer(
            source_attribution=self._source_attribution(),
            configs=self._configs(),
            **kwargs,  # type: ignore[arg-type]
        )

    async def test_alpha_source_uses_alpha_prefix(self) -> None:
        """Event from radio-alpha resolves origin_label=AlphaNet in target-local prefix."""
        renderer = self._make_renderer()
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert _payload_content(result)["body"] == "[Alice/AlphaNet]: hello mesh"
        # Verify origin_label is used rather than an unrelated placeholder value
        assert "UNUSED-Alpha" not in _payload_content(result)["body"]

    async def test_bravo_source_uses_bravo_prefix(self) -> None:
        """Event from radio-bravo resolves origin_label=BravoNet in target-local prefix."""
        renderer = self._make_renderer()
        event = _make_meshtastic_event(
            source_adapter="radio-bravo",
            native_data={"longname": "Bob"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert _payload_content(result)["body"] == "[Bob/BravoNet]: hello mesh"
        # Verify origin_label is used rather than an unrelated placeholder value
        assert "UNUSED-Bravo" not in _payload_content(result)["body"]

    async def test_unknown_source_renders_plain_output(self) -> None:
        """Event from a source outside the attribution registry renders a plain body."""
        renderer = MatrixRenderer(
            source_attribution=self._source_attribution(),
        )
        event = _make_meshtastic_event(
            source_adapter="radio-charlie",
            native_data={"longname": "Charlie"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Without a target-local relay_prefix the body stays plain.
        assert _payload_content(result)["body"] == "hello mesh"
        # No mesh provenance keys on outbound Matrix content
        assert "meshtastic_id" not in _payload_content(result)

    async def test_displayname_does_not_leak_into_sender_fields(self) -> None:
        """Matrix displayname must NOT populate the projected sender identity.

        Regression test: when native data carries a displayname but no
        Meshtastic longname/shortname, the projected sender label stays
        empty — the displayname never becomes sender identity.
        """
        renderer = self._make_renderer()
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"displayname": "Alice Display"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = _payload_content(result)["body"]
        assert "Alice Display" not in body

    async def test_reaction_prefix_resolves_per_source(self) -> None:
        """Reaction emote prefix resolves via target-local template with per-source origin_label."""
        renderer = self._make_renderer()
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
        body = _payload_content(result)["body"]
        assert "Alice/AlphaNet" in body

    async def test_reaction_meshnet_per_source(self) -> None:
        """Reaction emote body carries human attribution, no mesh wire keys."""
        renderer = self._make_renderer()
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
        assert "BravoNet" in _payload_content(result)["body"]
        assert not any(
            key.startswith("meshtastic_") for key in _payload_content(result)
        )

    async def test_non_meshtastic_source_ignores_meshtastic_configs(self) -> None:
        """Non-Meshtastic source renders plain output without any mesh attribution."""
        renderer = MatrixRenderer(
            source_attribution=self._source_attribution(),
            configs=self._configs(),
        )
        event = _make_event(
            event_id="evt-plain-1",
            payload={"body": "Hello from elsewhere"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # The prefix is target-local; a non-Meshtastic source renders with
        # empty projected sender variables, never "None".
        assert _payload_content(result)["body"] == "[/]: Hello from elsewhere"
        # No Meshtastic metadata keys
        assert "meshtastic_id" not in _payload_content(result)
        assert "meshtastic_meshnet" not in _payload_content(result)
        assert "meshtastic_longname" not in _payload_content(result)
        # Standard Matrix content
        assert _payload_content(result)["msgtype"] == "m.text"


# ---------------------------------------------------------------------------
# Runtime assembly tests
# ---------------------------------------------------------------------------


class TestRuntimeAssemblySourceConfig:
    """MatrixRenderer behavior under runtime assembly configuration.

    Runtime assembly passes ``source_attribution``; unknown and
    non-Meshtastic sources render plain Matrix output without a prefix.
    """

    @staticmethod
    def _configs() -> dict[str, _StubMatrixConfig]:
        """Target-local configs with per-origin prefix via origin_label variable."""
        return {
            "matrix-1": _StubMatrixConfig(
                adapter_id="matrix-1",
                relay_prefix="[{sender}/{origin_label}]: ",
            ),
        }

    @staticmethod
    def _source_attribution() -> dict[str, _StubSourceAttribution]:
        """Per-source adapter registry carrying each radio's origin label."""
        return {
            "radio-alpha": _StubSourceAttribution(
                adapter_id="radio-alpha",
                origin_label="AlphaNet",
            ),
            "radio-bravo": _StubSourceAttribution(
                adapter_id="radio-bravo",
                origin_label="BravoNet",
            ),
        }

    def _make_renderer(self, **kwargs: object) -> MatrixRenderer:
        return MatrixRenderer(
            source_attribution=self._source_attribution(),
            configs=self._configs(),
            **kwargs,  # type: ignore[arg-type]
        )

    async def test_runtime_source_a_renders_with_alpha_metadata(self) -> None:
        """Runtime assembly: event from radio-alpha uses alpha's prefix."""
        renderer = self._make_renderer()
        event = _make_meshtastic_event(
            source_adapter="radio-alpha",
            native_data={"longname": "Alice", "shortname": "A"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert _payload_content(result)["body"] == "[Alice/AlphaNet]: hello mesh"
        # Verify origin_label is used rather than an unrelated placeholder value
        assert "UNUSED-Alpha" not in _payload_content(result)["body"]

    async def test_runtime_source_b_renders_with_bravo_metadata(self) -> None:
        """Runtime assembly: event from radio-bravo uses bravo's prefix."""
        renderer = self._make_renderer()
        event = _make_meshtastic_event(
            source_adapter="radio-bravo",
            native_data={"longname": "Bob", "shortname": "B"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert _payload_content(result)["body"] == "[Bob/BravoNet]: hello mesh"
        # Verify origin_label is used rather than an unrelated placeholder value
        assert "UNUSED-Bravo" not in _payload_content(result)["body"]

    async def test_non_meshtastic_source_renders_plain_output(self) -> None:
        """Non-Meshtastic source renders plain Matrix output with no mesh keys."""
        renderer = MatrixRenderer(
            source_attribution=self._source_attribution(),
            configs=self._configs(),
        )
        event = _make_event(
            event_id="evt-matrix-1",
            payload={"body": "Hello from Matrix"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # The prefix is target-local; a non-Meshtastic source renders with
        # empty projected sender variables, never "None".
        assert _payload_content(result)["body"] == "[/]: Hello from Matrix"
        # No Meshtastic metadata keys
        assert "meshtastic_id" not in _payload_content(result)
        assert "meshtastic_meshnet" not in _payload_content(result)
        assert "meshtastic_longname" not in _payload_content(result)
        assert "meshtastic_shortname" not in _payload_content(result)
        assert "meshtastic_portnum" not in _payload_content(result)
        # Standard Matrix content
        assert _payload_content(result)["msgtype"] == "m.text"

    async def test_unknown_meshtastic_source_renders_plain_output(self) -> None:
        """Unknown Meshtastic source (outside the registry) renders plain output."""
        renderer = MatrixRenderer(
            source_attribution=self._source_attribution(),
            configs=self._configs(),
        )
        event = _make_meshtastic_event(
            source_adapter="radio-charlie",
            native_data={"longname": "Charlie"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # The prefix is target-local; the unregistered source projects its
        # own sender facts and has no registry origin label.
        assert _payload_content(result)["body"] == "[Charlie/]: hello mesh"
        assert "meshtastic_id" not in _payload_content(result)
        assert "meshtastic_meshnet" not in _payload_content(result)


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
            # Wrap the FLAT-shape test data into the meshtastic
            # versioned namespace.
            nd = meshtastic_native_data(native_data)
            metadata = EventMetadata(native=NativeMetadata(data=nd))
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
        assert _payload_content(result)["msgtype"] == "m.text"
        assert "m.relates_to" not in _payload_content(result)
        assert result.fallback_applied == "strategy_fallback_text"
        # Body should contain degraded relation info and original text
        assert "my reply" in _payload_content(result)["body"]

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
        assert "medre" in _payload_content(result)
        assert "envelope" in _payload_content(result)["medre"]

    async def test_fallback_text_truncation_respects_max_text_chars(self) -> None:
        """Truncation applies to the final body including relay prefix."""
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[Alice]: ",
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
        body: str = _payload_content(result)["body"]
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
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix=prefix,
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
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[Al]: ",
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
        assert _payload_content(result)["msgtype"] == "m.text"
        assert "m.relates_to" not in _payload_content(result)

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
        rendered_body: str = _payload_content(result)["body"]
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


class TestMatrixExplicitReactionKey:
    """Canonical keys survive transport rendering independently of payload hints.

    Missing-key rejection belongs to the adapter's own versioned policy suite.
    """

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
        assert "reacted ❤️ to" in _payload_content(result)["body"]


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
