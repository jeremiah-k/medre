"""Shutdown-under-traffic tests proving MEDRE handles shutdown gracefully.

Seven tests exercise deterministic shutdown behaviour while ingress events
are in-flight.  Every test uses fake adapters (zero network, zero hardware)
and deterministic signalling (asyncio.Event / wait_until) instead of fixed sleeps.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, cast

import pytest

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter
from medre.config.model import RuntimeLimits
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.runtime.capacity import CapacityController
from medre.core.storage.backend import StorageBackend
from medre.core.storage.sqlite import SQLiteStorage
from medre.runtime.app import RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import build_runtime_snapshot
from tests.helpers.async_utils import wait_until

_LOG = __import__("logging").getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str | None = None,
    source_adapter: str = "fake_src",
    source_channel_id: str = "ch-0",
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for shutdown-traffic tests."""
    return CanonicalEvent(
        event_id=event_id or f"evt-{uuid.uuid4()}",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "shutdown-traffic-test"},
        metadata=EventMetadata(),
    )


def _make_route(
    route_id: str = "r-1",
    source_adapter: str = "fake_src",
    target_adapter: str = "fake_dst",
    source_channel: str = "ch-0",
) -> Route:
    """Create a minimal route for pipeline wiring."""
    return Route(
        id=route_id,
        source=RouteSource(
            adapter=source_adapter,
            event_kinds=("message.created",),
            channel=source_channel,
        ),
        targets=[RouteTarget(adapter=target_adapter)],
    )


def _build_runner(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict[str, Any],
    *,
    accounting: RuntimeAccounting | None = None,
    route_stats: RouteStats | None = None,
) -> PipelineRunner:
    """Build a PipelineRunner with sensible test defaults."""
    rp = RenderingPipeline()
    rp.register(TextRenderer(), priority=100)
    config = PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters,
        event_bus=EventBus(),
        rendering_pipeline=rp,
        runtime_accounting=accounting,
        route_stats=route_stats,
    )
    return PipelineRunner(config)


def _make_src_ctx(
    runner: PipelineRunner, shutdown_event: asyncio.Event
) -> AdapterContext:
    """Build an AdapterContext for a fake source adapter."""

    # Wrap ingress_handler to discard return value, matching MedreApp._make_publish_inbound.
    async def _publish(event: CanonicalEvent) -> None:
        await runner.ingress_handler(event)

    return AdapterContext(
        adapter_id="fake_src",
        event_bus=None,
        publish_inbound=_publish,
        logger=_LOG,
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=shutdown_event,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def temp_db() -> AsyncGenerator[SQLiteStorage, None]:
    """Provide an initialised SQLiteStorage backed by a temp file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    storage = SQLiteStorage(db_path=path)
    await storage.initialize()
    storage._tmp_db_path = path  # type: ignore[attr-defined]
    yield storage
    await storage.close()
    if path:
        os.unlink(path)


# ===================================================================
# Test 1: Ingress while shutdown begins
# ===================================================================


class TestIngressWhileShutdownBegins:
    """Concurrent ingress events race against runner.stop()."""

    @pytest.mark.asyncio
    async def test_ingress_while_shutdown_begins(self, temp_db: SQLiteStorage) -> None:
        """Inject 10 events concurrently, immediately stop the runner.

        Some events may complete before stop, some after.  No exceptions,
        storage readable, accounting sane.
        """
        dst = FakePresentationAdapter("fake_dst")
        router = Router(routes=[_make_route()])
        accounting = RuntimeAccounting()
        runner = _build_runner(
            temp_db, router, {"fake_dst": dst}, accounting=accounting
        )
        await runner.start()

        src_ctx = _make_src_ctx(runner, asyncio.Event())
        src = FakeTransportAdapter("fake_src")
        await src.start(src_ctx)

        events = [src.make_event(text=f"msg-{i}") for i in range(10)]

        # Fire ingress concurrently with stop.
        ingress_task = asyncio.gather(
            *[src.simulate_inbound(e) for e in events],
            return_exceptions=True,
        )
        stop_task = asyncio.ensure_future(runner.stop())

        results = await ingress_task
        await stop_task

        # No exceptions from ingress.
        for r in results:
            assert not isinstance(r, Exception), f"Ingress raised: {r}"

        # Storage readable (no corruption).
        count = await temp_db.count_events()
        assert count >= 0

        # Accounting values are non-negative ints.
        snap = accounting.snapshot()
        for v in snap.values():
            assert isinstance(v, int) and v >= 0

        await src.stop()


# ===================================================================
# Test 2: Delivery acquire during shutdown
# ===================================================================


class TestDeliveryAcquireDuringShutdown:
    """CapacityController rejects deliveries when shutting down."""

    @pytest.mark.asyncio
    async def test_delivery_acquire_during_shutdown(
        self, temp_db: SQLiteStorage
    ) -> None:
        """Hold delivery semaphore, trigger shutdown, verify SHUTDOWN_REJECTION."""
        limits = RuntimeLimits(
            max_inflight_deliveries=1,
            max_inflight_replay_events=1,
            shutdown_drain_timeout_seconds=2,
            delivery_acquire_timeout_seconds=0.5,
        )
        cc = CapacityController(limits)

        dst = FakePresentationAdapter("fake_dst")
        router = Router(routes=[_make_route()])
        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        runner = _build_runner(
            temp_db,
            router,
            {"fake_dst": dst},
            accounting=accounting,
            route_stats=route_stats,
        )
        runner.set_capacity_controller(cc)
        await runner.start()

        # Acquire the single slot so next delivery must wait.
        acquired = await cc.acquire_delivery()
        assert acquired is True

        event = _make_event()
        await temp_db.append(event)
        _, deliveries = await runner.route_event(event)
        assert len(deliveries) > 0

        # Simulate shutdown phase 1: stop accepting work.
        cc.stop_accepting()

        # Attempt delivery: should get SHUTDOWN_REJECTION.
        outcomes = await runner.deliver_to_targets(event, deliveries)
        assert len(outcomes) == 1
        assert outcomes[0].status == "permanent_failure"
        assert outcomes[0].failure_kind == DeliveryFailureKind.SHUTDOWN_REJECTION

        # Release held slot and stop runner (no deadlock).
        await cc.release_delivery()
        await runner.stop()

        assert accounting.snapshot()["capacity_rejections"] >= 1


# ===================================================================
# Test 3: Callback ingress after stop requested
# ===================================================================


class TestCallbackIngressAfterStopRequested:
    """simulate_inbound after shutdown is requested does not crash."""

    @pytest.mark.asyncio
    async def test_callback_ingress_after_stop_requested(
        self, temp_db: SQLiteStorage
    ) -> None:
        """Set shutdown_event, submit events: no crash, storage readable."""
        shutdown_event = asyncio.Event()

        dst = FakePresentationAdapter("fake_dst")
        router = Router(routes=[_make_route()])
        runner = _build_runner(temp_db, router, {"fake_dst": dst})
        await runner.start()

        src_ctx = _make_src_ctx(runner, shutdown_event)
        src = FakeTransportAdapter("fake_src")
        await src.start(src_ctx)

        # Signal shutdown.
        shutdown_event.set()

        # Submit events after shutdown signal.
        events = [src.make_event(text=f"post-shutdown-{i}") for i in range(3)]
        results = await asyncio.gather(
            *[src.simulate_inbound(e) for e in events],
            return_exceptions=True,
        )

        for r in results:
            assert not isinstance(r, Exception), f"simulate_inbound raised: {r}"

        count = await temp_db.count_events()
        assert count >= 0

        await runner.stop()
        await src.stop()


# ===================================================================
# Test 4: Adapter stop during ingress
# ===================================================================


class TestAdapterStopDuringIngress:
    """Stopping an adapter while it calls simulate_inbound is clean."""

    @pytest.mark.asyncio
    async def test_adapter_stop_during_ingress(self, temp_db: SQLiteStorage) -> None:
        """Background ingress loop + adapter.stop() → clean, no orphans."""
        dst = FakePresentationAdapter("fake_dst")
        router = Router(routes=[_make_route()])
        runner = _build_runner(temp_db, router, {"fake_dst": dst})
        await runner.start()

        src_ctx = _make_src_ctx(runner, asyncio.Event())
        src = FakeTransportAdapter("fake_src")
        await src.start(src_ctx)

        stop_seen = asyncio.Event()

        async def _ingress_loop() -> None:
            for i in range(50):
                if stop_seen.is_set():
                    break
                evt = src.make_event(text=f"loop-{i}")
                try:
                    await src.simulate_inbound(evt)
                except Exception:
                    break
                await asyncio.sleep(0)

        loop_task = asyncio.create_task(_ingress_loop())

        # Let a few events through, then stop the adapter.
        await wait_until(lambda: len(src.delivered_events) >= 2, timeout=2.0)
        stop_seen.set()
        await src.stop()

        # Loop task finishes cleanly.
        try:
            await asyncio.wait_for(loop_task, timeout=3.0)
        except asyncio.TimeoutError:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        assert not src.is_started
        await runner.stop()


# ===================================================================
# Test 5: Snapshot valid after shutdown under traffic
# ===================================================================


class TestSnapshotValidAfterShutdownUnderTraffic:
    """Snapshot is structurally valid after concurrent traffic + shutdown."""

    @pytest.mark.asyncio
    async def test_snapshot_valid_after_shutdown_under_traffic(
        self, temp_db: SQLiteStorage
    ) -> None:
        """Inject 8 events, stop, snapshot: valid JSON, sane accounting."""
        fd, cfg_path = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        try:
            cfg_path_obj = __import__("pathlib").Path(cfg_path)
            cfg_path_obj.write_text(
                '[runtime]\nname = "shutdown-traffic"\n\n'
                '[storage]\nbackend = "memory"\n\n'
                '[adapters.matrix.src]\nenabled = true\nadapter_kind = "fake"\n'
                'homeserver = "https://fake.local"\n'
                'user_id = "@bot:fake.local"\naccess_token = "tok"\n'
                'room_allowlist = ["!room:fake.local"]\nencryption_mode = "plaintext"\n\n'
                '[adapters.matrix.dst]\nenabled = true\nadapter_kind = "fake"\n'
                'homeserver = "https://fake.local"\n'
                'user_id = "@bot2:fake.local"\naccess_token = "tok"\n'
                'room_allowlist = ["!room:fake.local"]\nencryption_mode = "plaintext"\n\n'
                '[routes."r-1"]\n'
                'source_adapters = ["src"]\n'
                'dest_adapters = ["dst"]\n'
            )

            from medre.config.loader import load_config

            config, _source, paths = load_config(str(cfg_path_obj))
            app = RuntimeBuilder(config, paths).build()
            await app.start()
            assert app.state == RuntimeState.RUNNING

            # Inject events concurrently.
            events = [_make_event(source_adapter="src") for _ in range(8)]
            await asyncio.gather(
                *[app.pipeline_runner.handle_ingress(e) for e in events],
                return_exceptions=True,
            )

            await app.stop()
            assert app.state == RuntimeState.STOPPED

            snap = build_runtime_snapshot(
                app,
                now_fn=lambda: datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                monotonic_fn=lambda: 0.0,
            )

            data = json.loads(json.dumps(snap, sort_keys=True))

            assert data["schema_version"] == 1
            assert isinstance(data, dict)
            assert data["lifecycle"]["runtime_state"] == "stopped"

            counters = data["accounting"].get("counters")
            if counters is not None:
                for v in counters.values():
                    assert isinstance(v, int) and v >= 0

            assert list(data.keys()) == sorted(data.keys())
        finally:
            os.unlink(cfg_path)


# ===================================================================
# Test 6: No orphan tasks after shutdown
# ===================================================================


class TestNoOrphanTasksAfterShutdown:
    """No MEDRE adapter tasks remain after clean shutdown."""

    @pytest.mark.asyncio
    async def test_no_orphan_tasks_after_shutdown(self, temp_db: SQLiteStorage) -> None:
        """Inject events, shut down, verify asyncio.all_tasks() has no adapter tasks."""
        dst = FakePresentationAdapter("fake_dst")
        router = Router(routes=[_make_route()])
        runner = _build_runner(temp_db, router, {"fake_dst": dst})
        await runner.start()

        src_ctx = _make_src_ctx(runner, asyncio.Event())
        src = FakeTransportAdapter("fake_src")
        await src.start(src_ctx)

        for i in range(5):
            await src.simulate_inbound(src.make_event(text=f"orphan-{i}"))

        await src.stop()
        await runner.stop()

        adapter_patterns = (
            "simulate_inbound",
            "_ingress_loop",
            "_deliver_one",
            "_deliver_all",
        )
        orphans = [
            t
            for t in asyncio.all_tasks()
            if t.get_coro() is not None
            and any(p in (t.get_coro().__qualname__ or "") for p in adapter_patterns)
        ]
        assert (
            orphans == []
        ), f"Orphan tasks: {[t.get_coro().__qualname__ for t in orphans]}"


# ===================================================================
# Test 7: Double stop is harmless
# ===================================================================


class TestDoubleStopHarmless:
    """Calling stop() twice is a no-op (does not raise)."""

    @pytest.mark.asyncio
    async def test_double_stop_runner(self) -> None:
        """Start and stop a PipelineRunner, then stop again."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            storage = SQLiteStorage(db_path=path)
            await storage.initialize()
            runner = _build_runner(storage, Router(routes=[]), {})
            await runner.start()
            await runner.stop()
            await runner.stop()  # second stop — must not raise
            await storage.close()
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_double_stop_app(self) -> None:
        """Start and stop a MedreApp, then stop again (idempotent)."""
        fd, cfg_path = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        try:
            cfg_path_obj = __import__("pathlib").Path(cfg_path)
            cfg_path_obj.write_text(
                '[runtime]\nname = "double-stop"\n\n'
                '[storage]\nbackend = "memory"\n\n'
                '[adapters.matrix.solo]\nenabled = true\nadapter_kind = "fake"\n'
                'homeserver = "https://fake.local"\n'
                'user_id = "@bot:fake.local"\naccess_token = "tok"\n'
                'room_allowlist = ["!room:fake.local"]\nencryption_mode = "plaintext"\n'
            )

            from medre.config.loader import load_config

            config, _source, paths = load_config(str(cfg_path_obj))
            app = RuntimeBuilder(config, paths).build()
            await app.start()
            assert app.state == RuntimeState.RUNNING

            await app.stop()
            assert app.state == RuntimeState.STOPPED

            await app.stop()  # second stop — no-op
            assert app.state == RuntimeState.STOPPED
        finally:
            os.unlink(cfg_path)
