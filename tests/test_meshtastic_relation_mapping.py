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

All tests use ``FakeMeshtasticAdapter`` / ``FakeMatrixAdapter``.  No live
services required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.bus import EventBus
from medre.core.events.metadata import NativeMetadata
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage

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
        rp.register(MeshtasticRenderer(), priority=50)
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
            assert "packet_id" in meta or "channel" in meta

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
    classification = classifier.classify(packet)
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
                mmrelay_compat=True,
                meshnet_name=_MESHNET_NAME,
                matrix_relay_prefix="[{longname}] ",
            ),
            priority=50,
        )
        rp.register_adapter_platform(_MATRIX_ADAPTER, "matrix")
        rp.register(MeshtasticRenderer(), priority=40)
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
            # MatrixRenderer wraps reply body with "> <sender> original\n\n"
            # then the relay-prefixed body.  The longname prefix appears
            # after the reply fallback header.
            assert f"[{_LONGNAME}]" in body

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
                mmrelay_compat=True,
                meshnet_name=_MESHNET_NAME,
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
