"""Erased-context reconciliation weighs exact-attempt native evidence first.

When a ceiling sweep has retired a route's dispatch context and content
authority is denied, a reserved outbox attempt with a stored ``failed``
receipt must not be dead-lettered from the receipt alone. Exact-attempt
native evidence is weighed through the handoff resolver before any receipt
finalization: positive evidence settles the obligation ``sent`` alongside
the immutable failed receipt, an unreadable read or a contradicting receipt
leaves the claim untouched, and a not-delivered verdict arms the terminal
write with the evidence revision the resolver read.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from medre.core.events import DeliveryReceipt
from medre.core.evidence.handoff_resolution import UncertainHandoffResolution
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
    OutboxRouteRetryBoundsWrite,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.storage_outbox import admit_event

_WORKER = "retry-worker-ordering"

# The ceiling sweep's documented purge outcome for one event's outbox
# generations: inline dispatch metadata and payload hash are cleared and the
# frozen snapshot rows are deleted. The content-free bounds facts survive.
_PURGE_SNAPSHOT_SQL = "DELETE FROM delivery_outbox_snapshots WHERE outbox_id = ?"

_BOUNDS_AT_LIMIT = OutboxRouteRetryBoundsWrite(
    max_attempts=1,
    backoff_base=2.0,
    max_delay_seconds=60.0,
    jitter=False,
)


async def _seed_erased_reserved_row(
    temp_storage,
    outbox_id: str,
    *,
    bounds: OutboxRouteRetryBoundsWrite | None,
) -> DeliveryOutboxItem:
    """Admit a row, reserve an attempt, then simulate the ceiling purge.

    The reservation has no evidence: reconciliation must decide its outcome
    from persisted evidence and the handoff resolver alone.
    """
    event_id = f"evt-{outbox_id}"
    await admit_event(temp_storage, event_id)
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event_id,
            route_id=f"route-{outbox_id}",
            delivery_plan_id=f"plan-{outbox_id}",
            target_adapter="fake",
            target_channel="ch-0",
            attempt_number=0,
            status="in_progress",
            locked_at=datetime.now(timezone.utc).isoformat(),
            lease_until=(
                datetime.now(timezone.utc) + timedelta(seconds=60)
            ).isoformat(),
            worker_id=_WORKER,
        ),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(kind="direct", context={}),
        route_retry_bounds=bounds,
    )
    reserved = await temp_storage.reserve_outbox_attempt(outbox_id, _WORKER, 0)
    assert reserved == 1
    await temp_storage._write(_PURGE_SNAPSHOT_SQL, (outbox_id,))
    item = await temp_storage.get_outbox_item(outbox_id)
    assert item is not None
    assert item.active_attempt == 1
    assert not item.metadata
    assert item.payload_hash is None
    assert (
        await temp_storage.get_outbox_dispatch_context(
            outbox_id,
            event_id=item.event_id,
            delivery_plan_id=item.delivery_plan_id,
            target_adapter=item.target_adapter,
            target_channel=item.target_channel,
        )
        is None
    )
    return item


def _erased_authority(storage) -> None:
    """Deny the purged route's content authority at the read seam."""
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="denied", event=None)
    )


def _delivered_resolution(*, observation_count: int = 0) -> UncertainHandoffResolution:
    """One bounded resolution: retained evidence proves the native handoff."""
    return UncertainHandoffResolution(
        verdict="delivered_with_evidence",
        attempt_number=1,
        uncertain=False,
        reasons=("provider_acceptance_retained",),
        has_checkpoint=False,
        accepted_parts=1,
        uncertain_parts=0,
        unsent_parts=0,
        observation_count=observation_count,
        checkpoint_revision=None,
        observation_scope_revision=None,
    )


def _not_delivered_resolution() -> UncertainHandoffResolution:
    """One bounded resolution: the evidence shows no native submission."""
    return UncertainHandoffResolution(
        verdict="definitively_not_submitted",
        attempt_number=1,
        uncertain=False,
        reasons=("grant_fence_never_committed",),
        has_checkpoint=False,
        observation_count=0,
        checkpoint_revision=None,
        observation_scope_revision=None,
    )


def _failed_receipt(item) -> DeliveryReceipt:
    """Seed the causative failed attempt receipt for a reserved erased row."""
    return DeliveryReceipt(
        receipt_id=f"rcpt-{item.outbox_id}",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        outbox_id=item.outbox_id,
        attempt_number=1,
        status="failed",
        failure_kind="adapter_transient",
        source="retry",
    )


def _resolver_worker(storage, *, max_attempts: int, resolver) -> RetryWorker:
    worker = RetryWorker(
        storage=storage,
        pipeline=MagicMock(),
        capacity_controller=None,
        enabled=True,
        max_attempts=max_attempts,
    )
    worker._handoff_resolver = resolver
    worker._emit = MagicMock()
    return worker


class _FixedResolutionResolver:
    """Return one prepared resolution and record the requested attempt."""

    def __init__(self, resolution: UncertainHandoffResolution) -> None:
        self.resolution = resolution
        self.calls: list[int] = []

    async def resolve(self, item, attempt_number: int) -> UncertainHandoffResolution:
        self.calls.append(attempt_number)
        return self.resolution


async def _assert_failed_receipt_unchanged(storage, item, *, total: int = 1) -> None:
    """The failed attempt receipt row stays immutable.

    *total* is 1 when nothing else may be appended (evidence settlement and
    refused finalizations fabricate no receipt) and 2 when the committed
    dead-letter also carries its immutable terminal lifecycle receipt.
    """
    receipts = await storage.list_receipts_for_outbox(item.outbox_id)
    assert len(receipts) == total
    failed = [receipt for receipt in receipts if receipt.status == "failed"]
    assert len(failed) == 1
    receipt = failed[0]
    assert receipt.receipt_id == f"rcpt-{item.outbox_id}"
    assert receipt.status == "failed"
    assert receipt.failure_kind == "adapter_transient"
    assert receipt.attempt_number == 1
    assert receipt.outbox_id == item.outbox_id


async def test_delivered_evidence_settles_sent_alongside_failed_receipt_bounds(
    temp_storage,
) -> None:
    """Positive exact-attempt evidence settles sent within the route's budget."""
    item = await _seed_erased_reserved_row(
        temp_storage, "obox-order-t1-bounds", bounds=_BOUNDS_AT_LIMIT
    )
    await temp_storage.append_receipt(_failed_receipt(item))
    _erased_authority(temp_storage)
    worker = _resolver_worker(
        temp_storage,
        max_attempts=1,
        resolver=_FixedResolutionResolver(_delivered_resolution()),
    )
    pipeline_calls = len(worker._pipeline.mock_calls)

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "sent"
    assert row.active_attempt is None
    assert row.next_attempt_at is None
    await _assert_failed_receipt_unchanged(temp_storage, item)
    assert len(worker._pipeline.mock_calls) == pipeline_calls


async def test_delivered_evidence_settles_sent_alongside_failed_receipt_no_bounds(
    temp_storage,
) -> None:
    """Positive exact-attempt evidence settles sent without persisted bounds."""
    item = await _seed_erased_reserved_row(
        temp_storage, "obox-order-t1-free", bounds=None
    )
    await temp_storage.append_receipt(_failed_receipt(item))
    _erased_authority(temp_storage)
    worker = _resolver_worker(
        temp_storage,
        max_attempts=10,
        resolver=_FixedResolutionResolver(_delivered_resolution()),
    )
    pipeline_calls = len(worker._pipeline.mock_calls)

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "sent"
    assert row.active_attempt is None
    await _assert_failed_receipt_unchanged(temp_storage, item)
    assert len(worker._pipeline.mock_calls) == pipeline_calls


async def test_not_delivered_evidence_dead_letters_at_the_budget(temp_storage) -> None:
    """Without positive evidence the failed receipt dead-letters the row."""
    item = await _seed_erased_reserved_row(
        temp_storage, "obox-order-t2-bounds", bounds=_BOUNDS_AT_LIMIT
    )
    await temp_storage.append_receipt(_failed_receipt(item))
    _erased_authority(temp_storage)
    worker = _resolver_worker(
        temp_storage,
        max_attempts=10,
        resolver=_FixedResolutionResolver(_not_delivered_resolution()),
    )

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "dead_lettered"
    assert row.failure_kind == "retry_exhausted"
    assert row.next_attempt_at is None
    await _assert_failed_receipt_unchanged(temp_storage, item, total=2)


async def test_not_delivered_evidence_dead_letters_without_bounds(temp_storage) -> None:
    """No bounds: the reconcile-only budget dead-letters the row terminally."""
    item = await _seed_erased_reserved_row(
        temp_storage, "obox-order-t2-free", bounds=None
    )
    await temp_storage.append_receipt(_failed_receipt(item))
    _erased_authority(temp_storage)
    worker = _resolver_worker(
        temp_storage,
        max_attempts=10,
        resolver=_FixedResolutionResolver(_not_delivered_resolution()),
    )

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "dead_lettered"
    assert row.failure_kind == "retry_exhausted"
    await _assert_failed_receipt_unchanged(temp_storage, item, total=2)


async def test_conflicting_receipt_provenance_leaves_the_claim(
    temp_storage,
) -> None:
    """Delivered evidence over a contradicting receipt never settles."""
    item = await _seed_erased_reserved_row(
        temp_storage, "obox-order-t4", bounds=_BOUNDS_AT_LIMIT
    )
    receipt = _failed_receipt(item)
    conflicting = DeliveryReceipt(
        receipt_id=receipt.receipt_id,
        event_id=receipt.event_id,
        delivery_plan_id=receipt.delivery_plan_id,
        target_adapter=receipt.target_adapter,
        target_channel=receipt.target_channel,
        outbox_id=receipt.outbox_id,
        attempt_number=1,
        status="failed",
        failure_kind="adapter_transient",
        source="replay",
    )
    await temp_storage.append_receipt(conflicting)
    _erased_authority(temp_storage)
    resolver = _FixedResolutionResolver(_delivered_resolution())
    worker = _resolver_worker(temp_storage, max_attempts=1, resolver=resolver)

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "in_progress"
    assert row.active_attempt == 1
    assert row.next_attempt_at is None
    assert resolver.calls == [1]
    await _assert_failed_receipt_unchanged(temp_storage, item)


async def test_unreadable_evidence_leaves_the_claim(temp_storage) -> None:
    """An unreadable evidence revision never arms or settles a claim."""
    item = await _seed_erased_reserved_row(
        temp_storage, "obox-order-b4", bounds=_BOUNDS_AT_LIMIT
    )
    await temp_storage.append_receipt(_failed_receipt(item))
    _erased_authority(temp_storage)
    resolution = UncertainHandoffResolution(
        verdict="unresolved",
        attempt_number=1,
        uncertain=True,
        reasons=("resolution_evidence_unavailable",),
        has_checkpoint=False,
        observation_count=None,
        checkpoint_revision=None,
        observation_scope_revision=None,
    )
    resolver = _FixedResolutionResolver(resolution)
    worker = _resolver_worker(temp_storage, max_attempts=1, resolver=resolver)
    pipeline_calls = len(worker._pipeline.mock_calls)

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "in_progress"
    assert row.active_attempt == 1
    assert row.next_attempt_at is None
    assert resolver.calls == [1]
    await _assert_failed_receipt_unchanged(temp_storage, item)
    assert len(worker._pipeline.mock_calls) == pipeline_calls


async def test_concurrent_observation_refuses_the_terminal_write(
    tmp_path,
) -> None:
    """A terminal write armed by the resolver's read refuses newer evidence.

    A second SQLite connection commits a positive observation between the
    resolver's read and the terminal write. The armed finalization refuses,
    the claim survives for a fresh pass, and that pass settles ``sent``
    against the newer evidence.
    """
    path = str(tmp_path / "erased-ordering-fence.sqlite")
    storage = SQLiteStorage(db_path=path)
    await storage.initialize()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        item = await _seed_erased_reserved_row(
            storage, "obox-order-t3", bounds=_BOUNDS_AT_LIMIT
        )
        await storage.append_receipt(_failed_receipt(item))
        _erased_authority(storage)
        read_done = threading.Event()
        commit_done = threading.Event()
        first_resolution = _not_delivered_resolution()
        second_resolution = _delivered_resolution(observation_count=1)

        class _GatedResolver:
            """Park between the resolver's read and the terminal write."""

            def __init__(self) -> None:
                self.calls: list[int] = []

            async def resolve(self, item, attempt_number: int):
                self.calls.append(attempt_number)
                if len(self.calls) == 1:
                    read_done.set()
                    await asyncio.to_thread(commit_done.wait, 30)
                    return first_resolution
                return second_resolution

        resolver = _GatedResolver()
        worker = _resolver_worker(storage, max_attempts=1, resolver=resolver)
        pipeline_calls = len(worker._pipeline.mock_calls)

        first_pass = asyncio.create_task(worker._retry_outbox_item(item))
        assert await asyncio.to_thread(read_done.wait, 30)
        conn.execute(
            "INSERT INTO delivery_observations "
            "(observation_id, event_id, delivery_plan_id, target_adapter, "
            "target_channel, outbox_id, attempt_number, state, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'delivered', ?)",
            (
                "obs-order-t3",
                item.event_id,
                item.delivery_plan_id,
                item.target_adapter,
                item.target_channel,
                item.outbox_id,
                1,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
        commit_done.set()
        await first_pass

        row = await storage.get_outbox_item(item.outbox_id)
        assert row is not None
        assert row.status == "in_progress", (
            "a terminal write armed by the resolver's read must refuse when "
            "exact-attempt evidence changed before the write"
        )
        assert row.active_attempt == 1

        await worker._retry_outbox_item(item)

        row = await storage.get_outbox_item(item.outbox_id)
        assert row is not None
        assert row.status == "sent"
        assert row.active_attempt is None
        assert resolver.calls == [1, 1]
        assert len(worker._pipeline.mock_calls) == pipeline_calls
        receipts = await storage.list_receipts_for_outbox(item.outbox_id)
        assert len(receipts) == 1
        assert receipts[0].status == "failed"
    finally:
        conn.close()
        await storage.close()
