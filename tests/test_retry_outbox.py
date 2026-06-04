"""Retry/recovery outbox integration tests: due outbox retry, retry exhaustion,
restart visibility, and Meshtastic ambiguous items.
"""

from __future__ import annotations

import uuid

from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage


def _make_outbox_item(
    delivery_plan_id: str = "plan-retry-1",
    target_adapter: str = "fake_presentation",
    target_channel: str | None = "ch-0",
    status: str = "pending",
    next_attempt_at: str | None = None,
    attempt_number: int = 1,
) -> DeliveryOutboxItem:
    """Build a minimal outbox item for retry tests.  Default status is
    ``pending``; other statuses (retry_wait, sent, dead_lettered, etc.)
    must be reached through ``mark_outbox_*`` transition methods, not
    by passing the status here."""
    return DeliveryOutboxItem(
        outbox_id=f"obox-{uuid.uuid4()}",
        event_id="evt-retry-1",
        route_id="route-1",
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        attempt_number=attempt_number,
        status=status,
        next_attempt_at=next_attempt_at,
    )


class TestDueOutboxRetry:
    """Due outbox items with status retry_wait should be claimable."""

    async def test_due_retry_wait_item_is_claimable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A retry_wait item past its next_attempt_at should be claimable."""
        item = _make_outbox_item(
            delivery_plan_id="plan-due-1",
        )
        created = await temp_storage.create_outbox_item(item)
        # Reach retry_wait via pending → claim → mark_retry_wait.
        await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00", worker_id="w1", lease_seconds=30, limit=10
        )
        await temp_storage.mark_outbox_retry_wait(
            created.outbox_id,
            next_attempt_at="2025-01-01T00:00:00",
            failure_kind="adapter_transient",
            error_summary="Connection timeout",
        )

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 1
        assert claimed[0].outbox_id == created.outbox_id
        assert claimed[0].status == "in_progress"

    async def test_not_due_retry_wait_not_claimable(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A retry_wait item NOT past its next_attempt_at should NOT be claimable."""
        future = "2099-01-01T00:00:00"
        item = _make_outbox_item(
            delivery_plan_id="plan-not-due-1",
        )
        created = await temp_storage.create_outbox_item(item)
        # Reach retry_wait via pending → claim → mark_retry_wait.
        await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00", worker_id="w1", lease_seconds=30, limit=10
        )
        await temp_storage.mark_outbox_retry_wait(
            created.outbox_id,
            next_attempt_at=future,
            failure_kind="adapter_transient",
            error_summary="Not yet due",
        )

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 0

    async def test_mark_retry_wait_with_next_attempt(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Claim an item, then mark it retry_wait with a next_attempt_at."""
        item = _make_outbox_item(
            delivery_plan_id="plan-mark-retry-1",
            status="pending",
        )
        await temp_storage.create_outbox_item(item)

        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        next_at = "2026-01-01T01:00:00"
        await temp_storage.mark_outbox_retry_wait(
            oid,
            next_attempt_at=next_at,
            failure_kind="adapter_transient",
            error_summary="Connection timeout",
        )

        item_after = await temp_storage.get_outbox_item(oid)
        assert item_after is not None
        assert item_after.status == "retry_wait"
        assert item_after.next_attempt_at == next_at
        assert item_after.failure_kind == "adapter_transient"


class TestRetryExhaustion:
    """When retries are exhausted, the outbox should be dead_lettered."""

    async def test_dead_lettered_from_retry_wait(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A retry_wait item can be marked dead_lettered."""
        item = _make_outbox_item(
            delivery_plan_id="plan-exhaust-1",
            attempt_number=3,
        )
        created = await temp_storage.create_outbox_item(item)
        # Reach retry_wait via pending → claim → mark_retry_wait.
        await temp_storage.claim_due_outbox_items(
            now="2026-01-01T00:00:00", worker_id="w1", lease_seconds=30, limit=10
        )
        await temp_storage.mark_outbox_retry_wait(
            created.outbox_id,
            next_attempt_at="2025-01-01T00:00:00",
            failure_kind="adapter_transient",
            error_summary="Transient failure",
        )

        # Now claim again (item is due) so we can mark dead_lettered.
        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        await temp_storage.mark_outbox_dead_lettered(
            oid,
            failure_kind="adapter_permanent",
            error_summary="All 3 retry attempts exhausted",
        )

        dl = await temp_storage.get_outbox_item(oid)
        assert dl is not None
        assert dl.status == "dead_lettered"
        assert dl.locked_at is None  # terminal clears lease


class TestRestartVisibility:
    """After restart (close/reopen), outbox items remain visible."""

    async def test_pending_visible_after_restart(self) -> None:
        import os
        import tempfile

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        storage: SQLiteStorage | None = None
        storage2: SQLiteStorage | None = None
        try:
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()

            item = _make_outbox_item(
                delivery_plan_id="plan-restart-pending",
                status="pending",
            )
            await storage.create_outbox_item(item)

            # Re-open
            storage2 = SQLiteStorage(db_path=db_path)
            await storage2.initialize()

            items = await storage2.list_outbox_items(status_filter=["pending"])
            matching = [
                i for i in items if i.delivery_plan_id == "plan-restart-pending"
            ]
            assert len(matching) == 1
            assert matching[0].status == "pending"
        finally:
            if storage is not None:
                await storage.close()
            if storage2 is not None:
                await storage2.close()
            os.unlink(db_path)

    async def test_due_retry_visible_after_restart(self) -> None:
        import os
        import tempfile

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        storage: SQLiteStorage | None = None
        storage2: SQLiteStorage | None = None
        try:
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()

            item = _make_outbox_item(
                delivery_plan_id="plan-restart-due",
            )
            created = await storage.create_outbox_item(item)
            # Reach retry_wait via pending → claim → mark_retry_wait.
            await storage.claim_due_outbox_items(
                now="2026-01-01T00:00:00", worker_id="w1", lease_seconds=30, limit=10
            )
            await storage.mark_outbox_retry_wait(
                created.outbox_id,
                next_attempt_at="2025-01-01T00:00:00",
                failure_kind="adapter_transient",
                error_summary="Transient failure",
            )

            # Re-open
            storage2 = SQLiteStorage(db_path=db_path)
            await storage2.initialize()

            # Should still be visible and claimable.
            now = "2026-01-01T00:00:00"
            claimed = await storage2.claim_due_outbox_items(
                now=now, worker_id="worker-1", lease_seconds=30, limit=10
            )
            assert len(claimed) >= 1
            matching = [c for c in claimed if c.delivery_plan_id == "plan-restart-due"]
            assert len(matching) == 1
        finally:
            if storage is not None:
                await storage.close()
            if storage2 is not None:
                await storage2.close()
            os.unlink(db_path)

    async def test_dead_lettered_visible_after_restart(self) -> None:
        import os
        import tempfile

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        storage: SQLiteStorage | None = None
        storage2: SQLiteStorage | None = None
        try:
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()

            item = _make_outbox_item(
                delivery_plan_id="plan-restart-dl",
            )
            await storage.create_outbox_item(item)
            # Reach "dead_lettered" via pending → claim → mark_dead_lettered (Pattern B).
            await storage.claim_due_outbox_items(
                now="2026-01-01T00:00:00", worker_id="w1", lease_seconds=30, limit=10
            )
            await storage.mark_outbox_dead_lettered(item.outbox_id, failure_kind="test")

            # Re-open
            storage2 = SQLiteStorage(db_path=db_path)
            await storage2.initialize()

            items = await storage2.list_outbox_items(status_filter=["dead_lettered"])
            matching = [i for i in items if i.delivery_plan_id == "plan-restart-dl"]
            assert len(matching) == 1
            assert matching[0].status == "dead_lettered"
        finally:
            if storage is not None:
                await storage.close()
            if storage2 is not None:
                await storage2.close()
            os.unlink(db_path)

    async def test_ambiguous_meshtastic_after_restart(self) -> None:
        """A Meshtastic-queued item after restart should remain visible
        as 'queued' (the pipeline recorded queue acceptance).  Recovery
        must decide whether to re-send or abandon."""
        import os
        import tempfile

        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

        storage: SQLiteStorage | None = None
        storage2: SQLiteStorage | None = None
        try:
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()

            item = _make_outbox_item(
                delivery_plan_id="plan-ambiguous-msh",
                status="in_progress",
                target_adapter="meshtastic",
            )
            await storage.create_outbox_item(item)
            # Reach "queued" via in_progress → mark_queued (Pattern C).
            await storage.mark_outbox_queued(item.outbox_id)

            # Re-open: queued item is visible.
            storage2 = SQLiteStorage(db_path=db_path)
            await storage2.initialize()

            items = await storage2.list_outbox_items(status_filter=["queued"])
            matching = [i for i in items if i.delivery_plan_id == "plan-ambiguous-msh"]
            assert len(matching) == 1
            assert matching[0].status == "queued"
            # Not claimable (status != pending/retry_wait).
            now = "2026-01-01T00:00:00"
            claimed = await storage2.claim_due_outbox_items(
                now=now, worker_id="worker-1", lease_seconds=30, limit=10
            )
            assert not any(c.delivery_plan_id == "plan-ambiguous-msh" for c in claimed)
        finally:
            if storage is not None:
                await storage.close()
            if storage2 is not None:
                await storage2.close()
            os.unlink(db_path)


# ===================================================================
# Group 5: RetryWorker NameError regression test
# ===================================================================


class TestRetryWorkerNameErrorRegression:
    """Verify that RetryWorker._retry_outbox_item does not raise NameError
    when Route/RouteTarget reconstruction fails.

    The fix ensures that _max_attempts, _backoff_base, _max_delay, and
    _jitter are initialised BEFORE the reconstruction try block so that
    the exception handler can safely reference them.
    """

    async def test_retry_outbox_item_handles_reconstruction_failure_gracefully(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When Route/RouteTarget reconstruction raises, RetryWorker should
        mark the item as retry_wait (or dead_lettered) without NameError,
        and deliver_to_target must NOT be awaited."""
        # Create and persist a canonical event for the outbox item to reference
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock, MagicMock, patch

        from medre.core.engine.pipeline import PipelineRunner
        from medre.core.events import CanonicalEvent, EventMetadata

        event = CanonicalEvent(
            event_id="evt-retry-regress-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "regression test"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        # Create an outbox item referencing that event
        item = _make_outbox_item(
            delivery_plan_id="plan-regress-1",
            target_adapter="fake_presentation",
            target_channel="ch-0",
            status="pending",
        )
        item.event_id = "evt-retry-regress-1"
        await temp_storage.create_outbox_item(item)

        # Claim the item so it becomes in_progress
        now = "2026-01-01T00:00:00"
        claimed = await temp_storage.claim_due_outbox_items(
            now=now, worker_id="worker-1", lease_seconds=30, limit=10
        )
        assert len(claimed) == 1
        oid = claimed[0].outbox_id

        # Construct a minimal RetryWorker with a mock pipeline.
        # deliver_to_target is tracked — it must NOT be called when
        # reconstruction fails.
        mock_pipeline = MagicMock(spec=PipelineRunner)
        mock_pipeline.deliver_to_target = AsyncMock()

        from medre.runtime.retry import RetryWorker

        worker = RetryWorker(
            storage=temp_storage,
            pipeline=mock_pipeline,
            capacity_controller=None,
            enabled=True,
            max_attempts=3,
        )

        # Monkeypatch RouteTarget to raise during construction so the failure
        # occurs in the reconstruction block, NOT in deliver_to_target.
        with patch(
            "medre.runtime.retry.RouteTarget",
            side_effect=ValueError("Simulated reconstruction failure"),
        ):
            # _retry_outbox_item should NOT raise NameError
            try:
                await worker._retry_outbox_item(claimed[0])
            except NameError as err:
                raise AssertionError(
                    "RetryWorker._retry_outbox_item raised NameError — "
                    "the _max_attempts fix is not in place"
                ) from err

        # deliver_to_target must NOT have been awaited — reconstruction
        # failed before reaching the delivery call.
        mock_pipeline.deliver_to_target.assert_not_awaited()

        # The outbox item should have transitioned away from in_progress
        # (either retry_wait or dead_lettered)
        item_after = await temp_storage.get_outbox_item(oid)
        assert item_after is not None
        assert item_after.status in ("retry_wait", "dead_lettered", "abandoned")
