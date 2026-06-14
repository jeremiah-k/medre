"""Tests for MeshtasticRenderer: comprehensive cross-platform reactions,
no-mapping fallbacks, native reaction preservation, display-name enrichment,
byte-budget truncation, config validation, adapter capabilities, target-aware
rendering, and multi-radio scenarios.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMetadata,
    NativeRef,
)
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_renderer(
    target_adapter: str = "mesh-1",
    *,
    radio_relay_prefix: str = "",
    max_text_bytes: int = 227,
) -> MeshtasticRenderer:
    """Create a MeshtasticRenderer with a single-adapter config mapping."""
    config = MeshtasticConfig(
        adapter_id=target_adapter,
        radio_relay_prefix=radio_relay_prefix,
        max_text_bytes=max_text_bytes,
    )
    return MeshtasticRenderer(configs={target_adapter: config})


def _make_event(
    event_id: str = "evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="mesh-1",
        source_transport_id="!node1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "hello mesh"},
        metadata=EventMetadata(),
    )


def _make_relation(
    relation_type: str = "reply",
    native_message_id: str | None = "42",
    key: str | None = None,
    fallback_text: str | None = None,
    adapter_id: str = "mesh-1",
) -> EventRelation:
    native_ref = None
    if native_message_id is not None:
        native_ref = NativeRef(
            adapter=adapter_id,
            native_channel_id="0",
            native_message_id=native_message_id,
        )
    return EventRelation(
        relation_type=relation_type,
        target_event_id="evt-0",
        target_native_ref=native_ref,
        key=key,
        fallback_text=fallback_text,
    )


def _make_matrix_event(
    event_id: str = "mx-evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
    source_adapter: str = "matrix-1",
    display_name: str = "Display Name",
) -> CanonicalEvent:
    """Create a CanonicalEvent simulating Matrix origin."""
    native_data: dict[str, object] = {
        "sender": "@user:example.com",
        "displayname": display_name,
    }
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.reacted",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter=source_adapter,
        source_transport_id="@user:example.com",
        source_channel_id="!room:example.com",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "👍"},
        metadata=EventMetadata(native=NativeMetadata(data=native_data)),
    )


def _make_cross_platform_relation(
    key: str = "👍",
    fallback_text: str | None = "original mesh message",
    meshtastic_reply_id: str | None = None,
    mesh_adapter: str = "mesh-1",
) -> EventRelation:
    """Create a reaction relation pointing at a Meshtastic message.

    If *meshtastic_reply_id* is given, sets both the target_native_ref
    (owned by *mesh_adapter*) and the mmrelay metadata fallback.
    """
    metadata: dict[str, object] = {}
    native_ref = None
    if meshtastic_reply_id is not None:
        native_ref = NativeRef(
            adapter=mesh_adapter,
            native_channel_id="0",
            native_message_id=meshtastic_reply_id,
        )
        metadata["meshtastic_reply_id"] = meshtastic_reply_id
    return EventRelation(
        relation_type="reaction",
        target_event_id="mesh-evt-0",
        target_native_ref=native_ref,
        key=key,
        fallback_text=fallback_text,
        metadata=metadata,
    )


# ===================================================================
# Test D: Matrix→Meshtastic comprehensive reaction rendering
# ===================================================================


class TestMatrixToMeshtasticReactionComprehensive:
    """Test D: Matrix→Meshtastic reaction with generic display name.

    Verifies: compact prefix with space before 'reacted', casing preserved,
    display-name spaces removed, reply_id == 2728143522 when mapping exists,
    no emoji=1 field.
    """

    async def test_comprehensive_descriptive_reaction(self) -> None:
        """All Test D requirements in one test: spaces, casing, reply_id, no emoji."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{sender}] ",
        )

        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="original mesh message text",
            meshtastic_reply_id="2728143522",
        )
        event = _make_matrix_event(
            display_name="Alpha Bravo",
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        payload = result.payload
        text = payload["text"]

        # Display-name spaces removed: "Alpha Bravo" → "AlphaBravo"
        assert "[AlphaBravo]" in text
        assert "[Alpha Bravo]" not in text

        # Casing preserved (not lowercased)
        assert "[AlphaBravo]" in text  # exact casing match
        assert "[alphabravo]" not in text  # would appear if lowercased

        # Space after compact prefix before 'reacted'
        assert "AlphaBravo] reacted" in text
        assert "AlphaBravo]reacted" not in text

        # Descriptive reaction pattern
        assert 'reacted 👍 to "original mesh message text"' in text

        # reply_id == 2728143522 when mapping exists
        assert payload["reply_id"] == 2728143522

        # No emoji=1 field (descriptive, not native tapback)
        assert "emoji" not in payload

    async def test_no_prefix_space_before_reacted(self) -> None:
        """Without a prefix template, text starts with 'reacted'."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="test",
        )
        event = _make_matrix_event(
            display_name="Some User",
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        # No prefix → text starts directly with "reacted"
        assert text.startswith("reacted 👍 to")

    async def test_compact_prefix_no_trailing_space_adds_separator(self) -> None:
        """Prefix without trailing space gets separator space before 'reacted'."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{sender}]",
        )

        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="hi",
        )
        event = _make_matrix_event(
            display_name="Test User",
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        # "[TestUser]" (no trailing space) + separator " " + "reacted"
        assert "[TestUser] reacted" in text
        assert "[TestUser]reacted" not in text


# ===================================================================
# Matrix→Meshtastic no-mapping fallback
# ===================================================================


class TestMatrixToMeshtasticNoMapping:
    """Missing mapping fallback: Matrix→Meshtastic reaction with no mapping.

    Sends descriptive text with no reply_id. No crash.
    """

    async def test_no_mapping_descriptive_text_no_reply_id(self) -> None:
        """Matrix reaction with no Meshtastic mapping → descriptive text, no reply_id."""
        renderer = _make_renderer("mesh-1")
        # No native ref, no meshtastic_reply_id
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="👍",
            fallback_text="a message from Matrix",
        )
        event = _make_matrix_event(
            display_name="Generic User",
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )

        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        text = result.payload["text"]
        assert "reacted 👍 to" in text
        assert "a message from Matrix" in text

    async def test_no_mapping_minimal_metadata_no_crash(self) -> None:
        """Matrix reaction with minimal metadata still renders without crash."""
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="🔥",
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="evt-minimal",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(UTC),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:server",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "🔥"},
            metadata=EventMetadata(native=NativeMetadata(data={})),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "reacted" in result.payload["text"]
        assert "reply_id" not in result.payload


# ===================================================================
# Native Meshtastic reactions still work (regression guard)
# ===================================================================


class TestNativeReactionPreserved:
    """Ensure native Meshtastic tapback behavior is unchanged."""

    async def test_native_reaction_emoji_1(self) -> None:
        """Native Meshtastic reaction still sets emoji=1."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="👍",
            adapter_id="mesh-1",
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["emoji"] == 1
        assert result.payload["reply_id"] == 55
        assert result.payload["text"] == "👍"

    async def test_native_reaction_no_reply_id_fallback(self) -> None:
        """Native reaction without reply_id → readable fallback."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id=None,
            key="❤️",
        )
        event = _make_event(
            payload={"body": "❤️"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "emoji" not in result.payload
        assert "[reacted: ❤️]" in result.payload["text"]

    async def test_native_reaction_with_mmrelay_meta(self) -> None:
        """Native reaction with mmrelay metadata still gets emoji=1."""
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="🔥",
            fallback_text=None,
            metadata={"meshtastic_reply_id": "77"},
        )
        event = _make_event(
            payload={"body": "🔥"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["reply_id"] == 77
        assert result.payload["emoji"] == 1


# ===================================================================
# Matrix display name enrichment → prefix rendering
# ===================================================================


class TestMatrixDisplayNameInPrefix:
    """Verify that Matrix display names flow through to the prefix template."""

    async def test_sender_in_prefix_from_matrix_display_name(self) -> None:
        """radio_relay_prefix {sender} uses Matrix display name."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{sender}]: ",
        )

        event = CanonicalEvent(
            event_id="mx-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(UTC),
            source_adapter="matrix-1",
            source_transport_id="@alice:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello from alice"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "sender": "@alice:example.com",
                        "displayname": "Alice Wonderland",
                    }
                )
            ),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["text"].startswith("[Alice Wonderland]: ")
        assert "hello from alice" in result.payload["text"]

    async def test_prefix_uses_display_name_not_mxid(self) -> None:
        """Prefix shows display name, not raw MXID like @user:server."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="{sender}: ",
        )

        event = CanonicalEvent(
            event_id="mx-2",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(UTC),
            source_adapter="matrix-1",
            source_transport_id="@alice:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hi"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "sender": "@alice:example.com",
                        "displayname": "Display Name",
                    }
                )
            ),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["text"].startswith("Display Name: ")
        assert "@alice" not in result.payload["text"].split(": hi")[0]


# ===================================================================
# Byte-budget truncation tests
# ===================================================================


class TestByteBudgetTruncation:
    """UTF-8 byte-budget truncation after final rendering."""

    async def test_under_budget_ascii_unchanged(self) -> None:
        """ASCII text well under the byte budget is unchanged."""
        renderer = _make_renderer("mesh-1")
        text = "hello mesh"
        event = _make_event(payload={"body": text})
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["text"] == text
        assert result.truncated is False

    async def test_over_budget_ascii_truncates_after_prefix(self) -> None:
        """ASCII text over budget truncates to fit within max_text_bytes."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{sender}]: ",
            max_text_bytes=20,
        )

        event = CanonicalEvent(
            event_id="evt-trunc",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(UTC),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "A" * 200},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={"sender": "@test:example.com", "displayname": "Test"}
                )
            ),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert text.startswith("[Test]: ")
        assert len(text.encode("utf-8")) <= 20
        assert result.truncated is True

    async def test_utf8_characters_not_split(self) -> None:
        """Multi-byte UTF-8 characters are never split mid-sequence."""
        renderer = _make_renderer("mesh-1")
        # Each emoji is 4 bytes in UTF-8
        emojis = "😀" * 100  # 400 bytes total
        event = _make_event(payload={"body": emojis})
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        # The text should contain only complete emoji characters
        for ch in text:
            assert ch == "😀"
        assert len(text.encode("utf-8")) <= 227

    async def test_max_text_bytes_zero_renders_empty(self) -> None:
        """max_text_bytes == 0 renders empty text."""
        renderer = _make_renderer("mesh-1", max_text_bytes=0)

        event = _make_event(payload={"body": "hello world"})
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["text"] == ""
        assert result.truncated is True
        # Metadata reflects the zero-budget truncation.
        assert result.metadata["max_text_bytes"] == 0
        assert result.metadata["truncated"] is True
        assert result.metadata["rendered_text_bytes"] == 0
        assert result.metadata["original_text_bytes"] == 11
        assert result.metadata["rendered_length"] == 0
        assert result.metadata["original_length"] == 11

    async def test_truncation_metadata_keys(self) -> None:
        """Metadata includes byte-budget evidence keys."""
        renderer = _make_renderer("mesh-1")
        text = "A" * 500
        event = _make_event(payload={"body": text})
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        meta = result.metadata
        assert "original_text_bytes" in meta
        assert "rendered_text_bytes" in meta
        assert "max_text_bytes" in meta
        assert "truncated" in meta
        assert "original_length" in meta
        assert "rendered_length" in meta
        assert meta["max_text_bytes"] == 227
        assert meta["truncated"] is True
        assert isinstance(meta["original_text_bytes"], int)
        assert isinstance(meta["rendered_text_bytes"], int)
        assert isinstance(meta["original_length"], int)
        assert isinstance(meta["rendered_length"], int)

    async def test_metadata_byte_counts_match_final_text(self) -> None:
        """rendered_text_bytes and rendered_length match the actual truncated text."""
        renderer = _make_renderer("mesh-1")
        text = "x" * 300
        event = _make_event(payload={"body": text})
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        rendered_text = result.payload["text"]
        rendered_bytes = len(rendered_text.encode("utf-8"))
        assert result.metadata["rendered_text_bytes"] == rendered_bytes
        assert result.metadata["original_text_bytes"] == 300
        assert result.metadata["rendered_length"] == len(rendered_text)

    async def test_no_truncation_metadata_when_under_budget(self) -> None:
        """Under budget: truncated is False, byte counts and lengths match."""
        renderer = _make_renderer("mesh-1")
        text = "short"
        event = _make_event(payload={"body": text})
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.truncated is False
        assert result.metadata["truncated"] is False
        assert result.metadata["original_text_bytes"] == 5
        assert result.metadata["rendered_text_bytes"] == 5
        assert result.metadata["original_length"] == 5
        assert result.metadata["rendered_length"] == 5


# ===================================================================
# Config-driven max_text_bytes tests
# ===================================================================


class TestMeshtasticConfigMaxTextBytes:
    """MeshtasticConfig max_text_bytes field validation."""

    def test_default_max_text_bytes_is_227(self) -> None:
        config = MeshtasticConfig(adapter_id="test")
        assert config.max_text_bytes == 227

    def test_rejects_negative_max_text_bytes(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError

        config = MeshtasticConfig(adapter_id="test", max_text_bytes=-1)
        with pytest.raises(MeshtasticConfigError, match="max_text_bytes"):
            config.validate()

    def test_rejects_bool_max_text_bytes(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError

        config = MeshtasticConfig(adapter_id="test", max_text_bytes=True)  # type: ignore[arg-type]
        with pytest.raises(MeshtasticConfigError, match="max_text_bytes"):
            config.validate()

    def test_rejects_float_max_text_bytes(self) -> None:
        from medre.config.adapters.errors import MeshtasticConfigError

        config = MeshtasticConfig(adapter_id="test", max_text_bytes=227.5)  # type: ignore[arg-type]
        with pytest.raises(MeshtasticConfigError, match="max_text_bytes"):
            config.validate()

    def test_zero_max_text_bytes_allowed(self) -> None:
        config = MeshtasticConfig(adapter_id="test", max_text_bytes=0)
        config.validate()  # should not raise


# ===================================================================
# Adapter capabilities reflect configured max_text_bytes
# ===================================================================


class TestAdapterCapabilitiesConfigured:
    """Adapter capabilities report configured max_text_bytes."""

    def test_real_adapter_default_max_text_bytes(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="caps-test")
        adapter = MeshtasticAdapter(config)
        assert adapter._capabilities.max_text_bytes == 227

    def test_real_adapter_custom_max_text_bytes(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="caps-test", max_text_bytes=100)
        adapter = MeshtasticAdapter(config)
        assert adapter._capabilities.max_text_bytes == 100

    def test_fake_adapter_default_max_text_bytes(self) -> None:
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter

        adapter = FakeMeshtasticAdapter()
        assert adapter._capabilities.max_text_bytes == 227

    def test_fake_adapter_custom_max_text_bytes(self) -> None:
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter

        config = MeshtasticConfig(adapter_id="fake-custom", max_text_bytes=50)
        adapter = FakeMeshtasticAdapter(config)
        assert adapter._capabilities.max_text_bytes == 50


# ===================================================================
# Cross-platform descriptive reaction byte-budget
# ===================================================================


class TestDescriptiveReactionByteBudget:
    """Descriptive reaction text is truncated after final assembly."""

    async def test_descriptive_reaction_truncates(self) -> None:
        """Long descriptive reaction text is truncated to byte budget."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{sender}] ",
            max_text_bytes=30,
        )

        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="A" * 200,
            meshtastic_reply_id="42",
        )
        event = _make_matrix_event(
            display_name="User",
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert len(text.encode("utf-8")) <= 30
        assert result.truncated is True
        # reply_id should still be set
        assert result.payload["reply_id"] == 42

    async def test_native_reaction_keeps_reply_id_and_emoji(self) -> None:
        """Native emoji reaction keeps reply_id/emoji while text is byte-budgeted."""
        renderer = _make_renderer("mesh-1")
        # Native reaction from same adapter
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="👍",
            adapter_id="mesh-1",
        )
        event = _make_event(
            payload={"body": "👍"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["emoji"] == 1
        assert result.payload["reply_id"] == 55
        # The emoji text should be within byte budget
        assert len(result.payload["text"].encode("utf-8")) <= 227
        assert result.truncated is False


# ===================================================================
# Target-aware renderer tests
# ===================================================================


class TestTargetAwareMeshtasticRenderer:
    """MeshtasticRenderer resolves per-adapter config at render time."""

    async def test_two_adapters_different_byte_budgets(self) -> None:
        """Rendering to adapter A uses 100-byte budget, adapter B uses 500."""
        config_a = MeshtasticConfig(adapter_id="radio-a", max_text_bytes=100)
        config_b = MeshtasticConfig(adapter_id="radio-b", max_text_bytes=500)

        renderer = MeshtasticRenderer(
            configs={"radio-a": config_a, "radio-b": config_b},
        )

        long_text = "x" * 400
        event = _make_event(payload={"body": long_text})

        # Adapter A: 100-byte budget
        result_a = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-a", delivery_strategy="direct"),
        )
        assert len(result_a.payload["text"].encode("utf-8")) <= 100
        assert result_a.truncated is True
        assert result_a.metadata["max_text_bytes"] == 100

        # Adapter B: 500-byte budget
        result_b = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-b", delivery_strategy="direct"),
        )
        assert len(result_b.payload["text"].encode("utf-8")) <= 500
        assert result_b.truncated is False
        assert result_b.metadata["max_text_bytes"] == 500

    async def test_two_adapters_different_prefixes(self) -> None:
        """Prefix matches target adapter, not a random one."""
        config_a = MeshtasticConfig(
            adapter_id="radio-a",
            radio_relay_prefix="[A]: ",
            max_text_bytes=227,
        )
        config_b = MeshtasticConfig(
            adapter_id="radio-b",
            radio_relay_prefix="[B]: ",
            max_text_bytes=227,
        )

        renderer = MeshtasticRenderer(
            configs={"radio-a": config_a, "radio-b": config_b},
        )

        event = _make_event(payload={"body": "hello"})

        result_a = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-a", delivery_strategy="direct"),
        )
        assert result_a.payload["text"].startswith("[A]: ")

        result_b = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-b", delivery_strategy="direct"),
        )
        assert result_b.payload["text"].startswith("[B]: ")

    async def test_unknown_target_adapter_raises_key_error(self) -> None:
        """Unknown target_adapter raises KeyError — no fallback."""
        config_a = MeshtasticConfig(adapter_id="radio-a", max_text_bytes=100)
        config_b = MeshtasticConfig(adapter_id="radio-b", max_text_bytes=500)

        renderer = MeshtasticRenderer(
            configs={"radio-a": config_a, "radio-b": config_b},
        )

        event = _make_event(payload={"body": "fallback test"})
        with pytest.raises(KeyError, match="unknown-radio"):
            await renderer.render(
                event,
                RenderingContext(
                    target_adapter="unknown-radio", delivery_strategy="direct"
                ),
            )

    async def test_metadata_reports_target_adapter_budget(self) -> None:
        """Metadata max_text_bytes matches the target adapter's config."""
        config_a = MeshtasticConfig(adapter_id="radio-a", max_text_bytes=100)
        config_b = MeshtasticConfig(adapter_id="radio-b", max_text_bytes=500)

        renderer = MeshtasticRenderer(
            configs={"radio-a": config_a, "radio-b": config_b},
        )

        event = _make_event(payload={"body": "short"})

        result_a = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-a", delivery_strategy="direct"),
        )
        assert result_a.metadata["max_text_bytes"] == 100

        result_b = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-b", delivery_strategy="direct"),
        )
        assert result_b.metadata["max_text_bytes"] == 500


# ===================================================================
# Multi-radio target-aware coverage (radio-alpha / radio-bravo)
# ===================================================================


def _make_multi_radio_renderer() -> MeshtasticRenderer:
    """Create a MeshtasticRenderer with two distinct adapter configs."""
    return MeshtasticRenderer(
        configs={
            "radio-alpha": MeshtasticConfig(
                adapter_id="radio-alpha",
                radio_relay_prefix="[{sender_short}@alpha] ",
                max_text_bytes=60,
            ),
            "radio-bravo": MeshtasticConfig(
                adapter_id="radio-bravo",
                radio_relay_prefix="[{sender_short}@bravo] ",
                max_text_bytes=200,
            ),
        }
    )


class TestMultiRadioTargetAware:
    """A single MeshtasticRenderer with multiple configs renders differently
    per target_adapter for prefix, byte budget, replies,
    reactions, and unknown target behavior.
    """

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _event_with_native(body: str = "hello") -> CanonicalEvent:
        """Event with native metadata for prefix template expansion."""
        return CanonicalEvent(
            event_id="evt-multi",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(UTC),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": body},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "sender": "@TestU:example.com",
                        "displayname": "TestUser",
                    }
                )
            ),
        )

    # -- distinct prefixes ---------------------------------------------

    async def test_alpha_prefix_contains_alpha(self) -> None:
        """Rendering to radio-alpha uses the alpha prefix template."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-alpha", delivery_strategy="direct"),
        )
        assert result.payload["text"].startswith("[TestU@alpha] ")

    async def test_bravo_prefix_contains_bravo(self) -> None:
        """Rendering to radio-bravo uses the bravo prefix template."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-bravo", delivery_strategy="direct"),
        )
        assert result.payload["text"].startswith("[TestU@bravo] ")

    async def test_same_event_different_prefixes(self) -> None:
        """Same event rendered to both adapters produces different prefixes."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        result_a = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-alpha", delivery_strategy="direct"),
        )
        result_b = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-bravo", delivery_strategy="direct"),
        )
        assert result_a.payload["text"] != result_b.payload["text"]
        assert "[TestU@alpha]" in result_a.payload["text"]
        assert "[TestU@bravo]" in result_b.payload["text"]

    # -- distinct byte budgets -----------------------------------------

    async def test_alpha_truncates_long_text(self) -> None:
        """Alpha (60-byte budget) truncates a 150-char body."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("A" * 150)
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-alpha", delivery_strategy="direct"),
        )
        assert result.truncated is True
        assert len(result.payload["text"].encode("utf-8")) <= 60
        assert result.metadata["max_text_bytes"] == 60

    async def test_bravo_keeps_long_text(self) -> None:
        """Bravo (200-byte budget) keeps the same 150-char body untruncated."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("A" * 150)
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-bravo", delivery_strategy="direct"),
        )
        assert result.truncated is False
        assert "A" * 150 in result.payload["text"]
        assert result.metadata["max_text_bytes"] == 200

    # -- unknown target ------------------------------------------------

    async def test_unknown_target_raises_key_error(self) -> None:
        """Rendering to an unknown adapter raises KeyError listing known ones."""
        renderer = _make_multi_radio_renderer()
        event = self._event_with_native("msg")
        with pytest.raises(KeyError, match="unknown-radio"):
            await renderer.render(
                event,
                RenderingContext(
                    target_adapter="unknown-radio", delivery_strategy="direct"
                ),
            )

    # -- reply uses target adapter config ------------------------------

    async def test_reply_uses_target_prefix_and_budget(self) -> None:
        """Reply to radio-alpha uses alpha's prefix and byte budget."""
        renderer = _make_multi_radio_renderer()
        rel = _make_relation(
            relation_type="reply",
            native_message_id="99",
            fallback_text="original",
            adapter_id="radio-alpha",
        )
        event = CanonicalEvent(
            event_id="evt-reply",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(UTC),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "A" * 150},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "sender": "@TestU:example.com",
                        "displayname": "TestUser",
                    }
                )
            ),
        )
        # Alpha: 60-byte budget, should truncate
        result_a = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-alpha", delivery_strategy="direct"),
        )
        assert result_a.payload["reply_id"] == 99
        assert len(result_a.payload["text"].encode("utf-8")) <= 60
        assert result_a.truncated is True

        # Bravo: 200-byte budget, should NOT truncate (plain reply text < 200)
        result_b = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-bravo", delivery_strategy="direct"),
        )
        # No reply_id — native ref is owned by radio-alpha, not radio-bravo
        assert "reply_id" not in result_b.payload
        # But bravo's prefix and budget are used
        assert "[TestU@bravo]" in result_b.payload["text"]

    # -- native reaction uses target adapter config --------------------

    async def test_native_reaction_targets_correct_adapter(self) -> None:
        """Native reaction to radio-alpha uses alpha config for budget."""
        renderer = _make_multi_radio_renderer()
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="55",
            key="👍",
            adapter_id="radio-alpha",
        )
        event = CanonicalEvent(
            event_id="evt-react",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(UTC),
            source_adapter="radio-alpha",
            source_transport_id="!node1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "👍"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "sender": "@TestU:example.com",
                        "displayname": "TestUser",
                    }
                )
            ),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-alpha", delivery_strategy="direct"),
        )
        assert result.payload["emoji"] == 1
        assert result.payload["reply_id"] == 55
        assert result.payload["text"] == "👍"
        assert result.metadata["max_text_bytes"] == 60

    # -- cross-platform reaction uses target config --------------------

    async def test_cross_platform_reaction_uses_target_prefix(self) -> None:
        """Cross-platform reaction to radio-bravo uses bravo's compact prefix."""
        renderer = _make_multi_radio_renderer()
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="original msg",
            meshtastic_reply_id="42",
            mesh_adapter="radio-bravo",
        )
        event = _make_matrix_event(
            display_name="Cross User",
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-bravo", delivery_strategy="direct"),
        )
        text = result.payload["text"]
        # Compact prefix uses MXID localpart, spaces stripped
        assert "[user@bravo]" in text
        assert "reacted 👍 to" in text
        assert result.payload["reply_id"] == 42
        assert "emoji" not in result.payload

    async def test_cross_platform_reaction_truncated_to_alpha_budget(
        self,
    ) -> None:
        """Cross-platform reaction to radio-alpha truncates to 60 bytes."""
        renderer = _make_multi_radio_renderer()
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="A" * 200,
            meshtastic_reply_id="10",
            mesh_adapter="radio-alpha",
        )
        event = _make_matrix_event(
            display_name="User",
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="radio-alpha", delivery_strategy="direct"),
        )
        assert len(result.payload["text"].encode("utf-8")) <= 60
        assert result.truncated is True
        assert result.payload["reply_id"] == 10


# ===================================================================
# Shared attribution wiring: MeshCore / LXMF / missing vars / metadata
# ===================================================================


async def test_meshcore_pubkey_prefix_in_sender_id() -> None:
    """MeshCore event with pubkey_prefix populates sender_id."""
    renderer = _make_renderer("mesh-1", radio_relay_prefix="{sender_id}: ")
    event = CanonicalEvent(
        event_id="mc-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="meshcore-1",
        source_transport_id="!mc-node1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello from meshcore"},
        metadata=EventMetadata(native=NativeMetadata(data={"pubkey_prefix": "a1b2c3"})),
    )
    result = await renderer.render(
        event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
    )
    text = result.payload["text"]
    assert text.startswith("a1b2c3: ")
    assert "None" not in text


async def test_meshcore_pubkey_prefix_in_sender_id_template() -> None:
    """MeshCore event pubkey_prefix is available as {sender_id}."""
    renderer = _make_renderer("mesh-1", radio_relay_prefix="<{sender_id}> ")
    event = CanonicalEvent(
        event_id="mc-2",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="meshcore-1",
        source_transport_id="!mc-node1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hi"},
        metadata=EventMetadata(
            native=NativeMetadata(data={"pubkey_prefix": "abcdef12"})
        ),
    )
    result = await renderer.render(
        event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
    )
    text = result.payload["text"]
    assert text.startswith("<abcdef12> ")


async def test_lxmf_source_hash_in_sender_id() -> None:
    """LXMF event with source_hash populates {sender_id}."""
    renderer = _make_renderer("mesh-1", radio_relay_prefix="{sender_id}: ")
    event = CanonicalEvent(
        event_id="lx-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="lxmf-1",
        source_transport_id="!lx-node1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello from lxmf"},
        metadata=EventMetadata(native=NativeMetadata(data={"source_hash": "deadbeef"})),
    )
    result = await renderer.render(
        event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
    )
    text = result.payload["text"]
    assert text.startswith("deadbeef: ")


async def test_lxmf_source_hash_in_sender_short() -> None:
    """LXMF event with source_hash: sender_short is empty (no short label)."""
    renderer = _make_renderer("mesh-1", radio_relay_prefix="{sender_short}: ")
    event = CanonicalEvent(
        event_id="lx-2",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="lxmf-1",
        source_transport_id="!lx-node1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello"},
        metadata=EventMetadata(native=NativeMetadata(data={"source_hash": "cafebaaa"})),
    )
    result = await renderer.render(
        event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
    )
    text = result.payload["text"]
    # No short label → sender_short renders empty
    assert text.startswith(": ")


async def test_no_native_data_no_none_in_prefix() -> None:
    """Event with no native metadata renders empty prefix vars, not 'None'."""
    renderer = _make_renderer(
        "mesh-1", radio_relay_prefix="[{sender}][{sender_short}]: "
    )
    event = CanonicalEvent(
        event_id="evt-empty",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="matrix-1",
        source_transport_id="",
        source_channel_id="",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello"},
        metadata=EventMetadata(),
    )
    result = await renderer.render(
        event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
    )
    text = result.payload["text"]
    assert "None" not in text
    assert text == "[][]: hello"


async def test_partial_native_data_no_none() -> None:
    """Event with only longname renders sender_short empty, not 'None'."""
    renderer = _make_renderer("mesh-1", radio_relay_prefix="{sender}/{sender_short}: ")
    event = CanonicalEvent(
        event_id="evt-partial",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="matrix-1",
        source_transport_id="",
        source_channel_id="",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hi"},
        metadata=EventMetadata(
            native=NativeMetadata(
                data={"sender": "@alice:example.com", "displayname": "Alice"}
            )
        ),
    )
    result = await renderer.render(
        event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
    )
    text = result.payload["text"]
    assert "None" not in text
    assert text.startswith("Alice/")


async def test_prefix_metadata_records_template_and_variables() -> None:
    """Result metadata includes relay_prefix_template and relay_prefix_variables_used."""
    renderer = _make_renderer("mesh-1", radio_relay_prefix="{sender_short}: ")
    event = CanonicalEvent(
        event_id="evt-meta",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="matrix-1",
        source_transport_id="@user:example.com",
        source_channel_id="!room:example.com",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello"},
        metadata=EventMetadata(
            native=NativeMetadata(data={"sender": "@TestUser:example.com"})
        ),
    )
    result = await renderer.render(
        event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
    )
    # Normalized keys
    assert result.metadata["relay_prefix_template"] == "{sender_short}: "
    assert result.metadata["relay_prefix_rendered"] == "TestUser: "
    assert "relay_prefix_variables_used" in result.metadata
    assert "sender_short" in result.metadata["relay_prefix_variables_used"]
    assert "relay_prefix_missing_variables" in result.metadata
    assert isinstance(result.metadata["relay_prefix_missing_variables"], tuple)
    assert "relay_prefix_unknown_variables" in result.metadata
    assert isinstance(result.metadata["relay_prefix_unknown_variables"], tuple)
    assert "relay_prefix_formatting_error" in result.metadata
    assert result.metadata["relay_prefix_formatting_error"] is None


async def test_prefix_metadata_no_prefix_keys_when_no_prefix() -> None:
    """When no prefix is configured, no prefix metadata keys are present."""
    renderer = _make_renderer("mesh-1")
    event = _make_event()
    result = await renderer.render(
        event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
    )
    assert "relay_prefix_template" not in result.metadata
    assert "relay_prefix_rendered" not in result.metadata


async def test_prefix_metadata_records_missing_variables() -> None:
    """Result metadata records variables that resolved to empty."""
    renderer = _make_renderer("mesh-1", radio_relay_prefix="{sender}: ")
    event = CanonicalEvent(
        event_id="evt-miss",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="matrix-1",
        source_transport_id="",
        source_channel_id="",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello"},
        metadata=EventMetadata(),
    )
    result = await renderer.render(
        event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
    )
    assert "relay_prefix_missing_variables" in result.metadata
    assert "sender" in result.metadata["relay_prefix_missing_variables"]
