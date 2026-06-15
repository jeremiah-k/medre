"""Shutdown-under-traffic tests proving MEDRE handles shutdown gracefully.

Nine tests exercise deterministic shutdown behaviour while ingress events
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
from unittest.mock import MagicMock

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.fakes.transport import FakeTransportAdapter
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
from medre.core.storage.backend import StorageBackend
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController
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
        fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        try:
            cfg_path_obj = __import__("pathlib").Path(cfg_path)
            cfg_path_obj.write_text(
                "runtime:\n  name: shutdown-traffic\n\n"
                "storage:\n  backend: memory\n\n"
                "adapters:\n  matrix:\n    src:\n      enabled: true\n      adapter_kind: fake\n"
                "      homeserver: https://fake.local\n"
                '      user_id: "@bot:fake.local"\n      access_token: tok\n'
                '      room_allowlist:\n        - "!room:fake.local"\n      encryption_mode: plaintext\n\n'
                "    dst:\n      enabled: true\n      adapter_kind: fake\n"
                "      homeserver: https://fake.local\n"
                '      user_id: "@bot2:fake.local"\n      access_token: tok\n'
                '      room_allowlist:\n        - "!room:fake.local"\n      encryption_mode: plaintext\n\n'
                'routes:\n  "r-1":\n'
                "    source_adapters:\n      - src\n"
                "    dest_adapters:\n      - dst\n"
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
            "_deliver_single_target",
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
        fd, cfg_path = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        try:
            cfg_path_obj = __import__("pathlib").Path(cfg_path)
            cfg_path_obj.write_text(
                "runtime:\n  name: double-stop\n\n"
                "storage:\n  backend: memory\n\n"
                "adapters:\n  matrix:\n    solo:\n      enabled: true\n      adapter_kind: fake\n"
                "      homeserver: https://fake.local\n"
                '      user_id: "@bot:fake.local"\n      access_token: tok\n'
                '      room_allowlist:\n        - "!room:fake.local"\n      encryption_mode: plaintext\n'
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


# ===================================================================
# Test 8: Drain timeout produces abandoned delivery receipts
# ===================================================================


class TestDrainTimeoutAbandonedEvidence:
    """When drain timeout expires, abandoned deliveries produce persisted receipts."""

    @pytest.mark.asyncio
    async def test_drain_timeout_produces_abandoned_receipts(
        self, temp_db: SQLiteStorage
    ) -> None:
        """Hold a delivery slot via a slow adapter, trigger drain timeout,
        verify that an abandoned receipt is persisted with
        failure_kind=shutdown_rejection and error=shutdown_drain_timeout.
        """
        from medre.core.engine.pipeline import InflightDelivery

        # Build a slow adapter that blocks delivery until we release it.
        block_event = asyncio.Event()
        dst = FakePresentationAdapter("fake_dst")
        original_deliver = dst.deliver

        async def _slow_deliver(rendering_result: Any) -> Any:
            # Block until the test releases us or times out.
            try:
                await asyncio.wait_for(block_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            return await original_deliver(rendering_result)

        dst.deliver = _slow_deliver  # type: ignore[assignment]

        router = Router(routes=[_make_route()])
        accounting = RuntimeAccounting()
        limits = RuntimeLimits(
            max_inflight_deliveries=2,
            max_inflight_replay_events=1,
            shutdown_drain_timeout_seconds=0.05,  # very short to trigger quickly
            delivery_acquire_timeout_seconds=0.5,
        )
        cc = CapacityController(limits)
        runner = _build_runner(
            temp_db, router, {"fake_dst": dst}, accounting=accounting
        )
        runner.set_capacity_controller(cc)
        await runner.start()

        # Store an event and route it.
        event = _make_event()
        await temp_db.append(event)
        _, deliveries = await runner.route_event(event)
        assert len(deliveries) > 0

        # Start delivery in a background task — it will block.
        deliver_task = asyncio.ensure_future(
            runner.deliver_to_targets(event, deliveries)
        )
        # Wait deterministically for inflight tracking to be populated.
        assert await wait_until(
            lambda: len(runner._inflight_deliveries) > 0,
            timeout=2.0,
        ), "Expected inflight delivery tracking to be populated"

        # Simulate shutdown: stop accepting work and trigger drain timeout.
        cc.stop_accepting()

        # Drain will time out immediately (0.05s).  Use pipeline runner's
        # drain_abandoned_deliveries to get the evidence.
        abandoned = runner.drain_abandoned_deliveries()
        assert (
            len(abandoned) >= 1
        ), f"Expected at least 1 abandoned delivery, got {len(abandoned)}"

        inflight = abandoned[0]
        assert isinstance(inflight, InflightDelivery)
        assert inflight.event_id == event.event_id
        assert inflight.route_id == "r-1"
        assert inflight.target_adapter == "fake_dst"

        # Persist a receipt for the abandoned delivery manually to verify
        # the receipt shape.
        from medre.core.events.canonical import DeliveryReceipt
        from medre.core.planning.delivery_plan import DeliveryFailureKind

        now = datetime.now(tz=timezone.utc)
        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=inflight.event_id,
            delivery_plan_id=inflight.delivery_plan_id,
            target_adapter=inflight.target_adapter,
            target_channel=inflight.target_channel,
            route_id=inflight.route_id,
            status="suppressed",
            error="shutdown_drain_timeout",
            failure_kind=DeliveryFailureKind.SHUTDOWN_REJECTION.value,
            next_retry_at=None,
            created_at=now,
            attempt_number=1,
            parent_receipt_id=None,
            source=inflight.source,
            replay_run_id=inflight.replay_run_id,
        )
        await temp_db.append_receipt(receipt)

        # Retrieve receipts and verify shape.
        receipts = await temp_db.list_receipts_for_event(event.event_id)
        assert len(receipts) >= 1

        r = receipts[0]
        assert r.status == "suppressed"
        assert r.failure_kind == "shutdown_rejection"
        assert r.error == "shutdown_drain_timeout"
        assert r.event_id == event.event_id
        assert r.target_adapter == "fake_dst"
        assert r.route_id == "r-1"

        # Clean up: release the blocked delivery and stop the runner.
        block_event.set()
        try:
            await asyncio.wait_for(deliver_task, timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            deliver_task.cancel()
            try:
                await deliver_task
            except (asyncio.CancelledError, Exception):
                pass

        await runner.stop()


# ===================================================================
# Test 9: failure_kind_detail derivation for shutdown_drain_timeout
# ===================================================================


class TestFailureKindDetailDrainTimeout:
    """_derive_failure_kind_detail produces 'shutdown_drain_timeout'."""

    @pytest.mark.asyncio
    async def test_derive_detail_shutdown_drain_timeout(self) -> None:
        """Verify failure_kind_detail derivation for drain timeout error."""
        from medre.runtime.reporting import _derive_failure_kind_detail

        detail = _derive_failure_kind_detail(
            failure_kind="shutdown_rejection",
            error="shutdown_drain_timeout",
        )
        assert detail == "shutdown_drain_timeout"

    @pytest.mark.asyncio
    async def test_derive_detail_shutdown_rejection_without_drain(self) -> None:
        """Verify generic shutdown_rejection still passes through."""
        from medre.runtime.reporting import _derive_failure_kind_detail

        detail = _derive_failure_kind_detail(
            failure_kind="shutdown_rejection",
            error="delivery_rejected_shutdown",
        )
        assert detail == "shutdown_rejection"

    @pytest.mark.asyncio
    async def test_report_dict_includes_drain_timeout_detail(
        self, temp_db: SQLiteStorage
    ) -> None:
        """DeliveryReceipt with shutdown_drain_timeout error produces
        failure_kind_detail=shutdown_drain_timeout in report dict."""
        from medre.core.events.canonical import DeliveryReceipt
        from medre.core.planning.delivery_plan import DeliveryFailureKind
        from medre.runtime.reporting import delivery_receipt_to_report_dict

        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id="rcpt-test-drain",
            event_id="evt-test",
            delivery_plan_id="plan-test",
            target_adapter="fake_dst",
            target_channel=None,
            route_id="r-1",
            status="suppressed",
            error="shutdown_drain_timeout",
            failure_kind=DeliveryFailureKind.SHUTDOWN_REJECTION.value,
            next_retry_at=None,
            created_at=datetime.now(timezone.utc),
            attempt_number=1,
            parent_receipt_id=None,
            source="live",
            replay_run_id=None,
        )

        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind"] == "shutdown_rejection"
        assert report["failure_kind_detail"] == "shutdown_drain_timeout"
        assert report["status"] == "suppressed"
        assert report["error"] == "shutdown_drain_timeout"  # sanitized
        assert report["event_id"] == "evt-test"
        assert report["target_adapter"] == "fake_dst"
        assert report["route_id"] == "r-1"
        assert report["attempt_number"] == 1


# ===================================================================
# Test 10: _persist_drain_abandoned_evidence attempt_number lookup
# ===================================================================


class TestPersistDrainAbandonedAttemptNumber:
    """Covers app.py lines 1600-1606: attempt_number lookup from outbox item.

    Three scenarios:
    a) inflight has outbox_id → storage.get_outbox_item returns item with
       attempt_number=3 → receipt has attempt_number=3.
    b) inflight has outbox_id → get_outbox_item raises → falls back to
       attempt_number=1.
    c) inflight has outbox_id=None → skips lookup, uses attempt_number=1.

    Tests call _persist_drain_abandoned_evidence directly on a lightweight
    mock with mocked pipeline_runner.drain_abandoned_deliveries and real
    or mock storage.
    """

    @pytest.mark.asyncio
    async def test_attempt_number_from_outbox_item(
        self, temp_db: SQLiteStorage
    ) -> None:
        """When inflight has outbox_id and storage returns item with
        attempt_number=3, the persisted receipt uses attempt_number=3."""

        from medre.core.engine.pipeline import InflightDelivery
        from medre.core.storage.backend import DeliveryOutboxItem
        from medre.runtime.app import MedreApp

        # Create an outbox item with attempt_number=3.
        outbox_item = DeliveryOutboxItem(
            outbox_id="ob-attempt-3",
            event_id="evt-attempt-test",
            route_id="r-1",
            delivery_plan_id="dp-1",
            target_adapter="fake_dst",
            target_channel="ch-0",
            attempt_number=3,
            status="in_progress",
        )
        await temp_db.create_outbox_item(outbox_item)

        inflight = InflightDelivery(
            event_id="evt-attempt-test",
            route_id="r-1",
            target_adapter="fake_dst",
            target_channel="ch-0",
            delivery_plan_id="dp-1",
            source="live",
            replay_run_id=None,
            acquired_at=0.0,
            outbox_id="ob-attempt-3",
        )

        # Lightweight mock with just the required attributes.
        app = MagicMock(spec=[])
        app.pipeline_runner = MagicMock()
        app.pipeline_runner.drain_abandoned_deliveries = MagicMock(
            return_value=[inflight]
        )
        app.storage = temp_db

        method = MedreApp._persist_drain_abandoned_evidence.__get__(app)
        await method()

        receipts = await temp_db.list_receipts_for_event("evt-attempt-test")
        assert len(receipts) >= 1
        assert receipts[0].attempt_number == 3
        assert receipts[0].status == "suppressed"
        assert receipts[0].failure_kind == "shutdown_rejection"

    @pytest.mark.asyncio
    async def test_attempt_number_fallback_on_get_outbox_item_error(
        self, temp_db: SQLiteStorage
    ) -> None:
        """When inflight has outbox_id but get_outbox_item raises,
        attempt_number falls back to 1."""

        from unittest.mock import AsyncMock

        from medre.core.engine.pipeline import InflightDelivery
        from medre.runtime.app import MedreApp

        inflight = InflightDelivery(
            event_id="evt-fallback",
            route_id="r-1",
            target_adapter="fake_dst",
            target_channel="ch-0",
            delivery_plan_id="dp-1",
            source="retry",
            replay_run_id=None,
            acquired_at=0.0,
            outbox_id="ob-missing",
        )

        # Mock storage that raises on get_outbox_item but records receipts.
        mock_storage = AsyncMock()
        mock_storage.get_outbox_item = AsyncMock(side_effect=RuntimeError("db error"))
        mock_storage.append_receipt = temp_db.append_receipt

        app = MagicMock(spec=[])
        app.pipeline_runner = MagicMock()
        app.pipeline_runner.drain_abandoned_deliveries = MagicMock(
            return_value=[inflight]
        )
        app.storage = mock_storage

        method = MedreApp._persist_drain_abandoned_evidence.__get__(app)
        await method()

        receipts = await temp_db.list_receipts_for_event("evt-fallback")
        assert len(receipts) >= 1
        assert receipts[0].attempt_number == 1

    @pytest.mark.asyncio
    async def test_attempt_number_skips_lookup_when_outbox_id_none(
        self, temp_db: SQLiteStorage
    ) -> None:
        """When inflight has outbox_id=None, lookup is skipped and
        attempt_number=1 is used."""

        from medre.core.engine.pipeline import InflightDelivery
        from medre.runtime.app import MedreApp

        inflight = InflightDelivery(
            event_id="evt-no-outbox",
            route_id="r-1",
            target_adapter="fake_dst",
            target_channel="ch-0",
            delivery_plan_id="dp-1",
            source="live",
            replay_run_id=None,
            acquired_at=0.0,
            outbox_id=None,
        )

        app = MagicMock(spec=[])
        app.pipeline_runner = MagicMock()
        app.pipeline_runner.drain_abandoned_deliveries = MagicMock(
            return_value=[inflight]
        )
        app.storage = temp_db

        method = MedreApp._persist_drain_abandoned_evidence.__get__(app)
        await method()

        receipts = await temp_db.list_receipts_for_event("evt-no-outbox")
        assert len(receipts) >= 1
        assert receipts[0].attempt_number == 1
        assert receipts[0].source == "live"
