"""Tests for SQLiteStorage: append/get, native ref resolve, relations,
receipts, query with EventFilter, idempotent native refs, append-only
receipts, ordering guarantees, and close/reopen persistence.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.storage import EventFilter, SQLiteStorage


# Helper to build a minimal event quickly.
def _make_event(
    event_id: str = "evt-1",
    event_kind: str = "message.created",
    payload: dict | None = None,
    source_adapter: str = "fake_transport",
    source_channel_id: str | None = "ch-0",
    relations: tuple[EventRelation, ...] | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# Append / Get round-trip
# ===================================================================


class TestAppendAndGet:
    """append() then get() must return an equivalent event."""

    async def test_append_and_get_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = _make_event()
        await temp_storage.append(event)
        retrieved = await temp_storage.get(event.event_id)
        assert retrieved is not None
        assert retrieved.event_id == event.event_id
        assert retrieved.event_kind == event.event_kind
        assert retrieved.payload == event.payload

    async def test_get_returns_none_for_unknown_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        result = await temp_storage.get("does-not-exist")
        assert result is None


# ===================================================================
# Native ref storage and resolution
# ===================================================================


class TestNativeRef:
    """store_native_ref / resolve_native_ref."""

    async def test_store_and_resolve_native_ref(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After storing a native ref, resolve returns the canonical event_id."""
        # Must have the event first for FK constraint.
        event = _make_event(event_id="evt-ref-1")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-1",
            event_id="evt-ref-1",
            adapter="fake_transport",
            native_channel_id="ch-0",
            native_message_id="native-msg-10",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)

        resolved = await temp_storage.resolve_native_ref(
            "fake_transport", "ch-0", "native-msg-10"
        )
        assert resolved == "evt-ref-1"

    async def test_resolve_native_ref_returns_none_for_unknown(
        self, temp_storage: SQLiteStorage
    ) -> None:
        result = await temp_storage.resolve_native_ref(
            "unknown_adapter", None, "no-msg"
        )
        assert result is None


# ===================================================================
# Relations storage
# ===================================================================


class TestRelations:
    """store_relation / list_relations."""

    async def test_store_and_list_relations(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = _make_event(event_id="evt-rel-1")
        await temp_storage.append(event)

        nref = NativeRef(
            adapter="discord",
            native_channel_id="room-1",
            native_message_id="msg-1",
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target-evt-1",
            target_native_ref=nref,
            key=None,
            fallback_text="original",
        )
        await temp_storage.store_relation("evt-rel-1", relation)

        relations = await temp_storage.list_relations("evt-rel-1")
        assert len(relations) == 1
        assert relations[0].relation_type == "reply"
        assert relations[0].target_event_id == "target-evt-1"
        assert relations[0].target_native_ref is not None
        assert relations[0].target_native_ref.adapter == "discord"

    async def test_list_relations_returns_empty_for_no_relations(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = _make_event(event_id="evt-no-rel")
        await temp_storage.append(event)

        relations = await temp_storage.list_relations("evt-no-rel")
        assert relations == []

    async def test_inline_relations_stored_on_append(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Relations embedded in an event are stored on append."""
        relation = EventRelation(
            relation_type="reaction",
            target_event_id="target-1",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_event(event_id="evt-inline", relations=(relation,))
        await temp_storage.append(event)

        stored = await temp_storage.list_relations("evt-inline")
        assert len(stored) == 1
        assert stored[0].relation_type == "reaction"
        assert stored[0].key == "👍"


# ===================================================================
# Delivery receipts
# ===================================================================


class TestReceipts:
    """append_receipt / delivery_status."""

    async def test_append_receipt_and_delivery_status(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = _make_event(event_id="evt-rcpt")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-1",
            event_id="evt-rcpt",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="sent",
        )
        await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status("plan-1", "fake_presentation")
        assert status is not None
        assert status.receipt_id == "rcpt-1"
        assert status.status == "sent"

    async def test_delivery_status_returns_latest_receipt(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After appending 3 receipts, delivery_status returns the latest."""
        event = _make_event(event_id="evt-multi-rcpt")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "confirmed"]):
            receipt = DeliveryReceipt(
                receipt_id=f"rcpt-{i}",
                event_id="evt-multi-rcpt",
                delivery_plan_id="plan-2",
                target_adapter="fake_presentation",
                status=st,  # type: ignore[arg-type]
            )
            await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status("plan-2", "fake_presentation")
        assert status is not None
        assert status.status == "confirmed"
        assert status.receipt_id == "rcpt-2"

    async def test_delivery_status_returns_none_for_unknown(
        self, temp_storage: SQLiteStorage
    ) -> None:
        status = await temp_storage.delivery_status("no-plan", "no-adapter")
        assert status is None


# ===================================================================
# Query with EventFilter
# ===================================================================


class TestQuery:
    """query() with EventFilter by event_kind, source_adapter, and limit."""

    async def _seed_events(self, storage: SQLiteStorage) -> None:
        events = [
            _make_event(event_id="q-1", event_kind="message.created"),
            _make_event(event_id="q-2", event_kind="message.text"),
            _make_event(event_id="q-3", event_kind="telemetry.received"),
            _make_event(
                event_id="q-4",
                event_kind="message.created",
                source_adapter="other_adapter",
            ),
        ]
        for e in events:
            await storage.append(e)

    async def test_query_by_event_kind(
        self, temp_storage: SQLiteStorage
    ) -> None:
        await self._seed_events(temp_storage)
        filt = EventFilter(event_kinds=["message.created"])
        results = [e async for e in temp_storage.query(filt)]
        ids = {e.event_id for e in results}
        assert ids == {"q-1", "q-4"}

    async def test_query_by_source_adapter(
        self, temp_storage: SQLiteStorage
    ) -> None:
        await self._seed_events(temp_storage)
        filt = EventFilter(source_adapters=["other_adapter"])
        results = [e async for e in temp_storage.query(filt)]
        assert len(results) == 1
        assert results[0].event_id == "q-4"

    async def test_query_with_limit(
        self, temp_storage: SQLiteStorage
    ) -> None:
        await self._seed_events(temp_storage)
        filt = EventFilter(limit=2)
        results = [e async for e in temp_storage.query(filt)]
        assert len(results) == 2

    async def test_query_returns_empty_when_no_match(
        self, temp_storage: SQLiteStorage
    ) -> None:
        await self._seed_events(temp_storage)
        filt = EventFilter(event_kinds=["nonexistent.kind"])
        results = [e async for e in temp_storage.query(filt)]
        assert results == []


# ===================================================================
# Close / Reopen persistence
# ===================================================================


class TestPersistence:
    """Data survives close() and re-initialize()."""

    async def test_reopen_reads_existing_events(self) -> None:
        """Events written before close() are readable after reopen."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Write phase.
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()
            event = _make_event(event_id="persist-1")
            await storage.append(event)
            await storage.close()

            # Read phase — new storage instance on same file.
            storage2 = SQLiteStorage(db_path=db_path)
            await storage2.initialize()
            retrieved = await storage2.get("persist-1")
            assert retrieved is not None
            assert retrieved.event_id == "persist-1"
            assert retrieved.event_kind == event.event_kind
            await storage2.close()
        finally:
            os.unlink(db_path)


# ===================================================================
# Idempotent native refs
# ===================================================================


class TestIdempotentNativeRef:
    """store_native_ref with duplicate (adapter, channel, message) is idempotent."""

    async def test_store_same_ref_twice_is_idempotent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Storing the same native ref twice must not raise and must resolve."""
        event = _make_event(event_id="evt-idem-1")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-idem-1",
            event_id="evt-idem-1",
            adapter="fake_transport",
            native_channel_id="ch-0",
            native_message_id="msg-dup",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)
        # Second store with identical (adapter, native_channel_id, native_message_id).
        await temp_storage.store_native_ref(ref)

        resolved = await temp_storage.resolve_native_ref(
            "fake_transport", "ch-0", "msg-dup"
        )
        assert resolved == "evt-idem-1"

    async def test_store_different_refs_same_event_allowed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Multiple distinct native refs pointing to the same event are allowed."""
        event = _make_event(event_id="evt-multi-ref")
        await temp_storage.append(event)

        ref_a = NativeMessageRef(
            id="nref-mr-a",
            event_id="evt-multi-ref",
            adapter="adapter_a",
            native_channel_id="ch-a",
            native_message_id="msg-a",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        ref_b = NativeMessageRef(
            id="nref-mr-b",
            event_id="evt-multi-ref",
            adapter="adapter_b",
            native_channel_id="ch-b",
            native_message_id="msg-b",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref_a)
        await temp_storage.store_native_ref(ref_b)

        assert await temp_storage.resolve_native_ref("adapter_a", "ch-a", "msg-a") == "evt-multi-ref"
        assert await temp_storage.resolve_native_ref("adapter_b", "ch-b", "msg-b") == "evt-multi-ref"

    async def test_missing_native_ref_returns_none(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Resolving a native ref that was never stored returns None."""
        result = await temp_storage.resolve_native_ref(
            "no_such_adapter", None, "no_such_msg"
        )
        assert result is None


# ===================================================================
# Append-only receipts
# ===================================================================


class TestAppendOnlyReceipts:
    """Receipts are append-only; delivery_status is a read-only projection."""

    async def test_append_receipt_creates_new_row_each_time(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Each append_receipt call creates a new row in delivery_receipts."""
        event = _make_event(event_id="evt-rcpt-row")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "confirmed"]):
            receipt = DeliveryReceipt(
                receipt_id=f"rcpt-row-{i}",
                event_id="evt-rcpt-row",
                delivery_plan_id="plan-row",
                target_adapter="adapter_x",
                status=st,  # type: ignore[arg-type]
            )
            await temp_storage.append_receipt(receipt)

        rows = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE delivery_plan_id = ? AND target_adapter = ? ORDER BY sequence ASC",
            ("plan-row", "adapter_x"),
        )
        assert len(rows) == 3
        assert rows[0]["status"] == "queued"
        assert rows[1]["status"] == "sent"
        assert rows[2]["status"] == "confirmed"

    async def test_delivery_status_is_projection_not_mutable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """delivery_status returns the latest receipt via MAX(sequence) projection."""
        event = _make_event(event_id="evt-proj")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "confirmed"]):
            receipt = DeliveryReceipt(
                receipt_id=f"rcpt-proj-{i}",
                event_id="evt-proj",
                delivery_plan_id="plan-proj",
                target_adapter="adapter_y",
                status=st,  # type: ignore[arg-type]
            )
            await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status("plan-proj", "adapter_y")
        assert status is not None
        assert status.status == "confirmed"
        assert status.receipt_id == "rcpt-proj-2"

    async def test_receipts_never_updated_or_deleted(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """All historical receipt rows persist after reading delivery_status."""
        event = _make_event(event_id="evt-hist")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "failed"]):
            receipt = DeliveryReceipt(
                receipt_id=f"rcpt-hist-{i}",
                event_id="evt-hist",
                delivery_plan_id="plan-hist",
                target_adapter="adapter_z",
                status=st,  # type: ignore[arg-type]
            )
            await temp_storage.append_receipt(receipt)

        # Consume delivery_status — this must not mutate receipt rows.
        await temp_storage.delivery_status("plan-hist", "adapter_z")

        rows = await temp_storage._read_all(
            "SELECT receipt_id, status FROM delivery_receipts WHERE delivery_plan_id = ? AND target_adapter = ? ORDER BY sequence ASC",
            ("plan-hist", "adapter_z"),
        )
        assert len(rows) == 3
        assert [r["receipt_id"] for r in rows] == ["rcpt-hist-0", "rcpt-hist-1", "rcpt-hist-2"]
        assert [r["status"] for r in rows] == ["queued", "sent", "failed"]


# ===================================================================
# Ordering guarantees
# ===================================================================


class TestOrderingGuarantees:
    """Relations and query results respect ordering guarantees."""

    async def test_list_relations_ordered_by_insertion(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Relations are returned in insertion (id ASC) order."""
        event = _make_event(event_id="evt-ord-rel")
        await temp_storage.append(event)

        relation_types = ["reply", "reaction", "thread"]
        for i, rt in enumerate(relation_types):
            relation = EventRelation(
                relation_type=rt,  # type: ignore[arg-type]
                target_event_id=f"target-{i}",
                target_native_ref=None,
                key=None,
                fallback_text=None,
            )
            await temp_storage.store_relation("evt-ord-rel", relation)

        relations = await temp_storage.list_relations("evt-ord-rel")
        assert [r.relation_type for r in relations] == ["reply", "reaction", "thread"]

    async def test_query_ordered_by_timestamp(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """query() returns events ordered by timestamp ascending."""
        base = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        events = [
            CanonicalEvent(
                event_id="ts-3",
                event_kind="message.created",
                schema_version=1,
                timestamp=base.replace(hour=3),
                source_adapter="fake_transport",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "hour3"},
                metadata=EventMetadata(),
            ),
            CanonicalEvent(
                event_id="ts-1",
                event_kind="message.created",
                schema_version=1,
                timestamp=base.replace(hour=1),
                source_adapter="fake_transport",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "hour1"},
                metadata=EventMetadata(),
            ),
            CanonicalEvent(
                event_id="ts-2",
                event_kind="message.created",
                schema_version=1,
                timestamp=base.replace(hour=2),
                source_adapter="fake_transport",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "hour2"},
                metadata=EventMetadata(),
            ),
        ]
        # Append in non-sorted order.
        for e in [events[0], events[1], events[2]]:
            await temp_storage.append(e)

        filt = EventFilter(limit=10)
        results = [e async for e in temp_storage.query(filt)]
        assert [e.event_id for e in results] == ["ts-1", "ts-2", "ts-3"]


# ===================================================================
# Receipt lineage
# ===================================================================


class TestReceiptLineage:
    """Receipt lineage: attempt_number, parent_receipt_id persistence
    and ordering via list_receipts_for_plan.
    """

    async def test_receipt_attempt_number_persisted(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """attempt_number is persisted and readable."""
        event = _make_event(event_id="evt-lineage-1")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-lin-1",
            event_id="evt-lineage-1",
            delivery_plan_id="plan-lin",
            target_adapter="adapter_a",
            status="failed",
            attempt_number=3,
            parent_receipt_id="rcpt-lin-0",
        )
        await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status("plan-lin", "adapter_a")
        assert status is not None
        assert status.attempt_number == 3
        assert status.parent_receipt_id == "rcpt-lin-0"

    async def test_receipt_lineage_chain(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A chain of receipts linked by parent_receipt_id."""
        event = _make_event(event_id="evt-chain")
        await temp_storage.append(event)

        r1 = DeliveryReceipt(
            receipt_id="rcpt-chain-1",
            event_id="evt-chain",
            delivery_plan_id="plan-chain",
            target_adapter="adapter_b",
            status="failed",
            attempt_number=1,
            parent_receipt_id=None,
        )
        await temp_storage.append_receipt(r1)

        r2 = DeliveryReceipt(
            receipt_id="rcpt-chain-2",
            event_id="evt-chain",
            delivery_plan_id="plan-chain",
            target_adapter="adapter_b",
            status="failed",
            attempt_number=2,
            parent_receipt_id="rcpt-chain-1",
        )
        await temp_storage.append_receipt(r2)

        r3 = DeliveryReceipt(
            receipt_id="rcpt-chain-3",
            event_id="evt-chain",
            delivery_plan_id="plan-chain",
            target_adapter="adapter_b",
            status="dead_lettered",
            attempt_number=3,
            parent_receipt_id="rcpt-chain-2",
        )
        await temp_storage.append_receipt(r3)

        # list_receipts_for_plan returns all in attempt order.
        receipts = await temp_storage.list_receipts_for_plan(
            "plan-chain", "adapter_b"
        )
        assert len(receipts) == 3
        assert [r.attempt_number for r in receipts] == [1, 2, 3]
        assert receipts[0].parent_receipt_id is None
        assert receipts[1].parent_receipt_id == "rcpt-chain-1"
        assert receipts[2].parent_receipt_id == "rcpt-chain-2"
        assert receipts[2].status == "dead_lettered"

    async def test_list_receipts_for_plan_empty(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """list_receipts_for_plan returns empty list for unknown plan."""
        receipts = await temp_storage.list_receipts_for_plan(
            "nonexistent-plan", "nonexistent-adapter"
        )
        assert receipts == []

    async def test_receipt_default_attempt_number_is_one(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Receipts without explicit attempt_number default to 1."""
        event = _make_event(event_id="evt-default-attempt")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-default",
            event_id="evt-default-attempt",
            delivery_plan_id="plan-default",
            target_adapter="adapter_c",
            status="sent",
        )
        await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status("plan-default", "adapter_c")
        assert status is not None
        assert status.attempt_number == 1
        assert status.parent_receipt_id is None

    async def test_receipt_lineage_different_adapters_independent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Receipts for different adapters under the same plan are independent."""
        event = _make_event(event_id="evt-indep")
        await temp_storage.append(event)

        r_a = DeliveryReceipt(
            receipt_id="rcpt-indep-a",
            event_id="evt-indep",
            delivery_plan_id="plan-indep",
            target_adapter="adapter_a",
            status="failed",
            attempt_number=2,
        )
        r_b = DeliveryReceipt(
            receipt_id="rcpt-indep-b",
            event_id="evt-indep",
            delivery_plan_id="plan-indep",
            target_adapter="adapter_b",
            status="sent",
            attempt_number=1,
        )
        await temp_storage.append_receipt(r_a)
        await temp_storage.append_receipt(r_b)

        receipts_a = await temp_storage.list_receipts_for_plan(
            "plan-indep", "adapter_a"
        )
        receipts_b = await temp_storage.list_receipts_for_plan(
            "plan-indep", "adapter_b"
        )
        assert len(receipts_a) == 1
        assert len(receipts_b) == 1
        assert receipts_a[0].attempt_number == 2
        assert receipts_b[0].attempt_number == 1
