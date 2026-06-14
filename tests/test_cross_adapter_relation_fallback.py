"""W6 cross-adapter relation fallback integration tests.

End-to-end tests verifying that native relations degrade correctly when
routed to transports that lack native support for replies or reactions.
Uses fake adapters and renderers at the ``fake_pipeline`` tier.

Covered scenarios
-----------------
Test 1  Matrix → MeshCore reply fallback
        A Matrix reply event rendered with ``fallback_text`` strategy
        produces inline ``[reply to: …]`` text via
        :class:`MeshCoreRenderer`.  No native relation fields (no
        ``reply_id``) appear in the MeshCore payload.

Test 2  Meshtastic → LXMF reaction fallback
        A Meshtastic reaction event rendered with ``fallback_text``
        strategy produces inline ``[reaction … to: …]`` text via
        :class:`LxmfRenderer`.  The LXMF fields envelope carries an
        **empty** relations list (no native LXMF relation activation).
        The ``0xFD`` envelope is present but free of structured relations.

Test 3  Missing native ref → cross-adapter graceful degradation
        An event whose relation targets a completely unmapped native ref
        (empty storage) is routed through the pipeline to MeshCore.  The
        pipeline does not crash; the event delivers with degraded fallback
        text and no malformed native relation fields.

Test 4  Meshtastic → Matrix MMRelay emote fallback verification
        A Meshtastic reaction event resolves through the pipeline so that
        the stored relation's ``target_event_id`` points to the correct
        canonical event and the Matrix output carries MMRelay-compatible
        emote metadata (``meshtastic_replyId``, ``meshtastic_emoji``)
        rather than a native ``m.annotation`` relation.

Tests 1–3 verify **new** cross-adapter fallback paths not covered by
``test_reply_roundtrip.py`` or ``test_reaction_roundtrip.py``.  Test 4
provides a focused verification of the Meshtastic→Matrix reaction
resolution (the full roundtrip already exists in
``test_reaction_roundtrip.py``).

All tests use ``FakeMeshCoreAdapter``, ``FakeLxmfAdapter``,
``FakeMatrixAdapter``, and ``FakeMeshtasticAdapter``.  No live services
required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.lxmf.fields import (
    FIELD_MEDRE_ENVELOPE,
    LXMF_NAMESPACE,
)
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingContext, RenderingPipeline
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
        matrix_relay_prefix: str = "",
        mmrelay_compatibility: bool = False,
    ) -> None:
        self.adapter_id = adapter_id
        self.matrix_relay_prefix = matrix_relay_prefix
        self.mmrelay_compatibility = mmrelay_compatibility


def _make_matrix_config(**overrides):
    defaults = dict(
        adapter_id="matrix-test",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok",
    )
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_reply_native_event(
    body: str,
    event_id: str,
    sender: str,
    reply_target: str,
    room_id: str,
):
    """Build a minimal native event object for a Matrix reply."""

    class _Fake:
        pass

    evt = _Fake()
    evt.body = body
    evt.sender = sender
    evt.event_id = event_id
    evt.source = {
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
    evt.room_id = room_id
    return evt


def _make_mesh_event(
    adapter_id: str,
    channel: int,
    pkt_id: int,
    text: str,
    reply_to_pkt_id: int | None = None,
    emoji: int | None = None,
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
    if emoji is not None:
        decoded["emoji"] = emoji

    packet: dict[str, object] = {
        "fromId": "!meshnode-w6",
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
    from msgspec.structs import replace as _replace

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
# Test 1: Matrix → MeshCore reply fallback
# ===================================================================


class TestMatrixToMeshCoreReplyFallback:
    """Matrix reply event rendered to MeshCore with fallback_text strategy
    produces inline degraded text and no native relation fields."""

    async def test_fallback_text_renders_inline_no_native_fields(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """End-to-end: Matrix reply event flows through pipeline to
        MeshCore adapter.  Because MeshCore replies='unsupported', the
        capability decision yields 'skip'.  We verify the renderer
        fallback_text path directly: inline '[reply to: …]' text, no
        reply_id or relation-specific fields in the MeshCore payload."""
        _MX = "mx-w6-1"
        _MC = "mc-w6-1"
        _ROOM = "!w6-1:server"
        _CANON_ID = "canon-w6-1"
        _MX_MSG = "$w6-1-orig"
        _MX_REPLY = "$w6-1-reply"

        # -- Seed: store a prior canonical event with native ref ------
        ts = datetime.now(timezone.utc)
        orig_event = CanonicalEvent(
            event_id=_CANON_ID,
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MX,
            source_transport_id="@orig:server",
            source_channel_id=_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Original message on Matrix"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MX,
                native_channel_id=_ROOM,
                native_message_id=_MX_MSG,
            ),
        )
        await temp_storage.append(orig_event)
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-w6-1-in",
                event_id=_CANON_ID,
                adapter=_MX,
                native_channel_id=_ROOM,
                native_message_id=_MX_MSG,
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # -- Decode a Matrix reply ------------------------------------
        config_mx = _make_matrix_config(adapter_id=_MX)
        codec = MatrixCodec(_MX, config_mx)

        reply_native = _make_reply_native_event(
            body="> <@orig:server> Original message on Matrix\n\nReply here",
            event_id=_MX_REPLY,
            sender="@replier:server",
            reply_target=_MX_MSG,
            room_id=_ROOM,
        )
        reply_event = codec.decode(reply_native, room_id=_ROOM)

        assert reply_event.payload["body"] == "Reply here"
        assert len(reply_event.relations) == 1
        rel = reply_event.relations[0]
        assert rel.relation_type == "reply"

        # -- Render via MeshCoreRenderer with fallback_text strategy --
        mc_config = MeshCoreConfig(adapter_id=_MC)
        renderer = MeshCoreRenderer(configs={_MC: mc_config})

        ctx = RenderingContext(
            target_adapter=_MC,
            target_channel="0",
            target_platform="meshcore",
            delivery_strategy="fallback_text",
        )

        result = await renderer.render(reply_event, ctx)
        payload = result.payload

        # -- Assertions ------------------------------------------------
        # Text contains inline degraded reply reference.
        text = str(payload["text"])
        assert "[reply to:" in text
        assert "Reply here" in text

        # MeshCore payload has standard fields only — no relation fields.
        assert "text" in payload
        assert "channel_index" in payload
        assert "reply_id" not in payload
        assert "emoji" not in payload

        # Fallback marker present.
        assert result.fallback_applied == "strategy_fallback_text"


# ===================================================================
# Test 2: Meshtastic → LXMF reaction fallback
# ===================================================================


class TestMeshtasticToLxmfReactionFallback:
    """Meshtastic reaction event rendered to LXMF with fallback_text
    strategy produces inline degraded text and the LXMF fields envelope
    has **empty** relations — no native LXMF relation activation."""

    async def test_fallback_text_and_empty_envelope_relations(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """End-to-end: Meshtastic reaction event rendered via LxmfRenderer
        with fallback_text strategy.  The content has inline reaction text.
        The 0xFD envelope exists (metadata_embedding=True) but carries
        an empty relations list.  No native LXMF FIELD_THREAD activation."""
        _RADIO = "radio-w6-2"
        _LXMF = "lxmf-w6-2"
        _EMOJI = "👍"
        _PKT_ID = 77000001
        _REACTION_PKT = 77000002
        _CANON_ID = "canon-w6-2"

        # -- Seed: store a prior canonical event with Meshtastic native ref
        ts = datetime.now(timezone.utc)
        orig_event = CanonicalEvent(
            event_id=_CANON_ID,
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_RADIO,
            source_transport_id="!meshnode-w6-2",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Hello from mesh"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_RADIO,
                native_channel_id="0",
                native_message_id=str(_PKT_ID),
            ),
        )
        await temp_storage.append(orig_event)
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-w6-2-in",
                event_id=_CANON_ID,
                adapter=_RADIO,
                native_channel_id="0",
                native_message_id=str(_PKT_ID),
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # -- Decode Meshtastic reaction packet -------------------------
        config_mesh = MeshtasticConfig(adapter_id=_RADIO)
        codec = MeshtasticCodec(_RADIO, config_mesh)
        classifier = MeshtasticPacketClassifier(config_mesh)

        packet: dict[str, object] = {
            "fromId": "!meshnode-w6-2",
            "toId": "",
            "channel": 0,
            "id": _REACTION_PKT,
            "decoded": {
                "portnum": "text_message",
                "text": _EMOJI,
                "replyId": _PKT_ID,
                "emoji": 1,
            },
        }
        classifier.classify(packet)
        reaction_event = codec.decode(packet)
        reaction_event = _inject_longname(reaction_event, "ReactNode", "RN")

        assert reaction_event.event_kind == "message.reacted"
        assert len(reaction_event.relations) == 1

        # -- Render via LxmfRenderer with fallback_text strategy -------
        renderer = LxmfRenderer(metadata_embedding=True)

        ctx = RenderingContext(
            target_adapter=_LXMF,
            target_channel="0",
            target_platform="lxmf",
            delivery_strategy="fallback_text",
        )

        result = await renderer.render(reaction_event, ctx)
        payload = result.payload

        # -- Assertions ------------------------------------------------
        # Content has inline degraded reaction text.
        content = str(payload["content"])
        assert "[reaction" in content
        assert _EMOJI in content

        # Fields envelope is present (metadata_embedding=True).
        fields = payload.get("fields")
        assert isinstance(fields, dict)

        # The 0xFD envelope exists.
        envelope_raw = fields.get(FIELD_MEDRE_ENVELOPE)
        assert envelope_raw is not None
        assert isinstance(envelope_raw, dict)

        # Envelope has the MEDRE namespace.
        envelope = envelope_raw.get(LXMF_NAMESPACE)
        assert isinstance(envelope, dict)
        assert "schema_version" in envelope
        assert envelope["event_id"] == reaction_event.event_id

        # Envelope relations list is EMPTY — fallback_text clears them.
        envelope_relations = envelope.get("relations", "MISSING")
        assert isinstance(envelope_relations, list)
        assert len(envelope_relations) == 0

        # Fallback marker present.
        assert result.fallback_applied == "strategy_fallback_text"

        # No native LXMF FIELD_THREAD (0x08) key.
        assert 0x08 not in fields


# ===================================================================
# Test 3: Missing native ref → cross-adapter graceful degradation
# ===================================================================


class TestMissingNativeRefCrossAdapterDegradation:
    """Pipeline crash-resistance and renderer robustness with fake default
    capabilities.

    These tests route events whose relation targets have **no stored native
    ref** through the full pipeline using ``FakeMeshCoreAdapter`` /
    ``FakeLxmfAdapter`` (which expose default ``AdapterCapabilities`` with
    replies/reactions="native").  They verify that the pipeline delivers
    without crashing and that no malformed native relation fields leak into
    the outbound payload.

    The explicit ``fallback_text`` degradation path (where the target
    adapter declares ``replies="unsupported"``) is covered by Tests 1–2
    above.  These tests complement those by exercising the *unresolved-ref*
    code path where the storage lookup returns nothing.
    """

    async def test_missing_ref_to_meshcore_graceful(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Pipeline delivers a reply event whose target has no stored
        native ref for MeshCore.  The event is delivered to the adapter
        with the relation unresolved (target_event_id=None) and the
        pipeline does not crash.  No malformed native relation fields
        appear in the MeshCore payload."""
        _MX = "mx-w6-3"
        _MC = "mc-w6-3"
        _ROOM = "!w6-3:server"

        # -- No seeding — storage is empty for the target native ref --

        # Decode a Matrix reply targeting a completely unknown event.
        config_mx = _make_matrix_config(adapter_id=_MX)
        codec = MatrixCodec(_MX, config_mx)

        reply_native = _make_reply_native_event(
            body="> <@sender:server> unknown message\n\nOrphan reply",
            event_id="$w6-3-reply",
            sender="@replier:server",
            reply_target="$nonexistent-event",
            room_id=_ROOM,
        )
        reply_event = codec.decode(reply_native, room_id=_ROOM)

        assert reply_event.payload["body"] == "Orphan reply"
        assert len(reply_event.relations) == 1

        # -- Setup MeshCore adapter and pipeline -----------------------
        mc_config = MeshCoreConfig(adapter_id=_MC)
        mc_adapter = FakeMeshCoreAdapter(mc_config)

        route = Route(
            id="mx-to-mc-w6-3",
            source=RouteSource(
                adapter=_MX,
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_MC, channel="0")],
        )

        rp = RenderingPipeline()
        rp.register(
            MeshCoreRenderer(configs={_MC: mc_config}),
            priority=50,
        )
        rp.register_adapter_platform(_MC, "meshcore")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[route]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MC: mc_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reply_event)

            # Pipeline does not crash — delivery succeeds.
            # NOTE: FakeMeshCoreAdapter does not expose _capabilities
            # to the pipeline, so the default AdapterCapabilities()
            # (replies="native") is used.  The event is delivered
            # with delivery_strategy='direct', not suppressed.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # MeshCore adapter received the payload.
            assert len(mc_adapter.delivered_payloads) == 1
            payload = mc_adapter.delivered_payloads[0].payload

            # No native relation fields in MeshCore payload.
            assert "reply_id" not in payload

            # Text is the reply body.
            assert "Orphan reply" in str(payload["text"])

            # Stored relation has unresolved target_event_id.
            stored = await temp_storage.get(reply_event.event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            assert stored.relations[0].target_event_id is None
        finally:
            await runner.stop()

    async def test_missing_ref_to_lxmf_graceful(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Pipeline delivers a reaction event whose target has no stored
        native ref for LXMF.  The event is delivered to the adapter with
        the relation unresolved (target_event_id=None).  The LXMF payload
        contains the emoji content and a fields envelope, but no malformed
        native relation fields.  The pipeline does not crash."""
        _RADIO = "radio-w6-3b"
        _LXMF = "lxmf-w6-3b"

        # Build a Meshtastic reaction event targeting an unmapped packet.
        reaction_event = _make_mesh_event(
            _RADIO, 0, 88000001, "🎉", reply_to_pkt_id=99999999, emoji=1
        )
        reaction_event = _inject_longname(reaction_event, "LostNode", "LN")

        assert reaction_event.event_kind == "message.reacted"

        # -- Setup LXMF adapter and pipeline ---------------------------
        lx_config = LxmfConfig(adapter_id=_LXMF)
        lx_adapter = FakeLxmfAdapter(lx_config)

        route = Route(
            id="radio-to-lx-w6-3b",
            source=RouteSource(
                adapter=_RADIO,
                event_kinds=("message.reacted",),
                channel="0",
            ),
            targets=[RouteTarget(adapter=_LXMF, channel="0")],
        )

        rp = RenderingPipeline()
        rp.register(
            LxmfRenderer(metadata_embedding=True),
            priority=50,
        )
        rp.register_adapter_platform(_LXMF, "lxmf")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[route]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_LXMF: lx_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reaction_event)

            # Pipeline does not crash — delivery succeeds.
            # NOTE: FakeLxmfAdapter does not expose _capabilities
            # to the pipeline, so the default AdapterCapabilities()
            # (reactions="native") is used.  The event is delivered
            # with delivery_strategy='direct', not suppressed.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # LXMF adapter received the payload.
            assert len(lx_adapter.delivered_payloads) == 1
            payload = lx_adapter.delivered_payloads[0].payload

            # Content is the reaction emoji text.
            content = str(payload["content"])
            assert "🎉" in content

            # Fields envelope exists but has unresolved relation
            # (target_event_id=None) — not malformed, just unresolved.
            fields = payload.get("fields")
            assert isinstance(fields, dict)
            envelope_raw = fields.get(FIELD_MEDRE_ENVELOPE)
            assert envelope_raw is not None
            envelope = envelope_raw.get(LXMF_NAMESPACE)
            assert isinstance(envelope, dict)
            envelope_relations = envelope.get("relations", [])
            assert isinstance(envelope_relations, list)
            assert len(envelope_relations) == 1
            assert envelope_relations[0]["target_event_id"] is None

            # No native LXMF FIELD_THREAD (0x08) key.
            assert 0x08 not in fields

            # Stored relation has unresolved target_event_id.
            stored = await temp_storage.get(reaction_event.event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            assert stored.relations[0].target_event_id is None
        finally:
            await runner.stop()


# ===================================================================
# Test 4: Meshtastic → Matrix reaction resolution verification
# ===================================================================


class TestMeshtasticToMatrixReactionResolution:
    """Focused verification that a Meshtastic reaction resolves through
    the pipeline to produce the correct stored relation target_event_id
    and Matrix output with MMRelay-compatible emote fallback metadata.

    The rendered Matrix payload carries ``meshtastic_replyId`` and
    ``meshtastic_emoji`` fields — the MMRelay emote-fallback metadata
    convention — rather than a true Matrix ``m.annotation`` relation.
    This verifies the Meshtastic→Matrix emote fallback path, not native
    Matrix reaction support."""

    async def test_reaction_resolves_target_event_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Meshtastic reaction targeting a previously-seen message
        resolves the relation to the correct canonical event_id.  The
        Matrix output carries meshtastic_replyId matching the original
        packet ID."""
        _MX = "mx-w6-4"
        _RADIO = "radio-w6-4"
        _ROOM = "!w6-4:server"
        _CANON_ID = "canon-w6-4"
        _MX_MSG = "$w6-4-orig"
        _MESH_PKT = 44000001
        _REACTION_PKT = 44000002
        _EMOJI = "❤️"

        # -- Seed: Matrix message bridged to Meshtastic -----------------
        ts = datetime.now(timezone.utc)
        orig_event = CanonicalEvent(
            event_id=_CANON_ID,
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MX,
            source_transport_id="@sender:server",
            source_channel_id=_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Target message"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MX,
                native_channel_id=_ROOM,
                native_message_id=_MX_MSG,
            ),
        )
        await temp_storage.append(orig_event)

        # Inbound Matrix ref.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-w6-4-mx-in",
                event_id=_CANON_ID,
                adapter=_MX,
                native_channel_id=_ROOM,
                native_message_id=_MX_MSG,
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )
        # Outbound Meshtastic ref.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-w6-4-mesh-out",
                event_id=_CANON_ID,
                adapter=_RADIO,
                native_channel_id="0",
                native_message_id=str(_MESH_PKT),
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=ts,
            )
        )

        # -- Decode Meshtastic reaction ---------------------------------
        reaction_event = _make_mesh_event(
            _RADIO,
            0,
            _REACTION_PKT,
            _EMOJI,
            reply_to_pkt_id=_MESH_PKT,
            emoji=1,
        )
        reaction_event = _inject_longname(reaction_event, "Reactor", "RX")

        assert reaction_event.event_kind == "message.reacted"
        assert len(reaction_event.relations) == 1
        rel = reaction_event.relations[0]
        assert rel.relation_type == "reaction"
        assert rel.key == _EMOJI

        # -- Setup pipeline ---------------------------------------------
        matrix_adapter = FakeMatrixAdapter(adapter_id=_MX, channel=_ROOM)

        route = Route(
            id="radio-to-mx-w6-4",
            source=RouteSource(
                adapter=_RADIO,
                event_kinds=("message.reacted",),
                channel="0",
            ),
            targets=[RouteTarget(adapter=_MX, channel=_ROOM)],
        )

        rp = RenderingPipeline()
        rp.register(
            MatrixRenderer(
                source_configs={
                    _RADIO: _StubMeshtasticConfig(
                        adapter_id=_RADIO,
                        mmrelay_compatibility=True,
                        # mmrelay KEY_MESHNET wire compat
                    ),
                },
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MX, "matrix")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=Router(routes=[route]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MX: matrix_adapter},
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
            payload = matrix_adapter.delivered_payloads[0].payload

            # meshtastic_replyId matches the original packet ID.
            assert str(payload.get("meshtastic_replyId")) == str(_MESH_PKT)

            # meshtastic_emoji flag set.
            assert payload.get("meshtastic_emoji") == 1

            # Native Matrix reaction annotation is NOT present — the
            # cross-adapter path uses meshtastic_* fields, not m.reaction.
            assert payload.get("_matrix_event_type") != "m.reaction"
            relates_to = payload.get("m.relates_to")
            if relates_to:
                assert relates_to.get("rel_type") != "m.annotation"

            # -- Verify stored relation resolved to correct event --------
            stored = await temp_storage.get(reaction_event.event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            stored_rel = stored.relations[0]
            assert stored_rel.target_event_id == _CANON_ID
        finally:
            await runner.stop()
