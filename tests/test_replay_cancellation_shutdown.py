"""Replay cancellation and shutdown tests: CancelledError and capacity release.

Deterministic tests verifying that CancelledError propagates correctly
during replay, no false successful receipts are created, capacity slots
are released on cancellation, and shutdown rejects remaining events.

Uses asyncio.Event for synchronization (no fixed sleeps).

Tests
-----
* test_cancelled_during_replay_stage — CancelledError propagates, no false receipt
* test_shutdown_during_replay — shutdown rejects remaining replay events
* test_replay_capacity_slot_released_on_exception — slot freed after cancellation
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.config.model import (
    AdapterConfigSet,
    MatrixRuntimeConfig,
    RuntimeConfig,
    RuntimeLimits,
    StorageConfig,
)
from medre.config.paths import MedrePaths
from medre.config.routes import RouteConfig, RouteConfigSet
from medre.core.events.canonical import CanonicalEvent, EventMetadata
from medre.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    collect_replay_summary,
)
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-001",
    source_adapter: str = "main",
) -> CanonicalEvent:
    """Minimal canonical event."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="fake-transport",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello cancel"},
        metadata=EventMetadata(),
    )


def _make_paths(tmp: Path) -> MedrePaths:
    """MedrePaths pointing at *tmp* directories."""
    return MedrePaths(
        config_dir=tmp / "config",
        config_file=tmp / "config" / "config.toml",
        state_dir=tmp / "state",
        data_dir=tmp / "data",
        cache_dir=tmp / "cache",
        log_dir=tmp / "logs",
        database_path=tmp / "state" / "medre.sqlite",
    )


def _make_config() -> RuntimeConfig:
    """Two fake Matrix adapters: main → secondary."""
    return RuntimeConfig(
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "main": MatrixRuntimeConfig(
                    adapter_id="main",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                ),
                "secondary": MatrixRuntimeConfig(
                    adapter_id="secondary",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                ),
            },
        ),
        routes=RouteConfigSet(
            routes=(
                RouteConfig(
                    route_id="route_a",
                    source_adapters=("main",),
                    dest_adapters=("secondary",),
                ),
            ),
        ),
    )


@pytest.fixture()
async def cancel_env(tmp_path: Path):
    """Full runtime with fake adapters and ReplayEngine for cancellation tests."""
    paths = _make_paths(tmp_path)
    config = _make_config()

    for d in (paths.state_dir, paths.data_dir, paths.cache_dir, paths.log_dir):
        d.mkdir(parents=True, exist_ok=True)

    app = RuntimeBuilder(config, paths).build()
    await app.start()

    storage = app.storage
    pipeline = app.pipeline_runner

    class Env:
        pass

    env = Env()
    env.app = app
    env.storage = storage
    env.pipeline = pipeline

    async def seed(event: CanonicalEvent | None = None) -> CanonicalEvent:
        if event is None:
            event = _make_event()
        await storage.append(event)
        return event

    env.seed = seed

    yield env

    await app.stop()


# ===================================================================
# Test 1: CancelledError during replay stage
# ===================================================================


@pytest.mark.asyncio
async def test_cancelled_during_replay_stage(cancel_env) -> None:
    """Cancel replay mid-stage: CancelledError propagates, no false receipt.

    Uses a blocking fake adapter that pauses at an asyncio.Event.
    Cancelling the replay task should propagate CancelledError (not
    swallowed) and should NOT create a false successful delivery receipt.
    """
    env = cancel_env
    await env.seed(_make_event(event_id="evt-cancel"))

    # Insert a blocking adapter by monkeypatching the secondary adapter's
    # deliver method to wait on an asyncio.Event (never resolves).
    secondary = env.app.adapters.get("secondary")
    assert secondary is not None

    blocker_event = asyncio.Event()
    original_deliver = secondary.deliver

    async def _blocking_deliver(result):
        await blocker_event.wait()
        return await original_deliver(result)

    secondary.deliver = _blocking_deliver

    try:
        accounting = RuntimeAccounting()
        replay = ReplayEngine(
            storage=env.storage,
            pipeline=env.pipeline,
            event_bus=env.app.event_bus,
            diagnostician=env.app.diagnostician,
            accounting=accounting,
        )

        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=["evt-cancel"],
        )

        # Start replay in a task, then cancel it.
        async def _run_replay():
            results = []
            async for r in replay.replay(request):
                results.append(r)
            return results

        replay_task = asyncio.create_task(_run_replay())

        # Give the replay time to reach the blocking deliver call.
        # Yield control so the task can progress.
        for _ in range(20):
            await asyncio.sleep(0)
            if replay_task.done():
                break

        # Cancel the replay task.
        replay_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await replay_task

        # No false successful receipt created.
        receipt_rows = await env.storage._read_all(
            "SELECT * FROM delivery_receipts WHERE event_id = ? AND source = 'replay'",
            ("evt-cancel",),
        )
        assert (
            len(receipt_rows) == 0
        ), "Cancelled replay should not produce a delivery receipt"
    finally:
        # Unblock the adapter so teardown doesn't hang.
        blocker_event.set()
        secondary.deliver = original_deliver


# ===================================================================
# Test 2: shutdown during replay
# ===================================================================


@pytest.mark.asyncio
async def test_shutdown_during_replay(cancel_env) -> None:
    """Shutdown during replay: remaining events get rejected, no hang.

    Starts a replay that processes events, then requests shutdown via
    CapacityController.stop_accepting().  Remaining replay events should
    get SHUTDOWN_REJECTION or CAPACITY_REJECTION.  No hang.
    """
    env = cancel_env

    # Seed 3 events.
    for i in range(3):
        await env.seed(_make_event(event_id=f"evt-shut-{i}"))

    limits = RuntimeLimits(
        max_inflight_deliveries=10,
        max_inflight_replay_events=1,  # Process one at a time.
    )
    cc = CapacityController(limits)
    accounting = RuntimeAccounting()

    replay = ReplayEngine(
        storage=env.storage,
        pipeline=env.pipeline,
        event_bus=env.app.event_bus,
        diagnostician=env.app.diagnostician,
        capacity_controller=cc,
        accounting=accounting,
    )

    # Block the first event's delivery so we can trigger shutdown mid-replay.
    secondary = env.app.adapters.get("secondary")
    assert secondary is not None
    blocker = asyncio.Event()
    original_deliver = secondary.deliver

    first_delivered = asyncio.Event()

    async def _slow_deliver(result):
        first_delivered.set()
        await blocker.wait()
        return await original_deliver(result)

    secondary.deliver = _slow_deliver

    try:
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        async def _run_replay():
            results = []
            async for r in replay.replay(request):
                results.append(r)
            return results

        replay_task = asyncio.create_task(_run_replay())

        # Wait for first event to hit the blocking deliver.
        await asyncio.wait_for(first_delivered.wait(), timeout=5.0)

        # Trigger shutdown — no more work accepted.
        cc.stop_accepting()

        # Unblock the first event's delivery so it can complete.
        blocker.set()

        # The replay should complete (not hang).
        results = await asyncio.wait_for(replay_task, timeout=10.0)

        # At least the first event should have results.
        assert len(results) >= 3, (
            f"Expected at least 3 results (store+route+deliver for first event), "
            f"got {len(results)}"
        )

        # Some events should have been rejected due to shutdown.
        deliver_results = [r for r in results if r.stage == "deliver"]
        errors = [
            r
            for r in deliver_results
            if r.status == "error" and "shutdown" in (r.error or "").lower()
        ]
        # At least the events after the first should be shutdown-rejected.
        assert (
            len(errors) >= 1
        ), "Expected at least one shutdown rejection during replay"

        # Partial results (for completed events) have correct receipts.
        passed_deliver = [r for r in deliver_results if r.status == "passed"]
        # The first event might have passed before shutdown.
        for dr in passed_deliver:
            assert dr.output is not None
    finally:
        blocker.set()
        secondary.deliver = original_deliver


# ===================================================================
# Test 3: capacity slot released on exception/cancellation
# ===================================================================


@pytest.mark.asyncio
async def test_replay_capacity_slot_released_on_exception(
    cancel_env,
) -> None:
    """Capacity slot is released after replay cancellation or exception.

    Sets capacity to 1 replay slot.  Starts a replay, cancels mid-flight.
    Asserts the slot is released so another replay can start afterwards.
    """
    env = cancel_env
    await env.seed(_make_event(event_id="evt-slot-1"))
    await env.seed(_make_event(event_id="evt-slot-2"))

    limits = RuntimeLimits(
        max_inflight_deliveries=10,
        max_inflight_replay_events=1,
    )
    cc = CapacityController(limits)

    replay = ReplayEngine(
        storage=env.storage,
        pipeline=env.pipeline,
        event_bus=env.app.event_bus,
        diagnostician=env.app.diagnostician,
        capacity_controller=cc,
    )

    # Block the secondary adapter so replay stalls.
    secondary = env.app.adapters.get("secondary")
    assert secondary is not None
    blocker = asyncio.Event()
    original_deliver = secondary.deliver
    reached_deliver = asyncio.Event()

    async def _blocking_deliver(result):
        reached_deliver.set()
        await blocker.wait()
        return await original_deliver(result)

    secondary.deliver = _blocking_deliver

    try:
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=["evt-slot-1"],
        )

        async def _run():
            results = []
            async for r in replay.replay(request):
                results.append(r)
            return results

        task = asyncio.create_task(_run())

        # Wait for the deliver stage to be reached.
        await asyncio.wait_for(reached_deliver.wait(), timeout=5.0)

        # Replay should be using 1 slot.
        assert (
            cc.replay_current == 1
        ), f"Expected 1 replay slot in use, got {cc.replay_current}"

        # Cancel the replay task.
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Slot should be released (capacity freed).
        # The finally block in _stage_deliver releases the slot.
        # After cancellation, the task cleans up.  We need to give
        # the event loop a chance to run the finally block.
        for _ in range(20):
            await asyncio.sleep(0)

        # The capacity slot should have been released by the finally block.
        # Note: if CancelledError interrupts before acquire, slot stays 0.
        # If it interrupts after acquire but before release in finally,
        # the finally block should release it.
        assert cc.replay_current == 0, (
            f"Expected 0 replay slots in use after cancellation, "
            f"got {cc.replay_current}"
        )

        # Now we can start another replay — slot is available.
        request2 = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=["evt-slot-2"],
        )

        # Unblock the adapter so the second replay can complete.
        blocker.set()

        summary = await collect_replay_summary(replay.replay(request2))
        assert (
            summary.events_replayed >= 1
        ), "Second replay should succeed after slot is released"
    finally:
        blocker.set()
        secondary.deliver = original_deliver
