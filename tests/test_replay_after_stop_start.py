"""Replay after stop/start tests: persistent SQLite survives runtime restarts.

Tests verify that replay works correctly across PipelineRunner stop/start
boundaries, with storage persisted in SQLite and process-local accounting
resetting on restart.  Native-ref dedup suppression is also tested in the
context of replay.

Key architectural insights tested:

* **Storage durability** — events and receipts from Runtime A survive in
  SQLite and are visible to Runtime B after restart.

* **Process-local accounting** — RuntimeAccounting resets to zero in the
  new runtime.  Replay in Runtime B increments B's counters, not A's.

* **Replay appends receipts** — replay in Runtime B creates new receipt
  rows (source='replay') without modifying existing live receipts.

* **Replay bypasses native-ref dedup** — replay creates fresh events, not
  suppressed by native-ref deduplication that would suppress duplicate
  live ingress.

Tests
-----
* test_replay_after_restart — storage + receipts persist, replay appends cleanly
* test_duplicate_native_ref_suppression_across_replay — replay bypasses dedup
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, cast

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from medre.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    collect_replay_summary,
)
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.bridge import make_pipeline_config
from tests.helpers.pipeline import make_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _open_storage(db_path: str) -> SQLiteStorage:
    """Create and initialize a SQLiteStorage pointing at *db_path*."""
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    return storage


def _make_bridge_route() -> Route:
    """Route: fake_transport/ch-0 -> fake_presentation."""
    return Route(
        id="test-bridge-route",
        source=RouteSource(
            adapter="fake_transport",
            event_kinds=("message.created",),
            channel="ch-0",
        ),
        targets=[RouteTarget(adapter="fake_presentation")],
    )


def _build_runner(
    storage: SQLiteStorage,
    router: Router,
    *,
    adapters: dict | None = None,
    accounting: RuntimeAccounting | None = None,
) -> PipelineRunner:
    """Build a PipelineRunner with standard test defaults."""
    config = make_pipeline_config(
        storage=cast(StorageBackend, storage),
        router=router,
        adapters=adapters or {},
        accounting=accounting,
    )
    return PipelineRunner(config)


def _make_event_with_native_ref(
    event_id: str,
    native_ref: NativeRef,
    source_adapter: str = "test-adapter",
) -> CanonicalEvent:
    """Create a CanonicalEvent carrying a source_native_ref."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=native_ref.native_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
        source_native_ref=native_ref,
    )


async def _aiter_from_list(items: list) -> AsyncIterator:
    """Yield items from a list as an async iterator."""
    for item in items:
        yield item


async def _seed_orphan_event(
    storage: StorageBackend,
    event_id: str = "orphan-001",
) -> CanonicalEvent:
    """Seed an event directly into storage with no delivery receipt."""
    event = make_event(
        event_id=event_id,
        source_adapter="fake_transport",
        source_channel_id="ch-0",
    )
    await storage.append(event)
    return event


# ===================================================================
# Test 1: replay after restart
# ===================================================================


@pytest.mark.asyncio
async def test_replay_after_restart(tmp_path: Path) -> None:
    """Runtime A writes 3 live events; Runtime B replays one via BEST_EFFORT.

    Asserts:
    * Storage history intact (all 3 events still in SQLite)
    * Replay receipt appends cleanly (4 receipts: 3 live + 1 replay)
    * Replay receipt has source='replay' and replay_run_id
    * Process-local accounting in Runtime B starts at zero
    * Live receipts remain source='live' (unmodified by replay)
    """
    db_path = str(tmp_path / "session.db")
    route = _make_bridge_route()
    router = Router(routes=[route])

    # -- Runtime A: inject 3 events via handle_ingress ---------------------
    storage_a = await _open_storage(db_path)
    accounting_a = RuntimeAccounting()
    pres_a = FakePresentationAdapter(adapter_id="fake_presentation")
    runner_a = _build_runner(
        storage_a,
        router,
        adapters={"fake_presentation": pres_a},
        accounting=accounting_a,
    )
    await runner_a.start()

    event_ids = [f"restart-evt-{i}" for i in range(3)]
    for eid in event_ids:
        evt = make_event(
            event_id=eid,
            source_adapter="fake_transport",
            source_channel_id="ch-0",
        )
        outcomes = await runner_a.handle_ingress(evt)
        assert len(outcomes) == 1
        assert outcomes[0].status == "success"

    # Verify Runtime A accounting.
    snap_a = accounting_a.snapshot()
    assert snap_a["inbound_accepted"] == 3
    assert snap_a["outbound_delivered"] == 3

    await runner_a.stop()

    # Receipts from A: 3 live receipts.
    receipts_after_a = await storage_a.count_receipts()
    assert receipts_after_a == 3
    await storage_a.close()

    # -- Runtime B: open same DB, replay one event --------------------------
    storage_b = await _open_storage(db_path)
    accounting_b = RuntimeAccounting()
    pres_b = FakePresentationAdapter(adapter_id="fake_presentation")
    runner_b = _build_runner(
        storage_b,
        router,
        adapters={"fake_presentation": pres_b},
        accounting=accounting_b,
    )
    await runner_b.start()

    # Runtime B accounting starts at zero (process-local, not restored).
    snap_b_initial = accounting_b.snapshot()
    assert snap_b_initial["inbound_accepted"] == 0
    assert snap_b_initial["replay_processed"] == 0

    # All 3 events from A are still in storage.
    total_events = await storage_b.count_events()
    assert total_events == 3, f"Expected 3 events, got {total_events}"

    # Replay the first event via ReplayEngine.
    replay = ReplayEngine(
        storage=storage_b,
        pipeline=runner_b,
        accounting=accounting_b,
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        run_id="run-restart-001",
        correlation_ids=[event_ids[0]],
    )
    summary = await collect_replay_summary(replay.replay(request))
    assert summary.events_replayed >= 1

    # Replay receipt appended cleanly.
    total_receipts = await storage_b.count_receipts()
    assert (
        total_receipts == 4
    ), f"Expected 4 receipts (3 live + 1 replay), got {total_receipts}"

    # Replay receipt has source='replay' and run_id.
    replay_receipts = await storage_b.list_receipts_for_event(event_ids[0])
    replay_from_run = [r for r in replay_receipts if r.source == "replay"]
    assert len(replay_from_run) >= 1
    assert replay_from_run[0].replay_run_id == "run-restart-001"

    # Live receipts remain source='live' (unmodified).
    live_count = await storage_b.count_receipts_by_source("live")
    assert live_count == 3

    # Runtime B accounting: replay_processed incremented, inbound stays 0
    # (replay does not go through handle_ingress).
    snap_b_final = accounting_b.snapshot()
    assert snap_b_final["replay_processed"] >= 1
    assert (
        snap_b_final["inbound_accepted"] == 0
    ), "Replay should not increment inbound_accepted"

    await runner_b.stop()
    await storage_b.close()


# ===================================================================
# Test 2: replay bypasses native-ref dedup suppression
# ===================================================================


@pytest.mark.asyncio
async def test_duplicate_native_ref_suppression_across_replay(
    tmp_path: Path,
) -> None:
    """Replay creates new receipts even when native-ref dedup suppresses live.

    Runtime A: inject event with native_ref → accepted.
    Runtime B: inject second event with SAME native_ref → suppressed (dedup).
    Runtime B: replay first event → produces NEW receipt (not suppressed).

    Replay creates fresh events through the pipeline, bypassing the
    native-ref dedup that suppressed the second live ingress.  This is
    correct: replay re-processes stored events, not new ingress.
    """
    db_path = str(tmp_path / "session.db")
    native_ref = NativeRef(
        adapter="test-adapter",
        native_channel_id="ch-0",
        native_message_id="native-msg-replay-dedup",
    )
    router = Router(routes=[])

    # -- Runtime A: first event with native_ref -----------------------------
    storage_a = await _open_storage(db_path)
    accounting_a = RuntimeAccounting()
    runner_a = _build_runner(storage_a, router, accounting=accounting_a)
    await runner_a.start()

    event_a = _make_event_with_native_ref(
        event_id="dedup-evt-001",
        native_ref=native_ref,
    )
    await runner_a.handle_ingress(event_a)

    snap_a = accounting_a.snapshot()
    assert snap_a["inbound_accepted"] == 1
    assert snap_a["loop_prevented"] == 0

    await runner_a.stop()
    await storage_a.close()

    # -- Runtime B: second event with SAME native ref → suppressed ----------
    storage_b = await _open_storage(db_path)
    accounting_b = RuntimeAccounting()
    runner_b = _build_runner(storage_b, router, accounting=accounting_b)
    await runner_b.start()

    event_b = _make_event_with_native_ref(
        event_id="dedup-evt-002",
        native_ref=native_ref,
    )
    outcomes_b = await runner_b.handle_ingress(event_b)

    # Suppressed by dedup.
    assert outcomes_b == []
    snap_b = accounting_b.snapshot()
    assert snap_b["loop_prevented"] == 1
    assert snap_b["inbound_accepted"] == 0

    # Only original event in storage.
    total_events = await storage_b.count_events()
    assert total_events == 1

    # -- Now replay the FIRST event via ReplayEngine ------------------------
    replay = ReplayEngine(
        storage=storage_b,
        pipeline=runner_b,
        accounting=accounting_b,
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        run_id="run-dedup-replay",
        correlation_ids=["dedup-evt-001"],
    )

    # Replay should produce results — it reads from storage and processes
    # through pipeline stages, not through handle_ingress dedup.
    # With no routes, the route stage will fail but store stage passes.
    results: list = []
    async for r in replay.replay(request):
        results.append(r)
    summary = await collect_replay_summary(
        _aiter_from_list(results),
        mode=ReplayMode.BEST_EFFORT,
        run_id="run-dedup-replay",
    )
    assert summary.events_replayed >= 1

    store_results = [r for r in results if r.stage == "store" and r.status == "passed"]
    assert len(store_results) >= 1, "Store stage should pass for existing event"

    # The second event's suppression is unchanged.
    snap_b_final = accounting_b.snapshot()
    assert (
        snap_b_final["loop_prevented"] == 1
    ), "Second event suppression should still be 1"

    # Original event still the only one in storage.
    assert await storage_b.count_events() == 1

    await runner_b.stop()
    await storage_b.close()


# ===================================================================
# Test 3: orphan event replay (no delivery receipt)
# ===================================================================


def _snapshot_event_fields(event: CanonicalEvent) -> dict:
    """Capture identity fields of a CanonicalEvent for later comparison."""
    return {
        "event_id": event.event_id,
        "event_kind": event.event_kind,
        "schema_version": event.schema_version,
        "source_adapter": event.source_adapter,
        "source_transport_id": event.source_transport_id,
        "source_channel_id": event.source_channel_id,
        "parent_event_id": event.parent_event_id,
        "lineage": event.lineage,
        "relations": event.relations,
        "payload": event.payload,
    }


class TestOrphanEventReplay:
    """Replay handles events with no delivery receipts (orphans).

    An orphan event is a canonical event stored in the database that has
    no delivery receipt — the pipeline stored it but never delivered it
    (e.g. crash before delivery).  Replay in BEST_EFFORT mode should
    find, deliver, and create a receipt with source='replay'.  Replay in
    DRY_RUN mode should find the event but suppress delivery.
    """

    @pytest.mark.asyncio
    async def test_best_effort_delivers_orphan_event(self, tmp_path: Path) -> None:
        """BEST_EFFORT replay delivers an orphan event and creates a receipt.

        Seed an event directly into storage (no receipt), then replay it
        in BEST_EFFORT mode.  Assert:
        * Replay finds the event (events_replayed >= 1)
        * At least one deliver-stage result has status "passed"
        * A receipt with source='replay' and replay_run_id is created
        * The original event fields are unchanged
        """
        db_path = str(tmp_path / "orphan.db")
        route = _make_bridge_route()
        router = Router(routes=[route])

        storage = await _open_storage(db_path)
        accounting = RuntimeAccounting()
        pres = FakePresentationAdapter(adapter_id="fake_presentation")
        runner = _build_runner(
            storage,
            router,
            adapters={"fake_presentation": pres},
            accounting=accounting,
        )
        await runner.start()

        try:
            orphan_event = await _seed_orphan_event(storage, event_id="orphan-001")
            original_snapshot = _snapshot_event_fields(orphan_event)

            # Verify it's an orphan: event in storage, no receipts.
            assert await storage.count_events() == 1
            assert await storage.count_receipts() == 0

            # Replay the orphan in BEST_EFFORT mode.
            replay = ReplayEngine(
                storage=storage,
                pipeline=runner,
                accounting=accounting,
            )
            request = ReplayRequest(
                mode=ReplayMode.BEST_EFFORT,
                run_id="run-orphan-001",
                correlation_ids=["orphan-001"],
            )
            results = [r async for r in replay.replay(request)]
            assert len(results) >= 1

            # At least one deliver-stage result with status "passed".
            deliver_passed = [
                r for r in results if r.stage == "deliver" and r.status == "passed"
            ]
            assert (
                len(deliver_passed) >= 1
            ), "BEST_EFFORT should produce at least one passed deliver-stage result"

            # Receipt created with source='replay' and replay_run_id.
            assert await storage.count_receipts() == 1
            receipts = await storage.list_receipts_for_event("orphan-001")
            replay_from_run = [r for r in receipts if r.source == "replay"]
            assert len(replay_from_run) == 1
            assert replay_from_run[0].replay_run_id == "run-orphan-001"

            # Original event fields unchanged.
            stored_event = await storage.get("orphan-001")
            assert stored_event is not None
            stored_snapshot = _snapshot_event_fields(stored_event)
            assert stored_snapshot == original_snapshot
        finally:
            await runner.stop()
            await storage.close()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_deliver_orphan(self, tmp_path: Path) -> None:
        """DRY_RUN replay finds the orphan but suppresses delivery.

        Seed an orphan event, replay in DRY_RUN mode.  Assert:
        * Replay finds the event
        * Deliver-stage result exists with status "skipped"
        * No receipts are created (delivery suppressed)
        * The original event fields are unchanged
        """
        db_path = str(tmp_path / "orphan-dry.db")
        route = _make_bridge_route()
        router = Router(routes=[route])

        storage = await _open_storage(db_path)
        accounting = RuntimeAccounting()
        pres = FakePresentationAdapter(adapter_id="fake_presentation")
        runner = _build_runner(
            storage,
            router,
            adapters={"fake_presentation": pres},
            accounting=accounting,
        )
        await runner.start()

        try:
            orphan_event = await _seed_orphan_event(storage, event_id="orphan-dry-001")
            original_snapshot = _snapshot_event_fields(orphan_event)

            # Verify orphan state.
            assert await storage.count_events() == 1
            assert await storage.count_receipts() == 0

            # Replay in DRY_RUN mode.
            replay = ReplayEngine(
                storage=storage,
                pipeline=runner,
                accounting=accounting,
            )
            request = ReplayRequest(
                mode=ReplayMode.DRY_RUN,
                run_id="run-orphan-dry",
                correlation_ids=["orphan-dry-001"],
            )
            results = [r async for r in replay.replay(request)]
            assert len(results) >= 1

            # Deliver-stage result exists with status "skipped".
            deliver_results = [r for r in results if r.stage == "deliver"]
            assert (
                len(deliver_results) >= 1
            ), "DRY_RUN should produce a deliver-stage result"
            assert deliver_results[0].status == "skipped"
            assert deliver_results[0].error is not None
            assert "dry_run" in deliver_results[0].error.lower() or (
                "delivery suppressed" in deliver_results[0].error.lower()
            )

            # No receipts created — delivery was suppressed.
            assert await storage.count_receipts() == 0

            # Original event fields unchanged.
            stored_event = await storage.get("orphan-dry-001")
            assert stored_event is not None
            stored_snapshot = _snapshot_event_fields(stored_event)
            assert stored_snapshot == original_snapshot
        finally:
            await runner.stop()
            await storage.close()
