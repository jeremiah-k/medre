"""Tests for SQLiteStorage: delivery receipts, append-only receipts,
ordering guarantees, receipt lineage, receipt query helpers,
receipt sequence monotonicity, receipt source/replay_run_id,
delivery_status failure_kind, list_due_retry_receipts integration,
and rendering_evidence persistence round-trip.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
)
from medre.core.rendering.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    RenderingEvidence,
)
from medre.core.storage.backend import EventFilter
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ===================================================================
# Delivery receipts
# ===================================================================


class TestReceipts:
    """append_receipt / delivery_status."""

    async def test_append_receipt_and_delivery_status(
        self, temp_storage: SQLiteStorage
    ) -> None:
        event = make_storage_event(event_id="evt-rcpt")
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
        event = make_storage_event(event_id="evt-multi-rcpt")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "suppressed"]):
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
        assert status.status == "suppressed"
        assert status.receipt_id == "rcpt-2"

    async def test_delivery_status_returns_none_for_unknown(
        self, temp_storage: SQLiteStorage
    ) -> None:
        status = await temp_storage.delivery_status("no-plan", "no-adapter")
        assert status is None


# ===================================================================
# Append-only receipts
# ===================================================================


class TestAppendOnlyReceipts:
    """Receipts are append-only; delivery_status is a read-only projection."""

    async def test_append_receipt_creates_new_row_each_time(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Each append_receipt call creates a new row in delivery_receipts."""
        event = make_storage_event(event_id="evt-rcpt-row")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "suppressed"]):
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
        assert rows[2]["status"] == "suppressed"

    async def test_delivery_status_is_projection_not_mutable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """delivery_status returns the latest receipt via MAX(sequence) projection."""
        event = make_storage_event(event_id="evt-proj")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "suppressed"]):
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
        assert status.status == "suppressed"
        assert status.receipt_id == "rcpt-proj-2"

    async def test_receipts_never_updated_or_deleted(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """All historical receipt rows persist after reading delivery_status."""
        event = make_storage_event(event_id="evt-hist")
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
        assert [r["receipt_id"] for r in rows] == [
            "rcpt-hist-0",
            "rcpt-hist-1",
            "rcpt-hist-2",
        ]
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
        event = make_storage_event(event_id="evt-ord-rel")
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
        event = make_storage_event(event_id="evt-lineage-1")
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

    async def test_receipt_lineage_chain(self, temp_storage: SQLiteStorage) -> None:
        """A chain of receipts linked by parent_receipt_id."""
        event = make_storage_event(event_id="evt-chain")
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
        receipts = await temp_storage.list_receipts_for_plan("plan-chain", "adapter_b")
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
        event = make_storage_event(event_id="evt-default-attempt")
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
        event = make_storage_event(event_id="evt-indep")
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
        event = make_storage_event(event_id="evt-replay-q")
        await temp_storage.append(event)

        # Live receipt (no replay_run_id)
        await temp_storage.append_receipt(
            self._make_receipt("rcpt-live-1", "evt-replay-q", "plan-a", "adapter_a")
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
        event = make_storage_event(event_id="evt-replay-order")
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
        event = make_storage_event(event_id="evt-ev-q")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            self._make_receipt("rcpt-ev-1", "evt-ev-q", "plan-x", "adapter_x")
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
        event = make_storage_event(event_id="evt-ev-order")
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
        """delivery_status view still functions with the updated 5-column index."""
        event = make_storage_event(event_id="evt-idx-verify")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "suppressed"]):
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
        assert status.status == "suppressed"
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
            event = make_storage_event(event_id=f"monoton-{i}")
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
            assert (
                seqs[i] > seqs[i - 1]
            ), f"Sequence not monotonic: {seqs[i]} <= {seqs[i-1]} at index {i}"

    async def test_receipt_ordering_across_retry_chain(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Retry chain: failed → failed → dead_lettered preserves sequence order."""
        event = make_storage_event(event_id="evt-retry-seq")
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
# Receipt source and replay_run_id round-trip
# ===================================================================


class TestReceiptSourceReplayRunId:
    """DeliveryReceipt source and replay_run_id fields round-trip through
    storage and are populated correctly by default.
    """

    async def test_live_receipt_round_trip(self, temp_storage: SQLiteStorage) -> None:
        """A receipt with source='live' and no replay_run_id round-trips."""
        event = make_storage_event(event_id="evt-live-rcpt")
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

    async def test_replay_receipt_round_trip(self, temp_storage: SQLiteStorage) -> None:
        """A receipt with source='replay' and replay_run_id round-trips."""
        event = make_storage_event(event_id="evt-replay-rcpt")
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

    async def test_default_source_is_live(self, temp_storage: SQLiteStorage) -> None:
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
        event = make_storage_event(event_id="evt-list-rcpt")
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
# delivery_status failure_kind round-trip
# ===================================================================


class TestDeliveryStatusFailureKind:
    """delivery_status view preserves failure_kind from delivery_receipts."""

    async def test_delivery_status_preserves_failure_kind(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A failed receipt with failure_kind='adapter_transient' preserves
        the value through the delivery_status view."""
        event = make_storage_event(event_id="evt-fk-1")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-fk-1",
            event_id="evt-fk-1",
            delivery_plan_id="plan-fk",
            target_adapter="adapter_fk",
            status="failed",
            failure_kind="adapter_transient",
            error="ConnectionError: timeout",
        )
        await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status("plan-fk", "adapter_fk")
        assert status is not None
        assert status.failure_kind == "adapter_transient"

    async def test_delivery_status_sent_returns_null_failure_kind(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A sent receipt has failure_kind=None through the delivery_status view."""
        event = make_storage_event(event_id="evt-fk-sent")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-fk-sent",
            event_id="evt-fk-sent",
            delivery_plan_id="plan-fk-sent",
            target_adapter="adapter_fk_sent",
            status="sent",
        )
        await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status("plan-fk-sent", "adapter_fk_sent")
        assert status is not None
        assert status.failure_kind is None


# ===================================================================
# list_due_retry_receipts / count_pending_retry — real SQL integration
# ===================================================================


class TestListDueRetryReceiptsIntegration:
    """Integration tests for list_due_retry_receipts and count_pending_retry
    against REAL SQLite (no mocks). Validates that failure_kind is stored
    and the SQL query correctly filters by failure_kind='adapter_transient'.
    """

    async def test_transient_failure_due_for_retry_returned(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A failed receipt with failure_kind='adapter_transient', next_retry_at
        in the past, and attempt_number=1 is returned by list_due_retry_receipts."""
        event = make_storage_event(event_id="evt-retry-1")
        await temp_storage.append(event)

        past = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-retry-1",
            event_id="evt-retry-1",
            delivery_plan_id="plan-retry-1",
            target_adapter="adapter_a",
            status="failed",
            error="ConnectionError: timeout",
            failure_kind="adapter_transient",
            next_retry_at=past,
            attempt_number=1,
        )
        await temp_storage.append_receipt(receipt)

        results = await temp_storage.list_due_retry_receipts(now)
        assert len(results) == 1
        assert results[0].receipt_id == "rcpt-retry-1"
        assert results[0].failure_kind == "adapter_transient"

    async def test_permanent_failure_excluded(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A failed receipt with failure_kind='adapter_permanent' is NOT
        returned by list_due_retry_receipts."""
        event = make_storage_event(event_id="evt-retry-2")
        await temp_storage.append(event)

        past = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-retry-2",
            event_id="evt-retry-2",
            delivery_plan_id="plan-retry-2",
            target_adapter="adapter_b",
            status="failed",
            error="InvalidPayload: rejected",
            failure_kind="adapter_permanent",
            next_retry_at=past,
            attempt_number=1,
        )
        await temp_storage.append_receipt(receipt)

        results = await temp_storage.list_due_retry_receipts(now)
        assert len(results) == 0

    async def test_max_attempts_exhausted(self, temp_storage: SQLiteStorage) -> None:
        """A transient failure with attempt_number >= 3 is NOT returned
        (retries exhausted)."""
        event = make_storage_event(event_id="evt-retry-3")
        await temp_storage.append(event)

        past = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-retry-3",
            event_id="evt-retry-3",
            delivery_plan_id="plan-retry-3",
            target_adapter="adapter_c",
            status="failed",
            error="TimeoutError",
            failure_kind="adapter_transient",
            next_retry_at=past,
            attempt_number=3,
        )
        await temp_storage.append_receipt(receipt)

        results = await temp_storage.list_due_retry_receipts(now)
        assert len(results) == 0

    async def test_count_pending_retry_matches_query(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """count_pending_retry returns the same count as len(list_due_retry_receipts)."""
        event = make_storage_event(event_id="evt-count-retry")
        await temp_storage.append(event)

        past = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        now = datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

        for i in range(2):
            receipt = DeliveryReceipt(
                receipt_id=f"rcpt-cnt-{i}",
                event_id="evt-count-retry",
                delivery_plan_id=f"plan-cnt-{i}",
                target_adapter=f"adapter_{i}",
                status="failed",
                error="TimeoutError",
                failure_kind="adapter_transient",
                next_retry_at=past,
                attempt_number=1,
            )
            await temp_storage.append_receipt(receipt)

        count = await temp_storage.count_pending_retry(now)
        results = await temp_storage.list_due_retry_receipts(now)
        assert count == 2
        assert len(results) == 2


# ===================================================================
# Rendering evidence persistence
# ===================================================================


class TestReceiptRenderingEvidence:
    """Rendering evidence persistence round-trip through SQLite."""

    @staticmethod
    def _sample_evidence_json() -> str:
        """Return a sample rendering evidence JSON string built from the
        canonical serializer (RenderingEvidence + to_dict())."""
        evidence = RenderingEvidence(
            schema_version=EVIDENCE_SCHEMA_VERSION,
            renderer="text",
            target_adapter="fake_presentation",
            target_platform=None,
            delivery_strategy="direct",
            target_channel="ch-1",
            max_text_chars=None,
            max_text_bytes=None,
            capability_level="native",
            capability_policy=None,
            fallback_applied=None,
            truncated=False,
            rendered_text_chars=5,
            rendered_text_bytes=5,
            original_text_chars=None,
            original_text_bytes=None,
        )
        return json.dumps(evidence.to_dict(), sort_keys=True)

    async def test_sent_receipt_with_evidence_persists(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A sent receipt with rendering_evidence survives store/readback."""
        event = make_storage_event(event_id="evt-rev-ev")
        await temp_storage.append(event)

        evidence_json = self._sample_evidence_json()
        receipt = DeliveryReceipt(
            receipt_id="rcpt-rev-1",
            event_id="evt-rev-ev",
            delivery_plan_id="plan-rev",
            target_adapter="fake_presentation",
            status="sent",
            rendering_evidence=evidence_json,
        )
        await temp_storage.append_receipt(receipt)

        # Read back via delivery_status.
        status = await temp_storage.delivery_status("plan-rev", "fake_presentation")
        assert status is not None
        assert status.receipt_id == "rcpt-rev-1"
        assert status.rendering_evidence is not None
        assert status.rendering_evidence == evidence_json

        # Verify the stored JSON is valid and parseable.
        parsed = json.loads(status.rendering_evidence)
        assert parsed["renderer"] == "text"
        assert parsed["truncated"] is False

    async def test_none_evidence_persists_as_none(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A receipt without rendering_evidence reads back as None."""
        event = make_storage_event(event_id="evt-no-ev")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-no-ev",
            event_id="evt-no-ev",
            delivery_plan_id="plan-no-ev",
            target_adapter="fake_presentation",
            status="sent",
            # rendering_evidence left as default None
        )
        await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status("plan-no-ev", "fake_presentation")
        assert status is not None
        assert status.rendering_evidence is None

    async def test_suppressed_receipt_evidence_none(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Suppressed receipts never carry rendering evidence."""
        event = make_storage_event(event_id="evt-supp-ev")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-supp-ev",
            event_id="evt-supp-ev",
            delivery_plan_id="plan-supp-ev",
            target_adapter="fake_presentation",
            status="suppressed",
            error="capability_suppressed",
            failure_kind="capability_suppressed",
        )
        await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status("plan-supp-ev", "fake_presentation")
        assert status is not None
        assert status.status == "suppressed"
        assert status.rendering_evidence is None

    async def test_list_receipts_preserves_evidence(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """list_receipts_for_event returns receipts with rendering_evidence intact."""
        event = make_storage_event(event_id="evt-list-ev")
        await temp_storage.append(event)

        evidence_json = self._sample_evidence_json()
        receipt = DeliveryReceipt(
            receipt_id="rcpt-list-ev",
            event_id="evt-list-ev",
            delivery_plan_id="plan-list-ev",
            target_adapter="fake_presentation",
            status="sent",
            rendering_evidence=evidence_json,
        )
        await temp_storage.append_receipt(receipt)

        receipts = await temp_storage.list_receipts_for_event("evt-list-ev")
        assert len(receipts) == 1
        assert receipts[0].rendering_evidence is not None
        assert receipts[0].rendering_evidence == evidence_json
        parsed = json.loads(receipts[0].rendering_evidence)
        assert parsed["schema_version"] == "1"

    async def test_failed_receipt_evidence_none(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Failed receipts have rendering_evidence=None (not populated)."""
        event = make_storage_event(event_id="evt-fail-ev")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-fail-ev",
            event_id="evt-fail-ev",
            delivery_plan_id="plan-fail-ev",
            target_adapter="fake_presentation",
            status="failed",
            error="TimeoutError: timed out",
            failure_kind="adapter_transient",
        )
        await temp_storage.append_receipt(receipt)

        receipts = await temp_storage.list_receipts_for_event("evt-fail-ev")
        assert len(receipts) == 1
        assert receipts[0].rendering_evidence is None

    async def test_queued_receipt_with_evidence_persists(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A queued receipt with rendering_evidence survives store/readback."""
        event = make_storage_event(event_id="evt-queued-ev")
        await temp_storage.append(event)

        evidence_json = self._sample_evidence_json()
        receipt = DeliveryReceipt(
            receipt_id="rcpt-queued-ev",
            event_id="evt-queued-ev",
            delivery_plan_id="plan-queued-ev",
            target_adapter="fake_presentation",
            status="queued",
            rendering_evidence=evidence_json,
        )
        await temp_storage.append_receipt(receipt)

        status = await temp_storage.delivery_status(
            "plan-queued-ev", "fake_presentation"
        )
        assert status is not None
        assert status.status == "queued"
        assert status.rendering_evidence == evidence_json

    async def test_e2e_render_evidence_receipt_sqlite_roundtrip(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """End-to-end: render → RenderingEvidence → DeliveryReceipt →
        SQLite append/readback → JSON parsed → key fields match.

        Exercises the full canonical serialization path
        (to_dict() + json.dumps) rather than msgspec.
        """
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer

        # Build evidence via the real rendering pipeline.
        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = make_storage_event(event_id="evt-e2e-evidence")
        await temp_storage.append(event)

        rendering_result = await pipeline.render(
            event,
            target_adapter="fake_presentation",
            target_channel="ch-1",
            target_platform=None,
        )

        # The pipeline must have attached evidence.
        assert rendering_result.rendering_evidence is not None
        evidence = rendering_result.rendering_evidence

        # Serialize using the canonical path (to_dict + json.dumps).
        evidence_json = json.dumps(evidence.to_dict(), sort_keys=True)

        # Build a receipt with the serialized evidence.
        receipt = DeliveryReceipt(
            receipt_id="rcpt-e2e",
            event_id="evt-e2e-evidence",
            delivery_plan_id="plan-e2e",
            target_adapter="fake_presentation",
            target_channel="ch-1",
            status="sent",
            rendering_evidence=evidence_json,
        )
        await temp_storage.append_receipt(receipt)

        # Read back via delivery_status.
        status = await temp_storage.delivery_status(
            "plan-e2e", "fake_presentation", "ch-1"
        )
        assert status is not None
        assert status.rendering_evidence is not None

        # Parse and verify key fields survive the round-trip.
        parsed = json.loads(status.rendering_evidence)
        assert parsed["schema_version"] == "1"
        assert parsed["renderer"] == "text"
        assert parsed["delivery_strategy"] == "direct"
        assert parsed["target_adapter"] == "fake_presentation"
        assert parsed["target_channel"] == "ch-1"
        assert parsed["truncated"] is False
        # Stable shape: None fields must be present as null.
        assert parsed["capability_policy"] is None
        assert parsed["fallback_applied"] is None
        assert "rendered_text_chars" in parsed

    async def test_queued_evidence_survives_to_sent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When a queued receipt carries rendering_evidence, the evidence
        survives through storage and can be retrieved intact — proving
        that evidence would be preserved across the queued → sent
        transition when the pipeline copies it from the queued receipt
        into the supplemental sent receipt."""
        event = make_storage_event(event_id="evt-qev-survive")
        await temp_storage.append(event)

        evidence_json = self._sample_evidence_json()

        # Append a queued receipt with evidence.
        queued = DeliveryReceipt(
            receipt_id="rcpt-queued-surv",
            event_id="evt-qev-survive",
            delivery_plan_id="plan-qev-surv",
            target_adapter="fake_presentation",
            status="queued",
            rendering_evidence=evidence_json,
        )
        await temp_storage.append_receipt(queued)

        # Verify queued receipt preserves evidence.
        receipts = await temp_storage.list_receipts_for_event("evt-qev-survive")
        assert len(receipts) == 1
        assert receipts[0].rendering_evidence == evidence_json

        # Simulate the pipeline's supplemental sent receipt (which copies
        # rendering_evidence from the queued receipt).
        sent = DeliveryReceipt(
            receipt_id="rcpt-sent-surv",
            event_id="evt-qev-survive",
            delivery_plan_id="plan-qev-surv",
            target_adapter="fake_presentation",
            status="sent",
            parent_receipt_id="rcpt-queued-surv",
            rendering_evidence=getattr(receipts[0], "rendering_evidence", None),
        )
        await temp_storage.append_receipt(sent)

        # Verify the sent receipt also carries the original evidence.
        status = await temp_storage.delivery_status(
            "plan-qev-surv", "fake_presentation"
        )
        assert status is not None
        assert status.status == "sent"
        assert status.rendering_evidence == evidence_json
        parsed = json.loads(status.rendering_evidence)
        assert parsed["renderer"] == "text"


# ===================================================================
# Unknown receipt status validation
# ===================================================================


class TestUnknownReceiptStatusRejected:
    """append_receipt raises ValueError for unknown receipt statuses
    and does not append a row."""

    async def test_unknown_receipt_status_raises_value_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Passing an unknown status to append_receipt raises ValueError."""
        event = make_storage_event(event_id="evt-unknown-rcpt")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-bad-status",
            event_id="evt-unknown-rcpt",
            delivery_plan_id="plan-bad-status",
            target_adapter="adapter_bad",
            status="not_a_real_status",  # type: ignore[arg-type]
        )

        with pytest.raises(ValueError, match="Unknown receipt status"):
            await temp_storage.append_receipt(receipt)

    async def test_unknown_receipt_status_does_not_append_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After a ValueError for unknown receipt status, no row exists."""
        event = make_storage_event(event_id="evt-unknown-row")
        await temp_storage.append(event)

        # Count receipts before.
        rows_before = await temp_storage._read_all(
            "SELECT COUNT(*) AS cnt FROM delivery_receipts WHERE event_id = ?",
            ("evt-unknown-row",),
        )
        count_before = rows_before[0]["cnt"]

        receipt = DeliveryReceipt(
            receipt_id="rcpt-no-row",
            event_id="evt-unknown-row",
            delivery_plan_id="plan-no-row",
            target_adapter="adapter_no",
            status="totally_invalid",  # type: ignore[arg-type]
        )

        with pytest.raises(ValueError, match="Unknown receipt status"):
            await temp_storage.append_receipt(receipt)

        # Count receipts after — must be unchanged.
        rows_after = await temp_storage._read_all(
            "SELECT COUNT(*) AS cnt FROM delivery_receipts WHERE event_id = ?",
            ("evt-unknown-row",),
        )
        count_after = rows_after[0]["cnt"]
        assert count_after == count_before
