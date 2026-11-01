# medre-test-tier: compatibility
"""Transaction and race qualification for the retention vertical.

PR E's race family: every scenario drives the REAL ``ContentCollectionWorker``
over REAL ``SQLiteStorage`` (or the full runtime) with deterministic
interleaving hooks — gates, injected busy windows, one-shot failure seams —
never sleep-and-hope. Each test names the plan invariant it proves:

- INV 1 (durable owners): a pending sibling, a queued native handoff, an
  uncertain in-flight send, unretired snapshots, shared attachments, radio
  plans, and reply excerpts owned by other events each block collection, and
  the worker never deletes their rows;
- INV 2 (guarded retirement only): a guarded terminal transition retires the
  frozen snapshot in the same transaction and only then does collection
  proceed; the retired context is unrecoverable;
- INV 4 (write-reservation revalidation): a hold revoked between the
  candidate page and the collector's decision is honored inside the
  collector's own ``BEGIN IMMEDIATE`` transaction;
- INV 5 (uncertain stays uncertain): collection never transitions delivery
  or receipt identity into a retryable state; a collected event's native
  submission and replay stay denied, native mappings survive, and receipts
  stay byte-identical;
- INV 6 (grants): observed revocation unblocks exactly at the guarded
  decision;
- INV 10 (no busy loop): a held SQLite write lock surfaces the typed
  transient error, defers the page with the cursor intact, backs off, and
  changes no data;
- durability: a collection committed before its sweep bookkeeping is
  idempotent across restart (no resurrection, no double receipt);
- cancellation mid-scan stops cleanly with no partial state;
- a failed candidate read defers without advancing past unprocessed work;
- replay scope never constructs the worker, and a CLI activation against a
  live bridge is adopted by the running worker without invalidating
  in-flight delivery.
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from contextlib import redirect_stdout
from dataclasses import replace as _dc_replace
from datetime import datetime, timedelta, timezone
from typing import Any

import msgspec
import pytest

from medre.cli.storage_commands import _storage_retention_activate
from medre.config.model import (
    AdapterConfigSet,
    ContentRetentionConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.events import EventRelation
from medre.core.events.canonical import NativeMessageRef
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.storage.backend import (
    ContentCollectionTransientError,
    DeferredHandoffFinalization,
    StorageError,
)
from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold
from medre.core.storage.content_retention import ContentRetentionPolicy
from medre.core.storage.read_contracts import AuthorizedEventRead
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.app import RuntimeState, StartupScope
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.collection import ContentCollectionWorker
from tests.helpers.async_utils import wait_until
from tests.helpers.content_lifecycle import (
    T0,
)
from tests.helpers.content_lifecycle import CollectRecording as Recording
from tests.helpers.content_lifecycle import (
    FakeClock,
    admit_and_stage,
    body_present,
    dispatch_pending_through_real_worker,
    make_worker,
    recording_storage,
    seed_event,
    settle_append_owner,
    settle_first_target,
)
from tests.helpers.fake_runtime import (
    build_and_start,
    clean_stop,
    make_multi_adapter_config,
)
from tests.helpers.initial_outbox import _pipeline
from tests.helpers.pipeline import make_event
from tests.helpers.storage import make_storage_event

_QUOTE = "RACE-QUOTE-SECRET"

_RELAY_ONLY = ContentRetentionConfig(
    enabled=True, mode="relay-only", interval_seconds=1, batch_limit=10
)


def _app_config(db_path: str, *, shutdown_timeout: int = 1) -> Any:
    base = make_multi_adapter_config()
    return _dc_replace(
        base,
        content_retention=_RELAY_ONLY,
        storage=StorageConfig(backend="sqlite", path=db_path),
        runtime=RuntimeOptions(
            name="lifecycle-races", shutdown_timeout_seconds=shutdown_timeout
        ),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pending sibling fanout (INV 1)
# ---------------------------------------------------------------------------


async def test_pending_sibling_blocks_then_settling_collects(tmp_path) -> None:
    """One sent target, one pending sibling: the worker pages the event and
    refuses collection; once the sibling settles through the real dispatcher
    the next cycle collects (plan invariant 1)."""
    store = SQLiteStorage(str(tmp_path / "sibling.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=T0)
        rows, runner = await admit_and_stage(store, "fanout")
        await settle_first_target(store, rows)

        record = Recording()
        worker = make_worker(recording_storage(store, record), FakeClock())
        await worker.sweep()

        assert worker.state.blocked == 1
        assert worker.state.collected == 0
        assert record.collects == ["fanout"]
        assert await body_present(store, "fanout")
        blocked = await store.collect_event_content("fanout", dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == [
            "active_outbox",
            "unsettled_mandatory_owners",
            "unretired_outbox_snapshots",
        ]
        holder = await store.get_outbox_item(rows["matrix"].outbox_id)
        assert holder is not None and holder.status == "pending"

        await dispatch_pending_through_real_worker(store, runner)
        settled = await store.list_outbox_items_for_event("fanout")
        assert all(row.status == "sent" for row in settled)

        await worker.sweep()
        assert worker.state.collected == 1
        assert not await body_present(store, "fanout")
        assert await store.count_collected_events() == 1
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Queued native handoff (INV 1 + 2)
# ---------------------------------------------------------------------------


async def test_queued_native_handoff_blocks_until_finalized(tmp_path) -> None:
    """A queued adapter-local handoff is live work: not collected, snapshot
    intact; the receipt-backed finalizer settles it and the next cycle
    collects (plan invariants 1 and 2)."""
    store = SQLiteStorage(str(tmp_path / "queued.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=T0)
        rows, _ = await admit_and_stage(store, "handoff")
        target = rows["matrix"]
        await settle_first_target(store, rows)
        claimed = await store.claim_due_outbox_items(
            _now_iso(), "worker", excluded_adapters=("radio",)
        )
        assert [item.outbox_id for item in claimed] == [target.outbox_id]
        # The production queue-send shape: the claimed worker reserves the
        # next attempt, then the adapter-local queue acceptance is recorded.
        assert await store.reserve_outbox_attempt(target.outbox_id, "worker", 0) == 1
        assert await store.mark_outbox_queued(target.outbox_id)

        record = Recording()
        worker = make_worker(recording_storage(store, record), FakeClock())
        await worker.sweep()
        assert worker.state.blocked == 1
        assert await body_present(store, "handoff")
        snapshot = await store.get_outbox_dispatch_context(
            target.outbox_id,
            event_id=target.event_id,
            delivery_plan_id=target.delivery_plan_id,
            target_adapter=target.target_adapter,
            target_channel=target.target_channel,
        )
        assert snapshot is not None, "a queued handoff keeps its frozen context"

        item = await store.get_outbox_item(target.outbox_id)
        assert item is not None
        receipt = build_delivery_receipt(
            event_id=item.event_id,
            delivery_plan_id=item.delivery_plan_id,
            target_adapter=item.target_adapter,
            target_channel=item.target_channel,
            route_id=item.route_id,
            status="sent",
            receipt_kind="attempt",
            adapter_message_id="native-queued-1",
            outbox_id=item.outbox_id,
            attempt_number=1,
        )
        native_ref = NativeMessageRef(
            id="nref-queued-1",
            event_id=item.event_id,
            adapter=item.target_adapter,
            native_channel_id=item.target_channel,
            native_message_id="native-queued-1",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        assert await store.finalize_deferred_handoff(
            DeferredHandoffFinalization(receipt=receipt, native_ref=native_ref)
        )
        finalized = await store.get_outbox_item(target.outbox_id)
        assert finalized is not None and finalized.status == "sent"
        retired = await store.get_outbox_dispatch_context(
            target.outbox_id,
            event_id=target.event_id,
            delivery_plan_id=target.delivery_plan_id,
            target_adapter=target.target_adapter,
            target_channel=target.target_channel,
        )
        assert retired is None, "guarded settlement retired the snapshot"

        await worker.sweep()
        assert worker.state.collected == 1
        assert not await body_present(store, "handoff")
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Uncertain accepted send and terminal snapshot retirement (INV 2 + 5)
# ---------------------------------------------------------------------------


async def test_uncertain_in_flight_send_fences_collection(tmp_path) -> None:
    """A claimed (in-flight, uncertain) native send fences collection: the
    owner state stays untouched, and only the guarded terminal transition
    releases the event (plan invariants 1 and 5)."""
    store = SQLiteStorage(str(tmp_path / "uncertain.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=T0)
        rows, _ = await admit_and_stage(store, "inflight")
        target = rows["matrix"]
        await settle_first_target(store, rows)
        claimed = await store.claim_due_outbox_items(
            _now_iso(), "worker", excluded_adapters=("radio",)
        )
        assert [item.outbox_id for item in claimed] == [target.outbox_id]

        worker = make_worker(store, FakeClock())
        await worker.sweep()
        assert worker.state.blocked == 1
        assert worker.state.collected == 0
        assert await body_present(store, "inflight")
        held = await store.get_outbox_item(target.outbox_id)
        assert (
            held is not None and held.status == "in_progress"
        ), "the worker never touches the uncertain attempt"

        assert await store.mark_outbox_sent(target.outbox_id)
        await worker.sweep()
        assert worker.state.collected == 1
        assert not await body_present(store, "inflight")
    finally:
        await store.close()


async def test_guarded_terminal_transition_retires_snapshot_then_collects(
    tmp_path,
) -> None:
    """The unretired snapshot blocks; the guarded terminal transition retires
    it in the same transaction; collection proceeds and the retired context
    is unrecoverable (plan invariant 2)."""
    store = SQLiteStorage(str(tmp_path / "retire.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=T0)
        rows, _ = await admit_and_stage(store, "terminal")
        target = rows["matrix"]
        await settle_first_target(store, rows)
        await store.claim_due_outbox_items(
            _now_iso(), "worker", excluded_adapters=("radio",)
        )
        worker = make_worker(store, FakeClock())
        await worker.sweep()
        assert worker.state.blocked == 1
        assert await body_present(store, "terminal")

        assert await store.mark_outbox_cancelled(target.outbox_id)
        retired = await store.get_outbox_dispatch_context(
            target.outbox_id,
            event_id=target.event_id,
            delivery_plan_id=target.delivery_plan_id,
            target_adapter=target.target_adapter,
            target_channel=target.target_channel,
        )
        assert retired is None

        await worker.sweep()
        assert worker.state.collected == 1
        assert not await body_present(store, "terminal")
        still_retired = await store.get_outbox_dispatch_context(
            target.outbox_id,
            event_id=target.event_id,
            delivery_plan_id=target.delivery_plan_id,
            target_adapter=target.target_adapter,
            target_channel=target.target_channel,
        )
        assert still_retired is None, "collection never resurrects context"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Optional grants: revocation observed between page and collect (INV 4 + 6)
# ---------------------------------------------------------------------------


def _hold_policy(clock: FakeClock, consumer: str) -> AvailabilityPolicy:
    return AvailabilityPolicy(
        hard_ceiling=clock.value + timedelta(days=365),
        max_optional_hold=timedelta(days=30),
        allowed_optional_consumers=frozenset({consumer}),
    )


async def _issue_hold(
    store: SQLiteStorage, event_id: str, clock: FakeClock, consumer: str
) -> None:
    hold = ContentHold(
        consumer=consumer,
        scope="body",
        expires_at=clock.value + timedelta(days=29),
        issued_at=clock.value,
    )
    await store.issue_optional_body_hold(
        event_id, hold, _hold_policy(clock, consumer), now=clock.value
    )


async def test_grant_revocation_observed_between_page_and_collect(tmp_path) -> None:
    """INV 4: the collector's own ``BEGIN IMMEDIATE`` revalidation is the
    only authority — a hold revoked after the candidate page (but before the
    decision) is honored inside the collection transaction, and a hold
    revoked before paging never even produces a blocked cycle."""
    store = SQLiteStorage(str(tmp_path / "grant-race.db"))
    clock = FakeClock()
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=clock.value)
        await seed_event(store, "pre-page", "2026-11-02T10:00:00+00:00", settled=False)
        await seed_event(store, "mid-cycle", "2026-11-02T11:00:00+00:00", settled=False)
        await seed_event(
            store, "still-held", "2026-11-02T12:00:00+00:00", settled=False
        )
        for event_id in ("pre-page", "mid-cycle", "still-held"):
            await _issue_hold(store, event_id, clock, "held-consumer")
            await settle_append_owner(store, event_id)

        # pre-page: revoked before the sweep ever pages.
        assert await store.revoke_optional_body_hold(
            "pre-page", "held-consumer", now=clock.value
        )

        original_collect = store.collect_event_content

        async def revoke_mid_cycle(
            event_id: str,
            *,
            dry_run: bool = True,
            retention_epoch: int | None = None,
        ) -> dict[str, Any]:
            if event_id == "mid-cycle":
                # Deterministic interleaving: the page already happened; the
                # revocation commits before the collector's decision runs.
                assert await store.revoke_optional_body_hold(
                    "mid-cycle", "held-consumer", now=clock.value
                )
            return await original_collect(
                event_id, dry_run=dry_run, retention_epoch=retention_epoch
            )

        store.collect_event_content = revoke_mid_cycle  # type: ignore[method-assign]
        worker = make_worker(store, clock)
        await worker.sweep()

        assert (
            worker.state.collected == 2
        ), "both revoked holds released inside the guarded decision"
        assert worker.state.blocked == 1, "the still-live hold blocks"
        assert not await body_present(store, "pre-page")
        assert not await body_present(store, "mid-cycle")
        assert await body_present(store, "still-held")
        for event_id in ("pre-page", "mid-cycle"):
            tombstone = await store._read_one(
                "SELECT revoked_at FROM event_content_access_grants "
                "WHERE event_id=? AND consumer='held-consumer'",
                (event_id,),
            )
            assert tombstone is not None and tombstone["revoked_at"] is not None
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Reply excerpts owned by other events; opaque metadata (INV 1 + 3)
# ---------------------------------------------------------------------------


async def test_reply_excerpt_on_another_event_gates_then_releases(tmp_path) -> None:
    """An incoming quote owned by another event blocks the referenced event;
    the quoting event's own excerpt is a collectible core surface, and once
    it is released the referenced event unblocks (plan invariant 1)."""
    store = SQLiteStorage(str(tmp_path / "incoming-quote.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=T0)
        await seed_event(store, "quoted", "2026-10-30T10:00:00.000000+00:00")
        runner, _, _ = _pipeline(store)
        reply = make_event(event_id="reply", source_adapter="source")
        relation = EventRelation("reply", "quoted", None, None, _QUOTE)
        reply = msgspec.structs.replace(reply, relations=(relation,))
        await runner.admit_ingress(reply, "live")
        # Settle the reply's own ingress owner through real settlement.
        claimed = await store.claim_ingress_work(worker_id="worker", limit=1)
        assert [item.event_id for item in claimed] == ["reply"]
        assert await store.complete_ingress_work("reply", worker_id="worker")

        worker = make_worker(store, FakeClock())
        await worker.sweep()
        # The quoting event collected; the quoted event stayed blocked while
        # the excerpt it does not own was live.
        assert worker.state.collected == 1
        assert worker.state.blocked == 1
        assert not await body_present(store, "reply")
        assert await body_present(store, "quoted")

        await worker.sweep()
        assert (
            worker.state.collected == 2
        ), "the quoted event unblocks once the borrowed excerpt is released"
        assert not await body_present(store, "quoted")
        excerpt = await store._read_one(
            "SELECT COUNT(*) AS n FROM event_relation_excerpts"
        )
        assert excerpt is not None and excerpt["n"] == 0
    finally:
        await store.close()


async def test_opaque_envelope_and_native_metadata_block_the_worker(tmp_path) -> None:
    """Opaque free-form envelope bytes and content-bearing native-reference
    metadata each block through the real worker, which deletes nothing
    (plan invariant 3)."""
    store = SQLiteStorage(str(tmp_path / "opaque.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=T0)
        event = msgspec.structs.replace(
            make_storage_event(event_id="opaque-envelope"),
            payload={"body": "QUALIFICATION-BODY", "custom_field": "opaque-blob"},
            metadata={},
        )
        await store.append(event)
        await store._write(
            "UPDATE canonical_events SET created_at=? WHERE event_id=?",
            ("2026-11-02T10:00:00+00:00", "opaque-envelope"),
        )
        await settle_append_owner(store, "opaque-envelope")
        await seed_event(store, "opaque-native", "2026-11-02T11:00:00+00:00")
        await store.store_native_ref(
            NativeMessageRef(
                id="nref-opaque",
                event_id="opaque-native",
                adapter="fake_transport",
                native_channel_id="ch-0",
                native_message_id="native-opaque",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                metadata={"provider_field": "opaque-native-blob"},
            )
        )

        worker = make_worker(store, FakeClock())
        await worker.sweep()
        assert worker.state.collected == 0
        assert worker.state.blocked == 2
        assert await body_present(store, "opaque-envelope")
        assert await body_present(store, "opaque-native")
        envelope = await store.collect_event_content("opaque-envelope", dry_run=False)
        assert envelope["status"] == "blocked"
        assert "opaque_envelope" in envelope["blockers"]
        native = await store.collect_event_content("opaque-native", dry_run=False)
        assert native["status"] == "blocked"
        assert "native_references" in native["blockers"]
        assert "opaque-blob" not in json.dumps(envelope)
        assert "opaque-native-blob" not in json.dumps(native)
    finally:
        await store.close()


async def test_shared_attachments_and_radio_plans_block_wholesale(tmp_path) -> None:
    """Attachment copies and radio packet plans block wholesale and the
    worker leaves their rows byte-identical (plan invariants 1 and 9)."""
    store = SQLiteStorage(str(tmp_path / "wholesale.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=T0)
        rows, runner = await admit_and_stage(store, "shared")
        await settle_first_target(store, rows)
        await dispatch_pending_through_real_worker(store, runner)
        settled = await store.list_outbox_items_for_event("shared")
        assert all(row.status == "sent" for row in settled)

        # A shared attachment copy blocks through the association; the
        # second referring event is admitted through the real storage path.
        sibling = make_event(event_id="attachment-sibling", payload={})
        await store.append(sibling)
        await settle_append_owner(store, "attachment-sibling")
        shared_ref = "sha256:" + "c" * 64
        await store._write(
            "INSERT INTO attachment_blobs "
            "(content_ref,size_bytes,media_kind,mime_type,created_at,data) "
            "VALUES (?, 3, NULL, NULL, '2026-10-31T00:00:00+00:00', ?)",
            (shared_ref, b"abc"),
        )
        await store._write(
            "INSERT INTO event_attachment_associations "
            "(event_id,content_ref,created_at) VALUES "
            "(?, ?, '2026-10-31T00:00:00+00:00'), "
            "('attachment-sibling', ?, '2026-10-31T00:00:00+00:00')",
            ("shared", shared_ref, shared_ref),
        )
        worker = make_worker(store, FakeClock())
        await worker.sweep()
        # Both association-bearing events block (the sibling carries its own
        # association row); the worker deleted nothing.
        assert worker.state.blocked == 2
        assert await body_present(store, "shared")
        blobs = await store._read_one("SELECT COUNT(*) AS n FROM attachment_blobs")
        assert blobs is not None and blobs["n"] == 1
        associations = await store._read_one(
            "SELECT COUNT(*) AS n FROM event_attachment_associations"
        )
        assert associations is not None and associations["n"] == 2

        await store._write("DELETE FROM event_attachment_associations")
        await store._write("DELETE FROM attachment_blobs")
        # ...and an immutable radio packet plan blocks under its own policy.
        await store._write(
            "INSERT INTO radio_attempt_progress "
            "(outbox_id,attempt_number,plan_fingerprint,plan_json,provenance_json,"
            "parts_json,revision,created_at,updated_at) "
            "SELECT outbox_id, 1, ?, '{}', '{}', '{}', 0, "
            "'2026-10-31T00:00:00+00:00', '2026-10-31T00:00:00+00:00' "
            "FROM delivery_outbox WHERE outbox_id=?",
            ("a" * 64, settled[0].outbox_id),
        )
        await worker.sweep()
        assert (
            worker.state.blocked == 3
        ), "the plan blocks; the bodyless sibling is empty"
        assert await body_present(store, "shared")
        plans = await store._read_one(
            "SELECT COUNT(*) AS n FROM radio_attempt_progress"
        )
        assert plans is not None and plans["n"] == 1

        await store._write("DELETE FROM radio_attempt_progress")
        await worker.sweep()
        assert worker.state.collected >= 1
        assert not await body_present(store, "shared")
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# SQLite lock contention: typed transient, deferred, no busy loop (INV 10)
# ---------------------------------------------------------------------------


async def test_held_write_lock_defers_with_backoff_and_no_busy_loop(tmp_path) -> None:
    """INV 10: a second real connection holding the write lock during the
    collector's transaction surfaces the typed transient error, aborts the
    page with the cursor untouched, backs off instead of spinning, and
    changes no data; the released lock lets a later sweep collect."""
    path = str(tmp_path / "lock.db")
    store = SQLiteStorage(path)
    await store.initialize()
    # Test-support seam: bound the injected busy window on this test's own
    # storage handle (the production default of 5000 ms stays untouched).
    await store._run_in_thread(store._require_db().execute, "PRAGMA busy_timeout=200")
    holder = sqlite3.connect(path)
    record = Recording()
    clock = FakeClock()
    worker: ContentCollectionWorker | None = None
    try:
        await store.open_retention_epoch(activated_by="operator", now=clock.value)
        original_collect = store.collect_event_content

        async def busy_collect(
            event_id: str,
            *,
            dry_run: bool = True,
            retention_epoch: int | None = None,
        ) -> dict[str, Any]:
            # Deterministic busy window: the holder owns the write lock for
            # exactly the duration of this collector call.
            holder.execute("BEGIN IMMEDIATE")
            try:
                return await original_collect(
                    event_id, dry_run=dry_run, retention_epoch=retention_epoch
                )
            finally:
                holder.rollback()

        store.collect_event_content = busy_collect  # type: ignore[method-assign]
        worker = ContentCollectionWorker(
            recording_storage(store, record),
            store,
            ContentRetentionPolicy(
                mode="relay-only", interval_seconds=1, batch_limit=1
            ),
            now=clock,
        )
        await worker.sweep()
        # Candidates are admitted after this worker establishes its scope.
        await seed_event(store, "locked", "2026-11-02T10:00:00+00:00")
        await seed_event(store, "second", "2026-11-02T11:00:00+00:00")
        await worker.sweep()

        assert worker.state.degraded is True
        assert worker.state.blocked == 0 and worker.state.collected == 0
        assert worker._cursor is None, "the cursor must not advance past the failure"
        assert worker._next_sweep_not_before == clock.value + timedelta(seconds=2)
        assert worker._idle_timeout() > 1.0, "the backoff extends the idle wait"
        assert await body_present(store, "locked")
        entry = worker.state.recent_errors[-1]
        assert entry.kind == "transient" and entry.event_id == "locked"
        assert await store.count_collected_events() == 0

        # The typed transient path is the real storage's classification.
        holder.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(ContentCollectionTransientError):
                await original_collect("locked", dry_run=False)
        finally:
            holder.rollback()

        # Lock released: the next sweep collects from the same head.
        store.collect_event_content = original_collect  # type: ignore[method-assign]
        await worker.sweep()
        assert worker.state.collected == 1
        assert worker.state.degraded is False
        assert worker._next_sweep_not_before is None
        assert not await body_present(store, "locked")
    finally:
        holder.close()
        if worker is not None:
            await worker.stop()
        await store.close()


# ---------------------------------------------------------------------------
# Read failure mid-page: defer without advancing past unprocessed work
# ---------------------------------------------------------------------------


async def test_candidate_read_failure_preserves_the_cursor(tmp_path) -> None:
    """A failed candidate read defers the whole cycle: the cursor stays at
    the failed page's head, nothing is skipped, and the loop survives to
    re-page the same work (plan PR B: a failed read must defer, not spin)."""
    store = SQLiteStorage(str(tmp_path / "read-failure.db"))
    await store.initialize()
    record = Recording()
    clock = FakeClock()
    worker: ContentCollectionWorker | None = None
    try:
        original_page = store.page_collection_candidates
        fail_next_page = True
        page_entered = asyncio.Event()
        release_page = asyncio.Event()

        async def failing_page(
            *, after: tuple[str, str] | None, limit: int, watermark: str | None
        ):
            nonlocal fail_next_page
            # Deterministic interleaving: hold the worker's first page call
            # until the candidates are admitted, then fail that read with
            # work pending behind it.
            page_entered.set()
            await release_page.wait()
            if fail_next_page:
                fail_next_page = False
                # The same failure class the storage raises for a real
                # sqlite3.Error during candidate paging.
                raise StorageError("Collection candidate paging failed")
            return await original_page(after=after, limit=limit, watermark=watermark)

        store.page_collection_candidates = failing_page  # type: ignore[method-assign]
        worker = ContentCollectionWorker(
            recording_storage(store, record),
            store,
            ContentRetentionPolicy(mode="relay-only", interval_seconds=1),
            now=clock,
        )
        # The started worker's own epoch opens on the empty store first;
        # the candidates are admitted after that boundary so the
        # read-failure path — not the epoch fence — governs them.
        await worker.start()
        assert await wait_until(page_entered.is_set, timeout=5.0)
        await seed_event(store, "first", "2026-11-02T10:00:00+00:00")
        await seed_event(store, "second", "2026-11-02T11:00:00+00:00")
        release_page.set()

        # Drive the real loop: the failed cycle is recorded as a cycle
        # error with the degraded state, and the loop survives it.
        assert await wait_until(lambda: worker.state.degraded, timeout=5.0)
        assert record.collects == [], "no collection may run on a failed read"
        assert worker._cursor is None
        assert any(e.kind == "cycle" for e in worker.state.recent_errors)

        # The next cycle re-pages from the same head; nothing was skipped.
        store.page_collection_candidates = original_page  # type: ignore[method-assign]
        assert await wait_until(
            lambda: record.collects == ["first", "second"], timeout=10.0
        )
        assert worker.state.collected == 2
        assert await wait_until(lambda: not worker.state.degraded, timeout=5.0)
    finally:
        if worker is not None:
            await worker.stop()
        await store.close()


# ---------------------------------------------------------------------------
# Full-runtime races: commit-before-ack restart, mid-scan cancellation
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_paths(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


async def _reopened_body(db_path: str, event_id: str) -> bool:
    store = SQLiteStorage(db_path)
    await store.initialize()
    try:
        return await body_present(store, event_id)
    finally:
        await store.close()


async def test_restart_after_commit_before_bookkeeping_is_idempotent(
    tmp_paths: MedrePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durability: a collection transaction that committed before its sweep
    bookkeeping (a forced stop in between) is idempotent across restart —
    no resurrection, no double receipt (plan PR D restart semantics)."""
    db_path = str(tmp_paths.state_dir / "commit-before-ack.sqlite")
    app = await build_and_start(_app_config(db_path), tmp_paths)
    release = asyncio.Event()
    committed = asyncio.Event()
    try:
        assert app.storage is not None

        async def _epoch_open() -> bool:
            return await app.storage.latest_retention_epoch() is not None

        assert await wait_until(_epoch_open, timeout=5.0)
        original = app.storage.collect_event_content

        async def gated(
            event_id: str,
            *,
            dry_run: bool = True,
            retention_epoch: int | None = None,
        ) -> dict[str, Any]:
            result = await original(
                event_id, dry_run=dry_run, retention_epoch=retention_epoch
            )
            if result.get("status") == "collected":
                # The guarded transaction committed; the sweep's bookkeeping
                # has not run. Hold here until the runtime is stopped.
                committed.set()
                await release.wait()
            return result

        monkeypatch.setattr(app.storage, "collect_event_content", gated)
        await seed_event(app.storage, "acked", "2026-11-02T10:00:00+00:00")
        assert await wait_until(lambda: committed.is_set(), timeout=10.0)
        row = await app.storage._read_one(
            "SELECT rows_removed FROM event_content_collection_log "
            "WHERE event_id='acked'"
        )
        assert row is not None, "the collection transaction committed"
        assert not await body_present(app.storage, "acked")
        worker = app._collection_worker
        assert worker is not None
        assert worker.state.collected == 0, "bookkeeping never ran for the commit"
    finally:
        await app.stop()
        release.set()
    assert app.state is RuntimeState.STOPPED
    assert not await _reopened_body(db_path, "acked")

    # Restart: the new worker opens its own epoch whose watermark is above
    # the committed event, so the receipt is never re-paged. Idempotency
    # means no resurrection, no second receipt, and no failed sweeps — and
    # the restarted worker still collects a post-restart admission.
    app2 = await build_and_start(_app_config(db_path), tmp_paths)
    try:
        assert app2.storage is not None
        worker2 = app2._collection_worker
        assert worker2 is not None

        async def _restart_boundary() -> bool:
            assert app2.storage is not None
            latest = await app2.storage.latest_retention_epoch()
            return (
                latest is not None
                and latest.epoch >= 2
                and latest.admission_watermark == "2026-11-02T10:00:00+00:00"
            )

        assert await wait_until(_restart_boundary, timeout=10.0)
        await seed_event(app2.storage, "after-restart", "2026-11-03T10:00:00+00:00")

        async def _post_restart_collected() -> bool:
            assert app2.storage is not None
            return not await body_present(app2.storage, "after-restart")

        assert await wait_until(_post_restart_collected, timeout=10.0)
        assert await app2.storage.count_collected_events() == 2
        assert not await body_present(app2.storage, "acked"), "no resurrection"
        assert worker2.state.already_collected == 0, "the receipt was never re-paged"
        assert worker2.state.failed == 0
        assert worker2.state.collected == 1
    finally:
        await clean_stop(app2)


async def test_stop_mid_scan_with_storage_mutation_ends_cleanly(
    tmp_paths: MedrePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation mid-scan: app.stop() while the worker is mid-page (with
    a real collection already committed) reaches a clean STOPPED and mutates
    nothing beyond the committed event (extends the wiring pin with storage
    actually mutating). The restart opens its own epoch above the pre-
    restart admissions: it collects only post-boundary events and leaves
    the gated candidate to the manual path."""
    db_path = str(tmp_paths.state_dir / "mid-scan.sqlite")
    app = await build_and_start(_app_config(db_path), tmp_paths)
    second_gate = asyncio.Event()
    calls = 0
    try:
        assert app.storage is not None

        async def _epoch_open() -> bool:
            return await app.storage.latest_retention_epoch() is not None

        assert await wait_until(_epoch_open, timeout=5.0)
        original = app.storage.collect_event_content

        async def gated(
            event_id: str,
            *,
            dry_run: bool = True,
            retention_epoch: int | None = None,
        ) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 2:
                await second_gate.wait()
            return await original(
                event_id, dry_run=dry_run, retention_epoch=retention_epoch
            )

        monkeypatch.setattr(app.storage, "collect_event_content", gated)
        await seed_event(app.storage, "scanned", "2026-11-02T10:00:00+00:00")
        await seed_event(app.storage, "unscanned", "2026-11-02T11:00:00+00:00")
        assert await wait_until(lambda: calls >= 2, timeout=10.0)
        assert not await body_present(app.storage, "scanned")
        assert await body_present(app.storage, "unscanned")
    finally:
        await app.stop()
        second_gate.set()
    assert app.state is RuntimeState.STOPPED
    worker = app._collection_worker
    assert worker is not None
    assert worker._task is None
    assert worker.state.abandoned is False
    assert worker.has_retained_work is False
    assert await _reopened_body(
        db_path, "unscanned"
    ), "the gated candidate was never mutated"

    app2 = await build_and_start(_app_config(db_path), tmp_paths)
    try:
        assert app2.storage is not None

        async def _restart_boundary() -> bool:
            assert app2.storage is not None
            latest = await app2.storage.latest_retention_epoch()
            return (
                latest is not None
                and latest.epoch >= 2
                and latest.admission_watermark == "2026-11-02T11:00:00+00:00"
            )

        assert await wait_until(_restart_boundary, timeout=10.0)
        await seed_event(app2.storage, "after-restart", "2026-11-03T10:00:00+00:00")

        async def _post_restart_collected() -> bool:
            assert app2.storage is not None
            return not await body_present(app2.storage, "after-restart")

        assert await wait_until(_post_restart_collected, timeout=10.0)
        assert await app2.storage.count_collected_events() == 2
        assert await body_present(
            app2.storage, "unscanned"
        ), "the pre-restart admission is never auto-collected"
    finally:
        await clean_stop(app2)


# ---------------------------------------------------------------------------
# Active replay pin + CLI activation against a live bridge
# ---------------------------------------------------------------------------


async def test_replay_scope_never_constructs_the_worker(tmp_paths: MedrePaths) -> None:
    """Replay reads event bodies a concurrent collector must not remove: the
    app-level pin holds under the qualified configuration."""
    config = _app_config(str(tmp_paths.state_dir / "replay.sqlite"))
    app = RuntimeBuilder(config, tmp_paths).build()
    await app.start(scope=StartupScope.REPLAY)
    try:
        assert app._collection_worker is None
        assert app.storage is not None
        assert await app.storage.latest_retention_epoch() is None
    finally:
        await app.stop()
    assert app.state is RuntimeState.STOPPED


def _activate_in_thread(db_path: str) -> str:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        asyncio.run(_storage_retention_activate(db_path, yes=True))
    return stdout.getvalue()


async def test_cli_activation_during_live_bridge_is_adopted(
    tmp_paths: MedrePaths,
) -> None:
    """A CLI ``storage retention activate`` against a live bridge opens a
    new operator epoch that the running worker adopts on its next sweep,
    while an in-flight durable delivery racing the activation completes
    exactly once with its evidence untouched."""
    db_path = str(tmp_paths.state_dir / "live-bridge.sqlite")
    config = _app_config(db_path, shutdown_timeout=5)
    # A second fake Matrix adapter gives the bridge a presentation-class
    # target: its outbound native ref carries no packet metadata, so a fully
    # delivered event stays collectible (radio fakes record packet facts,
    # which correctly block as native-reference content).
    groups = dict(config.adapters.groups())
    matrix = dict(groups["matrix"])
    matrix["fake_matrix_dst"] = type(matrix["fake_matrix"])(
        adapter_id="fake_matrix_dst", enabled=True, adapter_kind="fake"
    )
    groups["matrix"] = matrix
    config = _dc_replace(config, adapters=AdapterConfigSet(groups))
    app = await build_and_start(config, tmp_paths)
    collect_gate = asyncio.Event()
    try:
        assert app.storage is not None

        async def _epoch_open() -> bool:
            return await app.storage.latest_retention_epoch() is not None

        assert await wait_until(_epoch_open, timeout=5.0)
        original = app.storage.collect_event_content

        async def gated(
            event_id: str,
            *,
            dry_run: bool = True,
            retention_epoch: int | None = None,
        ) -> dict[str, Any]:
            await collect_gate.wait()
            return await original(
                event_id, dry_run=dry_run, retention_epoch=retention_epoch
            )

        # The worker holds the same storage object, so the instance patch
        # intercepts exactly its sweeps. The gate stays held until the
        # pre-activation collection is wanted.
        app.storage.collect_event_content = gated  # type: ignore[method-assign]
        app.router.add_route(
            Route(
                id="mx-to-mx-dst",
                source=RouteSource("fake_matrix", ("message.created",), None),
                targets=[RouteTarget("fake_matrix_dst")],
            )
        )
        mx = app.adapters["fake_matrix"]
        dst = app.adapters["fake_matrix_dst"]

        async def _deliver(event: Any) -> None:
            """Admit through the live durable ingress path and wait for the
            real dispatch to settle the target."""
            await mx.simulate_inbound(event)
            assert await wait_until(
                lambda: any(
                    payload.event_id == event.event_id
                    for payload in dst.delivered_payloads
                ),
                timeout=10.0,
            )

        first = mx.make_event(text="pre-activation body")
        await _deliver(first)
        receipts = await app.storage.list_receipts_for_event(first.event_id)
        assert len(receipts) == 1 and receipts[0].status == "sent"

        # Release the gate: the worker collects the settled pre-activation
        # event under epoch 1.
        collect_gate.set()

        async def _first_collected() -> bool:
            return not await body_present(app.storage, first.event_id)

        assert await wait_until(_first_collected, timeout=10.0)
        worker = app._collection_worker
        assert worker is not None
        assert worker.state.epoch == 1

        # The CLI activation races a live in-flight delivery on the same
        # database: both are small real transactions and both must succeed.
        loop = asyncio.get_running_loop()
        cli_task = loop.run_in_executor(None, _activate_in_thread, db_path)
        second = mx.make_event(text="in-flight during activation")
        await _deliver(second)
        activation = json.loads(await cli_task)
        assert activation["format"] == "medre-content-retention-activation"
        assert activation["epoch"] == 2

        assert await wait_until(
            lambda: worker.state.epoch == 2, timeout=10.0
        ), "the running worker adopts the operator epoch"

        # A post-activation event is inside epoch 2's scope and collects.
        post = mx.make_event(text="post-activation body")
        await _deliver(post)

        async def _post_collected() -> bool:
            return not await body_present(app.storage, post.event_id)

        assert await wait_until(_post_collected, timeout=10.0)

        # The delivery evidence of the live bridge is untouched: sent
        # outbox rows, exactly one receipt per event, and every persisted
        # NativeMessageRef mapping still resolves.
        for event in (first, second, post):
            rows = await app.storage.list_outbox_items_for_event(event.event_id)
            assert rows and all(row.status == "sent" for row in rows)
            event_receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(event_receipts) == 1
            ref_rows = await app.storage._read_all(
                "SELECT adapter, native_channel_id, native_message_id FROM "
                "native_message_refs WHERE event_id=?",
                (event.event_id,),
            )
            assert ref_rows
            for ref in ref_rows:
                resolved = await app.storage.resolve_native_ref(
                    ref["adapter"], ref["native_channel_id"], ref["native_message_id"]
                )
                assert resolved == event.event_id, "native mappings survive"
    finally:
        collect_gate.set()
        await clean_stop(app)
    assert app.state is RuntimeState.STOPPED


# ---------------------------------------------------------------------------
# No-resend invariant after worker collection (INV 5)
# ---------------------------------------------------------------------------


async def test_worker_collection_leaves_terminal_work_unresendable(tmp_path) -> None:
    """After the worker collects a fully delivered event: replay and native
    submission stay denied, no retry or resend is enabled by deletion, the
    ``NativeMessageRef`` mappings survive, and the receipts are immutable
    (no rewrite, no new rows — plan invariant 5)."""
    store = SQLiteStorage(str(tmp_path / "no-resend.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=T0)
        rows, runner = await admit_and_stage(store, "delivered")
        await settle_first_target(store, rows)
        await dispatch_pending_through_real_worker(store, runner)
        settled = await store.list_outbox_items_for_event("delivered")
        assert all(row.status == "sent" for row in settled)

        receipts_before = await store.list_receipts_for_event("delivered")
        native_refs_before = await store.count_native_refs()
        assert receipts_before and native_refs_before >= 1

        worker = make_worker(store, FakeClock())
        await worker.sweep()
        assert worker.state.collected == 1
        assert not await body_present(store, "delivered")

        assert (
            await store.list_receipts_for_event("delivered") == receipts_before
        ), "receipts are immutable across collection"
        assert await store.count_native_refs() == native_refs_before
        for row in settled:
            read = await store.read_event_for_outbox("delivered", row.outbox_id)
            assert read == AuthorizedEventRead("denied")
            assert not await store.authorize_native_submission(
                "delivered", row.outbox_id, worker_id="worker", attempt_number=1
            )
            assert not await store.mark_outbox_cancelled(row.outbox_id)
        # Every persisted NativeMessageRef mapping still resolves after
        # collection (the admission ref and the dispatched-send ref).
        ref_rows = await store._read_all(
            "SELECT adapter, native_channel_id, native_message_id FROM "
            "native_message_refs WHERE event_id='delivered'"
        )
        assert len(ref_rows) == native_refs_before
        for ref in ref_rows:
            resolved = await store.resolve_native_ref(
                ref["adapter"], ref["native_channel_id"], ref["native_message_id"]
            )
            assert resolved == "delivered", "native mappings survive collection"
        assert await store.read_event_for_replay("delivered") == AuthorizedEventRead(
            "denied"
        )
        body = await store.read_body_for_consumer("delivered", "history")
        assert body.status == "denied" and body.body is None
    finally:
        await store.close()
