"""Integration tests for delayed Meshtastic outbound NativeMessageRef mapping
and Meshtastic-to-Matrix reply resolution.

Test categories
---------------
Test A
    Matrix-originated canonical event routed to Meshtastic adapter.  Verifies
    outbound ``NativeMessageRef`` is persisted with correct adapter, channel,
    message ID, event ID, direction, and metadata after the fake adapter
    delivers.

Test B
    Inbound Meshtastic reply packet whose ``replyId`` references the packet ID
    from Test A.  Verifies the reply resolves through ``RelationResolver`` and
    ``_enrich_relations_for_target`` so that ``MatrixRenderer`` produces
    ``m.relates_to.m.in_reply_to.event_id`` pointing back to the original
    Matrix event.

Reaction-to-reaction suppression tests (Tests A–C) are in
test_meshtastic_relation_mapping_r2r.py.

All tests use ``FakeMeshtasticAdapter`` / ``FakeMatrixAdapter``.  No live
services required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
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
from medre.core.rendering.renderer import RenderingContext, RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage

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


# Fixed IDs used across both tests for traceability.
_CANON_EVENT_ID = "canon-matrix-original"
_MATRIX_ROOM = "!room:server"
_MATRIX_MSG_ID = "$matrix-original"
_RADIO_ADAPTER = "radio"
_MATRIX_ADAPTER = "matrix"
_MESHNET_NAME = "medre-radio"
_RADIO_PKT_ID = 2728143522
_REPLY_PKT_ID = 1186126098
_REPLY_TEXT = "Replying"
_LONGNAME = "Display Name"


# ===================================================================
# Test A: Matrix → Meshtastic outbound NativeMessageRef
# ===================================================================


class TestMatrixToMeshtasticOutboundNativeRef:
    """Matrix-originated event routed to Meshtastic adapter stores outbound
    NativeMessageRef with correct fields."""

    async def test_outbound_native_ref_stored(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Matrix canonical event is routed to Meshtastic adapter 'radio';
        FakeMeshtasticAdapter returns deterministic packet ID 2728143522;
        outbound NativeMessageRef is persisted in storage."""
        # -- Setup adapters ------------------------------------------------
        radio_config = MeshtasticConfig(adapter_id=_RADIO_ADAPTER)
        radio_adapter = FakeMeshtasticAdapter(radio_config)
        # Pre-set the fake client counter so first send returns 2728143522.
        radio_adapter.fake_client._next_id = _RADIO_PKT_ID

        # -- Route: matrix → radio -----------------------------------------
        route = Route(
            id="matrix-to-radio",
            source=RouteSource(
                adapter=_MATRIX_ADAPTER,
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_RADIO_ADAPTER, channel="0")],
        )
        router = Router(routes=[route])

        # -- Rendering pipeline --------------------------------------------
        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={_RADIO_ADAPTER: MeshtasticConfig(adapter_id=_RADIO_ADAPTER)}
            ),
            priority=50,
        )
        rp.register_adapter_platform(_RADIO_ADAPTER, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_RADIO_ADAPTER: radio_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        # -- Create Matrix-originated canonical event ----------------------
        nref = NativeRef(
            adapter=_MATRIX_ADAPTER,
            native_channel_id=_MATRIX_ROOM,
            native_message_id=_MATRIX_MSG_ID,
        )
        ts = datetime.now(timezone.utc)
        event = CanonicalEvent(
            event_id=_CANON_EVENT_ID,
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MATRIX_ADAPTER,
            source_transport_id="matrix-user-1",
            source_channel_id=_MATRIX_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Hello from Matrix"},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )

        try:
            outcomes = await runner.handle_ingress(event)

            # Event was delivered to radio adapter.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # FakeMeshtasticAdapter received the rendered payload.
            assert len(radio_adapter.delivered_payloads) == 1
            result = radio_adapter.delivered_payloads[0]
            assert result.metadata["renderer"] == "meshtastic"

            # -- Verify outbound NativeMessageRef --------------------------
            refs = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'outbound'",
                (_CANON_EVENT_ID,),
            )
            assert len(refs) == 1
            ref = refs[0]
            assert ref["adapter"] == _RADIO_ADAPTER
            assert ref["native_channel_id"] == "0"
            assert ref["native_message_id"] == str(_RADIO_PKT_ID)
            assert ref["event_id"] == _CANON_EVENT_ID
            assert ref["direction"] == "outbound"

            # Metadata includes useful delivery context (packet_id, channel).
            meta = json.loads(ref["metadata"])
            assert "packet_id" in meta
            assert "channel" in meta

            # -- Verify inbound ref also persisted -------------------------
            inbound_resolved = await temp_storage.resolve_native_ref(
                _MATRIX_ADAPTER, _MATRIX_ROOM, _MATRIX_MSG_ID
            )
            assert inbound_resolved == _CANON_EVENT_ID

            # -- Verify missing mapping fallback not broken ----------------
            missing = await temp_storage.resolve_native_ref(
                _MATRIX_ADAPTER, "!nonexistent:server", "$missing"
            )
            assert missing is None
        finally:
            await runner.stop()


# ===================================================================
# Helpers for Test B
# ===================================================================


async def _seed_test_a_state(storage: SQLiteStorage) -> None:
    """Pre-populate storage with the state Test A would leave behind.

    Stores:
    1. The canonical event 'canon-matrix-original'.
    2. Inbound native ref: adapter='matrix', !room:server, $matrix-original.
    3. Outbound native ref: adapter='radio', channel 0, packet 2728143522.
    """
    ts = datetime.now(timezone.utc)

    # 1. Store the canonical event.
    event = CanonicalEvent(
        event_id=_CANON_EVENT_ID,
        event_kind="message.created",
        schema_version=1,
        timestamp=ts,
        source_adapter=_MATRIX_ADAPTER,
        source_transport_id="matrix-user-1",
        source_channel_id=_MATRIX_ROOM,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "Hello from Matrix"},
        metadata=EventMetadata(),
        source_native_ref=NativeRef(
            adapter=_MATRIX_ADAPTER,
            native_channel_id=_MATRIX_ROOM,
            native_message_id=_MATRIX_MSG_ID,
        ),
    )
    await storage.append(event)

    # 2. Inbound native ref (matrix → canonical).
    inbound_ref = NativeMessageRef(
        id="nref-inbound-seed",
        event_id=_CANON_EVENT_ID,
        adapter=_MATRIX_ADAPTER,
        native_channel_id=_MATRIX_ROOM,
        native_message_id=_MATRIX_MSG_ID,
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
        metadata={"text": "Hello from Matrix"},
        created_at=ts,
    )
    await storage.store_native_ref(inbound_ref)

    # 3. Outbound native ref (radio → canonical).
    outbound_ref = NativeMessageRef(
        id="nref-outbound-seed",
        event_id=_CANON_EVENT_ID,
        adapter=_RADIO_ADAPTER,
        native_channel_id="0",
        native_message_id=str(_RADIO_PKT_ID),
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        metadata={
            "text": "Hello from Matrix",
            "packet_id": _RADIO_PKT_ID,
            "channel": 0,
        },
        created_at=ts,
    )
    await storage.store_native_ref(outbound_ref)


def _make_reply_packet() -> dict:
    """Build a Meshtastic text packet that is a reply to packet 2728143522."""
    return {
        "fromId": "!meshnode1",
        "toId": "",
        "channel": 0,
        "id": _REPLY_PKT_ID,
        "decoded": {
            "portnum": "text_message",
            "text": _REPLY_TEXT,
            "replyId": _RADIO_PKT_ID,
        },
    }


def _make_reply_event_with_longname() -> CanonicalEvent:
    """Decode a reply packet and inject generic longname/shortname.

    This mirrors what the real MeshtasticAdapter does when enriching
    longname from the SDK nodes dict, but without requiring a real client.
    """
    config = MeshtasticConfig(adapter_id=_RADIO_ADAPTER)
    codec = MeshtasticCodec(_RADIO_ADAPTER, config)
    classifier = MeshtasticPacketClassifier(config)

    packet = _make_reply_packet()
    classifier.classify(packet)
    event = codec.decode(packet)

    # Inject longname/shortname into native metadata (as the adapter would).
    if event.metadata.native is not None:
        from msgspec.structs import replace as _replace

        updated_data = dict(event.metadata.native.data)
        updated_data["longname"] = _LONGNAME
        updated_data["shortname"] = "DN"
        new_native = _replace(event.metadata.native, data=updated_data)
        new_metadata = _replace(event.metadata, native=new_native)
        event = _replace(event, metadata=new_metadata)

    return event


# ===================================================================
# Test B: Meshtastic → Matrix reply resolution
# ===================================================================


class TestMeshtasticToMatrixReplyResolution:
    """Inbound Meshtastic reply packet resolves to Matrix m.in_reply_to."""

    async def test_reply_resolves_to_matrix_in_reply_to(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inbound Meshtastic reply packet (replyId=2728143522) resolves
        through the pipeline so that MatrixRenderer output contains
        m.relates_to.m.in_reply_to.event_id == '$matrix-original' and
        meshtastic_replyId == '2728143522'.  Body prefix preserves generic
        longname spacing/casing."""
        # -- Seed state from Test A ----------------------------------------
        await _seed_test_a_state(temp_storage)

        # -- Setup adapters ------------------------------------------------
        matrix_adapter = FakeMatrixAdapter(
            adapter_id=_MATRIX_ADAPTER, channel=_MATRIX_ROOM
        )

        # -- Route: radio → matrix -----------------------------------------
        route = Route(
            id="radio-to-matrix",
            source=RouteSource(
                adapter=_RADIO_ADAPTER,
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter=_MATRIX_ADAPTER, channel=_MATRIX_ROOM)],
        )
        router = Router(routes=[route])

        # -- Rendering pipeline with MatrixRenderer ------------------------
        rp = RenderingPipeline()
        rp.register(
            MatrixRenderer(
                source_configs={
                    _RADIO_ADAPTER: _StubMeshtasticConfig(
                        adapter_id=_RADIO_ADAPTER,
                        mmrelay_compatibility=True,
                        meshnet_name=_MESHNET_NAME,
                        matrix_relay_prefix="[{longname}] ",
                    ),
                },
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MATRIX_ADAPTER, "matrix")
        rp.register(
            MeshtasticRenderer(
                configs={_RADIO_ADAPTER: MeshtasticConfig(adapter_id=_RADIO_ADAPTER)}
            ),
            priority=40,
        )
        rp.register_adapter_platform(_RADIO_ADAPTER, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MATRIX_ADAPTER: matrix_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        # -- Create the reply event ----------------------------------------
        reply_event = _make_reply_event_with_longname()

        try:
            outcomes = await runner.handle_ingress(reply_event)

            # Delivery succeeded to matrix adapter.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Matrix adapter received the rendered payload.
            assert len(matrix_adapter.delivered_payloads) == 1
            result = matrix_adapter.delivered_payloads[0]
            payload = result.payload

            # -- Verify m.relates_to.m.in_reply_to -------------------------
            relates_to_raw = payload.get("m.relates_to")
            assert isinstance(relates_to_raw, dict)
            relates_to: dict[str, object] = relates_to_raw
            in_reply_to_raw = relates_to.get("m.in_reply_to")
            assert isinstance(in_reply_to_raw, dict)
            in_reply_to: dict[str, object] = in_reply_to_raw
            assert in_reply_to["event_id"] == _MATRIX_MSG_ID

            # -- Verify meshtastic_replyId ----------------------------------
            assert payload.get("meshtastic_replyId") == str(_RADIO_PKT_ID)

            # -- Verify body prefix preserves longname spacing/casing -------
            body = str(payload.get("body", ""))
            # MatrixRenderer no longer adds fallback quoting; the body is
            # just the relay-prefixed text.
            assert f"[{_LONGNAME}]" in body
            assert "> <" not in body

            # -- Verify the reply's inbound native ref is stored ------------
            reply_event_id = reply_event.event_id
            reply_resolved = await temp_storage.resolve_native_ref(
                _RADIO_ADAPTER, "0", str(_REPLY_PKT_ID)
            )
            assert reply_resolved == reply_event_id

            # -- Verify the relation was resolved to canon event ID ---------
            stored = await temp_storage.get(reply_event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            rel = stored.relations[0]
            assert rel.relation_type == "reply"
            assert rel.target_event_id == _CANON_EVENT_ID
        finally:
            await runner.stop()

    async def test_missing_mapping_does_not_crash(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """An inbound reply with replyId that has no stored mapping does not
        crash the pipeline; the relation is left unresolved."""
        # Empty storage — no pre-seeded mappings.

        matrix_adapter = FakeMatrixAdapter(
            adapter_id=_MATRIX_ADAPTER, channel=_MATRIX_ROOM
        )

        route = Route(
            id="radio-to-matrix-nomatch",
            source=RouteSource(
                adapter=_RADIO_ADAPTER,
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter=_MATRIX_ADAPTER, channel=_MATRIX_ROOM)],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MatrixRenderer(
                source_configs={
                    _RADIO_ADAPTER: _StubMeshtasticConfig(
                        adapter_id=_RADIO_ADAPTER,
                        mmrelay_compatibility=True,
                        meshnet_name=_MESHNET_NAME,
                    ),
                },
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MATRIX_ADAPTER, "matrix")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MATRIX_ADAPTER: matrix_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        reply_event = _make_reply_event_with_longname()

        try:
            outcomes = await runner.handle_ingress(reply_event)

            # Pipeline should not crash — delivery still happens.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # The reply event was still delivered to the matrix adapter.
            assert len(matrix_adapter.delivered_payloads) == 1

            # The relation target_event_id stays unresolved (None)
            # because there is no native ref for the replyId in storage.
            stored = await temp_storage.get(reply_event.event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            rel = stored.relations[0]
            assert rel.relation_type == "reply"
            # target_event_id is None — relation could not be resolved.
            assert rel.target_event_id is None
        finally:
            await runner.stop()


# ===================================================================
# Test A: Pipeline text enrichment for reactions
# ===================================================================


class TestPipelineTextEnrichmentForReactions:
    """Pipeline enriches reaction relations with original text from the
    target event, enabling MeshtasticRenderer to preview the original
    message instead of falling back to the reaction event body."""

    async def test_text_enriched_from_target_event(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Matrix reaction event enriched with original text from the target
        canonical event's payload body."""
        # -- Seed a prior canonical event with known text ------------------
        ts = datetime.now(timezone.utc)
        prior_event = CanonicalEvent(
            event_id="orig-event-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MATRIX_ADAPTER,
            source_transport_id="matrix-user-1",
            source_channel_id=_MATRIX_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Hello from the original message"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$orig-msg-1",
            ),
        )
        await temp_storage.append(prior_event)

        # Store native refs for both adapters (Matrix + Meshtastic outbound).
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-orig-inbound",
                event_id="orig-event-1",
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$orig-msg-1",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-orig-outbound",
                event_id="orig-event-1",
                adapter=_RADIO_ADAPTER,
                native_channel_id="0",
                native_message_id="999888",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=ts,
            )
        )

        # -- Build a Matrix reaction event --------------------------------
        reaction_rel = EventRelation(
            relation_type="reaction",
            target_event_id="orig-event-1",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        reaction_event = CanonicalEvent(
            event_id="reaction-event-1",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_MATRIX_ADAPTER,
            source_transport_id="@user:example.com",
            source_channel_id=_MATRIX_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(reaction_rel,),
            payload={"body": "👍"},
            metadata=EventMetadata(),
        )

        # -- Set up pipeline targeting radio adapter -----------------------
        route = Route(
            id="matrix-to-radio-reaction",
            source=RouteSource(
                adapter=_MATRIX_ADAPTER,
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_RADIO_ADAPTER, channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={_RADIO_ADAPTER: MeshtasticConfig(adapter_id=_RADIO_ADAPTER)}
            ),
            priority=50,
        )
        rp.register_adapter_platform(_RADIO_ADAPTER, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        radio_config = MeshtasticConfig(adapter_id=_RADIO_ADAPTER)
        radio_adapter = FakeMeshtasticAdapter(radio_config)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_RADIO_ADAPTER: radio_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            # -- Enrich directly to test text enrichment ------------------
            enriched = await runner._enrich_relations_for_target(
                reaction_event, _RADIO_ADAPTER, target_channel="0"
            )

            rel = enriched.relations[0]
            # Text enrichment should populate both fields.
            assert rel.fallback_text == "Hello from the original message"
            assert (
                rel.metadata.get("original_text") == "Hello from the original message"
            )

            # Native ref enrichment should also have run.
            assert rel.target_native_ref is not None
            assert rel.target_native_ref.adapter == _RADIO_ADAPTER
            assert rel.target_native_ref.native_message_id == "999888"
        finally:
            await runner.stop()


# ===================================================================
# Test B: MeshtasticRenderer uses enriched text for reaction preview
# ===================================================================


class TestMeshtasticRendererEnrichedReactionText:
    """MeshtasticRenderer renders the original message text as preview
    when the relation has fallback_text and metadata["original_text"]
    populated by pipeline text enrichment."""

    async def test_renders_original_text_preview(self) -> None:
        """Reaction with enriched fallback_text shows original message,
        not the reaction event body."""
        renderer = MeshtasticRenderer(
            configs={"mesh-1": MeshtasticConfig(adapter_id="mesh-1")}
        )

        rel = EventRelation(
            relation_type="reaction",
            target_event_id="mesh-evt-0",
            target_native_ref=NativeRef(
                adapter="mesh-1",
                native_channel_id="0",
                native_message_id="42",
            ),
            key="👍",
            fallback_text="Hello from the original message",
            metadata={"original_text": "Hello from the original message"},
        )
        event = CanonicalEvent(
            event_id="reaction-render-test",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:server",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "👍"},
            metadata=EventMetadata(),
        )

        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = str(result.payload["text"])

        # Should contain the reaction key and the original text preview.
        assert "reacted 👍 to" in text
        assert "Hello from the original message" in text
        # Should NOT contain just the reaction body "👍" as the preview.
        # The text should be the descriptive pattern, not just the emoji.


# ===================================================================
# Test D1: Matrix reply to Meshtastic-originated message renders
# with native Meshtastic reply_id
# ===================================================================


class TestMatrixReplyToMeshtasticNativeReplyId:
    """Matrix reply to a Meshtastic-originated message, when rendered back
    to Meshtastic, carries native reply_id from the Meshtastic packet."""

    async def test_reply_carries_native_meshtastic_reply_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """End-to-end: Matrix reply to Meshtastic message → Meshtastic
        renderer output has reply_id matching the original Meshtastic
        packet ID and stripped reply body."""
        ts = datetime.now(timezone.utc)
        _CANON_ID = "canon-mesh-orig-d1"
        _MESH_PKT_ID = 12345
        _MATRIX_COPY_ID = "$matrix-copy-d1"
        _MESH_ADAPTER = "radio-d1"
        _MX_ADAPTER = "matrix-d1"
        _ROOM = "!room-d1:server"

        # 1. Store a Meshtastic-originated canonical event.
        orig_event = CanonicalEvent(
            event_id=_CANON_ID,
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MESH_ADAPTER,
            source_transport_id="!meshnode1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Hello from mesh"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MESH_ADAPTER,
                native_channel_id="0",
                native_message_id=str(_MESH_PKT_ID),
            ),
        )
        await temp_storage.append(orig_event)

        # 2. Store inbound Meshtastic native ref.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-d1-mesh-in",
                event_id=_CANON_ID,
                adapter=_MESH_ADAPTER,
                native_channel_id="0",
                native_message_id=str(_MESH_PKT_ID),
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # 3. Store outbound Matrix native ref (the relayed copy).
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-d1-mx-out",
                event_id=_CANON_ID,
                adapter=_MX_ADAPTER,
                native_channel_id=_ROOM,
                native_message_id=_MATRIX_COPY_ID,
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=ts,
            )
        )

        # 4. Store outbound Meshtastic native ref.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-d1-mesh-out",
                event_id=_CANON_ID,
                adapter=_MESH_ADAPTER,
                native_channel_id="0",
                native_message_id=str(_MESH_PKT_ID),
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=ts,
            )
        )

        # 5. Decode a Matrix reply event via MatrixCodec.
        config_mx = _make_matrix_config(adapter_id=_MX_ADAPTER)
        codec = MatrixCodec(_MX_ADAPTER, config_mx)

        reply_native = _make_reply_native_event(
            body="> <@sender:server> Hello from mesh\n\nHi",
            event_id="$reply-evt-d1",
            sender="@replyer:server",
            reply_target=_MATRIX_COPY_ID,
            room_id=_ROOM,
        )
        reply_event = codec.decode(reply_native, room_id=_ROOM)

        # Verify codec stripped the fallback and created a reply relation.
        assert reply_event.payload["body"] == "Hi"
        assert len(reply_event.relations) == 1
        rel = reply_event.relations[0]
        assert rel.relation_type == "reply"
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.native_message_id == _MATRIX_COPY_ID

        # 6. Process through pipeline: resolve + enrich + render.
        route = Route(
            id="mx-to-mesh-d1",
            source=RouteSource(
                adapter=_MX_ADAPTER,
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_MESH_ADAPTER, channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={
                    _MESH_ADAPTER: MeshtasticConfig(
                        adapter_id=_MESH_ADAPTER, radio_relay_prefix=""
                    )
                }
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MESH_ADAPTER, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        radio_config = MeshtasticConfig(adapter_id=_MESH_ADAPTER)
        radio_adapter = FakeMeshtasticAdapter(radio_config)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MESH_ADAPTER: radio_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reply_event)

            # 7. Assert delivery succeeded.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # 8. Verify renderer output has reply_id and stripped text.
            assert len(radio_adapter.delivered_payloads) == 1
            payload = radio_adapter.delivered_payloads[0].payload
            assert payload["reply_id"] == _MESH_PKT_ID
            assert payload["text"] == "Hi"
        finally:
            await runner.stop()


# ===================================================================
# Test D2: Missing mapping — reply still sends safely
# ===================================================================


class TestMatrixReplyMissingMappingNoCrash:
    """Matrix reply where the target Matrix event has no canonical mapping.
    The message still sends to Meshtastic safely with no reply_id and
    no crash."""

    async def test_reply_without_mapping_sends_safely(
        self, temp_storage: SQLiteStorage
    ) -> None:
        _MESH_ADAPTER = "radio-d2"
        _MX_ADAPTER = "matrix-d2"
        _ROOM = "!room-d2:server"

        # No pre-seeded native refs — the reply target is unmapped.

        # Decode a Matrix reply via codec.
        config_mx = _make_matrix_config(adapter_id=_MX_ADAPTER)
        codec = MatrixCodec(_MX_ADAPTER, config_mx)

        reply_native = _make_reply_native_event(
            body="> <@sender:server> unknown msg\n\nMy reply",
            event_id="$reply-evt-d2",
            sender="@replyer:server",
            reply_target="$unknown-matrix-event",
            room_id=_ROOM,
        )
        reply_event = codec.decode(reply_native, room_id=_ROOM)

        assert reply_event.payload["body"] == "My reply"

        route = Route(
            id="mx-to-mesh-d2",
            source=RouteSource(
                adapter=_MX_ADAPTER,
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_MESH_ADAPTER, channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={
                    _MESH_ADAPTER: MeshtasticConfig(
                        adapter_id=_MESH_ADAPTER, radio_relay_prefix=""
                    )
                }
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MESH_ADAPTER, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        radio_config = MeshtasticConfig(adapter_id=_MESH_ADAPTER)
        radio_adapter = FakeMeshtasticAdapter(radio_config)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MESH_ADAPTER: radio_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reply_event)

            # Delivery succeeded — no crash.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Renderer output has no reply_id but text is clean.
            assert len(radio_adapter.delivered_payloads) == 1
            payload = radio_adapter.delivered_payloads[0].payload
            assert "reply_id" not in payload
            # Text should be just the reply body, no "[replying to: ...]" prefix.
            assert str(payload["text"]) == "My reply"
        finally:
            await runner.stop()


# ===================================================================
# Test E1: Matrix→Matrix reply still links on meshnet
# ===================================================================


class TestMatrixToMatrixReplyLinksOnMeshnet:
    """When a Matrix user replies to a Matrix-originated message that was
    also relayed to Meshtastic, the reply rendered for Meshtastic carries
    the Meshtastic native reply_id (cross-adapter enrichment)."""

    async def test_matrix_reply_to_matrix_msg_has_mesh_reply_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        ts = datetime.now(timezone.utc)
        _CANON_ID = "canon-matrix-orig-e1"
        _MATRIX_MSG_ID = "$matrix-orig-e1"
        _MESH_PKT_ID = 99887766
        _MESH_ADAPTER = "radio-e1"
        _MX_ADAPTER = "matrix-e1"
        _ROOM = "!room-e1:server"

        # 1. Store a Matrix-originated canonical event.
        orig_event = CanonicalEvent(
            event_id=_CANON_ID,
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MX_ADAPTER,
            source_transport_id="@orig-sender:server",
            source_channel_id=_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Original Matrix message"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MX_ADAPTER,
                native_channel_id=_ROOM,
                native_message_id=_MATRIX_MSG_ID,
            ),
        )
        await temp_storage.append(orig_event)

        # 2. Inbound Matrix native ref.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-e1-mx-in",
                event_id=_CANON_ID,
                adapter=_MX_ADAPTER,
                native_channel_id=_ROOM,
                native_message_id=_MATRIX_MSG_ID,
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # 3. Outbound Meshtastic native ref.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-e1-mesh-out",
                event_id=_CANON_ID,
                adapter=_MESH_ADAPTER,
                native_channel_id="0",
                native_message_id=str(_MESH_PKT_ID),
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=ts,
            )
        )

        # 4. Decode a Matrix reply to the original Matrix message.
        config_mx = _make_matrix_config(adapter_id=_MX_ADAPTER)
        codec = MatrixCodec(_MX_ADAPTER, config_mx)

        reply_native = _make_reply_native_event(
            body="> <@orig-sender:server> Original Matrix message\n\nReply from Matrix",
            event_id="$reply-evt-e1",
            sender="@replier:server",
            reply_target=_MATRIX_MSG_ID,
            room_id=_ROOM,
        )
        reply_event = codec.decode(reply_native, room_id=_ROOM)

        assert reply_event.payload["body"] == "Reply from Matrix"

        # 5. Route to Meshtastic — enrichment should find the mesh ref.
        route = Route(
            id="mx-to-mesh-e1",
            source=RouteSource(
                adapter=_MX_ADAPTER,
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_MESH_ADAPTER, channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={
                    _MESH_ADAPTER: MeshtasticConfig(
                        adapter_id=_MESH_ADAPTER, radio_relay_prefix=""
                    )
                }
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MESH_ADAPTER, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        radio_config = MeshtasticConfig(adapter_id=_MESH_ADAPTER)
        radio_adapter = FakeMeshtasticAdapter(radio_config)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MESH_ADAPTER: radio_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reply_event)

            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Renderer output has Meshtastic reply_id from the mesh ref.
            assert len(radio_adapter.delivered_payloads) == 1
            payload = radio_adapter.delivered_payloads[0].payload
            assert payload["reply_id"] == _MESH_PKT_ID
            assert payload["text"] == "Reply from Matrix"
        finally:
            await runner.stop()


# ===================================================================
# Shared test helpers for D/E tests
# ===================================================================


def _make_matrix_config(**overrides):
    """Build a minimal MatrixConfig for testing."""
    from medre.config.adapters.matrix import MatrixConfig

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
    """Build a minimal native event object that looks like a Matrix reply."""

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
