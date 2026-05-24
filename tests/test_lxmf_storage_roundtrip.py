"""LXMF-shaped CanonicalEvent storage round-trip tests.

Verifies that canonical events produced by the LXMF codec (with
LXMF-specific native metadata: source_hash, destination_hash, message_id,
timestamp, title, delivery_method, has_fields) round-trip correctly through
SQLiteStorage: store, retrieve, resolve native refs, and query via timeline.

These tests do NOT require a live Reticulum instance. They exercise the
storage layer with LXMF-shaped data to ensure LXMF events are first-class
citizens in the evidence pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.lxmf.codec import LxmfCodec
from medre.config.adapters.lxmf import LxmfConfig
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


def _lxmf_config() -> LxmfConfig:
    """Build a minimal LxmfConfig for codec tests."""
    return LxmfConfig(
        adapter_id="lxmf-test",
        connection_type="fake",
    )


def _make_fake_lxmf_packet(
    source_hash: str = "ab" * 16,
    destination_hash: str = "00" * 16,
    message_id: str = "cd" * 32,
    content: str = "Hello from LXMF",
    title: str = "",
    timestamp: float = 1700000000.0,
    delivery_method: str = "direct",
    fields: dict | None = None,
    signature_validated: bool = True,
) -> dict:
    """Build a minimal LXMF packet dict for codec testing.

    Returns a plain dict matching the shape expected by LxmfCodec.decode().
    """
    return {
        "source_hash": source_hash,
        "destination_hash": destination_hash,
        "message_id": message_id,
        "content": content,
        "title": title,
        "fields": fields if fields is not None else {},
        "timestamp": timestamp,
        "delivery_method": delivery_method,
        "signature_validated": signature_validated,
        "has_fields": fields is not None and len(fields) > 0,
    }


def _make_lxmf_canonical_event(
    event_id: str = "ce-001",
    source_hash: str = "ab" * 16,
    lxmf_message_id: str = "cd" * 32,
    content: str = "Hello from LXMF",
    title: str = "",
    destination_hash: str = "00" * 16,
) -> CanonicalEvent:
    """Build an LXMF-shaped CanonicalEvent directly (bypassing codec)."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        source_adapter="lxmf-test",
        source_transport_id=source_hash,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={
            "body": content,
            "portnum": "lxmf",
            **({"title": title} if title else {}),
        },
        metadata=EventMetadata(
            native=NativeMetadata(
                data={
                    "source_hash": source_hash,
                    "destination_hash": destination_hash,
                    "message_id": lxmf_message_id,
                    "timestamp": 1700000000.0,
                    "title": title,
                    "delivery_method": "direct",
                    "has_fields": False,
                }
            )
        ),
        source_native_ref=NativeRef(
            adapter="lxmf-test",
            native_channel_id=None,
            native_message_id=lxmf_message_id,
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


class TestLxmfCodecEventStorageRoundtrip:
    """Events produced by LxmfCodec round-trip through storage."""

    async def test_codec_event_stored_and_retrieved(self, temp_storage) -> None:
        """An LxmfCodec-decoded event can be stored and retrieved unchanged."""
        config = _lxmf_config()
        codec = LxmfCodec("lxmf-test", config)
        native = _make_fake_lxmf_packet(
            source_hash="ef" * 16,
            content="Storage round-trip test",
            message_id="aa" * 32,
        )
        canonical = codec.decode(native)

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None, "Event not found in storage"
        assert retrieved.event_id == canonical.event_id
        assert retrieved.source_adapter == "lxmf-test"
        assert retrieved.source_channel_id is None
        assert retrieved.source_transport_id == "ef" * 16
        assert retrieved.event_kind == "message.created"

        # Payload preserved
        assert retrieved.payload.get("body") == "Storage round-trip test"
        assert retrieved.payload.get("portnum") == "lxmf"

    async def test_codec_event_native_metadata_preserved(self, temp_storage) -> None:
        """LXMF-specific native metadata survives storage round-trip."""
        config = _lxmf_config()
        codec = LxmfCodec("lxmf-test", config)
        native = _make_fake_lxmf_packet(
            source_hash="11" * 16,
            destination_hash="22" * 16,
            message_id="33" * 32,
            content="Metadata test",
        )
        canonical = codec.decode(native)

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None
        ndata = retrieved.metadata.native.data
        assert ndata["source_hash"] == "11" * 16
        assert ndata["destination_hash"] == "22" * 16
        assert ndata["message_id"] == "33" * 32
        assert ndata["delivery_method"] == "direct"

    async def test_codec_event_source_native_ref_stored(self, temp_storage) -> None:
        """source_native_ref from LxmfCodec survives storage round-trip."""
        config = _lxmf_config()
        codec = LxmfCodec("lxmf-test", config)
        native = _make_fake_lxmf_packet(
            source_hash="44" * 16,
            message_id="55" * 32,
        )
        canonical = codec.decode(native)

        assert canonical.source_native_ref is not None
        assert canonical.source_native_ref.adapter == "lxmf-test"
        assert canonical.source_native_ref.native_message_id == "55" * 32

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None
        assert retrieved.source_native_ref is not None
        assert retrieved.source_native_ref.native_message_id == "55" * 32


# ---------------------------------------------------------------------------
# Tests: Native refs for LXMF events
# ---------------------------------------------------------------------------


class TestLxmfNativeRefStorage:
    """NativeMessageRef for LXMF events can be stored and resolved."""

    async def test_lxmf_native_ref_stored_and_resolved(self, temp_storage) -> None:
        """An LXMF NativeMessageRef stores and resolves correctly."""
        event = _make_lxmf_canonical_event(
            event_id="ce-nref-1",
            source_hash="aa" * 16,
            lxmf_message_id="bb" * 32,
        )
        await temp_storage.append(event)

        nref = NativeMessageRef(
            id="nref-1",
            event_id="ce-nref-1",
            adapter="lxmf-test",
            native_channel_id=None,
            native_message_id="bb" * 32,
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(nref)

        resolved = await temp_storage.resolve_native_ref("lxmf-test", None, "bb" * 32)
        assert resolved == "ce-nref-1"

    async def test_lxmf_inbound_native_ref_resolves(self, temp_storage) -> None:
        """An inbound LXMF native ref resolves to the correct event."""
        event = _make_lxmf_canonical_event(
            event_id="ce-inbound-1",
            source_hash="cc" * 16,
            lxmf_message_id="dd" * 32,
        )
        await temp_storage.append(event)

        nref = NativeMessageRef(
            id="nref-inbound-1",
            event_id="ce-inbound-1",
            adapter="lxmf-test",
            native_channel_id=None,
            native_message_id="dd" * 32,
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(nref)

        resolved = await temp_storage.resolve_native_ref("lxmf-test", None, "dd" * 32)
        assert resolved == "ce-inbound-1"


# ---------------------------------------------------------------------------
# Tests: Multiple LXMF events and event counts
# ---------------------------------------------------------------------------


class TestLxmfStorageCounts:
    """Storage correctly counts and queries LXMF-shaped events."""

    async def test_event_count_with_lxmf_events(self, temp_storage) -> None:
        """Event count reflects LXMF events stored."""
        for i in range(5):
            event = _make_lxmf_canonical_event(
                event_id=f"ce-count-{i}",
                source_hash="ee" * 16,
                lxmf_message_id=f"{i:064x}",
            )
            await temp_storage.append(event)

        count = await temp_storage.count_events()
        assert count == 5

    async def test_receipt_count_with_lxmf_delivery(self, temp_storage) -> None:
        """Receipt count reflects LXMF delivery receipts."""
        event = _make_lxmf_canonical_event(event_id="ce-rcpt-1")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-1",
            event_id="ce-rcpt-1",
            target_adapter="lxmf-test",
            status="sent",
            source="pipeline",
            route_id="route-1",
        )
        await temp_storage.append_receipt(receipt)

        count = await temp_storage.count_receipts()
        assert count == 1
