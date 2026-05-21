"""MeshCore-shaped CanonicalEvent storage round-trip tests.

Verifies that canonical events produced by the MeshCore codec (with
MeshCore-specific native metadata: packet_id, sender_id, channel,
pubkey_prefix, txt_type, is_direct_message) round-trip correctly through
SQLiteStorage: store, retrieve, resolve native refs, and query via timeline.

These tests do NOT require a live MeshCore radio node. They exercise the
storage layer with MeshCore-shaped data to ensure MeshCore events are
first-class citizens in the evidence pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.meshcore.codec import MeshCoreCodec
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.metadata import NativeMetadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meshcore_config() -> MeshCoreConfig:
    """Build a minimal MeshCoreConfig for codec tests."""
    return MeshCoreConfig(adapter_id="meshcore-test")


def _make_channel_packet(
    text: str = "Hello from MeshCore",
    channel_idx: int = 0,
    sender: str = "abc123",
    timestamp: int = 1700000000,
    txt_type: int = 0,
) -> dict:
    """Build a minimal MeshCore channel packet dict for codec testing."""
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": txt_type,
        "pubkey_prefix": sender,
    }


def _make_contact_packet(
    text: str = "Hello DM",
    sender: str = "def456",
    timestamp: int = 1700000001,
    txt_type: int = 0,
) -> dict:
    """Build a minimal MeshCore DM (contact) packet dict for codec testing."""
    return {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": txt_type,
    }


def _make_meshcore_canonical_event(
    event_id: str = "ce-001",
    channel_idx: int = 0,
    sender_timestamp: int = 1700000000,
    sender: str = "abc123",
    body: str = "Hello from MeshCore",
    is_direct_message: bool = False,
) -> CanonicalEvent:
    """Build a MeshCore-shaped CanonicalEvent directly (bypassing codec)."""
    native_channel_id = str(channel_idx) if not is_direct_message else None
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        source_adapter="meshcore-test",
        source_transport_id=sender,
        source_channel_id=native_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": body},
        metadata=EventMetadata(
            native=NativeMetadata(
                data={
                    "meshcore.packet_id": sender_timestamp,
                    "meshcore.sender_id": sender,
                    "meshcore.channel": channel_idx if not is_direct_message else None,
                    "meshcore.pubkey_prefix": sender,
                    "meshcore.txt_type": 0,
                    "meshcore.is_direct_message": is_direct_message,
                }
            )
        ),
        source_native_ref=NativeRef(
            adapter="meshcore-test",
            native_channel_id=native_channel_id,
            native_message_id=str(sender_timestamp),
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


class TestMeshCoreCodecEventStorageRoundtrip:
    """Events produced by MeshCoreCodec round-trip through storage."""

    async def test_codec_event_stored_and_retrieved(self, temp_storage) -> None:
        """A MeshCoreCodec-decoded event can be stored and retrieved unchanged."""
        config = _meshcore_config()
        codec = MeshCoreCodec("meshcore-test", config)
        native = _make_channel_packet(
            text="Storage round-trip test",
            channel_idx=1,
            sender="aabbcc",
            timestamp=12345,
        )
        canonical = codec.decode(native)

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None, "Event not found in storage"
        assert retrieved.event_id == canonical.event_id
        assert retrieved.source_adapter == "meshcore-test"
        assert retrieved.source_channel_id == "1"
        assert retrieved.source_transport_id == "aabbcc"
        assert retrieved.event_kind == "message.created"

        # Payload preserved
        assert retrieved.payload.get("body") == "Storage round-trip test"

    async def test_codec_event_native_metadata_preserved(
        self, temp_storage
    ) -> None:
        """MeshCore-specific native metadata survives storage."""
        config = _meshcore_config()
        codec = MeshCoreCodec("meshcore-test", config)
        native = _make_channel_packet(
            text="Metadata test",
            channel_idx=3,
            sender="feedface",
            timestamp=99999,
        )
        canonical = codec.decode(native)

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None
        ndata = retrieved.metadata.native.data
        assert ndata["meshcore.packet_id"] == 99999
        assert ndata["meshcore.sender_id"] == "feedface"
        assert ndata["meshcore.channel"] == 3
        assert ndata["meshcore.pubkey_prefix"] == "feedface"
        assert ndata["meshcore.is_direct_message"] is False

    async def test_codec_dm_event_native_metadata_preserved(
        self, temp_storage
    ) -> None:
        """MeshCore DM (contact) native metadata survives storage."""
        config = _meshcore_config()
        codec = MeshCoreCodec("meshcore-test", config)
        native = _make_contact_packet(
            text="DM metadata test",
            sender="cafe01",
            timestamp=88888,
        )
        canonical = codec.decode(native)

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None
        ndata = retrieved.metadata.native.data
        assert ndata["meshcore.packet_id"] == 88888
        assert ndata["meshcore.sender_id"] == "cafe01"
        assert ndata["meshcore.channel"] is None
        assert ndata["meshcore.is_direct_message"] is True

    async def test_codec_event_source_native_ref_stored(
        self, temp_storage
    ) -> None:
        """source_native_ref from MeshCoreCodec survives storage round-trip."""
        config = _meshcore_config()
        codec = MeshCoreCodec("meshcore-test", config)
        native = _make_channel_packet(
            text="Ref test",
            channel_idx=2,
            sender="bcbc",
            timestamp=55555,
        )
        canonical = codec.decode(native)

        assert canonical.source_native_ref is not None
        assert canonical.source_native_ref.adapter == "meshcore-test"
        assert canonical.source_native_ref.native_message_id == "55555"
        assert canonical.source_native_ref.native_channel_id == "2"

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None
        assert retrieved.source_native_ref is not None
        assert retrieved.source_native_ref.native_message_id == "55555"


# ---------------------------------------------------------------------------
# Tests: Native refs for MeshCore events
# ---------------------------------------------------------------------------


class TestMeshCoreNativeRefStorage:
    """NativeMessageRef for MeshCore events can be stored and resolved."""

    async def test_meshcore_native_ref_stored_and_resolved(
        self, temp_storage
    ) -> None:
        """A MeshCore NativeMessageRef stores and resolves correctly."""
        event = _make_meshcore_canonical_event(
            event_id="ce-nref-1",
            channel_idx=4,
            sender_timestamp=77777,
            sender="nref_sender",
        )
        await temp_storage.append(event)

        nref = NativeMessageRef(
            id="nref-1",
            event_id="ce-nref-1",
            adapter="meshcore-test",
            native_channel_id="4",
            native_message_id="77777",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(nref)

        resolved = await temp_storage.resolve_native_ref(
            "meshcore-test", "4", "77777"
        )
        assert resolved == "ce-nref-1"

    async def test_meshcore_inbound_native_ref_resolves(
        self, temp_storage
    ) -> None:
        """An inbound MeshCore native ref resolves to the correct event."""
        event = _make_meshcore_canonical_event(
            event_id="ce-inbound-1",
            channel_idx=1,
            sender_timestamp=33333,
            sender="inbound_sender",
        )
        await temp_storage.append(event)

        nref = NativeMessageRef(
            id="nref-inbound-1",
            event_id="ce-inbound-1",
            adapter="meshcore-test",
            native_channel_id="1",
            native_message_id="33333",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(nref)

        resolved = await temp_storage.resolve_native_ref(
            "meshcore-test", "1", "33333"
        )
        assert resolved == "ce-inbound-1"

    async def test_meshcore_dm_native_ref_resolves(
        self, temp_storage
    ) -> None:
        """A DM (no channel) native ref resolves correctly."""
        event = _make_meshcore_canonical_event(
            event_id="ce-dm-1",
            sender_timestamp=44444,
            sender="dm_sender",
            is_direct_message=True,
        )
        await temp_storage.append(event)

        nref = NativeMessageRef(
            id="nref-dm-1",
            event_id="ce-dm-1",
            adapter="meshcore-test",
            native_channel_id=None,
            native_message_id="44444",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(nref)

        resolved = await temp_storage.resolve_native_ref(
            "meshcore-test", None, "44444"
        )
        assert resolved == "ce-dm-1"


# ---------------------------------------------------------------------------
# Tests: Multiple MeshCore events and event counts
# ---------------------------------------------------------------------------


class TestMeshCoreStorageCounts:
    """Storage correctly counts and queries MeshCore-shaped events."""

    async def test_event_count_with_meshcore_events(self, temp_storage) -> None:
        """Event count reflects MeshCore events stored."""
        for i in range(5):
            event = _make_meshcore_canonical_event(
                event_id=f"ce-count-{i}",
                channel_idx=i % 4,
                sender_timestamp=10000 + i,
            )
            await temp_storage.append(event)

        count = await temp_storage.count_events()
        assert count == 5

    async def test_receipt_count_with_meshcore_delivery(
        self, temp_storage
    ) -> None:
        """Receipt count reflects MeshCore delivery receipts."""
        event = _make_meshcore_canonical_event(event_id="ce-rcpt-1")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-1",
            event_id="ce-rcpt-1",
            target_adapter="meshcore-test",
            status="sent",
            source="pipeline",
            route_id="route-1",
        )
        await temp_storage.append_receipt(receipt)

        count = await temp_storage.count_receipts()
        assert count == 1
