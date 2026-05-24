"""Bidirectional Matrix↔Meshtastic reply roundtrip tests.

End-to-end tests verifying that replies resolve through the full pipeline
using SQLite NativeMessageRef mappings stored during a *prior* delivery pass.

Unlike the manually-seeded tests in ``test_meshtastic_relation_mapping.py``
(which insert refs via ``_seed_test_a_state``), these tests flow events
through the pipeline twice: the first pass stores the outbound native ref,
and the second pass resolves the reply through that stored mapping.

Test categories
---------------
Test 1: Matrix → Meshtastic → Matrix roundtrip
    Matrix message delivered to Meshtastic stores an outbound native ref
    (packet ID), then a Meshtastic reply referencing that packet ID resolves
    back to Matrix with the correct ``m.in_reply_to``.

Test 2: Meshtastic → Matrix → Meshtastic roundtrip
    Meshtastic message delivered to Matrix stores an outbound native ref
    (Matrix event ID), then a Matrix reply resolves to Meshtastic with the
    correct ``reply_id`` matching the original packet ID.

Test 3a: Multi-radio cross-target fallback
    Message from ``radio-alpha`` is delivered to Matrix.  A Matrix reply
    routed to ``radio-bravo`` (which has no native ref for the original
    message) falls back safely with no ``reply_id`` — plain text only.

Test 3b: Multi-radio correct target selection
    Matrix message is broadcast to both ``radio-alpha`` and ``radio-bravo``
    (each gets its own packet ID).  A Matrix reply routed to ``radio-bravo``
    selects ``radio-bravo``'s packet ID, not ``radio-alpha``'s.

Test 4: Missing native ref roundtrip fallback
    Matrix reply targeting a completely unknown event (empty storage)
    does not crash and renders as plain text with no ``reply_id``.

All tests use ``FakeMeshtasticAdapter`` / ``FakeMatrixAdapter`` and
deterministic packet / event IDs.  No live services required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from msgspec.structs import replace as _replace

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.packet_classifier import (
    MeshtasticPacketClassifier,
)
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeRef,
)
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage


# ===================================================================
# Shared helpers for source_configs construction
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

# ===================================================================
# Shared helpers
# ===================================================================


def _matrix_config(**overrides: object):
    """Build a minimal MatrixConfig for testing."""
    from medre.config.adapters.matrix import MatrixConfig

    defaults: dict[str, object] = dict(
        adapter_id="mx-test",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok",
    )
    defaults.update(overrides)
    return MatrixConfig(**defaults)  # type: ignore[arg-type]


class _FakeNativeEvent:
    """Minimal Matrix native event object for ``MatrixCodec.decode()``.

    Mimics the attributes that nio ``RoomMessage*`` events expose
    (``.body``, ``.sender``, ``.event_id``, ``.source``, ``.room_id``).
    """

    def __init__(
        self,
        body: str,
        event_id: str,
        sender: str,
        reply_target: str,
        room_id: str,
    ) -> None:
        self.body = body
        self.sender = sender
        self.event_id = event_id
        self.room_id = room_id
        self.source: dict[str, object] = {
            "content": {
                "msgtype": "m.text",
                "body": body,
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": reply_target},
                },
            },
            "event_id": event_id,
            "room_id": room_id,
            "sender": sender,
            "type": "m.room.message",
        }


def _make_mesh_event(
    adapter_id: str,
    channel: int,
    pkt_id: int,
    text: str,
    reply_to_pkt_id: int | None = None,
) -> CanonicalEvent:
    """Create a CanonicalEvent by decoding a Meshtastic packet dict.

    When *reply_to_pkt_id* is provided, the packet includes ``replyId``
    so the codec produces a reply relation.
    """
    config = MeshtasticConfig(adapter_id=adapter_id)
    codec = MeshtasticCodec(adapter_id, config)
    classifier = MeshtasticPacketClassifier(config)

    decoded: dict[str, object] = {
        "portnum": "text_message",
        "text": text,
    }
    if reply_to_pkt_id is not None:
        decoded["replyId"] = reply_to_pkt_id

    packet: dict[str, object] = {
        "fromId": "!meshnode-rt",
        "toId": "",
        "channel": channel,
        "id": pkt_id,
        "decoded": decoded,
    }
    classifier.classify(packet)
    return codec.decode(packet)


def _inject_longname(
    event: CanonicalEvent,
    longname: str = "MeshUser",
    shortname: str = "MU",
) -> CanonicalEvent:
    """Inject longname / shortname into native metadata for renderers."""
    if event.metadata.native is not None:
        data = dict(event.metadata.native.data)
        data["longname"] = longname
        data["shortname"] = shortname
        return _replace(
            event,
            metadata=_replace(
                event.metadata,
                native=_replace(event.metadata.native, data=data),
            ),
        )
    return event


# ===================================================================
# Test 1: Matrix → Meshtastic → Matrix roundtrip
# ===================================================================


class TestMatrixToMeshtasticToMatrixRoundtrip:
    """Matrix message → Meshtastic (stores outbound native ref with packet
    ID), then Meshtastic reply referencing that packet ID resolves back to
    Matrix with the correct ``m.in_reply_to`` pointing to the original
    Matrix event.
    """

    async def test_full_roundtrip(self, temp_storage: SQLiteStorage) -> None:
        MX = "mx-rt1"
        RADIO = "radio-rt1"
        ROOM = "!rt1:server"
        MX_MSG = "$rt1-mx-orig"
        PKT_ID = 11111111
        REPLY_PKT = 22222222
        CANON_ID = "canon-rt1"

        # -- Adapters ---------------------------------------------------
        radio = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=RADIO))
        radio.fake_client._next_id = PKT_ID
        matrix = FakeMatrixAdapter(adapter_id=MX, channel=ROOM)

        # -- Routes (both directions in one runner) ---------------------
        routes = [
            Route(
                id="mx→radio-rt1",
                source=RouteSource(
                    adapter=MX,
                    event_kinds=("message.created",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter=RADIO, channel="0")],
            ),
            Route(
                id="radio→mx-rt1",
                source=RouteSource(
                    adapter=RADIO,
                    event_kinds=("message.created",),
                    channel="0",
                ),
                targets=[RouteTarget(adapter=MX, channel=ROOM)],
            ),
        ]

        # -- Rendering pipeline -----------------------------------------
        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(configs={RADIO: MeshtasticConfig(adapter_id=RADIO)}),
            priority=50,
        )
        rp.register_adapter_platform(RADIO, "meshtastic")
        rp.register(
            MatrixRenderer(
                source_configs={
                    RADIO: _StubMeshtasticConfig(
                        adapter_id=RADIO,
                        mmrelay_compatibility=True,
                        meshnet_name="testnet",
                    ),
                },
            ),
            priority=50,
        )
        rp.register_adapter_platform(MX, "matrix")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=routes),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={RADIO: radio, MX: matrix},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            # ── Pass 1: Matrix → Meshtastic ───────────────────────────
            orig = CanonicalEvent(
                event_id=CANON_ID,
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter=MX,
                source_transport_id="mx-user-1",
                source_channel_id=ROOM,
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "Hello from Matrix"},
                metadata=EventMetadata(),
                source_native_ref=NativeRef(
                    adapter=MX,
                    native_channel_id=ROOM,
                    native_message_id=MX_MSG,
                ),
            )

            outcomes = await runner.handle_ingress(orig)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Radio adapter received the rendered payload.
            assert len(radio.delivered_payloads) == 1

            # Outbound native ref persisted: (RADIO, "0", PKT_ID) → CANON_ID
            outbound = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'outbound'",
                (CANON_ID,),
            )
            assert len(outbound) == 1
            assert outbound[0]["adapter"] == RADIO
            assert outbound[0]["native_message_id"] == str(PKT_ID)

            # Inbound native ref persisted: (MX, ROOM, MX_MSG) → CANON_ID
            assert await temp_storage.resolve_native_ref(MX, ROOM, MX_MSG) == CANON_ID

            # ── Pass 2: Meshtastic reply → Matrix ─────────────────────
            reply_event = _make_mesh_event(
                RADIO, 0, REPLY_PKT, "Replying!", reply_to_pkt_id=PKT_ID
            )
            reply_event = _inject_longname(reply_event)

            outcomes = await runner.handle_ingress(reply_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Matrix adapter received the reply.
            assert len(matrix.delivered_payloads) == 1
            payload = matrix.delivered_payloads[0].payload

            # m.in_reply_to points to the ORIGINAL Matrix event.
            relates_to = payload.get("m.relates_to")
            assert isinstance(relates_to, dict)
            in_reply_to = relates_to.get("m.in_reply_to")
            assert isinstance(in_reply_to, dict)
            assert in_reply_to["event_id"] == MX_MSG

            # Stored reply event has a resolved relation targeting CANON_ID.
            stored = await temp_storage.get(reply_event.event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            rel = stored.relations[0]
            assert rel.relation_type == "reply"
            assert rel.target_event_id == CANON_ID
        finally:
            await runner.stop()


# ===================================================================
# Test 2: Meshtastic → Matrix → Meshtastic roundtrip
# ===================================================================


class TestMeshtasticToMatrixToMeshtasticRoundtrip:
    """Meshtastic message → Matrix (stores outbound native ref with Matrix
    event ID), then a Matrix reply resolves to Meshtastic with the correct
    ``reply_id`` matching the original Meshtastic packet ID.
    """

    async def test_full_roundtrip(self, temp_storage: SQLiteStorage) -> None:
        MX = "mx-rt2"
        RADIO = "radio-rt2"
        ROOM = "!rt2:server"
        MESH_PKT = 33333333

        # -- Adapters ---------------------------------------------------
        radio = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=RADIO))
        radio.fake_client._next_id = 44444444  # For reply delivery
        matrix = FakeMatrixAdapter(adapter_id=MX, channel=ROOM)

        # -- Routes (both directions) -----------------------------------
        routes = [
            Route(
                id="radio→mx-rt2",
                source=RouteSource(
                    adapter=RADIO,
                    event_kinds=("message.created",),
                    channel="0",
                ),
                targets=[RouteTarget(adapter=MX, channel=ROOM)],
            ),
            Route(
                id="mx→radio-rt2",
                source=RouteSource(
                    adapter=MX,
                    event_kinds=("message.created",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter=RADIO, channel="0")],
            ),
        ]

        # -- Rendering pipeline -----------------------------------------
        rp = RenderingPipeline()
        rp.register(
            MatrixRenderer(
                source_configs={
                    RADIO: _StubMeshtasticConfig(
                        adapter_id=RADIO,
                        meshnet_name="testnet",
                    ),
                },
            ),
            priority=50,
        )
        rp.register_adapter_platform(MX, "matrix")
        rp.register(
            MeshtasticRenderer(
                configs={
                    RADIO: MeshtasticConfig(adapter_id=RADIO, radio_relay_prefix="")
                }
            ),
            priority=50,
        )
        rp.register_adapter_platform(RADIO, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=routes),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={RADIO: radio, MX: matrix},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            # ── Pass 1: Meshtastic → Matrix ───────────────────────────
            mesh_event = _make_mesh_event(RADIO, 0, MESH_PKT, "Hello from mesh")
            mesh_event = _inject_longname(mesh_event)
            mesh_canon_id = mesh_event.event_id

            outcomes = await runner.handle_ingress(mesh_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Matrix adapter received the message.
            assert len(matrix.delivered_payloads) == 1

            # Outbound Matrix native ref persisted.
            outbound = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'outbound'",
                (mesh_canon_id,),
            )
            assert len(outbound) == 1
            mx_native_id: str = outbound[0]["native_message_id"]
            assert mx_native_id.startswith("$fake_")

            # Inbound Mesh ref: (RADIO, "0", MESH_PKT) → mesh_canon_id
            assert (
                await temp_storage.resolve_native_ref(RADIO, "0", str(MESH_PKT))
                == mesh_canon_id
            )

            # ── Pass 2: Matrix reply → Meshtastic ─────────────────────
            reply_native = _FakeNativeEvent(
                body="> <@sender:server> Hello from mesh\n\nReply from Matrix",
                event_id="$rt2-mx-reply",
                sender="@replyer:server",
                reply_target=mx_native_id,
                room_id=ROOM,
            )
            mx_codec = MatrixCodec(MX, _matrix_config(adapter_id=MX))
            reply_event = mx_codec.decode(reply_native, room_id=ROOM)

            # Codec strips fallback quoting and creates reply relation.
            assert reply_event.payload["body"] == "Reply from Matrix"
            assert len(reply_event.relations) == 1
            tnref = reply_event.relations[0].target_native_ref
            assert tnref is not None
            assert tnref.native_message_id == mx_native_id

            outcomes = await runner.handle_ingress(reply_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Radio adapter received the reply.
            assert len(radio.delivered_payloads) == 1
            payload = radio.delivered_payloads[0].payload

            # reply_id matches the ORIGINAL Meshtastic packet ID.
            assert payload["reply_id"] == MESH_PKT
            assert payload["text"] == "Reply from Matrix"
        finally:
            await runner.stop()


# ===================================================================
# Test 3a: Multi-radio cross-target fallback
# ===================================================================


class TestMultiRadioCrossTargetFallback:
    """Message from ``radio-alpha`` is delivered to Matrix.  A Matrix reply
    routed to ``radio-bravo`` (which has no native ref for the original
    message) falls back safely — no ``reply_id``, plain text only."""

    async def test_cross_radio_fallback_no_reply_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        MX = "mx-rt3a"
        ALPHA = "radio-alpha"
        BRAVO = "radio-bravo"
        ROOM = "!rt3a:server"
        ALPHA_PKT = 55555555

        # ── Step 1: Meshtastic from alpha → Matrix ────────────────────
        alpha = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=ALPHA))
        alpha.fake_client._next_id = ALPHA_PKT
        matrix = FakeMatrixAdapter(adapter_id=MX, channel=ROOM)

        routes1 = [
            Route(
                id="alpha→mx",
                source=RouteSource(
                    adapter=ALPHA,
                    event_kinds=("message.created",),
                    channel="0",
                ),
                targets=[RouteTarget(adapter=MX, channel=ROOM)],
            ),
        ]

        rp1 = RenderingPipeline()
        rp1.register(
            MatrixRenderer(
                source_configs={
                    ALPHA: _StubMeshtasticConfig(
                        adapter_id=ALPHA,
                        meshnet_name="testnet",
                    ),
                },
            ),
            priority=50,
        )
        rp1.register_adapter_platform(MX, "matrix")
        rp1.register(TextRenderer(), priority=100)

        runner1 = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=routes1),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={ALPHA: alpha, MX: matrix},
                event_bus=EventBus(),
                rendering_pipeline=rp1,
            )
        )
        await runner1.start()

        mesh_event = _make_mesh_event(ALPHA, 0, ALPHA_PKT, "From alpha")
        mesh_event = _inject_longname(mesh_event)
        mesh_canon_id = mesh_event.event_id

        try:
            outcomes = await runner1.handle_ingress(mesh_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"
        finally:
            await runner1.stop()

        # Retrieve the Matrix native event ID from the outbound ref.
        outbound = await temp_storage._read_all(
            "SELECT * FROM native_message_refs "
            "WHERE event_id = ? AND direction = 'outbound'",
            (mesh_canon_id,),
        )
        assert len(outbound) == 1
        mx_native_id: str = outbound[0]["native_message_id"]

        # ── Step 2: Matrix reply → Bravo (different radio) ────────────
        bravo = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=BRAVO))

        routes2 = [
            Route(
                id="mx→bravo",
                source=RouteSource(
                    adapter=MX,
                    event_kinds=("message.created",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter=BRAVO, channel="0")],
            ),
        ]

        rp2 = RenderingPipeline()
        rp2.register(
            MeshtasticRenderer(
                configs={
                    BRAVO: MeshtasticConfig(adapter_id=BRAVO, radio_relay_prefix="")
                }
            ),
            priority=50,
        )
        rp2.register_adapter_platform(BRAVO, "meshtastic")
        rp2.register(TextRenderer(), priority=100)

        runner2 = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=routes2),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={BRAVO: bravo},
                event_bus=EventBus(),
                rendering_pipeline=rp2,
            )
        )
        await runner2.start()

        reply_native = _FakeNativeEvent(
            body="> <@sender:server> From alpha\n\nCross-radio reply",
            event_id="$rt3a-reply",
            sender="@replyer:server",
            reply_target=mx_native_id,
            room_id=ROOM,
        )
        mx_codec = MatrixCodec(MX, _matrix_config(adapter_id=MX))
        reply_event = mx_codec.decode(reply_native, room_id=ROOM)

        try:
            outcomes = await runner2.handle_ingress(reply_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            assert len(bravo.delivered_payloads) == 1
            payload = bravo.delivered_payloads[0].payload

            # Bravo has no native ref → safe fallback, no reply_id.
            assert "reply_id" not in payload
            # Text is delivered as a plain message.
            assert payload["text"] == "Cross-radio reply"
        finally:
            await runner2.stop()


# ===================================================================
# Test 3b: Multi-radio correct target selection
# ===================================================================


class TestMultiRadioCorrectTargetSelection:
    """Matrix message is broadcast to both ``radio-alpha`` and
    ``radio-bravo`` (each gets its own packet ID).  A Matrix reply routed
    to ``radio-bravo`` selects ``radio-bravo``'s packet ID as
    ``reply_id``, not ``radio-alpha``'s.
    """

    async def test_reply_selects_bravo_ref_not_alpha(
        self, temp_storage: SQLiteStorage
    ) -> None:
        MX = "mx-rt3b"
        ALPHA = "radio-alpha-3b"
        BRAVO = "radio-bravo-3b"
        ROOM = "!rt3b:server"
        ALPHA_PKT = 60000001
        BRAVO_PKT = 60000002
        CANON_ID = "canon-rt3b"
        MX_MSG = "$rt3b-orig"

        # ── Step 1: Matrix message → both radios ──────────────────────
        alpha = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=ALPHA))
        alpha.fake_client._next_id = ALPHA_PKT
        bravo = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=BRAVO))
        bravo.fake_client._next_id = BRAVO_PKT

        route1 = Route(
            id="mx→both",
            source=RouteSource(
                adapter=MX,
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter=ALPHA, channel="0"),
                RouteTarget(adapter=BRAVO, channel="0"),
            ],
        )

        rp1 = RenderingPipeline()
        rp1.register(
            MeshtasticRenderer(
                configs={
                    ALPHA: MeshtasticConfig(adapter_id=ALPHA, radio_relay_prefix=""),
                    BRAVO: MeshtasticConfig(adapter_id=BRAVO, radio_relay_prefix=""),
                }
            ),
            priority=50,
        )
        rp1.register_adapter_platform(ALPHA, "meshtastic")
        rp1.register_adapter_platform(BRAVO, "meshtastic")
        rp1.register(TextRenderer(), priority=100)

        runner1 = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[route1]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={ALPHA: alpha, BRAVO: bravo},
                event_bus=EventBus(),
                rendering_pipeline=rp1,
            )
        )
        await runner1.start()

        orig = CanonicalEvent(
            event_id=CANON_ID,
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=MX,
            source_transport_id="mx-user",
            source_channel_id=ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Broadcast msg"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=MX,
                native_channel_id=ROOM,
                native_message_id=MX_MSG,
            ),
        )

        try:
            outcomes = await runner1.handle_ingress(orig)
            assert len(outcomes) == 2  # Delivered to both targets
            assert all(o.status == "success" for o in outcomes)

            # Both radios received the message.
            assert len(alpha.delivered_payloads) == 1
            assert len(bravo.delivered_payloads) == 1
        finally:
            await runner1.stop()

        # Verify outbound refs for BOTH radios.
        refs = await temp_storage._read_all(
            "SELECT * FROM native_message_refs "
            "WHERE event_id = ? AND direction = 'outbound'",
            (CANON_ID,),
        )
        assert len(refs) == 2
        adapters = {r["adapter"] for r in refs}
        assert adapters == {ALPHA, BRAVO}

        # ── Step 2: Matrix reply → Bravo only ─────────────────────────
        route2 = Route(
            id="mx→bravo-only",
            source=RouteSource(
                adapter=MX,
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=BRAVO, channel="0")],
        )

        rp2 = RenderingPipeline()
        rp2.register(
            MeshtasticRenderer(
                configs={
                    BRAVO: MeshtasticConfig(adapter_id=BRAVO, radio_relay_prefix="")
                }
            ),
            priority=50,
        )
        rp2.register_adapter_platform(BRAVO, "meshtastic")
        rp2.register(TextRenderer(), priority=100)

        runner2 = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[route2]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={BRAVO: bravo},
                event_bus=EventBus(),
                rendering_pipeline=rp2,
            )
        )
        await runner2.start()

        # Matrix reply referencing the original Matrix message.
        reply_native = _FakeNativeEvent(
            body="> <@mx-user:server> Broadcast msg\n\nReply to bravo",
            event_id="$rt3b-reply",
            sender="@replyer:server",
            reply_target=MX_MSG,
            room_id=ROOM,
        )
        mx_codec = MatrixCodec(MX, _matrix_config(adapter_id=MX))
        reply_event = mx_codec.decode(reply_native, room_id=ROOM)

        try:
            outcomes = await runner2.handle_ingress(reply_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # bravo.delivered_payloads now has 2 items (step 1 + step 2).
            assert len(bravo.delivered_payloads) == 2
            reply_payload = bravo.delivered_payloads[1].payload

            # reply_id is BRAVO's packet ID, not ALPHA's.
            assert reply_payload["reply_id"] == BRAVO_PKT
            assert reply_payload["text"] == "Reply to bravo"
        finally:
            await runner2.stop()


# ===================================================================
# Test 4: Missing native ref roundtrip fallback
# ===================================================================


class TestMissingNativeRefRoundtrip:
    """Matrix reply targeting a completely unknown event (empty storage)
    does not crash.  The Meshtastic renderer produces plain text with no
    ``reply_id``.
    """

    async def test_no_crash_empty_storage(self, temp_storage: SQLiteStorage) -> None:
        MX = "mx-rt4"
        RADIO = "radio-rt4"
        ROOM = "!rt4:server"

        radio = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=RADIO))

        route = Route(
            id="mx→radio-rt4",
            source=RouteSource(
                adapter=MX,
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=RADIO, channel="0")],
        )

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={
                    RADIO: MeshtasticConfig(adapter_id=RADIO, radio_relay_prefix="")
                }
            ),
            priority=50,
        )
        rp.register_adapter_platform(RADIO, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[route]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={RADIO: radio},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        # Matrix reply with a completely unknown reply target.
        reply_native = _FakeNativeEvent(
            body="> <@sender:server> unknown\n\nOrphan reply",
            event_id="$rt4-reply",
            sender="@replyer:server",
            reply_target="$nonexistent-event",
            room_id=ROOM,
        )
        mx_codec = MatrixCodec(MX, _matrix_config(adapter_id=MX))
        reply_event = mx_codec.decode(reply_native, room_id=ROOM)

        try:
            outcomes = await runner.handle_ingress(reply_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            assert len(radio.delivered_payloads) == 1
            payload = radio.delivered_payloads[0].payload

            # No crash, no reply_id, plain text only.
            assert "reply_id" not in payload
            assert payload["text"] == "Orphan reply"
        finally:
            await runner.stop()
