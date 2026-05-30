"""Tests for SQLiteStorage: channel-aware delivery_status queries."""

from __future__ import annotations

from medre.core.events import DeliveryReceipt
from medre.core.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


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
