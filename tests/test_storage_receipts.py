"""Tests for SQLiteStorage: delivery receipts, append-only receipts,
ordering guarantees, receipt lineage, receipt query helpers,
receipt sequence monotonicity, receipt source/replay_run_id,
delivery_status failure_kind, list_due_retry_receipts integration,
and rendering_evidence persistence round-trip.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
)
from medre.core.storage import EventFilter, SQLiteStorage
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
# Channel-aware delivery_status queries
# ===================================================================


class TestDeliveryStatusByChannel:
    """delivery_status groups by target_channel; optional channel filter
    distinguishes receipts for the same plan+adapter but different channels.
    """

    @staticmethod
    def _make_channel_receipt(
        receipt_id: str,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
        status: str = "sent",
        attempt_number: int = 1,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            receipt_id=receipt_id,
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            status=status,  # type: ignore[arg-type]
            attempt_number=attempt_number,
        )

    async def test_same_plan_adapter_different_channels_distinct(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """delivery_status with target_channel distinguishes two channels
        under the same plan + adapter."""
        event = make_storage_event(event_id="evt-ch-distinct")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            self._make_channel_receipt(
                "rcpt-ch-a",
                "evt-ch-distinct",
                "plan-ch",
                "adapter_ch",
                "channel-a",
                status="sent",
            )
        )
        await temp_storage.append_receipt(
            self._make_channel_receipt(
                "rcpt-ch-b",
                "evt-ch-distinct",
                "plan-ch",
                "adapter_ch",
                "channel-b",
                status="failed",
            )
        )

        status_a = await temp_storage.delivery_status(
            "plan-ch", "adapter_ch", "channel-a"
        )
        status_b = await temp_storage.delivery_status(
            "plan-ch", "adapter_ch", "channel-b"
        )

        assert status_a is not None
        assert status_a.receipt_id == "rcpt-ch-a"
        assert status_a.target_channel == "channel-a"
        assert status_a.status == "sent"

        assert status_b is not None
        assert status_b.receipt_id == "rcpt-ch-b"
        assert status_b.target_channel == "channel-b"
        assert status_b.status == "failed"

    async def test_channel_filter_returns_none_for_unknown_channel(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """delivery_status with a non-existent channel returns None even when
        the plan + adapter has receipts on other channels."""
        event = make_storage_event(event_id="evt-ch-none")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            self._make_channel_receipt(
                "rcpt-ch-exist",
                "evt-ch-none",
                "plan-none",
                "adapter_none",
                "channel-x",
            )
        )

        status = await temp_storage.delivery_status(
            "plan-none", "adapter_none", "channel-z"
        )
        assert status is None

    async def test_channel_progression_returns_latest_for_channel(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Multiple receipts on the same channel: delivery_status with
        target_channel returns the latest receipt for that channel only."""
        event = make_storage_event(event_id="evt-ch-prog")
        await temp_storage.append(event)

        for i, st in enumerate(["queued", "sent", "suppressed"]):
            await temp_storage.append_receipt(
                self._make_channel_receipt(
                    f"rcpt-prog-{i}",
                    "evt-ch-prog",
                    "plan-prog",
                    "adapter_prog",
                    "channel-prog",
                    status=st,
                    attempt_number=i + 1,
                )
            )

        status = await temp_storage.delivery_status(
            "plan-prog", "adapter_prog", "channel-prog"
        )
        assert status is not None
        assert status.status == "suppressed"
        assert status.attempt_number == 3

    async def test_null_channel_receipt_queryable_with_none(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A receipt with target_channel=None is returned when querying
        with target_channel=None (the default)."""
        event = make_storage_event(event_id="evt-ch-null")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-null-ch",
            event_id="evt-ch-null",
            delivery_plan_id="plan-null",
            target_adapter="adapter_null",
            target_channel=None,
            status="sent",
        )
        await temp_storage.append_receipt(receipt)

        # Default target_channel=None returns the NULL-channel receipt.
        status = await temp_storage.delivery_status("plan-null", "adapter_null")
        assert status is not None
        assert status.receipt_id == "rcpt-null-ch"
        assert status.target_channel is None

        # Explicit target_channel=None also returns it.
        status2 = await temp_storage.delivery_status(
            "plan-null", "adapter_null", target_channel=None
        )
        assert status2 is not None
        assert status2.receipt_id == "rcpt-null-ch"

    async def test_named_channel_does_not_match_null(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A named-channel filter does NOT match a NULL-channel receipt."""
        event = make_storage_event(event_id="evt-ch-mix")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-mix-null",
                event_id="evt-ch-mix",
                delivery_plan_id="plan-mix",
                target_adapter="adapter_mix",
                target_channel=None,
                status="sent",
            )
        )
        await temp_storage.append_receipt(
            self._make_channel_receipt(
                "rcpt-mix-named",
                "evt-ch-mix",
                "plan-mix",
                "adapter_mix",
                "channel-named",
                status="failed",
            )
        )

        # Filter for named channel returns only the named receipt.
        status_named = await temp_storage.delivery_status(
            "plan-mix", "adapter_mix", "channel-named"
        )
        assert status_named is not None
        assert status_named.receipt_id == "rcpt-mix-named"

        # Default (None) returns only the NULL-channel receipt.
        status_null = await temp_storage.delivery_status(
            "plan-mix", "adapter_mix", target_channel=None
        )
        assert status_null is not None
        assert status_null.receipt_id == "rcpt-mix-null"

    async def test_null_does_not_match_named_channel(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Explicit target_channel=None returns only NULL-channel receipts,
        never named-channel receipts."""
        event = make_storage_event(event_id="evt-ch-null-only")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            self._make_channel_receipt(
                "rcpt-no-named",
                "evt-ch-null-only",
                "plan-no",
                "adapter_no",
                "channel-x",
                status="sent",
            )
        )

        # Querying for NULL channel should not find the named-channel receipt.
        status = await temp_storage.delivery_status("plan-no", "adapter_no", None)
        assert status is None

    async def test_multiple_named_channels_remain_distinct(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Multiple named channels under the same plan+adapter are independently
        queryable."""
        event = make_storage_event(event_id="evt-ch-multi")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            self._make_channel_receipt(
                "rcpt-ma",
                "evt-ch-multi",
                "plan-multi",
                "adapter_multi",
                "channel-a",
                status="sent",
            )
        )
        await temp_storage.append_receipt(
            self._make_channel_receipt(
                "rcpt-mb",
                "evt-ch-multi",
                "plan-multi",
                "adapter_multi",
                "channel-b",
                status="failed",
            )
        )

        status_a = await temp_storage.delivery_status(
            "plan-multi", "adapter_multi", "channel-a"
        )
        status_b = await temp_storage.delivery_status(
            "plan-multi", "adapter_multi", "channel-b"
        )

        assert status_a is not None
        assert status_a.receipt_id == "rcpt-ma"
        assert status_a.status == "sent"

        assert status_b is not None
        assert status_b.receipt_id == "rcpt-mb"
        assert status_b.status == "failed"

    async def test_empty_string_channel_normalized_to_null(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Passing target_channel='' is normalised to NULL at storage time.
        The stored receipt reads back as target_channel=None, and querying
        with target_channel=None returns it."""
        event = make_storage_event(event_id="evt-empty-ch")
        await temp_storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-empty",
            event_id="evt-empty-ch",
            delivery_plan_id="plan-empty",
            target_adapter="adapter_empty",
            target_channel="",
            status="sent",
        )
        await temp_storage.append_receipt(receipt)

        # Stored value should be NULL, not empty string.
        rows = await temp_storage._read_all(
            "SELECT target_channel FROM delivery_receipts WHERE receipt_id = ?",
            ("rcpt-empty",),
        )
        assert len(rows) == 1
        assert rows[0]["target_channel"] is None

        # Querying with target_channel=None returns the receipt.
        status = await temp_storage.delivery_status("plan-empty", "adapter_empty", None)
        assert status is not None
        assert status.receipt_id == "rcpt-empty"
        assert status.target_channel is None

        # Querying with target_channel="" also returns it (view COALESCE groups them).
        status_empty = await temp_storage.delivery_status(
            "plan-empty", "adapter_empty", ""
        )
        assert status_empty is not None
        assert status_empty.receipt_id == "rcpt-empty"

    async def test_empty_string_does_not_create_distinct_group(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Receipts with target_channel="" and target_channel=None are not
        stored as separate groups — empty string is normalised to NULL."""
        event = make_storage_event(event_id="evt-dup-ch")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-dup-null",
                event_id="evt-dup-ch",
                delivery_plan_id="plan-dup",
                target_adapter="adapter_dup",
                target_channel=None,
                status="queued",
            )
        )
        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-dup-empty",
                event_id="evt-dup-ch",
                delivery_plan_id="plan-dup",
                target_adapter="adapter_dup",
                target_channel="",
                status="sent",
            )
        )

        # Both receipts should be in the same (NULL) channel group.
        # delivery_status with target_channel=None returns the latest (sent).
        status = await temp_storage.delivery_status("plan-dup", "adapter_dup", None)
        assert status is not None
        assert status.receipt_id == "rcpt-dup-empty"
        assert status.status == "sent"
        assert status.target_channel is None

        # Verify there is only one group in the view.
        view_rows = await temp_storage._read_all(
            "SELECT * FROM delivery_status WHERE delivery_plan_id = ? AND target_adapter = ?",
            ("plan-dup", "adapter_dup"),
        )
        assert len(view_rows) == 1


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
        """Return a sample rendering evidence JSON string."""
        return json.dumps({
            "schema_version": "1",
            "renderer": "text",
            "delivery_strategy": "direct",
            "target_adapter": "fake_presentation",
            "target_platform": None,
            "target_channel": "ch-1",
            "max_text_chars": None,
            "max_text_bytes": None,
            "capability_level": "native",
            "fallback_applied": None,
            "truncated": False,
            "rendered_text_chars": 5,
            "rendered_text_bytes": 5,
            "original_text_chars": None,
        })

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
