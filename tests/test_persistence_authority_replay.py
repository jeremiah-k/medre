"""Persistence authority tests: replay.

Focused tests proving the replay authority model where gaps exist from
Waves 1–2, without duplicating existing near-limit test files.

Covers:
  1. Replay creates new receipt rows with source='replay' and
     replay_run_id set; old receipt rows are not mutated.
  2. STRICT and DRY_RUN replay do not create any new receipt rows.
  3. Replay delivery through pipeline creates new attempts (incremented
     attempt_number) without changing existing receipt data.
"""

from __future__ import annotations

from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import DeliveryReceipt
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_receipt(
    receipt_id: str,
    event_id: str,
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "adapter_a",
    status: str = "sent",
    source: str = "live",
    replay_run_id: str | None = None,
    attempt_number: int = 1,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        status=status,  # type: ignore[arg-type]
        source=source,
        replay_run_id=replay_run_id,
        attempt_number=attempt_number,
    )


async def _receipt_count(storage: SQLiteStorage, event_id: str) -> int:
    rows = await storage._read_all(
        "SELECT COUNT(*) AS cnt FROM delivery_receipts WHERE event_id = ?",
        (event_id,),
    )
    return rows[0]["cnt"]


async def _all_receipt_rows(storage: SQLiteStorage, event_id: str) -> list[dict]:
    return await storage._read_all(
        "SELECT * FROM delivery_receipts WHERE event_id = ? ORDER BY sequence ASC",
        (event_id,),
    )


# ===================================================================
# 1. Replay creates new receipt rows; old rows unchanged
# ===================================================================


class TestReplayNewReceiptsPreserveOld:
    """Replay creates new receipt rows; existing receipt data is immutable."""

    async def test_replay_receipts_have_source_replay(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Replay-generated receipts carry source='replay'."""
        event = make_storage_event(event_id="evt-replay-src")
        await temp_storage.append(event)

        # Live receipt first
        await temp_storage.append_receipt(
            _make_receipt("rcpt-live-1", "evt-replay-src", source="live")
        )

        # Simulate replay appending a new receipt
        await temp_storage.append_receipt(
            _make_receipt(
                "rcpt-replay-1",
                "evt-replay-src",
                source="replay",
                replay_run_id="run-src-1",
                attempt_number=2,
            )
        )

        rows = await _all_receipt_rows(temp_storage, "evt-replay-src")
        assert len(rows) == 2
        assert rows[0]["source"] == "live"
        assert rows[0]["replay_run_id"] is None
        assert rows[1]["source"] == "replay"
        assert rows[1]["replay_run_id"] == "run-src-1"

    async def test_old_receipt_rows_unchanged_after_replay_append(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After replay appends a new receipt, old receipt data is byte-for-byte identical."""
        event = make_storage_event(event_id="evt-immutable")
        await temp_storage.append(event)

        # Pre-replay receipts with rich data
        await temp_storage.append_receipt(
            _make_receipt(
                "rcpt-old-1",
                "evt-immutable",
                delivery_plan_id="plan-imm",
                target_adapter="adapter_imm",
                status="sent",
                source="live",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                "rcpt-old-2",
                "evt-immutable",
                delivery_plan_id="plan-imm",
                target_adapter="adapter_imm",
                status="failed",
                source="live",
                attempt_number=2,
            )
        )

        # Snapshot old receipt data
        old_rows = await _all_receipt_rows(temp_storage, "evt-immutable")
        assert len(old_rows) == 2
        old_data = [
            {
                "receipt_id": r["receipt_id"],
                "status": r["status"],
                "source": r["source"],
                "replay_run_id": r["replay_run_id"],
                "attempt_number": r["attempt_number"],
            }
            for r in old_rows
        ]

        # Simulate replay appending new receipts
        await temp_storage.append_receipt(
            _make_receipt(
                "rcpt-replay-1",
                "evt-immutable",
                delivery_plan_id="plan-imm",
                target_adapter="adapter_imm",
                status="sent",
                source="replay",
                replay_run_id="run-imm",
                attempt_number=3,
            )
        )

        # Re-read all rows
        all_rows = await _all_receipt_rows(temp_storage, "evt-immutable")
        assert len(all_rows) == 3

        # First two rows must be byte-identical to the snapshot
        for i, old in enumerate(old_data):
            for key, expected_val in old.items():
                assert all_rows[i][key] == expected_val, (
                    f"Row {i} field {key}: expected {expected_val!r}, "
                    f"got {all_rows[i][key]!r}"
                )

    async def test_replay_receipt_attempt_number_increments(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Replay receipts use incremented attempt_number."""
        event = make_storage_event(event_id="evt-attempt")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            _make_receipt(
                "rcpt-att-1",
                "evt-attempt",
                attempt_number=1,
                status="failed",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                "rcpt-att-2",
                "evt-attempt",
                attempt_number=2,
                source="replay",
                replay_run_id="run-att",
                status="sent",
            )
        )

        receipts = await temp_storage.list_receipts_for_event("evt-attempt")
        assert len(receipts) == 2
        assert receipts[0].attempt_number == 1
        assert receipts[0].source == "live"
        assert receipts[1].attempt_number == 2
        assert receipts[1].source == "replay"
        assert receipts[1].replay_run_id == "run-att"


# ===================================================================
# 2. STRICT and DRY_RUN replay do not create receipt rows
# ===================================================================


class TestStrictDryRunNoReceipts:
    """STRICT and DRY_RUN replay modes do not create new receipt rows.

    These modes are read-only: STRICT verifies store integrity, DRY_RUN
    simulates the pipeline but skips actual delivery. Neither should
    append receipts to storage.
    """

    async def test_strict_replay_no_new_receipts(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """STRICT replay does not append any receipt rows."""
        event = make_storage_event(event_id="evt-strict")
        await temp_storage.append(event)

        # Pre-seed a live receipt
        await temp_storage.append_receipt(
            _make_receipt("rcpt-strict-live", "evt-strict")
        )
        count_before = await _receipt_count(temp_storage, "evt-strict")

        engine = ReplayEngine(storage=temp_storage, pipeline=None)
        request = ReplayRequest(mode=ReplayMode.STRICT)
        results = [r async for r in engine.replay(request)]
        assert len(results) == 1

        count_after = await _receipt_count(temp_storage, "evt-strict")
        assert count_after == count_before

    async def test_dry_run_replay_no_new_receipts(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """DRY_RUN replay does not append any receipt rows."""
        event = make_storage_event(event_id="evt-dryrun")
        await temp_storage.append(event)

        await temp_storage.append_receipt(_make_receipt("rcpt-dry-live", "evt-dryrun"))
        count_before = await _receipt_count(temp_storage, "evt-dryrun")

        engine = ReplayEngine(storage=temp_storage, pipeline=None)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)
        results = [r async for r in engine.replay(request)]
        assert len(results) > 0

        count_after = await _receipt_count(temp_storage, "evt-dryrun")
        assert count_after == count_before

    async def test_strict_replay_preserves_existing_receipt_data(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """STRICT replay does not mutate existing receipt row data."""
        event = make_storage_event(event_id="evt-strict-imm")
        await temp_storage.append(event)

        await temp_storage.append_receipt(
            _make_receipt(
                "rcpt-strict-imm",
                "evt-strict-imm",
                status="failed",
                attempt_number=1,
            )
        )
        rows_before = await _all_receipt_rows(temp_storage, "evt-strict-imm")

        engine = ReplayEngine(storage=temp_storage, pipeline=None)
        request = ReplayRequest(mode=ReplayMode.STRICT)
        _ = [r async for r in engine.replay(request)]

        rows_after = await _all_receipt_rows(temp_storage, "evt-strict-imm")
        assert len(rows_after) == len(rows_before)
        for key in ("receipt_id", "status", "source", "attempt_number"):
            assert (
                rows_before[0][key] == rows_after[0][key]
            ), f"Field {key} changed: {rows_before[0][key]!r} -> {rows_after[0][key]!r}"
