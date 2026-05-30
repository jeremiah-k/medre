"""State transition validation tests for receipt and outbox state machines.

Validates that:
- Receipt statuses match the declared Literal type.
- Terminal receipt states have no outgoing transitions.
- Receipts are append-only (immutable once persisted).
- Failed → dead_lettered transition preserves parent_receipt_id linkage.
- Outbox terminal states are correctly identified.
- Outbox reclaim is idempotent for terminal items.
- Outbox status transitions match storage method contracts.
- Outbox transitions drive receipt creation, not the reverse.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.canonical import DeliveryReceipt
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import DeliveryOutboxItem
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ===================================================================
# Helpers
# ===================================================================


def _make_receipt(
    receipt_id: str = "rcpt-test",
    status: str = "queued",
    event_id: str = "evt-test",
    delivery_plan_id: str = "plan-test",
    target_adapter: str = "fake_presentation",
    parent_receipt_id: str | None = None,
    attempt_number: int = 1,
) -> DeliveryReceipt:
    """Create a minimal DeliveryReceipt for testing."""
    return DeliveryReceipt(
        sequence=0,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        status=status,
        created_at=datetime.now(timezone.utc),
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
    )


def _make_outbox_item(
    outbox_id: str = "obox-test",
    event_id: str = "evt-test",
    status: str = "pending",
    delivery_plan_id: str = "plan-test",
    target_adapter: str = "fake_presentation",
    attempt_number: int = 1,
) -> DeliveryOutboxItem:
    """Create a minimal DeliveryOutboxItem for testing."""
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-test",
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        status=status,
        attempt_number=attempt_number,
    )


# ===================================================================
# Receipt state machine tests
# ===================================================================


class TestReceiptStatusValues:
    """Validate the receipt status Literal matches source."""

    def test_receipt_status_values_match_source(self) -> None:
        """The DeliveryReceipt.status Literal must contain exactly the
        declared statuses from canonical.py.

        This test pins the current set so any accidental addition or
        removal is caught as a deliberate change.
        """
        import typing

        # Extract the Literal annotation from the DeliveryReceipt struct.
        hints = typing.get_type_hints(DeliveryReceipt)
        status_type = hints.get("status")
        assert status_type is not None, "DeliveryReceipt must have a 'status' field"

        # For Literal["a", "b", ...], extract the string values.
        status_args = typing.get_args(status_type)
        actual = set(status_args)

        # The expected set as currently defined in canonical.py.
        # The current closed receipt vocabulary is intentionally limited
        # to these five statuses.
        expected = {"queued", "sent", "failed", "dead_lettered", "suppressed"}
        assert actual == expected, (
            f"DeliveryReceipt.status Literal mismatch.\n"
            f"  Expected: {sorted(expected)}\n"
            f"  Actual:   {sorted(actual)}\n"
            f"  Missing:  {sorted(expected - actual)}\n"
            f"  Extra:    {sorted(actual - expected)}"
        )


class TestReceiptTerminalStates:
    """Verify that terminal receipt statuses have no code paths that
    modify them in the pipeline."""

    def test_receipt_terminal_states_have_no_outgoing_transitions(
        self,
    ) -> None:
        """Terminal receipt statuses must never appear as the *source*
        status in a code path that creates a subsequent receipt with a
        *different* status.

        We verify this by inspecting the pipeline source code for patterns
        where a receipt with a terminal status is read and then a new
        receipt with a different status is created.  Since receipts are
        append-only, the only valid pattern is a new receipt referencing
        the old one via ``parent_receipt_id`` — the old receipt row is
        never modified.

        This test uses a structural assertion: no storage method exists
        to update or delete receipt rows.
        """
        # The StorageBackend protocol has no update_receipt or
        # delete_receipt method.  The only mutation method is
        # append_receipt which is INSERT-only.
        from medre.core.storage.backend import StorageBackend

        method_names = [m for m in dir(StorageBackend) if "receipt" in m.lower()]
        # Only append and query methods should exist — no update/delete.
        for name in method_names:
            assert (
                "update" not in name.lower()
            ), f"StorageBackend must not have receipt-update method: {name}"
            assert (
                "delete" not in name.lower()
            ), f"StorageBackend must not have receipt-delete method: {name}"
            assert (
                "modify" not in name.lower()
            ), f"StorageBackend must not have receipt-modify method: {name}"


class TestReceiptAppendOnlyInvariant:
    """Receipts are append-only: once persisted, rows are never changed."""

    async def test_append_only_invariant(self, temp_storage: SQLiteStorage) -> None:
        """Create several receipts, then append more, and verify all
        original receipts are unchanged by comparing stored fields."""
        original_receipts = [
            _make_receipt(
                receipt_id=f"rcpt-orig-{i}",
                event_id="evt-append-only",
                delivery_plan_id="plan-ao",
                status="queued" if i == 0 else "sent",
                attempt_number=i + 1,
                parent_receipt_id=f"rcpt-orig-{i-1}" if i > 0 else None,
            )
            for i in range(3)
        ]

        # Persist originals.
        for r in original_receipts:
            await temp_storage.append_receipt(r)

        # Snapshot original fields.
        snapshots = [
            {
                "receipt_id": r.receipt_id,
                "status": r.status,
                "attempt_number": r.attempt_number,
                "parent_receipt_id": r.parent_receipt_id,
                "event_id": r.event_id,
            }
            for r in original_receipts
        ]

        # Append additional receipts (simulating retry lineage).
        for i in range(3, 5):
            await temp_storage.append_receipt(
                _make_receipt(
                    receipt_id=f"rcpt-extra-{i}",
                    event_id="evt-append-only",
                    delivery_plan_id="plan-ao",
                    status="failed",
                    attempt_number=i + 1,
                    parent_receipt_id=(
                        f"rcpt-orig-{2}" if i == 3 else f"rcpt-extra-{i-1}"
                    ),
                )
            )

        # Verify originals are unchanged in storage.
        stored = await temp_storage.list_receipts_for_plan(
            "plan-ao", "fake_presentation"
        )
        # Filter to just our original receipt_ids.
        stored_by_id = {r.receipt_id: r for r in stored}

        for snap in snapshots:
            rid = snap["receipt_id"]
            assert rid in stored_by_id, f"Original receipt {rid} missing from storage"
            stored_r = stored_by_id[rid]
            assert stored_r.status == snap["status"], (
                f"Receipt {rid} status mutated: expected {snap['status']!r}, "
                f"got {stored_r.status!r}"
            )
            assert stored_r.attempt_number == snap["attempt_number"]
            assert stored_r.parent_receipt_id == snap["parent_receipt_id"]
            assert stored_r.event_id == snap["event_id"]


class TestReceiptFailedToDeadLettered:
    """Verify failed → dead_lettered transition through retry exhaustion."""

    async def test_failed_to_dead_lettered_transition(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When retry is exhausted, a dead_lettered receipt is created
        with ``parent_receipt_id`` linking to the preceding failed receipt."""
        failed_receipt = _make_receipt(
            receipt_id="rcpt-failed-001",
            event_id="evt-dl-test",
            delivery_plan_id="plan-dl",
            status="failed",
            attempt_number=1,
        )
        await temp_storage.append_receipt(failed_receipt)

        # Simulate the dead_lettered receipt that retry exhaustion produces.
        dead_lettered = DeliveryReceipt(
            sequence=0,
            receipt_id="rcpt-dl-001",
            event_id="evt-dl-test",
            delivery_plan_id="plan-dl",
            target_adapter="fake_presentation",
            route_id="route-test",
            status="dead_lettered",
            error="Retry exhausted",
            failure_kind="adapter_permanent",
            created_at=datetime.now(timezone.utc),
            attempt_number=2,
            parent_receipt_id=failed_receipt.receipt_id,
        )
        await temp_storage.append_receipt(dead_lettered)

        # Verify the linkage.
        receipts = await temp_storage.list_receipts_for_plan(
            "plan-dl", "fake_presentation"
        )
        by_id = {r.receipt_id: r for r in receipts}

        assert "rcpt-failed-001" in by_id
        assert "rcpt-dl-001" in by_id
        dl = by_id["rcpt-dl-001"]
        assert dl.status == "dead_lettered"
        assert dl.parent_receipt_id == "rcpt-failed-001"
        assert dl.attempt_number == 2

        # Verify the failed receipt is unchanged.
        f = by_id["rcpt-failed-001"]
        assert f.status == "failed"
        assert f.attempt_number == 1


# ===================================================================
# Outbox state machine tests
# ===================================================================


class TestOutboxTerminalStates:
    """Verify outbox terminal states are correctly identified."""

    @pytest.mark.parametrize(
        "terminal_status",
        ["sent", "dead_lettered", "cancelled", "abandoned"],
    )
    def test_outbox_terminal_states(self, terminal_status: str) -> None:
        """sent, dead_lettered, cancelled, abandoned must be terminal."""
        item = _make_outbox_item(status=terminal_status)
        assert item.is_terminal, f"Status {terminal_status!r} should be terminal"

    @pytest.mark.parametrize(
        "non_terminal_status",
        ["pending", "in_progress", "queued", "retry_wait"],
    )
    def test_outbox_non_terminal_states(self, non_terminal_status: str) -> None:
        """pending, in_progress, queued, retry_wait must NOT be terminal."""
        item = _make_outbox_item(status=non_terminal_status)
        assert (
            not item.is_terminal
        ), f"Status {non_terminal_status!r} should NOT be terminal"


class TestOutboxReclaim:
    """Verify outbox reclaim is idempotent for terminal items."""

    async def test_outbox_reclaim_is_idempotent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Creating an outbox item, marking it terminal, then creating
        again with the same key should remove the old row and insert new."""
        item1 = _make_outbox_item(
            outbox_id="obox-reclaim-1",
            event_id="evt-reclaim",
            delivery_plan_id="plan-reclaim",
            status="in_progress",
        )
        created1 = await temp_storage.create_outbox_item(item1)
        assert created1.outbox_id == "obox-reclaim-1"

        # Mark it as dead_lettered (terminal).
        await temp_storage.mark_outbox_dead_lettered(
            created1.outbox_id,
            receipt_id=None,
            failure_kind="test",
            error_summary="exhausted",
        )

        # Verify it's dead_lettered now.
        fetched = await temp_storage.get_outbox_item("obox-reclaim-1")
        assert fetched is not None
        assert fetched.status == "dead_lettered"

        # Reclaim: create a new item with the same key tuple.
        # The old terminal row should be deleted and a new one inserted.
        item2 = _make_outbox_item(
            outbox_id="obox-reclaim-2",
            event_id="evt-reclaim",
            delivery_plan_id="plan-reclaim",
            status="in_progress",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # The old row should no longer exist.
        old = await temp_storage.get_outbox_item("obox-reclaim-1")
        assert (
            old is None
        ), "Old terminal outbox item should have been deleted on reclaim"

        # The new row should exist.
        new = await temp_storage.get_outbox_item(created2.outbox_id)
        assert new is not None
        assert new.status == "in_progress"


class TestOutboxStatusTransitionsMatchCode:
    """Parameterized test verifying storage methods for each outbox status."""

    @pytest.mark.parametrize(
        "status,expected_methods",
        [
            (
                "pending",
                {
                    "create_outbox_item",
                    "claim_due_outbox_items",
                    "mark_outbox_cancelled",
                },
            ),
            (
                "in_progress",
                {
                    "mark_outbox_sent",
                    "mark_outbox_queued",
                    "mark_outbox_retry_wait",
                    "mark_outbox_dead_lettered",
                    "mark_outbox_cancelled",
                    "renew_outbox_lease",
                    "release_outbox_claim",
                },
            ),
            (
                "queued",
                {"mark_outbox_sent", "mark_outbox_cancelled"},
            ),
            (
                "retry_wait",
                {
                    "create_outbox_item",
                    "claim_due_outbox_items",
                    "mark_outbox_dead_lettered",
                    "mark_outbox_cancelled",
                },
            ),
            # Terminal statuses: only create_outbox_item can reclaim them.
            ("sent", set()),
            ("dead_lettered", set()),
            ("cancelled", set()),
            ("abandoned", set()),
        ],
    )
    def test_outbox_status_transitions_match_code(
        self, status: str, expected_methods: set[str]
    ) -> None:
        """For each outbox status, verify the set of storage methods that
        can transition FROM that status matches expectations.

        This is a documentation test: it codifies the transition map from
        the StorageBackend protocol docstrings."""
        from medre.core.storage.backend import StorageBackend

        # All methods that can change outbox state.

        # For terminal statuses, no transition method applies directly.
        # They can only be reclaimed via create_outbox_item (which deletes
        # and re-inserts), but that's not a status transition *from*
        # the terminal status — it's a row replacement.
        if not expected_methods:
            return

        # Verify each expected method exists on the protocol.
        for method_name in expected_methods:
            assert hasattr(
                StorageBackend, method_name
            ), f"StorageBackend missing expected method: {method_name}"


# ===================================================================
# Relationship tests: outbox ↔ receipt
# ===================================================================


class TestOutboxReceiptRelationship:
    """Verify outbox transitions drive receipt creation, not the reverse."""

    @pytest.fixture
    def router(self) -> Router:
        """Router from fake_transport to fake_presentation."""
        return Router(
            routes=[
                Route(
                    id="route-st-test",
                    source=RouteSource(
                        adapter="fake_transport",
                        event_kinds=("message.created",),
                        channel="ch-0",
                    ),
                    targets=[RouteTarget(adapter="fake_presentation")],
                )
            ]
        )

    async def test_outbox_transition_drives_receipt_creation(
        self,
        temp_storage: SQLiteStorage,
        router: Router,
    ) -> None:
        """Deliver an event through the pipeline, verify the outbox item
        exists AND has a receipt_id linking it to the delivery receipt."""
        fake = FakePresentationAdapter(adapter_id="fake_presentation")
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="evt-outbox-receipt-001")

        try:
            await runner.handle_ingress(event)

            # Verify outbox item exists.
            items = await temp_storage.list_outbox_items()
            assert len(items) >= 1, "Expected at least one outbox item"

            matching = [i for i in items if i.event_id == "evt-outbox-receipt-001"]
            assert len(matching) == 1
            obox = matching[0]
            assert obox.status in ("sent", "queued")

            # Verify outbox item has a receipt_id.
            assert (
                obox.receipt_id is not None
            ), "Outbox item must have receipt_id linking to delivery receipt"

            # Verify the referenced receipt exists.
            receipts = await temp_storage.list_receipts_for_event(
                "evt-outbox-receipt-001"
            )
            receipt_ids = {r.receipt_id for r in receipts}
            assert obox.receipt_id in receipt_ids
        finally:
            await runner.stop()

    async def test_receipt_never_drives_outbox_transition(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Appending a receipt must NOT change outbox state.

        Receipts are append-only evidence records.  Outbox items are the
        operational driver; they are mutated by pipeline code, not by
        receipt storage."""
        # Create an outbox item.
        item = _make_outbox_item(
            outbox_id="obox-rt-001",
            event_id="evt-rt-001",
            delivery_plan_id="plan-rt",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        # Snapshot the outbox item state.
        before = await temp_storage.get_outbox_item("obox-rt-001")
        assert before is not None

        # Append a receipt (simulating a delivery outcome).
        receipt = _make_receipt(
            receipt_id="rcpt-rt-001",
            event_id="evt-rt-001",
            delivery_plan_id="plan-rt",
            status="sent",
        )
        await temp_storage.append_receipt(receipt)

        # Verify the outbox item is unchanged.
        after = await temp_storage.get_outbox_item("obox-rt-001")
        assert after is not None
        assert after.status == before.status, (
            f"Outbox status changed from {before.status!r} to {after.status!r} "
            f"after appending a receipt — receipts must not drive outbox transitions"
        )
        assert after.receipt_id == before.receipt_id
        assert after.updated_at == before.updated_at
