"""Stop/start bridge tests proving persistent SQLite storage survives runtime restarts.

These tests verify that events, delivery receipts, and native-ref deduplication
mappings persisted in SQLite survive a clean PipelineRunner stop + restart cycle.

Key architectural insights tested here:

* **SQLite persistence** – Two different PipelineRunner instances can point to
  the same SQLite database file. Data written by the first runner is visible
  to the second after re-initialization.

* **Process-local accounting** – RuntimeAccounting lives in memory only. A new
  PipelineRunner with a fresh RuntimeAccounting starts at zero, regardless of
  what the previous runner processed. Counters are per-process, not per-database.

* **Durable loop prevention** – Native-ref deduplication mappings are stored in
  SQLite, so a restart does not open a window for echo loops. This includes
  the edge case where native_channel_id is NULL (e.g., direct-message transports
  without channel scoping).

Tests
-----
* ``test_events_survive_stop_start`` – Canonical events persist across restarts.
* ``test_receipts_survive_stop_start`` – Delivery receipts persist across restarts.
* ``test_native_ref_dedup_survives_restart`` – Native-ref dedup survives process
  restart, proving loop prevention is durable.
* ``test_native_ref_dedup_null_channel_id_across_restart`` – Same dedup
  guarantee holds when native_channel_id is NULL.
* ``test_accounting_resets_on_new_runner`` – RuntimeAccounting is process-local
  and resets on new runner creation (not persisted to SQLite).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.bridge import make_pipeline_config
from tests.helpers.pipeline import make_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _open_storage(db_path: str) -> SQLiteStorage:
    """Create and initialize a SQLiteStorage pointing at *db_path*.

    The caller is responsible for calling ``storage.close()`` when done.
    """
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    return storage


def _make_event_with_native_ref(
    event_id: str,
    native_ref: NativeRef,
    source_adapter: str = "test-adapter",
) -> CanonicalEvent:
    """Create a CanonicalEvent carrying a source_native_ref.

    Used for native-ref dedup tests where we need control over the
    NativeRef tuple.  ``source_channel_id`` is set to match
    ``native_ref.native_channel_id`` so the event header stays consistent.
    """
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
    """Build a PipelineRunner with sensible test defaults.

    Delegates to ``make_pipeline_config`` which auto-registers
    TextRenderer as fallback and wires up FallbackResolver and
    RelationResolver.
    """
    config = make_pipeline_config(
        storage=cast(StorageBackend, storage),
        router=router,
        adapters=adapters or {},
        accounting=accounting,
    )
    return PipelineRunner(config)


# ---------------------------------------------------------------------------
# Test 1: Events survive stop/start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_survive_stop_start(tmp_path: Path) -> None:
    """Canonical events persisted by runner A remain after runner B restarts.

    Runner A writes 3 events, stops, and closes its storage connection.
    Runner B opens a new storage connection to the same SQLite file,
    writes 2 more events, and verifies all 5 are present.
    """
    db_path = str(tmp_path / "session.db")

    # -- Runner A ---------------------------------------------------------
    storage_a = await _open_storage(db_path)
    router = Router(routes=[])
    runner_a = _build_runner(storage_a, router)

    await runner_a.start()

    # Inject 3 events via handle_ingress (direct pipeline path).
    events_a = [
        make_event(event_id=f"evt-a-{i}", source_adapter="src") for i in range(3)
    ]
    for evt in events_a:
        await runner_a.handle_ingress(evt)

    await runner_a.stop()

    # Events survive in storage before close.
    assert await storage_a.count_events() == 3
    await storage_a.close()

    # -- Runner B (new storage, same DB file) -----------------------------
    storage_b = await _open_storage(db_path)
    runner_b = _build_runner(storage_b, router)

    await runner_b.start()

    # Inject 2 more events.
    events_b = [
        make_event(event_id=f"evt-b-{i}", source_adapter="src") for i in range(2)
    ]
    for evt in events_b:
        await runner_b.handle_ingress(evt)

    await runner_b.stop()

    # Total events: 3 from A + 2 from B = 5.
    total = await storage_b.count_events()
    assert total == 5, f"Expected 5 events, got {total}"

    # Verify the original 3 events from runner A survive.
    for i in range(3):
        retrieved = await storage_b.get(f"evt-a-{i}")
        assert retrieved is not None, f"Event evt-a-{i} missing after restart"
        assert retrieved.source_adapter == "src"

    await storage_b.close()


# ---------------------------------------------------------------------------
# Test 2: Receipts survive stop/start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receipts_survive_stop_start(tmp_path: Path) -> None:
    """Delivery receipts created by runner A remain after runner B restarts.

    Each event that matches a route produces a delivery receipt. Runner A
    creates 2 receipts, runner B creates 1 more. All 3 must survive in
    the shared SQLite database.
    """
    db_path = str(tmp_path / "session.db")
    route = _make_bridge_route()
    router = Router(routes=[route])

    # -- Runner A ---------------------------------------------------------
    storage_a = await _open_storage(db_path)
    pres_a = FakePresentationAdapter(adapter_id="fake_presentation")
    runner_a = _build_runner(storage_a, router, adapters={"fake_presentation": pres_a})

    await runner_a.start()

    # Inject 2 events that match the route → 2 deliveries → 2 receipts.
    for i in range(2):
        evt = make_event(
            event_id=f"rcpt-evt-a-{i}",
            source_adapter="fake_transport",
            source_channel_id="ch-0",
        )
        outcomes = await runner_a.handle_ingress(evt)
        assert len(outcomes) == 1, f"Expected 1 delivery outcome, got {len(outcomes)}"
        assert outcomes[0].status == "success"

    await runner_a.stop()

    receipts_after_a = await storage_a.count_receipts()
    assert receipts_after_a == 2, f"Expected 2 receipts after A, got {receipts_after_a}"
    await storage_a.close()

    # -- Runner B (same DB, new storage instance) -------------------------
    storage_b = await _open_storage(db_path)
    pres_b = FakePresentationAdapter(adapter_id="fake_presentation")
    runner_b = _build_runner(storage_b, router, adapters={"fake_presentation": pres_b})

    await runner_b.start()

    # Inject 1 more event → 1 more receipt.
    evt = make_event(
        event_id="rcpt-evt-b-0",
        source_adapter="fake_transport",
        source_channel_id="ch-0",
    )
    outcomes = await runner_b.handle_ingress(evt)
    assert len(outcomes) == 1
    assert outcomes[0].status == "success"

    await runner_b.stop()

    # Total receipts: 2 from A + 1 from B = 3.
    total_receipts = await storage_b.count_receipts()
    assert total_receipts == 3, f"Expected 3 receipts total, got {total_receipts}"

    # Old receipts from A are still accessible.
    receipt_a0 = await storage_b.list_receipts_for_event("rcpt-evt-a-0")
    assert len(receipt_a0) >= 1, "Receipt from runner A event not found"

    receipt_a1 = await storage_b.list_receipts_for_event("rcpt-evt-a-1")
    assert len(receipt_a1) >= 1, "Receipt from runner A event not found"

    receipt_b0 = await storage_b.list_receipts_for_event("rcpt-evt-b-0")
    assert len(receipt_b0) >= 1, "Receipt from runner B event not found"

    await storage_b.close()


# ---------------------------------------------------------------------------
# Test 3: Native-ref dedup survives restart (critical loop prevention)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_ref_dedup_survives_restart(tmp_path: Path) -> None:
    """Native-ref deduplication survives process restart, preventing echo loops.

    This is the critical loop-prevention test from the gap analysis. If a
    native ref was stored by runner A, runner B (with the same SQLite DB)
    must suppress a second event carrying the identical native ref.

    The dedup key is ``(adapter, native_channel_id, native_message_id)``.
    Here native_channel_id is a non-None value ("ch-0").
    """
    db_path = str(tmp_path / "session.db")
    native_ref = NativeRef(
        adapter="test-adapter",
        native_channel_id="ch-0",
        native_message_id="native-msg-001",
    )
    router = Router(routes=[])

    # -- Runner A: inject event with source_native_ref -------------------
    storage_a = await _open_storage(db_path)
    accounting_a = RuntimeAccounting()
    runner_a = _build_runner(storage_a, router, accounting=accounting_a)

    await runner_a.start()

    event_a = _make_event_with_native_ref(
        event_id="dedup-evt-001",
        native_ref=native_ref,
    )
    outcomes_a = await runner_a.handle_ingress(event_a)
    assert outcomes_a == [], "No routes matched; outcomes should be empty"

    # The event was stored and native ref persisted to SQLite.
    assert await storage_a.count_events() == 1
    snap_a = accounting_a.snapshot()
    assert snap_a["inbound_accepted"] == 1
    assert snap_a["loop_prevented"] == 0

    await runner_a.stop()
    await storage_a.close()

    # -- Runner B: inject DUPLICATE native ref → suppressed ---------------
    storage_b = await _open_storage(db_path)
    accounting_b = RuntimeAccounting()
    runner_b = _build_runner(storage_b, router, accounting=accounting_b)

    await runner_b.start()

    event_b = _make_event_with_native_ref(
        event_id="dedup-evt-002",
        native_ref=native_ref,
    )
    outcomes_b = await runner_b.handle_ingress(event_b)

    # The duplicate was suppressed: empty outcomes, loop_prevented bumped.
    assert outcomes_b == [], (
        "Duplicate native ref should have been suppressed; "
        f"got {len(outcomes_b)} outcomes"
    )

    snap_b = accounting_b.snapshot()
    assert (
        snap_b["loop_prevented"] == 1
    ), f"loop_prevented should be 1, got {snap_b['loop_prevented']}"
    assert (
        snap_b["inbound_accepted"] == 0
    ), "Duplicate event should NOT increment inbound_accepted"

    # Only the ORIGINAL event should be in storage (dedup-evt-002 never stored).
    total_events = await storage_b.count_events()
    assert total_events == 1, f"Expected 1 event (original only), got {total_events}"

    original = await storage_b.get("dedup-evt-001")
    assert original is not None, "Original event dedup-evt-001 lost"
    assert original.event_id == "dedup-evt-001"

    duplicate = await storage_b.get("dedup-evt-002")
    assert duplicate is None, "Duplicate event should not have been stored"

    await runner_b.stop()
    await storage_b.close()


# ---------------------------------------------------------------------------
# Test 4: Native-ref dedup with NULL channel_id across restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_ref_dedup_null_channel_id_across_restart(
    tmp_path: Path,
) -> None:
    """Native-ref dedup works across restarts even when native_channel_id is NULL.

    Some transports (e.g., direct-message protocols) do not scope messages
    by channel, so native_channel_id is None.  This test verifies that the
    dedup key ``(adapter, NULL, native_message_id)`` survives a runner
    restart — closing the same gap tested in test_self_message_prevention but
    across two separate PipelineRunner instances sharing one SQLite file.
    """
    db_path = str(tmp_path / "session.db")
    native_ref = NativeRef(
        adapter="nullch-adapter",
        native_channel_id=None,
        native_message_id="null-ch-restart-001",
    )
    router = Router(routes=[])

    # -- Runner A: first event with NULL channel_id native ref ------------
    storage_a = await _open_storage(db_path)
    accounting_a = RuntimeAccounting()
    runner_a = _build_runner(storage_a, router, accounting=accounting_a)

    await runner_a.start()

    event_a = _make_event_with_native_ref(
        event_id="nullch-evt-001",
        native_ref=native_ref,
        source_adapter="nullch-adapter",
    )
    outcomes_a = await runner_a.handle_ingress(event_a)
    assert outcomes_a == [], "No routes matched; outcomes should be empty"

    assert await storage_a.count_events() == 1
    snap_a = accounting_a.snapshot()
    assert snap_a["inbound_accepted"] == 1
    assert snap_a["loop_prevented"] == 0

    await runner_a.stop()
    await storage_a.close()

    # -- Runner B: duplicate native ref (NULL channel_id) → suppressed ----
    storage_b = await _open_storage(db_path)
    accounting_b = RuntimeAccounting()
    runner_b = _build_runner(storage_b, router, accounting=accounting_b)

    await runner_b.start()

    event_b = _make_event_with_native_ref(
        event_id="nullch-evt-002",
        native_ref=native_ref,
        source_adapter="nullch-adapter",
    )
    outcomes_b = await runner_b.handle_ingress(event_b)

    # Suppressed: dedup key (adapter, NULL, msg_id) persisted across restart.
    assert (
        outcomes_b == []
    ), "Duplicate NULL-channel native ref should be suppressed across restart"

    snap_b = accounting_b.snapshot()
    assert snap_b["loop_prevented"] == 1
    assert snap_b["inbound_accepted"] == 0

    # Only the original event in storage (nullch-evt-002 never stored).
    total_events = await storage_b.count_events()
    assert total_events == 1, f"Expected 1 event (original only), got {total_events}"

    original = await storage_b.get("nullch-evt-001")
    assert original is not None, "Original NULL-channel event lost after restart"

    duplicate = await storage_b.get("nullch-evt-002")
    assert duplicate is None, "Duplicate NULL-channel event should not be stored"

    await runner_b.stop()
    await storage_b.close()


# ---------------------------------------------------------------------------
# Test 5: Accounting resets on new runner (process-local)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accounting_resets_on_new_runner(tmp_path: Path) -> None:
    """RuntimeAccounting is process-local and resets on new runner creation.

    This proves that accounting counters live in memory only, not in SQLite.
    A new PipelineRunner with a fresh RuntimeAccounting starts at zero,
    regardless of what the previous runner processed. This is the complement
    of the persistence tests above: everything *except* accounting is durable.
    """
    db_path = str(tmp_path / "session.db")
    route = _make_bridge_route()
    router = Router(routes=[route])

    # -- Runner A ---------------------------------------------------------
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

    # Process 3 events through runner A.
    for i in range(3):
        evt = make_event(
            event_id=f"acc-evt-a-{i}",
            source_adapter="fake_transport",
            source_channel_id="ch-0",
        )
        await runner_a.handle_ingress(evt)

    snap_a = accounting_a.snapshot()
    assert (
        snap_a["inbound_accepted"] == 3
    ), f"Runner A should have accepted 3 events, got {snap_a['inbound_accepted']}"
    assert snap_a["outbound_attempts"] == 3
    assert snap_a["outbound_delivered"] == 3

    await runner_a.stop()
    await storage_a.close()

    # -- Runner B (same DB, new accounting instance) ----------------------
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

    # Accounting B starts at zero (process-local, not restored from DB).
    snap_b_initial = accounting_b.snapshot()
    assert (
        snap_b_initial["inbound_accepted"] == 0
    ), "New accounting instance should start at 0 for inbound_accepted"
    assert snap_b_initial["outbound_attempts"] == 0
    assert snap_b_initial["outbound_delivered"] == 0

    # Process 2 more events through runner B.
    for i in range(2):
        evt = make_event(
            event_id=f"acc-evt-b-{i}",
            source_adapter="fake_transport",
            source_channel_id="ch-0",
        )
        await runner_b.handle_ingress(evt)

    snap_b_final = accounting_b.snapshot()
    assert (
        snap_b_final["inbound_accepted"] == 2
    ), f"Runner B should have accepted 2 events, got {snap_b_final['inbound_accepted']}"
    assert snap_b_final["outbound_attempts"] == 2
    assert snap_b_final["outbound_delivered"] == 2

    # Storage has all 5 events (3 from A + 2 from B) but accounting
    # only reflects B's 2 events — proving accounting is not persisted.
    total_events = await storage_b.count_events()
    assert (
        total_events == 5
    ), f"Expected 5 total events in persistent storage, got {total_events}"

    # Runner A's accounting object still holds the old values — it was
    # not reset by runner B's creation (separate in-memory objects).
    assert snap_a["inbound_accepted"] == 3, "Runner A's accounting should still show 3"

    await runner_b.stop()
    await storage_b.close()
