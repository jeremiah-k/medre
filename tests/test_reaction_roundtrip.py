"""Bidirectional Matrix↔Meshtastic reaction roundtrip tests.

Verifies that reactions traverse the full pipeline (codec → relation
resolution → enrichment → rendering → adapter delivery) using fake
adapters and SQLite storage.  No live services required.

Test categories
---------------
Test 1  Meshtastic native tapback resolves to Matrix emote fallback
        (reaction with emoji=1 and replyId resolved via NativeMessageRef).

Test 2  Matrix m.annotation reaction renders to Meshtastic descriptive
        output (cross-platform reaction with abbreviated original text).

Test 3  Multi-radio: reaction from radio-alpha routes to Matrix and
        outward to radio-bravo with source/target-aware rendering.

Test 4  Reaction-to-reaction suppression holds through the roundtrip
        pipeline (reuses the r2r suppression mechanism).

Test 5  Missing native ref fallback: reaction where the target event has
        no stored native ref for the target adapter does not crash and
        still delivers safely.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage

# Shared constants
_RADIO_ALPHA = "radio-alpha"
_RADIO_BRAVO = "radio-bravo"
_MATRIX = "matrix"
_ROOM = "!room:server"
_MESHNET = "testnet"


# ===================================================================
# Shared helper for source_configs construction
# ===================================================================


class _StubMeshtasticConfig:
    """Minimal duck-typed config for MatrixRenderer source_configs."""

    def __init__(
        self,
        adapter_id: str = "radio",
        meshnet_name: str = "",
        matrix_relay_prefix: str = "",
        mmrelay_compatibility: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.meshnet_name = meshnet_name
        self.matrix_relay_prefix = matrix_relay_prefix
        self.mmrelay_compatibility = mmrelay_compatibility


# =========================================================================
# Helpers
# =========================================================================


def _make_matrix_config(**overrides):
    defaults = dict(
        adapter_id=_MATRIX,
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok",
    )
    defaults.update(overrides)
    return MatrixConfig(**defaults)


async def _seed_matrix_message_on_mesh(
    storage: SQLiteStorage,
    *,
    canon_id: str = "canon-orig-1",
    matrix_msg_id: str = "$orig-matrix-1",
    matrix_room: str = _ROOM,
    radio_adapter: str = _RADIO_ALPHA,
    radio_pkt_id: int = 111111,
    text: str = "Hello from Matrix",
) -> None:
    """Seed storage with a Matrix message bridged to Meshtastic.

    Stores:
    1. The canonical event.
    2. Inbound Matrix native ref.
    3. Outbound Meshtastic native ref (packet ID on the radio).
    """
    ts = datetime.now(timezone.utc)
    event = CanonicalEvent(
        event_id=canon_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=ts,
        source_adapter=_MATRIX,
        source_transport_id="@sender:server",
        source_channel_id=matrix_room,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": text},
        metadata=EventMetadata(),
        source_native_ref=NativeRef(
            adapter=_MATRIX,
            native_channel_id=matrix_room,
            native_message_id=matrix_msg_id,
        ),
    )
    await storage.append(event)

    await storage.store_native_ref(
        NativeMessageRef(
            id="nref-inbound-seed",
            event_id=canon_id,
            adapter=_MATRIX,
            native_channel_id=matrix_room,
            native_message_id=matrix_msg_id,
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=ts,
        )
    )
    await storage.store_native_ref(
        NativeMessageRef(
            id="nref-outbound-seed",
            event_id=canon_id,
            adapter=radio_adapter,
            native_channel_id="0",
            native_message_id=str(radio_pkt_id),
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            created_at=ts,
        )
    )


def _make_mesh_reaction_packet(
    *,
    emoji: str = "👍",
    reply_id: int = 111111,
    pkt_id: int = 222222,
    from_id: str = "!meshnode1",
    channel: int = 0,
) -> dict:
    """Build a Meshtastic text packet with emoji=1 tapback (reaction)."""
    return {
        "fromId": from_id,
        "toId": "",
        "channel": channel,
        "id": pkt_id,
        "decoded": {
            "portnum": "text_message",
            "text": emoji,
            "replyId": reply_id,
            "emoji": 1,
        },
    }


def _decode_mesh_reaction_packet(
    packet: dict,
    adapter_id: str = _RADIO_ALPHA,
    longname: str = "TestNode",
    shortname: str = "TN",
) -> CanonicalEvent:
    """Decode a Meshtastic reaction packet and inject longname/shortname."""
    config = MeshtasticConfig(adapter_id=adapter_id)
    codec = MeshtasticCodec(adapter_id, config)
    classifier = MeshtasticPacketClassifier(config)

    classifier.classify(packet)
    event = codec.decode(packet)

    from msgspec.structs import replace as _replace

    if event.metadata.native is not None:
        updated_data = dict(event.metadata.native.data)
        updated_data["longname"] = longname
        updated_data["shortname"] = shortname
        new_native = _replace(event.metadata.native, data=updated_data)
        new_metadata = _replace(event.metadata, native=new_native)
        event = _replace(event, metadata=new_metadata)

    return event


def _make_matrix_reaction_native(
    *,
    emoji: str = "❤️",
    target_event_id: str = "$orig-matrix-1",
    event_id: str = "$rxn-001",
    sender: str = "@reactor:server",
    room_id: str = _ROOM,
):
    """Build a minimal native event object for a Matrix m.annotation reaction."""

    class _Fake:
        pass

    evt = _Fake()
    evt.body = emoji
    evt.sender = sender
    evt.event_id = event_id
    evt.source = {
        "content": {
            "msgtype": "m.text",
            "body": emoji,
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target_event_id,
                "key": emoji,
            },
        },
        "event_id": event_id,
        "sender": sender,
        "type": "m.room.message",
    }
    evt.room_id = room_id
    return evt


# =========================================================================
# Test 1: Meshtastic native tapback → Matrix emote fallback
# =========================================================================


class TestMeshtasticTapbackToMatrixRoundtrip:
    """Meshtastic emoji tapback resolves via NativeMessageRef and renders
    as an MMRelay-compatible emote on the Matrix side."""

    async def test_tapback_resolves_and_renders(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """End-to-end: radio-alpha node reacts with 👍 to a bridged
        Matrix message.  The reaction resolves through SQLite
        NativeMessageRef, and MatrixRenderer produces an m.emote with
        the correct emoji, original text preview, and meshtastic_replyId."""
        _PKT_ID = 111111
        _REACTION_PKT_ID = 222222
        _EMOJI = "👍"
        _LONGNAME = "AlphaNode"
        _SHORTNAME = "AN"

        # -- Seed: Matrix message bridged to radio-alpha -------------------
        await _seed_matrix_message_on_mesh(
            temp_storage,
            canon_id="canon-rt1",
            matrix_msg_id="$orig-rt1",
            radio_adapter=_RADIO_ALPHA,
            radio_pkt_id=_PKT_ID,
            text="Hello from the Matrix world",
        )

        # -- Decode Meshtastic reaction packet -----------------------------
        packet = _make_mesh_reaction_packet(
            emoji=_EMOJI,
            reply_id=_PKT_ID,
            pkt_id=_REACTION_PKT_ID,
        )
        reaction_event = _decode_mesh_reaction_packet(
            packet,
            adapter_id=_RADIO_ALPHA,
            longname=_LONGNAME,
            shortname=_SHORTNAME,
        )

        assert reaction_event.event_kind == "message.reacted"
        assert len(reaction_event.relations) == 1
        rel = reaction_event.relations[0]
        assert rel.relation_type == "reaction"
        assert rel.key == _EMOJI

        # -- Setup adapters and pipeline -----------------------------------
        matrix_adapter = FakeMatrixAdapter(adapter_id=_MATRIX, channel=_ROOM)

        route = Route(
            id="alpha-to-matrix-reaction",
            source=RouteSource(
                adapter=_RADIO_ALPHA,
                event_kinds=("message.reacted",),
                channel="0",
            ),
            targets=[RouteTarget(adapter=_MATRIX, channel=_ROOM)],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MatrixRenderer(
                source_configs={
                    _RADIO_ALPHA: _StubMeshtasticConfig(
                        adapter_id=_RADIO_ALPHA,
                        mmrelay_compatibility=True,
                        meshnet_name=_MESHNET,
                        matrix_relay_prefix="[{longname}] ",
                    ),
                },
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MATRIX, "matrix")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MATRIX: matrix_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reaction_event)

            # Delivery succeeded.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Matrix adapter received the rendered payload.
            assert len(matrix_adapter.delivered_payloads) == 1
            result = matrix_adapter.delivered_payloads[0]
            payload = result.payload

            # -- Verify emote reaction rendering --------------------------
            assert payload["msgtype"] == "m.emote"
            body = str(payload["body"])

            # Emoji preserved in body.
            assert f"reacted {_EMOJI}" in body

            # Original text preview (abbreviated).
            assert "Hello from the Matrix world" in body

            # Longname prefix preserved.
            assert f"[{_LONGNAME}]" in body

            # meshtastic_replyId == the original packet ID.
            assert str(payload.get("meshtastic_replyId")) == str(_PKT_ID)

            # meshtastic_emoji flag set.
            assert payload.get("meshtastic_emoji") == 1

            # -- Verify relation resolved to canonical event ---------------
            stored = await temp_storage.get(reaction_event.event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            stored_rel = stored.relations[0]
            assert stored_rel.target_event_id == "canon-rt1"
        finally:
            await runner.stop()


# =========================================================================
# Test 2: Matrix reaction → Meshtastic descriptive output
# =========================================================================


class TestMatrixReactionToMeshtasticRoundtrip:
    """Matrix m.annotation reaction renders to Meshtastic as descriptive
    cross-platform reaction text with abbreviated original preview."""

    async def test_matrix_reaction_renders_descriptive(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """End-to-end: Matrix user reacts with ❤️ to a message; reaction
        routes to Meshtastic and renders as descriptive text with
        abbreviated original message preview."""
        _EMOJI = "❤️"
        _MESH_PKT_ID = 333333
        _CANON_ID = "canon-rt2"
        _MATRIX_MSG_ID = "$orig-rt2"

        # -- Seed: Matrix message bridged to radio-alpha -------------------
        await _seed_matrix_message_on_mesh(
            temp_storage,
            canon_id=_CANON_ID,
            matrix_msg_id=_MATRIX_MSG_ID,
            radio_adapter=_RADIO_ALPHA,
            radio_pkt_id=_MESH_PKT_ID,
            text="Original message text from Matrix for testing",
        )

        # -- Decode a Matrix m.annotation reaction -------------------------
        config_mx = _make_matrix_config(adapter_id=_MATRIX)
        codec = MatrixCodec(_MATRIX, config_mx)

        native = _make_matrix_reaction_native(
            emoji=_EMOJI,
            target_event_id=_MATRIX_MSG_ID,
            event_id="$rxn-rt2",
            sender="@reactor:server",
            room_id=_ROOM,
        )
        reaction_event = codec.decode(native, room_id=_ROOM)

        assert reaction_event.event_kind == "message.reacted"
        assert len(reaction_event.relations) == 1
        rel = reaction_event.relations[0]
        assert rel.relation_type == "reaction"
        assert rel.key == _EMOJI

        # -- Setup adapters and pipeline -----------------------------------
        radio_config = MeshtasticConfig(
            adapter_id=_RADIO_ALPHA,
            radio_relay_prefix="[{longname}] ",
        )
        radio_adapter = FakeMeshtasticAdapter(radio_config)

        route = Route(
            id="matrix-to-alpha-reaction",
            source=RouteSource(
                adapter=_MATRIX,
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_RADIO_ALPHA, channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={_RADIO_ALPHA: radio_config},
            ),
            priority=50,
        )
        rp.register_adapter_platform(_RADIO_ALPHA, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_RADIO_ALPHA: radio_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reaction_event)

            # Delivery succeeded.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Radio adapter received the payload.
            assert len(radio_adapter.delivered_payloads) == 1
            payload = radio_adapter.delivered_payloads[0].payload

            # -- Verify descriptive reaction text --------------------------
            text = str(payload["text"])

            # Contains "reacted" and the emoji.
            assert "reacted" in text
            assert _EMOJI in text

            # Contains abbreviated original text preview (40 char truncation).
            assert "Original message text from Matrix for te..." in text

            # NOT a native tapback — no emoji=1 flag.
            assert "emoji" not in payload

            # reply_id is set from the enriched Meshtastic native ref.
            assert payload.get("reply_id") == _MESH_PKT_ID

            # -- Verify enrichment happened --------------------------------
            stored = await temp_storage.get(reaction_event.event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            stored_rel = stored.relations[0]
            assert stored_rel.target_event_id == _CANON_ID
        finally:
            await runner.stop()


# =========================================================================
# Test 3: Multi-radio reaction roundtrip
# =========================================================================


class TestMultiRadioReactionRoundtrip:
    """Reaction from radio-alpha routes to both Matrix and radio-bravo
    with source/target-aware rendering."""

    async def test_multi_radio_reaction_renders_correctly(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """radio-alpha node reacts to a message; reaction routes to Matrix
        (emote fallback) and radio-bravo (descriptive text, not native
        tapback because source_adapter != target_adapter)."""
        _ORIG_PKT_ALPHA = 444444
        _ORIG_PKT_BRAVO = 555555
        _REACTION_PKT = 666666
        _CANON_ID = "canon-rt3"
        _EMOJI = "🔥"
        _LONGNAME = "AlphaUser"

        # -- Seed: Matrix message bridged to both radios -------------------
        ts = datetime.now(timezone.utc)
        orig_event = CanonicalEvent(
            event_id=_CANON_ID,
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MATRIX,
            source_transport_id="@sender:server",
            source_channel_id=_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Multi-radio test message"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX,
                native_channel_id=_ROOM,
                native_message_id="$orig-rt3",
            ),
        )
        await temp_storage.append(orig_event)

        # Inbound Matrix ref.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-rt3-inbound",
                event_id=_CANON_ID,
                adapter=_MATRIX,
                native_channel_id=_ROOM,
                native_message_id="$orig-rt3",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )
        # Outbound ref for radio-alpha.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-rt3-alpha-out",
                event_id=_CANON_ID,
                adapter=_RADIO_ALPHA,
                native_channel_id="0",
                native_message_id=str(_ORIG_PKT_ALPHA),
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=ts,
            )
        )
        # Outbound ref for radio-bravo.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-rt3-bravo-out",
                event_id=_CANON_ID,
                adapter=_RADIO_BRAVO,
                native_channel_id="0",
                native_message_id=str(_ORIG_PKT_BRAVO),
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=ts,
            )
        )

        # -- Decode Meshtastic reaction from radio-alpha -------------------
        packet = _make_mesh_reaction_packet(
            emoji=_EMOJI,
            reply_id=_ORIG_PKT_ALPHA,
            pkt_id=_REACTION_PKT,
        )
        reaction_event = _decode_mesh_reaction_packet(
            packet,
            adapter_id=_RADIO_ALPHA,
            longname=_LONGNAME,
        )

        # -- Setup adapters -----------------------------------------------
        matrix_adapter = FakeMatrixAdapter(adapter_id=_MATRIX, channel=_ROOM)
        bravo_config = MeshtasticConfig(adapter_id=_RADIO_BRAVO)
        bravo_adapter = FakeMeshtasticAdapter(bravo_config)

        # Routes: radio-alpha → matrix, radio-alpha → radio-bravo
        routes = [
            Route(
                id="alpha-to-matrix",
                source=RouteSource(
                    adapter=_RADIO_ALPHA,
                    event_kinds=("message.reacted",),
                    channel="0",
                ),
                targets=[RouteTarget(adapter=_MATRIX, channel=_ROOM)],
            ),
            Route(
                id="alpha-to-bravo",
                source=RouteSource(
                    adapter=_RADIO_ALPHA,
                    event_kinds=("message.reacted",),
                    channel="0",
                ),
                targets=[RouteTarget(adapter=_RADIO_BRAVO, channel="0")],
            ),
        ]
        router = Router(routes=routes)

        rp = RenderingPipeline()
        rp.register(
            MatrixRenderer(
                source_configs={
                    _RADIO_ALPHA: _StubMeshtasticConfig(
                        adapter_id=_RADIO_ALPHA,
                        mmrelay_compatibility=True,
                        meshnet_name=_MESHNET,
                        matrix_relay_prefix="[{longname}] ",
                    ),
                },
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MATRIX, "matrix")
        rp.register(
            MeshtasticRenderer(
                configs={_RADIO_BRAVO: bravo_config},
            ),
            priority=50,
        )
        rp.register_adapter_platform(_RADIO_BRAVO, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    _MATRIX: matrix_adapter,
                    _RADIO_BRAVO: bravo_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reaction_event)

            # Both deliveries succeeded.
            assert len(outcomes) == 2
            assert all(o.status == "success" for o in outcomes)

            # -- Verify Matrix output (emote fallback) ---------------------
            assert len(matrix_adapter.delivered_payloads) == 1
            mx_payload = matrix_adapter.delivered_payloads[0].payload
            assert mx_payload["msgtype"] == "m.emote"
            mx_body = str(mx_payload["body"])
            assert f"reacted {_EMOJI}" in mx_body
            assert f"[{_LONGNAME}]" in mx_body

            # -- Verify radio-bravo output (descriptive, NOT native tapback)
            assert len(bravo_adapter.delivered_payloads) == 1
            bravo_payload = bravo_adapter.delivered_payloads[0].payload
            bravo_text = str(bravo_payload["text"])

            # Descriptive reaction text (cross-platform).
            assert "reacted" in bravo_text
            assert _EMOJI in bravo_text

            # NOT a native tapback — source_adapter != target_adapter.
            assert "emoji" not in bravo_payload

            # reply_id should point to the bravo-side packet ID.
            assert bravo_payload.get("reply_id") == _ORIG_PKT_BRAVO
        finally:
            await runner.stop()


# =========================================================================
# Test 4: Reaction-to-reaction suppression through roundtrip
# =========================================================================


class TestReactionToReactionSuppressionRoundtrip:
    """Reaction targeting another reaction is suppressed through the full
    pipeline but inbound native ref is still stored."""

    async def test_r2r_suppressed_through_pipeline(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Seed a stored Meshtastic reaction event; ingest a new reaction
        targeting it — pipeline returns [] (suppressed) but the inbound
        native ref for the new reaction is still persisted."""
        ts = datetime.now(timezone.utc)
        _FIRST_PKT = 777777
        _SECOND_PKT = 888888
        _FIRST_EMOJI = "👍"
        _SECOND_EMOJI = "❤️"

        # -- Seed: first reaction from radio-alpha -------------------------
        first_reaction = CanonicalEvent(
            event_id="first-rxn-rt4",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=ts,
            source_adapter=_RADIO_ALPHA,
            source_transport_id="!meshnode1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="some-original-msg",
                    target_native_ref=None,
                    key=_FIRST_EMOJI,
                    fallback_text=None,
                ),
            ),
            payload={"body": _FIRST_EMOJI},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_RADIO_ALPHA,
                native_channel_id="0",
                native_message_id=str(_FIRST_PKT),
            ),
        )
        await temp_storage.append(first_reaction)
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-first-rxn",
                event_id="first-rxn-rt4",
                adapter=_RADIO_ALPHA,
                native_channel_id="0",
                native_message_id=str(_FIRST_PKT),
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # -- Second reaction targeting the first reaction ------------------
        second_rel = EventRelation(
            relation_type="reaction",
            target_event_id="first-rxn-rt4",
            target_native_ref=None,
            key=_SECOND_EMOJI,
            fallback_text=None,
        )
        second_reaction = CanonicalEvent(
            event_id="second-rxn-rt4",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_RADIO_ALPHA,
            source_transport_id="!meshnode2",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(second_rel,),
            payload={"body": _SECOND_EMOJI},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_RADIO_ALPHA,
                native_channel_id="0",
                native_message_id=str(_SECOND_PKT),
            ),
        )

        # -- Setup pipeline -----------------------------------------------
        matrix_adapter = FakeMatrixAdapter(adapter_id=_MATRIX, channel=_ROOM)

        route = Route(
            id="alpha-to-matrix-r2r",
            source=RouteSource(
                adapter=_RADIO_ALPHA,
                event_kinds=("message.reacted",),
                channel="0",
            ),
            targets=[RouteTarget(adapter=_MATRIX, channel=_ROOM)],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MATRIX: matrix_adapter},
                event_bus=EventBus(),
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(second_reaction)

            # Suppressed — no delivery.
            assert outcomes == []

            # No delivery to Matrix.
            assert len(matrix_adapter.delivered_payloads) == 0

            # Inbound native ref still stored.
            resolved = await temp_storage.resolve_native_ref(
                _RADIO_ALPHA, "0", str(_SECOND_PKT)
            )
            assert resolved == "second-rxn-rt4"

            # The event itself is stored.
            stored = await temp_storage.get("second-rxn-rt4")
            assert stored is not None
            assert stored.event_kind == "message.reacted"
        finally:
            await runner.stop()


# =========================================================================
# Test 5: Missing native ref fallback — no crash
# =========================================================================


class TestMissingNativeRefFallbackRoundtrip:
    """Reaction where target event has no stored native ref for the target
    adapter still delivers safely without crashing."""

    async def test_missing_ref_delivers_safely_to_matrix(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Meshtastic reaction targeting an event with NO stored Meshtastic
        outbound ref still delivers to Matrix safely (MMRelay emote)."""
        ts = datetime.now(timezone.utc)

        # Seed only a Matrix-originated event with inbound ref but NO
        # outbound Meshtastic ref — simulating the case where the original
        # message was never bridged to Meshtastic.
        orig_event = CanonicalEvent(
            event_id="canon-no-mesh-ref",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MATRIX,
            source_transport_id="@sender:server",
            source_channel_id=_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "A message that never went to mesh"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX,
                native_channel_id=_ROOM,
                native_message_id="$no-mesh-ref",
            ),
        )
        await temp_storage.append(orig_event)
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-no-mesh",
                event_id="canon-no-mesh-ref",
                adapter=_MATRIX,
                native_channel_id=_ROOM,
                native_message_id="$no-mesh-ref",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # Build a Meshtastic reaction with replyId that does NOT map to
        # any stored outbound ref.
        packet = _make_mesh_reaction_packet(
            emoji="🎉",
            reply_id=999999,  # No outbound ref for this
            pkt_id=888888,
        )
        reaction_event = _decode_mesh_reaction_packet(
            packet,
            adapter_id=_RADIO_ALPHA,
            longname="LostNode",
        )

        matrix_adapter = FakeMatrixAdapter(adapter_id=_MATRIX, channel=_ROOM)

        route = Route(
            id="alpha-to-matrix-no-ref",
            source=RouteSource(
                adapter=_RADIO_ALPHA,
                event_kinds=("message.reacted",),
                channel="0",
            ),
            targets=[RouteTarget(adapter=_MATRIX, channel=_ROOM)],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MatrixRenderer(
                source_configs={
                    _RADIO_ALPHA: _StubMeshtasticConfig(
                        adapter_id=_RADIO_ALPHA,
                        mmrelay_compatibility=True,
                        meshnet_name=_MESHNET,
                    ),
                },
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MATRIX, "matrix")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MATRIX: matrix_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reaction_event)

            # Pipeline does not crash — delivery succeeds.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Matrix adapter received a payload.
            assert len(matrix_adapter.delivered_payloads) == 1
            payload = matrix_adapter.delivered_payloads[0].payload

            # Safe emote rendering — no crash, valid output.
            assert payload["msgtype"] == "m.emote"
            assert "reacted" in str(payload["body"])

            # The relation target_event_id is unresolved (None) because
            # the replyId has no stored mapping.
            stored = await temp_storage.get(reaction_event.event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            assert stored.relations[0].target_event_id is None
        finally:
            await runner.stop()

    async def test_missing_ref_delivers_safely_to_meshtastic(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Matrix reaction to an event with no Meshtastic outbound ref
        still delivers to Meshtastic safely as descriptive text."""
        ts = datetime.now(timezone.utc)

        # Seed a Matrix-originated event with NO Meshtastic outbound ref.
        orig_event = CanonicalEvent(
            event_id="canon-no-mesh-out",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MATRIX,
            source_transport_id="@sender:server",
            source_channel_id=_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Never bridged to mesh"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX,
                native_channel_id=_ROOM,
                native_message_id="$no-mesh-out",
            ),
        )
        await temp_storage.append(orig_event)
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-no-mesh-out",
                event_id="canon-no-mesh-out",
                adapter=_MATRIX,
                native_channel_id=_ROOM,
                native_message_id="$no-mesh-out",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # Decode a Matrix reaction targeting this event.
        config_mx = _make_matrix_config(adapter_id=_MATRIX)
        codec = MatrixCodec(_MATRIX, config_mx)

        native = _make_matrix_reaction_native(
            emoji="🚀",
            target_event_id="$no-mesh-out",
            event_id="$rxn-no-mesh",
            sender="@rocket:server",
            room_id=_ROOM,
        )
        reaction_event = codec.decode(native, room_id=_ROOM)

        # Route to Meshtastic.
        radio_config = MeshtasticConfig(adapter_id=_RADIO_ALPHA)
        radio_adapter = FakeMeshtasticAdapter(radio_config)

        route = Route(
            id="matrix-to-alpha-no-ref",
            source=RouteSource(
                adapter=_MATRIX,
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_RADIO_ALPHA, channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={_RADIO_ALPHA: radio_config},
            ),
            priority=50,
        )
        rp.register_adapter_platform(_RADIO_ALPHA, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_RADIO_ALPHA: radio_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reaction_event)

            # Pipeline does not crash — delivery succeeds.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Radio adapter received a payload.
            assert len(radio_adapter.delivered_payloads) == 1
            payload = radio_adapter.delivered_payloads[0].payload

            # Safe descriptive rendering.
            text = str(payload["text"])
            assert "reacted" in text
            assert "🚀" in text

            # No reply_id because there is no Meshtastic native ref.
            assert "reply_id" not in payload
        finally:
            await runner.stop()
