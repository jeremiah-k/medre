"""Tests for SQLiteStorage: append/get, native ref resolve, relations,
receipts, query with EventFilter, idempotent native refs, append-only
receipts, ordering guarantees, and close/reopen persistence.
"""

from __future__ import annotations

import os
import sqlite3
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
from medre.core.storage.backend import (
    DuplicateEventError,
    StorageError,
    StorageInitializationError,
)


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


# ===================================================================
# Receipt query helpers: list_receipts_by_replay_run, list_receipts_for_event
# ===================================================================


class TestReceiptQueryHelpers:
    """list_receipts_by_replay_run and list_receipts_for_event round-trip
    and ordering guarantees.
    """

    @staticmethod
    def _make_receipt(
        receipt_id: str,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        status: str = "sent",
        attempt_number: int = 1,
        source: str = "live",
        replay_run_id: str | None = None,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            receipt_id=receipt_id,
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            status=status,  # type: ignore[arg-type]
            attempt_number=attempt_number,
            source=source,
            replay_run_id=replay_run_id,
        )

    async def test_list_receipts_by_replay_run_returns_matching(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """list_receipts_by_replay_run returns only receipts with matching run_id."""
        event = _make_event(event_id="evt-replay-q")
        await temp_storage.append(event)

        # Live receipt (no replay_run_id)
        await temp_storage.append_receipt(
            self._make_receipt(
                "rcpt-live-1", "evt-replay-q", "plan-a", "adapter_a"
            )
        )
        # Replay receipt with run_id="run-42"
        await temp_storage.append_receipt(
            self._make_receipt(
                "rcpt-replay-1",
                "evt-replay-q",
                "plan-b",
                "adapter_b",
                source="replay",
                replay_run_id="run-42",
            )
        )
        # Replay receipt with run_id="run-99" (different run)
        await temp_storage.append_receipt(
            self._make_receipt(
                "rcpt-replay-2",
                "evt-replay-q",
                "plan-c",
                "adapter_c",
                source="replay",
                replay_run_id="run-99",
            )
        )

        receipts = await temp_storage.list_receipts_by_replay_run("run-42")
        assert len(receipts) == 1
        assert receipts[0].receipt_id == "rcpt-replay-1"
        assert receipts[0].source == "replay"
        assert receipts[0].replay_run_id == "run-42"

    async def test_list_receipts_by_replay_run_empty_for_unknown(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """list_receipts_by_replay_run returns empty list for unknown run_id."""
        receipts = await temp_storage.list_receipts_by_replay_run("nonexistent-run")
        assert receipts == []

    async def test_list_receipts_by_replay_run_ordered_by_sequence(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Multiple receipts for same replay run are ordered by sequence."""
        event = _make_event(event_id="evt-replay-order")
        await temp_storage.append(event)

        for i in range(4):
            await temp_storage.append_receipt(
                self._make_receipt(
                    f"rcpt-order-{i}",
                    "evt-replay-order",
                    f"plan-order-{i}",
                    f"adapter_{i}",
                    source="replay",
                    replay_run_id="run-order",
                )
            )

        receipts = await temp_storage.list_receipts_by_replay_run("run-order")
        assert len(receipts) == 4
        # Sequences must be strictly ascending.
        seqs = [r.sequence for r in receipts]
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1]

    async def test_list_receipts_for_event_returns_all(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """list_receipts_for_event returns all receipts for a given event."""
        event = _make_event(event_id="evt-ev-q")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            self._make_receipt(
                "rcpt-ev-1", "evt-ev-q", "plan-x", "adapter_x"
            )
        )
        await temp_storage.append_receipt(
            self._make_receipt(
                "rcpt-ev-2",
                "evt-ev-q",
                "plan-y",
                "adapter_y",
                source="replay",
                replay_run_id="run-7",
            )
        )
        await temp_storage.append_receipt(
            self._make_receipt(
                "rcpt-ev-3",
                "evt-ev-q",
                "plan-x",
                "adapter_x",
                attempt_number=2,
            )
        )

        receipts = await temp_storage.list_receipts_for_event("evt-ev-q")
        assert len(receipts) == 3
        ids = {r.receipt_id for r in receipts}
        assert ids == {"rcpt-ev-1", "rcpt-ev-2", "rcpt-ev-3"}

    async def test_list_receipts_for_event_ordered_by_sequence(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Receipts for an event are returned in sequence (append) order."""
        event = _make_event(event_id="evt-ev-order")
        await temp_storage.append(event)

        for i, adapter in enumerate(["a", "b", "c"]):
            await temp_storage.append_receipt(
                self._make_receipt(
                    f"rcpt-evo-{i}", "evt-ev-order", f"plan-evo-{i}", adapter
                )
            )

        receipts = await temp_storage.list_receipts_for_event("evt-ev-order")
        assert len(receipts) == 3
        seqs = [r.sequence for r in receipts]
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1]

    async def test_list_receipts_for_event_empty_for_unknown(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """list_receipts_for_event returns empty list for unknown event."""
        receipts = await temp_storage.list_receipts_for_event("nonexistent-event")
        assert receipts == []

    async def test_delivery_status_still_works_after_index_change(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """delivery_status view still functions with the updated 4-column index."""
        event = _make_event(event_id="evt-idx-verify")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "confirmed"]):
            await temp_storage.append_receipt(
                self._make_receipt(
                    f"rcpt-idx-{i}",
                    "evt-idx-verify",
                    "plan-idx",
                    "adapter_idx",
                    status=st,
                    attempt_number=i + 1,
                )
            )

        status = await temp_storage.delivery_status("plan-idx", "adapter_idx")
        assert status is not None
        assert status.status == "confirmed"
        assert status.attempt_number == 3


# ===================================================================
# Track 6: Receipt sequence monotonicity
# ===================================================================


class TestReceiptSequenceMonotonicity:
    """Receipt sequence numbers are strictly monotonic across all receipts."""

    async def test_sequence_monotonic_across_many_events(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Appending receipts for 10 events × 3 adapters yields monotonic sequences."""
        for i in range(10):
            event = _make_event(event_id=f"monoton-{i}")
            await temp_storage.append(event)

            for adapter in ("adapter_x", "adapter_y", "adapter_z"):
                receipt = DeliveryReceipt(
                    receipt_id=f"rcpt-mono-{i}-{adapter}",
                    event_id=event.event_id,
                    delivery_plan_id=f"plan-mono-{i}",
                    target_adapter=adapter,
                    status="sent",
                )
                await temp_storage.append_receipt(receipt)

        rows = await temp_storage._read_all(
            "SELECT sequence FROM delivery_receipts ORDER BY sequence ASC",
            (),
        )
        assert len(rows) == 30

        seqs = [r["sequence"] for r in rows]
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1], (
                f"Sequence not monotonic: {seqs[i]} <= {seqs[i-1]} at index {i}"
            )

    async def test_receipt_ordering_across_retry_chain(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Retry chain: failed → failed → dead_lettered preserves sequence order."""
        event = _make_event(event_id="evt-retry-seq")
        await temp_storage.append(event)

        statuses = ["failed", "failed", "dead_lettered"]
        prev_id = None
        for i, st in enumerate(statuses):
            receipt = DeliveryReceipt(
                receipt_id=f"rcpt-retry-seq-{i}",
                event_id="evt-retry-seq",
                delivery_plan_id="plan-retry-seq",
                target_adapter="retry_adapter",
                status=st,  # type: ignore[arg-type]
                attempt_number=i + 1,
                parent_receipt_id=prev_id,
            )
            await temp_storage.append_receipt(receipt)
            prev_id = receipt.receipt_id

        rows = await temp_storage._read_all(
            "SELECT sequence, status, attempt_number, parent_receipt_id "
            "FROM delivery_receipts WHERE event_id = ? ORDER BY sequence ASC",
            ("evt-retry-seq",),
        )
        assert len(rows) == 3
        assert [r["status"] for r in rows] == ["failed", "failed", "dead_lettered"]
        assert [r["attempt_number"] for r in rows] == [1, 2, 3]
        assert rows[0]["parent_receipt_id"] is None
        assert rows[1]["parent_receipt_id"] == "rcpt-retry-seq-0"
        assert rows[2]["parent_receipt_id"] == "rcpt-retry-seq-1"

        # Sequences strictly increasing
        seqs = [r["sequence"] for r in rows]
        assert seqs[0] < seqs[1] < seqs[2]


# ===================================================================
# source_native_ref round-trip
# ===================================================================


class TestSourceNativeRefRoundTrip:
    """Events with / without source_native_ref round-trip through storage."""

    async def test_event_without_source_native_ref_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Event with source_native_ref=None survives append/get."""
        event = _make_event(event_id="evt-no-snr")
        await temp_storage.append(event)
        retrieved = await temp_storage.get("evt-no-snr")
        assert retrieved is not None
        assert retrieved.source_native_ref is None

    async def test_event_with_source_native_ref_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Event with populated source_native_ref survives append/get."""
        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$event-001",
            native_thread_id=None,
        )
        event = CanonicalEvent(
            event_id="evt-with-snr",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix",
            source_transport_id="node-1",
            source_channel_id="!room:server",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )
        await temp_storage.append(event)
        retrieved = await temp_storage.get("evt-with-snr")
        assert retrieved is not None
        assert retrieved.source_native_ref is not None
        assert retrieved.source_native_ref.adapter == "matrix"
        assert retrieved.source_native_ref.native_channel_id == "!room:server"
        assert retrieved.source_native_ref.native_message_id == "$event-001"
        assert retrieved.source_native_ref.native_thread_id is None

    async def test_inbound_native_ref_duplicate_is_idempotent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Storing the same inbound NativeMessageRef twice is idempotent."""
        event = _make_event(event_id="evt-inbound-idem")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-inbound-1",
            event_id="evt-inbound-idem",
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)
        # Second store with same (adapter, native_channel_id, native_message_id) is silently ignored.
        ref2 = NativeMessageRef(
            id="nref-inbound-1-dup",
            event_id="evt-inbound-idem",
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref2)

        resolved = await temp_storage.resolve_native_ref(
            "matrix", "!room:server", "$msg-001"
        )
        assert resolved == "evt-inbound-idem"

    async def test_resolve_native_ref_returns_event_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """resolve_native_ref(adapter, channel, message_id) returns canonical event_id."""
        event = _make_event(event_id="evt-resolve-target")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-resolve-1",
            event_id="evt-resolve-target",
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$target-msg",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)

        result = await temp_storage.resolve_native_ref(
            "matrix", "!room:server", "$target-msg"
        )
        assert result == "evt-resolve-target"


# ===================================================================
# target_native_thread_id round-trip in event_relations
# ===================================================================


class TestRelationTargetNativeThreadId:
    """target_native_ref.native_thread_id is preserved through storage."""

    async def test_target_native_thread_id_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A relation whose target_native_ref has native_thread_id survives
        append/get round-trip without silent data loss."""
        event = _make_event(event_id="evt-thread-rt")
        await temp_storage.append(event)

        nref = NativeRef(
            adapter="discord",
            native_channel_id="channel-1",
            native_message_id="msg-thread-1",
            native_thread_id="thread-42",
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        await temp_storage.store_relation("evt-thread-rt", relation)

        relations = await temp_storage.list_relations("evt-thread-rt")
        assert len(relations) == 1
        tnref = relations[0].target_native_ref
        assert tnref is not None
        assert tnref.native_thread_id == "thread-42"
        assert tnref.adapter == "discord"
        assert tnref.native_channel_id == "channel-1"
        assert tnref.native_message_id == "msg-thread-1"

    async def test_inline_relation_with_thread_id_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Relations with native_thread_id embedded in an event round-trip."""
        nref = NativeRef(
            adapter="slack",
            native_channel_id="C123",
            native_message_id="123.456",
            native_thread_id="thread-abc",
        )
        relation = EventRelation(
            relation_type="thread",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            event_id="evt-inline-thread", relations=(relation,)
        )
        await temp_storage.append(event)

        retrieved = await temp_storage.get("evt-inline-thread")
        assert retrieved is not None
        assert len(retrieved.relations) == 1
        assert retrieved.relations[0].target_native_ref is not None
        assert retrieved.relations[0].target_native_ref.native_thread_id == "thread-abc"


# ===================================================================
# NULL channel native ref idempotency
# ===================================================================


class TestNullChannelNativeRefIdempotency:
    """Native refs with native_channel_id=None dedupe deterministically."""

    async def test_null_channel_ref_stores_once(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Storing two native refs with NULL channel and same message_id
        results in a single stored row (deterministic idempotency)."""
        event = _make_event(event_id="evt-null-ch")
        await temp_storage.append(event)

        ref1 = NativeMessageRef(
            id="nref-null-1",
            event_id="evt-null-ch",
            adapter="radio",
            native_channel_id=None,
            native_message_id="msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=datetime.now(timezone.utc),
        )
        await temp_storage.store_native_ref(ref1)

        # Second ref with same (adapter, NULL, native_message_id) but different id.
        ref2 = NativeMessageRef(
            id="nref-null-2",
            event_id="evt-null-ch",
            adapter="radio",
            native_channel_id=None,
            native_message_id="msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
            created_at=datetime.now(timezone.utc),
        )
        await temp_storage.store_native_ref(ref2)

        # Should resolve to the first ref's event_id.
        resolved = await temp_storage.resolve_native_ref("radio", None, "msg-001")
        assert resolved == "evt-null-ch"

        # Only one row should exist.
        rows = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE adapter = ? AND native_channel_id IS NULL AND native_message_id = ?",
            ("radio", "msg-001"),
        )
        assert len(rows) == 1
        assert rows[0]["id"] == "nref-null-1"


# ===================================================================
# DuplicateEventError on duplicate append
# ===================================================================


class TestDuplicateEventError:
    """Appending the same event_id twice raises DuplicateEventError."""

    async def test_duplicate_append_raises_duplicate_event_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Appending an event whose event_id already exists raises
        DuplicateEventError (a StorageError subclass)."""
        event = _make_event(event_id="evt-dup-test")
        await temp_storage.append(event)

        with pytest.raises(DuplicateEventError) as exc_info:
            await temp_storage.append(event)
        assert "evt-dup-test" in str(exc_info.value) or "Duplicate" in str(
            exc_info.value
        )

    async def test_duplicate_event_error_is_storage_error_subclass(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """DuplicateEventError is a subclass of StorageError."""
        event = _make_event(event_id="evt-subclass-test")
        await temp_storage.append(event)

        with pytest.raises(DuplicateEventError) as exc_info:
            await temp_storage.append(event)
        assert isinstance(exc_info.value, StorageError)


# ===================================================================
# UTC-aware default created_at
# ===================================================================


class TestUtcAwareDefaultCreatedAt:
    """NativeMessageRef and DeliveryReceipt default created_at is UTC-aware."""

    def test_native_message_ref_default_created_at_is_utc_aware(self) -> None:
        """NativeMessageRef(created_at not passed) gets a UTC-aware datetime."""
        ref = NativeMessageRef(
            id="nref-utc",
            event_id="evt-utc",
            adapter="test",
            native_channel_id="ch",
            native_message_id="msg",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        assert ref.created_at.tzinfo is not None
        assert ref.created_at.tzinfo == timezone.utc

    def test_delivery_receipt_default_created_at_is_utc_aware(self) -> None:
        """DeliveryReceipt(created_at not passed) gets a UTC-aware datetime."""
        receipt = DeliveryReceipt(
            receipt_id="rcpt-utc",
            event_id="evt-utc",
            delivery_plan_id="plan-utc",
            target_adapter="test",
        )
        assert receipt.created_at.tzinfo is not None
        assert receipt.created_at.tzinfo == timezone.utc


# ===================================================================
# Schema shape validation
# ===================================================================


class TestSchemaShapeValidation:
    """Detect old pre-release DBs whose schema_version=1 but column shape
    predates the current DDL.

    initialize() must raise StorageInitializationError with clear guidance
    to recreate the database.
    """

    async def test_old_event_relations_missing_columns(self) -> None:
        """An event_relations table lacking target_native_thread_id
        triggers StorageInitializationError even though schema_version=1."""
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Build an old-shape database: event_relations without
            # target_native_thread_id (and a few other newer columns).
            raw = sqlite3.connect(db_path)
            raw.executescript("""
                CREATE TABLE IF NOT EXISTS canonical_events (
                    event_id TEXT PRIMARY KEY,
                    event_kind TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    source_adapter TEXT NOT NULL,
                    source_transport_id TEXT NOT NULL,
                    source_channel_id TEXT,
                    parent_event_id TEXT,
                    lineage TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL DEFAULT '{}',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    depth INTEGER NOT NULL DEFAULT 0,
                    trace_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_event_id TEXT,
                    target_native_adapter TEXT,
                    target_native_channel_id TEXT,
                    target_native_message_id TEXT,
                    key TEXT,
                    fallback_text TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS native_message_refs (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    native_channel_id TEXT,
                    native_message_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delivery_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id TEXT UNIQUE NOT NULL,
                    event_id TEXT NOT NULL,
                    delivery_plan_id TEXT NOT NULL,
                    target_adapter TEXT NOT NULL,
                    route_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT,
                    adapter_message_id TEXT,
                    next_retry_at TEXT,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    parent_receipt_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plugin_state (
                    plugin_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(plugin_id, key)
                );
                CREATE TABLE IF NOT EXISTS _medre_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO _medre_schema_meta (key, value)
                    VALUES ('schema_version', '1');
            """)
            raw.close()

            storage = SQLiteStorage(db_path=db_path)
            with pytest.raises(StorageInitializationError, match="schema shape mismatch"):
                await storage.initialize()
        finally:
            os.unlink(db_path)

    async def test_old_native_message_refs_missing_columns(self) -> None:
        """A native_message_refs table lacking native_thread_id triggers
        StorageInitializationError."""
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            raw = sqlite3.connect(db_path)
            # Create all tables with current shape EXCEPT native_message_refs
            # which is missing native_thread_id and native_relation_id.
            raw.executescript("""
                CREATE TABLE IF NOT EXISTS canonical_events (
                    event_id TEXT PRIMARY KEY,
                    event_kind TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    source_adapter TEXT NOT NULL,
                    source_transport_id TEXT NOT NULL,
                    source_channel_id TEXT,
                    parent_event_id TEXT,
                    lineage TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL DEFAULT '{}',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    depth INTEGER NOT NULL DEFAULT 0,
                    trace_id TEXT,
                    source_native_adapter TEXT,
                    source_native_channel_id TEXT,
                    source_native_message_id TEXT,
                    source_native_thread_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_event_id TEXT,
                    target_native_adapter TEXT,
                    target_native_channel_id TEXT,
                    target_native_message_id TEXT,
                    target_native_thread_id TEXT,
                    key TEXT,
                    fallback_text TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS native_message_refs (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    native_channel_id TEXT,
                    native_message_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delivery_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id TEXT UNIQUE NOT NULL,
                    event_id TEXT NOT NULL,
                    delivery_plan_id TEXT NOT NULL,
                    target_adapter TEXT NOT NULL,
                    route_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT,
                    adapter_message_id TEXT,
                    next_retry_at TEXT,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    parent_receipt_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plugin_state (
                    plugin_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(plugin_id, key)
                );
                CREATE TABLE IF NOT EXISTS _medre_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO _medre_schema_meta (key, value)
                    VALUES ('schema_version', '1');
            """)
            raw.close()

            storage = SQLiteStorage(db_path=db_path)
            with pytest.raises(StorageInitializationError, match="schema shape mismatch"):
                await storage.initialize()
        finally:
            os.unlink(db_path)

    async def test_fresh_db_passes_shape_validation(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A freshly initialized DB must pass shape validation without error."""
        # temp_storage fixture calls initialize() which now includes shape
        # validation.  If we reach this point, validation passed.
        event = _make_event()
        await temp_storage.append(event)
        retrieved = await temp_storage.get(event.event_id)
        assert retrieved is not None


# ===================================================================
# Receipt source and replay_run_id round-trip
# ===================================================================


class TestReceiptSourceReplayRunId:
    """DeliveryReceipt source and replay_run_id fields round-trip through
    storage and are populated correctly by default."""

    async def test_live_receipt_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A receipt with source='live' and no replay_run_id round-trips."""
        event = _make_event(event_id="evt-live-rcpt")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-live-1",
            event_id="evt-live-rcpt",
            delivery_plan_id="plan-live",
            target_adapter="adapter_a",
            status="sent",
            source="live",
        )
        await temp_storage.append_receipt(receipt)

        fetched = await temp_storage.delivery_status("plan-live", "adapter_a")
        assert fetched is not None
        assert fetched.source == "live"
        assert fetched.replay_run_id is None

    async def test_replay_receipt_round_trip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A receipt with source='replay' and replay_run_id round-trips."""
        event = _make_event(event_id="evt-replay-rcpt")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-replay-1",
            event_id="evt-replay-rcpt",
            delivery_plan_id="plan-replay",
            target_adapter="adapter_b",
            status="sent",
            source="replay",
            replay_run_id="run-abc-123",
        )
        await temp_storage.append_receipt(receipt)

        fetched = await temp_storage.delivery_status("plan-replay", "adapter_b")
        assert fetched is not None
        assert fetched.source == "replay"
        assert fetched.replay_run_id == "run-abc-123"

    async def test_default_source_is_live(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """DeliveryReceipt default source is 'live' and replay_run_id is None."""
        receipt = DeliveryReceipt(
            receipt_id="rcpt-default",
            event_id="evt-default",
            delivery_plan_id="plan-default",
            target_adapter="adapter_c",
            status="queued",
        )
        assert receipt.source == "live"
        assert receipt.replay_run_id is None

    async def test_list_receipts_preserves_source_fields(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """list_receipts_for_plan preserves source and replay_run_id."""
        event = _make_event(event_id="evt-list-rcpt")
        await temp_storage.append(event)

        r1 = DeliveryReceipt(
            receipt_id="rcpt-list-1",
            event_id="evt-list-rcpt",
            delivery_plan_id="plan-list",
            target_adapter="adapter_d",
            status="sent",
            source="live",
            attempt_number=1,
        )
        r2 = DeliveryReceipt(
            receipt_id="rcpt-list-2",
            event_id="evt-list-rcpt",
            delivery_plan_id="plan-list",
            target_adapter="adapter_d",
            status="sent",
            source="replay",
            replay_run_id="run-xyz",
            attempt_number=2,
            parent_receipt_id="rcpt-list-1",
        )
        await temp_storage.append_receipt(r1)
        await temp_storage.append_receipt(r2)

        receipts = await temp_storage.list_receipts_for_plan("plan-list", "adapter_d")
        assert len(receipts) == 2
        assert receipts[0].source == "live"
        assert receipts[0].replay_run_id is None
        assert receipts[1].source == "replay"
        assert receipts[1].replay_run_id == "run-xyz"


# ===================================================================
# IntegrityError classification in _write_batch
# ===================================================================


class TestIntegrityErrorClassification:
    """_write_batch distinguishes canonical_events PK violations from other
    IntegrityErrors."""

    async def test_duplicate_event_raises_duplicate_event_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Appending a duplicate canonical event raises DuplicateEventError."""
        event = _make_event(event_id="evt-dup-classify")
        await temp_storage.append(event)

        with pytest.raises(DuplicateEventError):
            await temp_storage.append(event)

    async def test_non_canonical_integrity_error_raises_storage_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A UNIQUE constraint violation on delivery_receipts (not
        canonical_events) raises StorageError, not DuplicateEventError."""
        event = _make_event(event_id="evt-unique-rcpt")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-dup-unique",
            event_id="evt-unique-rcpt",
            delivery_plan_id="plan-unique",
            target_adapter="adapter_u",
            status="sent",
        )
        await temp_storage.append_receipt(receipt)

        # Insert same receipt_id again — UNIQUE constraint on
        # delivery_receipts.receipt_id, NOT canonical_events.
        with pytest.raises(StorageError) as exc_info:
            await temp_storage.append_receipt(receipt)
        # Must NOT be a DuplicateEventError.
        assert not isinstance(exc_info.value, DuplicateEventError)


# ===================================================================
# Storage indexes
# ===================================================================


class TestStorageIndexes:
    """Targeted indexes matching actual query patterns are created on init."""

    @staticmethod
    async def _index_columns(storage: SQLiteStorage, table: str) -> dict[str, frozenset[str]]:
        """Return {index_name: frozenset of column names} for *table*."""
        rows = await storage._read_all(
            f"PRAGMA index_list({table})", ()
        )
        result: dict[str, frozenset[str]] = {}
        for row in rows:
            idx_name = row["name"]
            # Skip SQLite autoindices (internal names like sqlite_autoindex_...)
            if idx_name.startswith("sqlite_autoindex"):
                continue
            cols = await storage._read_all(
                f"PRAGMA index_info({idx_name})", ()
            )
            result[idx_name] = frozenset(r["name"] for r in cols)
        return result

    async def test_canonical_events_timestamp_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_events_timestamp on canonical_events(timestamp, event_id)."""
        indexes = await self._index_columns(temp_storage, "canonical_events")
        assert "idx_events_timestamp" in indexes
        assert indexes["idx_events_timestamp"] == frozenset({"timestamp", "event_id"})

    async def test_event_relations_event_id_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_relations_event_id on event_relations(event_id, id)."""
        indexes = await self._index_columns(temp_storage, "event_relations")
        assert "idx_relations_event_id" in indexes
        assert indexes["idx_relations_event_id"] == frozenset({"event_id", "id"})

    async def test_native_message_refs_event_id_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_nrefs_event_created on native_message_refs(event_id, created_at).

        Replaces the older idx_nrefs_event_id(event_id).  The composite
        index covers both the WHERE event_id=? filter and the ORDER BY
        created_at ASC used by _SELECT_NREFS_FOR_EVENT.

        The UNIQUE(adapter, native_channel_id, native_message_id) constraint
        creates an autoindex; we do NOT assert a manual index for that triple.
        """
        indexes = await self._index_columns(temp_storage, "native_message_refs")
        assert "idx_nrefs_event_created" in indexes
        assert indexes["idx_nrefs_event_created"] == frozenset({"event_id", "created_at"})

    async def test_receipts_plan_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_receipts_plan on delivery_receipts(delivery_plan_id, target_adapter, attempt_number, sequence)."""
        indexes = await self._index_columns(temp_storage, "delivery_receipts")
        assert "idx_receipts_plan" in indexes
        assert indexes["idx_receipts_plan"] == frozenset(
            {"delivery_plan_id", "target_adapter", "attempt_number", "sequence"}
        )

    async def test_receipts_event_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_receipts_event on delivery_receipts(event_id, sequence)."""
        indexes = await self._index_columns(temp_storage, "delivery_receipts")
        assert "idx_receipts_event" in indexes
        assert indexes["idx_receipts_event"] == frozenset({"event_id", "sequence"})

    async def test_receipts_source_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_receipts_source on delivery_receipts(source, replay_run_id)."""
        indexes = await self._index_columns(temp_storage, "delivery_receipts")
        assert "idx_receipts_source" in indexes
        assert indexes["idx_receipts_source"] == frozenset({"source", "replay_run_id"})

    async def test_receipts_replay_run_index(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """idx_receipts_replay_run on delivery_receipts(replay_run_id).

        Serves _SELECT_RECEIPTS_BY_REPLAY_RUN which filters by replay_run_id
        alone (without source).  idx_receipts_source(source, replay_run_id)
        cannot serve this query because source is not in the WHERE clause.
        """
        indexes = await self._index_columns(temp_storage, "delivery_receipts")
        assert "idx_receipts_replay_run" in indexes
        assert indexes["idx_receipts_replay_run"] == frozenset({"replay_run_id"})

    async def test_no_manual_index_for_unique_autoindex(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """No manual index duplicates the UNIQUE autoindex on
        native_message_refs(adapter, native_channel_id, native_message_id).

        The UNIQUE constraint already creates an autoindex; a manual
        duplicate would be redundant.
        """
        indexes = await self._index_columns(temp_storage, "native_message_refs")
        for name, cols in indexes.items():
            # None of our manual indexes should cover the UNIQUE triple.
            if cols == frozenset({"adapter", "native_channel_id", "native_message_id"}):
                pytest.fail(
                    f"Redundant manual index '{name}' duplicates UNIQUE autoindex"
                )


# ===================================================================
# open_readonly — strict read-only open for inspect commands
# ===================================================================


class TestOpenReadonly:
    """SQLiteStorage.open_readonly() opens existing DBs without mutation."""

    async def test_missing_file_raises(self) -> None:
        """open_readonly raises StorageInitializationError for missing file."""
        with pytest.raises(StorageInitializationError, match="does not exist"):
            await SQLiteStorage.open_readonly("/nonexistent/path/test.db")

    async def test_missing_file_not_created(self) -> None:
        """open_readonly does not create the file even transiently."""
        db_path = os.path.join(tempfile.gettempdir(), f"medre-test-nocreate-{os.getpid()}.db")
        assert not os.path.exists(db_path)
        try:
            with pytest.raises(StorageInitializationError):
                await SQLiteStorage.open_readonly(db_path)
            assert not os.path.exists(db_path)
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    async def test_valid_db_reads_events(self) -> None:
        """open_readonly on a valid initialized DB can read events."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Write phase — normal initialize.
            storage = SQLiteStorage(db_path)
            await storage.initialize()
            event = _make_event(event_id="readonly-evt-1")
            await storage.append(event)
            await storage.close()

            # Read-only phase.
            ro = await SQLiteStorage.open_readonly(db_path)
            retrieved = await ro.get("readonly-evt-1")
            assert retrieved is not None
            assert retrieved.event_id == "readonly-evt-1"
            await ro.close()
        finally:
            os.unlink(db_path)

    async def test_fresh_empty_db_raises(self) -> None:
        """open_readonly on a file with no tables raises StorageInitializationError."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # File exists but is an empty SQLite database — no tables.
            raw = sqlite3.connect(db_path)
            raw.close()

            with pytest.raises(StorageInitializationError, match="no schema version"):
                await SQLiteStorage.open_readonly(db_path)
        finally:
            os.unlink(db_path)

    async def test_old_shape_db_raises(self) -> None:
        """open_readonly on an old-shape DB raises StorageInitializationError."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            raw = sqlite3.connect(db_path)
            # Minimal old-shape: event_relations without target_native_thread_id.
            raw.executescript("""
                CREATE TABLE canonical_events (
                    event_id TEXT PRIMARY KEY,
                    event_kind TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    source_adapter TEXT NOT NULL,
                    source_transport_id TEXT NOT NULL,
                    source_channel_id TEXT,
                    parent_event_id TEXT,
                    lineage TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL DEFAULT '{}',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    depth INTEGER NOT NULL DEFAULT 0,
                    trace_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE event_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_event_id TEXT,
                    target_native_adapter TEXT,
                    target_native_channel_id TEXT,
                    target_native_message_id TEXT,
                    key TEXT,
                    fallback_text TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE native_message_refs (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    native_channel_id TEXT,
                    native_message_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE delivery_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id TEXT UNIQUE NOT NULL,
                    event_id TEXT NOT NULL,
                    delivery_plan_id TEXT NOT NULL,
                    target_adapter TEXT NOT NULL,
                    route_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT,
                    adapter_message_id TEXT,
                    next_retry_at TEXT,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    parent_receipt_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE plugin_state (
                    plugin_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(plugin_id, key)
                );
                CREATE TABLE _medre_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO _medre_schema_meta (key, value)
                    VALUES ('schema_version', '1');
            """)
            raw.close()

            with pytest.raises(StorageInitializationError, match="schema shape mismatch"):
                await SQLiteStorage.open_readonly(db_path)
        finally:
            os.unlink(db_path)

    async def test_readonly_rejects_writes(self) -> None:
        """open_readonly connection rejects INSERT (SQLite mode=ro)."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Create a valid DB with one event.
            storage = SQLiteStorage(db_path)
            await storage.initialize()
            event = _make_event(event_id="ro-write-test")
            await storage.append(event)
            await storage.close()

            # Open read-only and attempt to write.
            ro = await SQLiteStorage.open_readonly(db_path)
            with pytest.raises(Exception):
                # SQLite will reject the INSERT in mode=ro.
                duplicate = _make_event(event_id="should-fail")
                await ro.append(duplicate)
            await ro.close()
        finally:
            os.unlink(db_path)
