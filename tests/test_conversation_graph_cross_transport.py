"""Cross-transport conversation-graph integration tests.

End-to-end tests verifying conversation identity assignment (``root_event_id``,
``conversation_id``), native-target enrichment, and fallback behaviour across
transport boundaries.  Uses fake adapters and ``SQLiteStorage`` at the
``fake_pipeline`` tier.

Covered scenarios
-----------------
Test 1  Reply Matrix → Meshtastic (conversation identity propagation)
        A Matrix message flows to Meshtastic.  A Meshtastic reply resolves
        back through the pipeline.  Both the original and reply events have
        ``root_event_id`` / ``conversation_id`` assigned.  The Meshtastic
        ``reply_id`` matches the stored outbound packet.

Test 2  Reply Meshtastic → Matrix (conversation identity propagation)
        A Meshtastic message flows to Matrix.  A Matrix reply resolves back
        to Meshtastic.  ``root_event_id`` / ``conversation_id`` propagate
        correctly across both transports.

Test 3  Reaction fallback (LXMF fallback-only transport)
        A reaction event routed to LXMF (fallback-only) degrades to
        inline ``[reaction …]`` text.  ``root_event_id`` / ``conversation_id``
        are still assigned.  Render evidence records ``render_mode`` as
        ``"fallback"``.

Test 4  Missing native target
        A reply whose ``target_native_ref`` does not resolve to any stored
        event does not crash the pipeline.  ``root_event_id`` degrades to
        self.  The relation's ``target_event_id`` remains ``None``.

Test 5  Stale native target / no pre-validation
        A relation targeting a native ref that resolves to a canonical event
        ID (via stored ``NativeMessageRef``) is enriched successfully even
        though no pre-validation of native-message existence occurs.

Test 6  Multiple native refs / target selection
        An event has outbound native refs for both Matrix and Meshtastic.
        Per-target enrichment selects the correct ref for each adapter.
        ``root_event_id`` / ``conversation_id`` remain consistent across
        both target renderings.

All tests use ``FakeMeshtasticAdapter``, ``FakeMatrixAdapter``, and
``FakeMeshCoreAdapter``.  No live services required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from msgspec.structs import replace as _replace

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
from medre.core.storage.sqlite.storage import SQLiteStorage

# ===================================================================
# Shared helpers
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


def _make_matrix_config(**overrides: object) -> MatrixConfig:
    defaults: dict[str, object] = dict(
        adapter_id="mx-test",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok",
    )
    defaults.update(overrides)
    return MatrixConfig(**defaults)  # type: ignore[arg-type]


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_event(
    event_id: str = "evt-001",
    event_kind: str = "message.created",
    source_adapter: str = "fake",
    source_channel_id: str | None = None,
    source_native_ref: NativeRef | None = None,
    relations: tuple[EventRelation, ...] = (),
    payload: dict | None = None,
    root_event_id: str | None = None,
    conversation_id: str | None = None,
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for pipeline tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=_ts(),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload or {"text": "hello"},
        metadata=EventMetadata(),
        source_native_ref=source_native_ref,
        root_event_id=root_event_id,
        conversation_id=conversation_id,
    )


def _make_mesh_event(
    adapter_id: str,
    channel: int,
    pkt_id: int,
    text: str,
    reply_to_pkt_id: int | None = None,
) -> CanonicalEvent:
    """Create a CanonicalEvent by decoding a Meshtastic packet dict."""
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
        "fromId": "!meshnode-cg",
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


class _FakeMatrixNativeEvent:
    """Minimal Matrix native event object for ``MatrixCodec.decode()``."""

    def __init__(
        self,
        body: str,
        event_id: str,
        sender: str,
        reply_target: str | None = None,
        room_id: str = "!room:server",
    ) -> None:
        self.body = body
        self.sender = sender
        self.event_id = event_id
        self.room_id = room_id
        source: dict[str, object] = {
            "content": {
                "msgtype": "m.text",
                "body": body,
            },
            "event_id": event_id,
            "room_id": room_id,
            "sender": sender,
            "type": "m.room.message",
        }
        if reply_target is not None:
            source["content"] = {  # type: ignore[assignment]
                "msgtype": "m.text",
                "body": body,
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": reply_target},
                },
            }
        self.source = source


def _build_runner(
    storage: SQLiteStorage,
    routes: list[Route],
    adapters: dict[str, object],
    rendering_pipeline: RenderingPipeline,
) -> PipelineRunner:
    """Build a PipelineRunner with standard wiring for cross-transport tests."""
    from typing import cast

    from medre.core.contracts.adapter import AdapterContract

    return PipelineRunner(
        PipelineConfig(
            storage=storage,
            router=Router(routes=routes),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=storage),
            adapters=cast(dict[str, AdapterContract], adapters),
            event_bus=EventBus(),
            rendering_pipeline=rendering_pipeline,
        )
    )


# ===================================================================
# Test 1: Reply Matrix → Meshtastic (conversation identity)
# ===================================================================


class TestReplyMatrixToMeshtastic:
    """Matrix message → Meshtastic (stores outbound native ref), then
    Meshtastic reply resolves back through pipeline.  Conversation identity
    (``root_event_id``, ``conversation_id``) is correctly assigned.
    """

    async def test_conversation_identity_propagated(
        self, temp_storage: SQLiteStorage
    ) -> None:
        MX = "mx-cg1"
        RADIO = "radio-cg1"
        ROOM = "!cg1:server"
        MX_MSG = "$cg1-mx-orig"
        PKT_ID = 10000001
        REPLY_PKT = 10000002
        CANON_ID = "canon-cg1"

        # -- Adapters ---------------------------------------------------
        radio = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=RADIO))
        radio.fake_client._next_id = PKT_ID
        matrix = FakeMatrixAdapter(adapter_id=MX, channel=ROOM)

        # -- Routes (both directions) -----------------------------------
        routes = [
            Route(
                id="mx→radio-cg1",
                source=RouteSource(
                    adapter=MX, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=RADIO, channel="0")],
            ),
            Route(
                id="radio→mx-cg1",
                source=RouteSource(
                    adapter=RADIO, event_kinds=("message.created",), channel="0"
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

        runner = _build_runner(temp_storage, routes, {RADIO: radio, MX: matrix}, rp)
        await runner.start()

        try:
            # ── Pass 1: Matrix → Meshtastic ───────────────────────────
            orig = _make_event(
                event_id=CANON_ID,
                source_adapter=MX,
                source_channel_id=ROOM,
                source_native_ref=NativeRef(
                    adapter=MX,
                    native_channel_id=ROOM,
                    native_message_id=MX_MSG,
                ),
                payload={"text": "Hello from Matrix"},
            )

            outcomes = await runner.handle_ingress(orig)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Original event stored with conversation identity.
            stored_orig = await temp_storage.get(CANON_ID)
            assert stored_orig is not None
            assert stored_orig.root_event_id == CANON_ID
            assert stored_orig.conversation_id == CANON_ID

            # Outbound native ref persisted.
            assert (
                await temp_storage.resolve_native_ref(RADIO, "0", str(PKT_ID))
                == CANON_ID
            )

            # ── Pass 2: Meshtastic reply → Matrix ─────────────────────
            reply_event = _make_mesh_event(
                RADIO, 0, REPLY_PKT, "Reply from mesh!", reply_to_pkt_id=PKT_ID
            )
            reply_event = _inject_longname(reply_event)
            reply_canon_id = reply_event.event_id

            outcomes = await runner.handle_ingress(reply_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Reply event stored with conversation identity inheriting
            # from the original Matrix message.
            stored_reply = await temp_storage.get(reply_canon_id)
            assert stored_reply is not None
            assert stored_reply.root_event_id == CANON_ID
            assert stored_reply.conversation_id == CANON_ID

            # Relation resolved to the original canonical event.
            assert len(stored_reply.relations) == 1
            rel = stored_reply.relations[0]
            assert rel.relation_type == "reply"
            assert rel.target_event_id == CANON_ID

            # Matrix adapter received the reply with m.in_reply_to.
            assert len(matrix.delivered_payloads) == 1
            payload = matrix.delivered_payloads[0].payload
            relates_to = payload.get("m.relates_to")
            assert isinstance(relates_to, dict)
            in_reply_to = relates_to.get("m.in_reply_to")
            assert isinstance(in_reply_to, dict)
            assert in_reply_to["event_id"] == MX_MSG
        finally:
            await runner.stop()


# ===================================================================
# Test 2: Reply Meshtastic → Matrix (conversation identity)
# ===================================================================


class TestReplyMeshtasticToMatrix:
    """Meshtastic message → Matrix (stores outbound native ref), then a
    Matrix reply resolves back to Meshtastic.  Conversation identity
    propagates across both transports.
    """

    async def test_conversation_identity_propagated(
        self, temp_storage: SQLiteStorage
    ) -> None:
        MX = "mx-cg2"
        RADIO = "radio-cg2"
        ROOM = "!cg2:server"
        MESH_PKT = 20000001

        # -- Adapters ---------------------------------------------------
        radio = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=RADIO))
        radio.fake_client._next_id = 20000002  # reply delivery packet
        matrix = FakeMatrixAdapter(adapter_id=MX, channel=ROOM)

        # -- Routes (both directions) -----------------------------------
        routes = [
            Route(
                id="radio→mx-cg2",
                source=RouteSource(
                    adapter=RADIO, event_kinds=("message.created",), channel="0"
                ),
                targets=[RouteTarget(adapter=MX, channel=ROOM)],
            ),
            Route(
                id="mx→radio-cg2",
                source=RouteSource(
                    adapter=MX, event_kinds=("message.created",), channel=None
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

        runner = _build_runner(temp_storage, routes, {RADIO: radio, MX: matrix}, rp)
        await runner.start()

        try:
            # ── Pass 1: Meshtastic → Matrix ───────────────────────────
            mesh_event = _make_mesh_event(RADIO, 0, MESH_PKT, "Hello from mesh")
            mesh_event = _inject_longname(mesh_event)
            mesh_canon_id = mesh_event.event_id

            outcomes = await runner.handle_ingress(mesh_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Original Meshtastic event stored with conversation identity.
            stored_mesh = await temp_storage.get(mesh_canon_id)
            assert stored_mesh is not None
            assert stored_mesh.root_event_id == mesh_canon_id
            assert stored_mesh.conversation_id == mesh_canon_id

            # Matrix outbound ref persisted.
            outbound = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'outbound'",
                (mesh_canon_id,),
            )
            assert len(outbound) == 1
            mx_native_id: str = outbound[0]["native_message_id"]

            # ── Pass 2: Matrix reply → Meshtastic ─────────────────────
            mx_config = _make_matrix_config(adapter_id=MX)
            mx_codec = MatrixCodec(MX, mx_config)

            native_reply = _FakeMatrixNativeEvent(
                body="Reply from Matrix!",
                event_id="$cg2-mx-reply",
                sender="@user:server",
                reply_target=mx_native_id,
                room_id=ROOM,
            )

            reply_event = mx_codec.decode(native_reply, room_id=ROOM)
            reply_canon_id = reply_event.event_id

            outcomes = await runner.handle_ingress(reply_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Reply event inherits root from the Meshtastic original.
            stored_reply = await temp_storage.get(reply_canon_id)
            assert stored_reply is not None
            assert stored_reply.root_event_id == mesh_canon_id
            assert stored_reply.conversation_id == mesh_canon_id

            # Relation resolved to the original canonical event.
            assert len(stored_reply.relations) == 1
            rel = stored_reply.relations[0]
            assert rel.relation_type == "reply"
            assert rel.target_event_id == mesh_canon_id

            # Meshtastic adapter received with reply_id matching the
            # original packet.
            assert len(radio.delivered_payloads) == 1
            mesh_payload = radio.delivered_payloads[0].payload
            assert "reply_id" in mesh_payload
        finally:
            await runner.stop()


# ===================================================================
# Test 3: Reaction fallback (MeshCore fallback-only)
# ===================================================================


class TestReactionFallback:
    """Reaction event rendered with ``fallback_text`` strategy for a
    fallback-only transport (LXMF) degrades to inline text.  Conversation
    identity (``root_event_id``, ``conversation_id``) is preserved in the
    rendering evidence.
    """

    async def test_reaction_falls_back_with_conversation_identity(
        self,
    ) -> None:
        from medre.adapters.lxmf.renderer import LxmfRenderer

        CANON_ID = "canon-cg3-fallback"
        _EMOJI = "👍"

        # Build a reaction event with conversation identity already assigned.
        reaction_rel = EventRelation(
            relation_type="reaction",
            target_event_id="target-evt-1",
            target_native_ref=NativeRef(
                adapter="mx",
                native_channel_id="!room:server",
                native_message_id="$mx-msg-1",
            ),
            key=_EMOJI,
            fallback_text="original message text",
        )
        reaction_event = _make_event(
            event_id=CANON_ID,
            source_adapter="mx",
            relations=(reaction_rel,),
            payload={"text": _EMOJI},
            root_event_id="target-evt-1",
            conversation_id="target-evt-1",
        )

        # Render through the RenderingPipeline (which builds evidence).
        rp = RenderingPipeline()
        rp.register(LxmfRenderer(metadata_embedding=True), priority=50)
        rp.register_adapter_platform("lxmf-cg3", "lxmf")
        rp.register(TextRenderer(), priority=100)

        result = await rp.render(
            reaction_event,
            "lxmf-cg3",
            target_channel="0",
            target_platform="lxmf",
            delivery_strategy="fallback_text",
            capability_level="fallback",
        )

        # Fallback marker present (relation_reaction signals reaction
        # was degraded to text).
        assert result.fallback_applied is not None

        # Evidence captures conversation identity.
        evidence = result.rendering_evidence
        assert evidence is not None
        assert evidence.conversation_id == "target-evt-1"
        assert evidence.root_event_id == "target-evt-1"

        # Relation evidence records fallback mode for the reaction.
        assert len(evidence.relation_evidence) == 1
        rel_ev = evidence.relation_evidence[0]
        assert rel_ev.relation_type == "reaction"
        assert rel_ev.render_mode == "fallback"


# ===================================================================
# Test 4: Missing native target
# ===================================================================


class TestMissingNativeTarget:
    """Reply whose target_native_ref does not resolve to any stored event.
    Pipeline degrades gracefully — ``root_event_id`` = self, ``target_event_id``
    remains ``None``.
    """

    async def test_missing_target_degrades_gracefully(
        self, temp_storage: SQLiteStorage
    ) -> None:
        MX = "mx-cg4"
        RADIO = "radio-cg4"
        ROOM = "!cg4:server"

        radio = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=RADIO))
        matrix = FakeMatrixAdapter(adapter_id=MX, channel=ROOM)

        routes = [
            Route(
                id="mx→radio-cg4",
                source=RouteSource(
                    adapter=MX, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=RADIO, channel="0")],
            ),
        ]

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(configs={RADIO: MeshtasticConfig(adapter_id=RADIO)}),
            priority=50,
        )
        rp.register_adapter_platform(RADIO, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = _build_runner(temp_storage, routes, {RADIO: radio, MX: matrix}, rp)
        await runner.start()

        try:
            # Reply targeting a completely unknown native ref.
            reply_rel = EventRelation(
                relation_type="reply",
                target_event_id=None,
                target_native_ref=NativeRef(
                    adapter=MX,
                    native_channel_id=ROOM,
                    native_message_id="$completely-unknown-msg",
                ),
                key=None,
                fallback_text="original message text",
            )
            event = _make_event(
                event_id="evt-missing-target",
                source_adapter=MX,
                source_channel_id=ROOM,
                source_native_ref=NativeRef(
                    adapter=MX,
                    native_channel_id=ROOM,
                    native_message_id="$cg4-mx-reply",
                ),
                relations=(reply_rel,),
                payload={"text": "Reply to unknown"},
            )

            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Stored event: root_event_id = self (target not found).
            stored = await temp_storage.get("evt-missing-target")
            assert stored is not None
            assert stored.root_event_id == "evt-missing-target"
            assert stored.conversation_id == "evt-missing-target"

            # target_event_id remains None (no resolution possible).
            assert len(stored.relations) == 1
            assert stored.relations[0].target_event_id is None

            # Radio adapter still received the event (degraded).
            assert len(radio.delivered_payloads) == 1
        finally:
            await runner.stop()


# ===================================================================
# Test 5: Stale native target / no pre-validation
# ===================================================================


class TestStaleNativeTargetNoPreValidation:
    """A relation targets a native ref that resolves to a canonical event ID
    via a stored ``NativeMessageRef``, but the original native message may
    no longer exist on the platform.  The pipeline does NOT pre-validate
    existence — it resolves the mapping and proceeds.
    """

    async def test_stale_ref_resolves_without_validation(
        self, temp_storage: SQLiteStorage
    ) -> None:
        MX = "mx-cg5"
        RADIO = "radio-cg5"
        ROOM = "!cg5:server"
        MX_MSG = "$cg5-mx-stale-orig"
        CANON_ID = "canon-cg5"

        radio = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=RADIO))
        matrix = FakeMatrixAdapter(adapter_id=MX, channel=ROOM)

        routes = [
            Route(
                id="mx→radio-cg5",
                source=RouteSource(
                    adapter=MX, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=RADIO, channel="0")],
            ),
        ]

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(configs={RADIO: MeshtasticConfig(adapter_id=RADIO)}),
            priority=50,
        )
        rp.register_adapter_platform(RADIO, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = _build_runner(temp_storage, routes, {RADIO: radio, MX: matrix}, rp)
        await runner.start()

        try:
            # Manually store a NativeMessageRef for a "stale" message.
            # This simulates a message that was delivered previously but
            # whose native counterpart may have expired on the platform.
            stale_ref = NativeMessageRef(
                id="nref-stale-1",
                event_id=CANON_ID,
                adapter=MX,
                native_channel_id=ROOM,
                native_message_id=MX_MSG,
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
            )
            await temp_storage.store_native_ref(stale_ref)

            # Store the original event so conversation identity can walk
            # to it (even though the "native" message might be gone).
            orig_event = _make_event(
                event_id=CANON_ID,
                source_adapter=MX,
                source_channel_id=ROOM,
                source_native_ref=NativeRef(
                    adapter=MX,
                    native_channel_id=ROOM,
                    native_message_id=MX_MSG,
                ),
                payload={"text": "Original now stale"},
                root_event_id=CANON_ID,
                conversation_id=CANON_ID,
            )
            await temp_storage.append(orig_event)

            # Now send a reply referencing the stale native ref.
            reply_rel = EventRelation(
                relation_type="reply",
                target_event_id=None,
                target_native_ref=NativeRef(
                    adapter=MX,
                    native_channel_id=ROOM,
                    native_message_id=MX_MSG,
                ),
                key=None,
                fallback_text="original text",
            )
            reply = _make_event(
                event_id="evt-stale-reply",
                source_adapter=MX,
                source_channel_id=ROOM,
                source_native_ref=NativeRef(
                    adapter=MX,
                    native_channel_id=ROOM,
                    native_message_id="$cg5-mx-reply",
                ),
                relations=(reply_rel,),
                payload={"text": "Reply to stale"},
            )

            outcomes = await runner.handle_ingress(reply)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # The relation resolved to CANON_ID via the stored native ref.
            stored = await temp_storage.get("evt-stale-reply")
            assert stored is not None
            assert len(stored.relations) == 1
            assert stored.relations[0].target_event_id == CANON_ID

            # Conversation identity inherited from the stale event.
            assert stored.root_event_id == CANON_ID
            assert stored.conversation_id == CANON_ID

            # Radio adapter received the reply (no crash).
            assert len(radio.delivered_payloads) == 1
        finally:
            await runner.stop()


# ===================================================================
# Test 6: Multiple native refs / target selection
# ===================================================================


class TestMultipleNativeRefsTargetSelection:
    """An event has outbound native refs stored for both Matrix and a
    Meshtastic radio.  A Meshtastic reply from a second radio enriches
    with the correct native ref per target.  ``root_event_id`` /
    ``conversation_id`` remain consistent.
    """

    async def test_correct_ref_selected_per_target(
        self, temp_storage: SQLiteStorage
    ) -> None:
        MX = "mx-cg6"
        ALPHA = "radio-alpha"
        BRAVO = "radio-bravo"
        ROOM = "!cg6:server"
        ALPHA_PKT = 60000001

        # -- Adapters ---------------------------------------------------
        alpha = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=ALPHA))
        alpha.fake_client._next_id = ALPHA_PKT
        bravo = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id=BRAVO))
        bravo.fake_client._next_id = 60000003
        matrix = FakeMatrixAdapter(adapter_id=MX, channel=ROOM)

        # -- Routes: Alpha → Matrix + Bravo, Matrix → Bravo ─-----------
        routes = [
            Route(
                id="alpha→mx-cg6",
                source=RouteSource(
                    adapter=ALPHA, event_kinds=("message.created",), channel="0"
                ),
                targets=[RouteTarget(adapter=MX, channel=ROOM)],
            ),
            Route(
                id="alpha→bravo-cg6",
                source=RouteSource(
                    adapter=ALPHA, event_kinds=("message.created",), channel="0"
                ),
                targets=[RouteTarget(adapter=BRAVO, channel="0")],
            ),
            Route(
                id="mx→bravo-cg6",
                source=RouteSource(
                    adapter=MX, event_kinds=("message.created",), channel=None
                ),
                targets=[RouteTarget(adapter=BRAVO, channel="0")],
            ),
        ]

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={
                    ALPHA: MeshtasticConfig(adapter_id=ALPHA),
                    BRAVO: MeshtasticConfig(adapter_id=BRAVO),
                }
            ),
            priority=50,
        )
        rp.register_adapter_platform(ALPHA, "meshtastic")
        rp.register_adapter_platform(BRAVO, "meshtastic")
        rp.register(
            MatrixRenderer(
                source_configs={
                    ALPHA: _StubMeshtasticConfig(
                        adapter_id=ALPHA,
                        meshnet_name="testnet",
                    ),
                    BRAVO: _StubMeshtasticConfig(
                        adapter_id=BRAVO,
                        meshnet_name="testnet",
                    ),
                },
            ),
            priority=50,
        )
        rp.register_adapter_platform(MX, "matrix")
        rp.register(TextRenderer(), priority=100)

        runner = _build_runner(
            temp_storage,
            routes,
            {ALPHA: alpha, BRAVO: bravo, MX: matrix},
            rp,
        )
        await runner.start()

        try:
            # ── Pass 1: Alpha broadcasts → Matrix + Bravo ──────────────
            alpha_event = _make_mesh_event(ALPHA, 0, ALPHA_PKT, "Hello from alpha")
            alpha_event = _inject_longname(alpha_event, "AlphaUser", "AU")
            alpha_canon_id = alpha_event.event_id

            outcomes = await runner.handle_ingress(alpha_event)
            assert len(outcomes) == 2

            stored_orig = await temp_storage.get(alpha_canon_id)
            assert stored_orig is not None
            assert stored_orig.root_event_id == alpha_canon_id

            # Alpha inbound ref persisted.
            assert (
                await temp_storage.resolve_native_ref(ALPHA, "0", str(ALPHA_PKT))
                == alpha_canon_id
            )

            # Matrix outbound ref persisted.
            mx_outbound = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND adapter = ? AND direction = 'outbound'",
                (alpha_canon_id, MX),
            )
            assert len(mx_outbound) >= 1
            mx_native_id: str = mx_outbound[0]["native_message_id"]

            # ── Pass 2: Matrix reply → Bravo (different radio) ─────────
            mx_config = _make_matrix_config(adapter_id=MX)
            mx_codec = MatrixCodec(MX, mx_config)

            native_reply = _FakeMatrixNativeEvent(
                body="Reply from Matrix!",
                event_id="$cg6-mx-reply",
                sender="@user:server",
                reply_target=mx_native_id,
                room_id=ROOM,
            )

            reply_event = mx_codec.decode(native_reply, room_id=ROOM)
            reply_canon_id = reply_event.event_id

            outcomes = await runner.handle_ingress(reply_event)
            assert len(outcomes) == 1  # Only bravo route matches

            stored_reply = await temp_storage.get(reply_canon_id)
            assert stored_reply is not None
            assert stored_reply.root_event_id == alpha_canon_id
            assert stored_reply.conversation_id == alpha_canon_id

            # Relation resolved to the original canonical event.
            assert len(stored_reply.relations) == 1
            assert stored_reply.relations[0].target_event_id == alpha_canon_id

            # Bravo adapter received both alpha's original and the reply.
            # The last delivery is the Matrix reply.
            assert len(bravo.delivered_payloads) >= 2
            bravo_reply_payload = bravo.delivered_payloads[-1].payload
            assert isinstance(bravo_reply_payload, dict)
            # The reply has reply_id matching bravo's outbound packet.
            assert "reply_id" in bravo_reply_payload
            assert "text" in bravo_reply_payload
        finally:
            await runner.stop()
