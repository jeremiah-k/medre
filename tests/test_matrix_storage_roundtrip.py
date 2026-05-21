"""Matrix-shaped CanonicalEvent storage round-trip tests.

Verifies that canonical events produced by the Matrix codec (with
Matrix-specific native metadata: room_id, event_id, sender in native.data)
round-trip correctly through SQLiteStorage: store, retrieve, resolve native
refs, and query via timeline.

These tests do NOT require a live Matrix homeserver. They exercise the
storage layer with Matrix-shaped data to ensure Matrix events are
first-class citizens in the evidence pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.matrix.codec import MatrixCodec
from medre.config.adapters.matrix import MatrixConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.metadata import NativeMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matrix_config() -> MatrixConfig:
    """Build a minimal MatrixConfig for codec tests."""
    return MatrixConfig(
        adapter_id="matrix-test",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="fake_test_token",
    )


def _make_fake_matrix_event(
    event_id: str = "$event123:example.com",
    sender: str = "@alice:example.com",
    body: str = "Hello from Matrix",
    room_id: str = "!room:example.com",
    msgtype: str = "m.text",
    origin_server_ts: int = 1700000000000,
) -> object:
    """Build a minimal nio-like event object for codec testing.

    Returns a simple namespace object with the attributes the codec expects:
    .sender, .body, .event_id, .source, .server_timestamp.
    """

    class FakeEvent:
        __slots__ = ("sender", "body", "event_id", "msgtype", "source", "server_timestamp")

        def __init__(self) -> None:
            self.sender: str = ""
            self.body: str = ""
            self.event_id: str = ""
            self.msgtype: str = ""
            self.source: dict[str, object] = {}
            self.server_timestamp: int = 0

    evt = FakeEvent()
    evt.sender = sender
    evt.body = body
    evt.event_id = event_id
    evt.msgtype = msgtype
    evt.source = {
        "content": {
            "msgtype": msgtype,
            "body": body,
        },
        "origin_server_ts": origin_server_ts,
        "sender": sender,
        "event_id": event_id,
        "type": "m.room.message",
    }
    evt.server_timestamp = origin_server_ts
    return evt


def _make_matrix_canonical_event(
    event_id: str = "ce-001",
    room_id: str = "!room:example.com",
    mx_event_id: str = "$mx001:example.com",
    sender: str = "@alice:example.com",
    body: str = "Hello from Matrix",
) -> CanonicalEvent:
    """Build a Matrix-shaped CanonicalEvent directly (bypassing codec)."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="matrix-test",
        source_transport_id=sender,
        source_channel_id=room_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": body, "msgtype": "m.text"},
        metadata=EventMetadata(
            native=NativeMetadata(
                data={
                    "room_id": room_id,
                    "event_id": mx_event_id,
                    "sender": sender,
                }
            )
        ),
        source_native_ref=NativeRef(
            adapter="matrix-test",
            native_channel_id=room_id,
            native_message_id=mx_event_id,
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


class TestMatrixCodecEventStorageRoundtrip:
    """Events produced by MatrixCodec round-trip through storage."""

    async def test_codec_event_stored_and_retrieved(self, temp_storage) -> None:
        """A MatrixCodec-decoded event can be stored and retrieved unchanged."""
        config = _matrix_config()
        codec = MatrixCodec("matrix-test", config)
        native = _make_fake_matrix_event(
            event_id="$store_rt_1:example.com",
            sender="@bob:example.com",
            body="Storage round-trip test",
            room_id="!test_room:example.com",
        )
        canonical = codec.decode(native, room_id="!test_room:example.com")

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None, "Event not found in storage"
        assert retrieved.event_id == canonical.event_id
        assert retrieved.source_adapter == "matrix-test"
        assert retrieved.source_channel_id == "!test_room:example.com"
        assert retrieved.source_transport_id == "@bob:example.com"
        assert retrieved.event_kind == "message.created"

        # Payload preserved
        assert retrieved.payload.get("body") == "Storage round-trip test"
        assert retrieved.payload.get("msgtype") == "m.text"

    async def test_codec_event_native_metadata_preserved(
        self, temp_storage
    ) -> None:
        """Matrix-specific native metadata (room_id, event_id, sender) survives storage."""
        config = _matrix_config()
        codec = MatrixCodec("matrix-test", config)
        native = _make_fake_matrix_event(
            event_id="$meta_test:example.com",
            sender="@carol:example.com",
            body="Metadata test",
            room_id="!meta_room:example.com",
        )
        canonical = codec.decode(native, room_id="!meta_room:example.com")

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None
        ndata = retrieved.metadata.native.data
        assert ndata["room_id"] == "!meta_room:example.com"
        assert ndata["event_id"] == "$meta_test:example.com"
        assert ndata["sender"] == "@carol:example.com"

    async def test_codec_event_source_native_ref_stored(
        self, temp_storage
    ) -> None:
        """source_native_ref from MatrixCodec survives storage round-trip."""
        config = _matrix_config()
        codec = MatrixCodec("matrix-test", config)
        native = _make_fake_matrix_event(
            event_id="$ref_test:example.com",
            sender="@dave:example.com",
        )
        canonical = codec.decode(native, room_id="!ref_room:example.com")

        assert canonical.source_native_ref is not None
        assert canonical.source_native_ref.adapter == "matrix-test"
        assert canonical.source_native_ref.native_message_id == "$ref_test:example.com"
        assert canonical.source_native_ref.native_channel_id == "!ref_room:example.com"

        await temp_storage.append(canonical)
        retrieved = await temp_storage.get(canonical.event_id)

        assert retrieved is not None
        assert retrieved.source_native_ref is not None
        assert retrieved.source_native_ref.native_message_id == "$ref_test:example.com"


# ---------------------------------------------------------------------------
# Tests: Native refs for Matrix events
# ---------------------------------------------------------------------------


class TestMatrixNativeRefStorage:
    """NativeMessageRef for Matrix events can be stored and resolved."""

    async def test_matrix_native_ref_stored_and_resolved(
        self, temp_storage
    ) -> None:
        """A Matrix NativeMessageRef stores and resolves correctly."""
        event = _make_matrix_canonical_event(
            event_id="ce-nref-1",
            room_id="!nref_room:example.com",
            mx_event_id="$nref_mx:example.com",
        )
        await temp_storage.append(event)

        nref = NativeMessageRef(
            id="nref-1",
            event_id="ce-nref-1",
            adapter="matrix-test",
            native_channel_id="!nref_room:example.com",
            native_message_id="$nref_mx:example.com",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(nref)

        resolved = await temp_storage.resolve_native_ref(
            "matrix-test", "!nref_room:example.com", "$nref_mx:example.com"
        )
        assert resolved == "ce-nref-1"

    async def test_matrix_inbound_native_ref_resolves(
        self, temp_storage
    ) -> None:
        """An inbound Matrix native ref resolves to the correct event."""
        event = _make_matrix_canonical_event(
            event_id="ce-inbound-1",
            room_id="!inbound:example.com",
            mx_event_id="$inbound_mx:example.com",
            sender="@sender:example.com",
        )
        await temp_storage.append(event)

        nref = NativeMessageRef(
            id="nref-inbound-1",
            event_id="ce-inbound-1",
            adapter="matrix-test",
            native_channel_id="!inbound:example.com",
            native_message_id="$inbound_mx:example.com",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(nref)

        resolved = await temp_storage.resolve_native_ref(
            "matrix-test", "!inbound:example.com", "$inbound_mx:example.com"
        )
        assert resolved == "ce-inbound-1"

    async def test_matrix_reply_relation_preserves_native_target(
        self, temp_storage
    ) -> None:
        """A Matrix reply event with a native target ref stores correctly."""
        event = _make_matrix_canonical_event(
            event_id="ce-reply-1",
            room_id="!reply_room:example.com",
            mx_event_id="$reply_mx:example.com",
        )
        # Replace relations with a reply
        reply_event = CanonicalEvent(
            event_id="ce-reply-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-test",
            source_transport_id="@alice:example.com",
            source_channel_id="!reply_room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id=None,
                    target_native_ref=NativeRef(
                        adapter="matrix-test",
                        native_channel_id="!reply_room:example.com",
                        native_message_id="$original_mx:example.com",
                    ),
                    key=None,
                    fallback_text=None,
                ),
            ),
            payload={"body": "Reply to original", "msgtype": "m.text"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "room_id": "!reply_room:example.com",
                        "event_id": "$reply_mx:example.com",
                        "sender": "@alice:example.com",
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
        assert rel.target_native_ref.native_message_id == "$original_mx:example.com"


# ---------------------------------------------------------------------------
# Tests: Multiple Matrix events and event counts
# ---------------------------------------------------------------------------


class TestMatrixStorageCounts:
    """Storage correctly counts and queries Matrix-shaped events."""

    async def test_event_count_with_matrix_events(self, temp_storage) -> None:
        """Event count reflects Matrix events stored."""
        for i in range(5):
            event = _make_matrix_canonical_event(
                event_id=f"ce-count-{i}",
                room_id="!count_room:example.com",
                mx_event_id=f"$count_mx_{i}:example.com",
            )
            await temp_storage.append(event)

        count = await temp_storage.count_events()
        assert count == 5

    async def test_receipt_count_with_matrix_delivery(
        self, temp_storage
    ) -> None:
        """Receipt count reflects Matrix delivery receipts."""
        from medre.core.events.canonical import DeliveryReceipt

        event = _make_matrix_canonical_event(event_id="ce-rcpt-1")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-1",
            event_id="ce-rcpt-1",
            target_adapter="matrix-test",
            status="sent",
            source="pipeline",
            route_id="route-1",
        )
        await temp_storage.append_receipt(receipt)

        count = await temp_storage.count_receipts()
        assert count == 1
