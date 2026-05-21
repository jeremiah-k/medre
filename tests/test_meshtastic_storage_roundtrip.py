"""Meshtastic-shaped CanonicalEvent storage round-trip tests.

Verifies that canonical events produced by the Meshtastic codec (with
Meshtastic-specific native metadata: packet_id, from_id, channel in
native.data) round-trip correctly through SQLiteStorage: store, retrieve,
resolve native refs, and query via timeline.

These tests do NOT require a live Meshtastic radio. They exercise the
storage layer with Meshtastic-shaped data to ensure Meshtastic events are
first-class citizens in the evidence pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.metadata import NativeMetadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meshtastic_config() -> MeshtasticConfig:
    """Build a minimal MeshtasticConfig for codec tests."""
    return MeshtasticConfig(adapter_id="mesh-test")


def _make_fake_meshtastic_packet(
    text: str = "hello",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
    to_id: str = "",
    reply_id: int | None = None,
) -> dict:
    """Build a minimal Meshtastic packet dict for codec testing.

    Returns a plain dict with the fields the codec / classifier expect:
    fromId, toId, channel, id, decoded.portnum, decoded.text.
    """
    decoded: dict = {
        "portnum": "text_message",
        "text": text,
    }
    if reply_id is not None:
        decoded["replyId"] = reply_id
    return {
        "fromId": sender,
        "toId": to_id,
        "channel": channel,
        "id": packet_id,
        "decoded": decoded,
    }


def _make_meshtastic_canonical_event(
    event_id: str = "ce-001",
    node_id: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
    body: str = "Hello from mesh",
) -> CanonicalEvent:
    """Build a Meshtastic-shaped CanonicalEvent directly (bypassing codec)."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        source_adapter="mesh-test",
        source_transport_id=node_id,
        source_channel_id=str(channel),
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": body, "portnum": "text_message"},
        metadata=EventMetadata(
            native=NativeMetadata(
                data={
                    "packet_id": packet_id,
                    "from_id": node_id,
                    "channel": channel,
                    "portnum": "text_message",
                    "to_id": "",
                    "is_direct_message": False,
                    "longname": "",
                    "shortname": "",
                    "reply_id": None,
                    "emoji": None,
                    "emoji_flag": None,
                }
            )
        ),
        source_native_ref=NativeRef(
            adapter="mesh-test",
            native_channel_id=str(channel),
            native_message_id=str(packet_id),
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Uses the temp_storage fixture from tests/conftest.py which provides
# an initialized SQLiteStorage backed by a temporary file.


# ---------------------------------------------------------------------------
# Tests: Codec-produced events round-trip through storage
# ---------------------------------------------------------------------------


class TestMeshtasticCodecEventStorageRoundtrip:
    """Events produced by MeshtasticCodec round-trip through storage."""

    async def test_codec_event_stored_and_retrieved(self, temp_storage) -> None:
        """A MeshtasticCodec-decoded event can be stored and retrieved unchanged."""
        config = _meshtastic_config()
        codec = MeshtasticCodec("mesh-test", config)
        packet = _make_fake_meshtastic_packet(
            text="Storage round-trip test",
            sender="!node42",
            channel=0,
            packet_id=101,
        )
        canonical = codec.decode(packet)

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None, "Event not found in storage"
        assert retrieved.event_id == canonical.event_id
        assert retrieved.source_adapter == "mesh-test"
        assert retrieved.source_channel_id == "0"
        assert retrieved.source_transport_id == "!node42"
        assert retrieved.event_kind == "message.created"

        # Payload preserved
        assert retrieved.payload.get("body") == "Storage round-trip test"
        assert retrieved.payload.get("portnum") == "text_message"

    async def test_codec_event_native_metadata_preserved(self, temp_storage) -> None:
        """Meshtastic-specific native metadata (packet_id, from_id, channel) survives storage."""
        config = _meshtastic_config()
        codec = MeshtasticCodec("mesh-test", config)
        packet = _make_fake_meshtastic_packet(
            text="Metadata test",
            sender="!node99",
            channel=3,
            packet_id=777,
        )
        canonical = codec.decode(packet)

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None
        ndata = retrieved.metadata.native.data
        assert ndata["packet_id"] == 777
        assert ndata["from_id"] == "!node99"
        assert ndata["channel"] == 3

    async def test_codec_event_source_native_ref_stored(self, temp_storage) -> None:
        """source_native_ref from MeshtasticCodec survives storage round-trip."""
        config = _meshtastic_config()
        codec = MeshtasticCodec("mesh-test", config)
        packet = _make_fake_meshtastic_packet(
            text="Ref test",
            sender="!node55",
            channel=1,
            packet_id=555,
        )
        canonical = codec.decode(packet)

        assert canonical.source_native_ref is not None
        assert canonical.source_native_ref.adapter == "mesh-test"
        assert canonical.source_native_ref.native_message_id == "555"
        assert canonical.source_native_ref.native_channel_id == "1"

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None
        assert retrieved.source_native_ref is not None
        assert retrieved.source_native_ref.native_message_id == "555"


# ---------------------------------------------------------------------------
# Tests: Native refs for Meshtastic events
# ---------------------------------------------------------------------------


class TestMeshtasticNativeRefStorage:
    """NativeMessageRef for Meshtastic events can be stored and resolved."""

    async def test_meshtastic_native_ref_stored_and_resolved(
        self, temp_storage
    ) -> None:
        """A Meshtastic NativeMessageRef stores and resolves correctly."""
        event = _make_meshtastic_canonical_event(
            event_id="ce-nref-1",
            node_id="!nodeA",
            channel=2,
            packet_id=200,
        )
        await temp_storage.append(event)

        nref = NativeMessageRef(
            id="nref-1",
            event_id="ce-nref-1",
            adapter="mesh-test",
            native_channel_id="2",
            native_message_id="200",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(nref)

        resolved = await temp_storage.resolve_native_ref(
            "mesh-test", "2", "200"
        )
        assert resolved == "ce-nref-1"

    async def test_meshtastic_inbound_native_ref_resolves(
        self, temp_storage
    ) -> None:
        """An inbound Meshtastic native ref resolves to the correct event."""
        event = _make_meshtastic_canonical_event(
            event_id="ce-inbound-1",
            node_id="!sender_node",
            channel=0,
            packet_id=300,
        )
        await temp_storage.append(event)

        nref = NativeMessageRef(
            id="nref-inbound-1",
            event_id="ce-inbound-1",
            adapter="mesh-test",
            native_channel_id="0",
            native_message_id="300",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(nref)

        resolved = await temp_storage.resolve_native_ref(
            "mesh-test", "0", "300"
        )
        assert resolved == "ce-inbound-1"

    async def test_meshtastic_reply_relation_preserves_native_target(
        self, temp_storage
    ) -> None:
        """A Meshtastic reply event with a native target ref stores correctly."""
        # Build reply event with a native target relation
        reply_event = CanonicalEvent(
            event_id="ce-reply-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_adapter="mesh-test",
            source_transport_id="!nodeB",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id=None,
                    target_native_ref=NativeRef(
                        adapter="mesh-test",
                        native_channel_id="0",
                        native_message_id="400",
                    ),
                    key=None,
                    fallback_text=None,
                ),
            ),
            payload={"body": "Reply to original", "portnum": "text_message"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "packet_id": 401,
                        "from_id": "!nodeB",
                        "channel": 0,
                        "portnum": "text_message",
                        "to_id": "",
                        "is_direct_message": False,
                        "longname": "",
                        "shortname": "",
                        "reply_id": 400,
                        "emoji": None,
                        "emoji_flag": None,
                    }
                )
            ),
        )

        await temp_storage.append(reply_event)
        retrieved = await temp_storage.get("ce-reply-1")

        assert retrieved is not None
        assert len(retrieved.relations) == 1
        rel = retrieved.relations[0]
        assert rel.relation_type == "reply"
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.native_message_id == "400"


# ---------------------------------------------------------------------------
# Tests: Multiple Meshtastic events and event counts
# ---------------------------------------------------------------------------


class TestMeshtasticStorageCounts:
    """Storage correctly counts and queries Meshtastic-shaped events."""

    async def test_event_count_with_meshtastic_events(self, temp_storage) -> None:
        """Event count reflects Meshtastic events stored."""
        for i in range(5):
            event = _make_meshtastic_canonical_event(
                event_id=f"ce-count-{i}",
                node_id="!count_node",
                channel=0,
                packet_id=500 + i,
            )
            await temp_storage.append(event)

        count = await temp_storage.count_events()
        assert count == 5

    async def test_receipt_count_with_meshtastic_delivery(
        self, temp_storage
    ) -> None:
        """Receipt count reflects Meshtastic delivery receipts."""
        event = _make_meshtastic_canonical_event(event_id="ce-rcpt-1")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-1",
            event_id="ce-rcpt-1",
            target_adapter="mesh-test",
            status="sent",
            source="pipeline",
            route_id="route-1",
        )
        await temp_storage.append_receipt(receipt)

        count = await temp_storage.count_receipts()
        assert count == 1
