"""A stalled delivery must leave other adapters free to process later ingress."""

from __future__ import annotations

import asyncio
from pathlib import Path

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.config.model import RuntimeLimits
from medre.core.engine.pipeline import PipelineRunner
from medre.core.ingress import DurableIngressWorker
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.capacity import CapacityController
from medre.runtime.retry import RetryWorker
from tests.helpers.async_utils import wait_until
from tests.helpers.delivery_runtime import _GatedTarget, _runtime
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


async def test_later_message_reaches_healthy_targets_while_first_send_stalls(
    tmp_path: Path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "bridge.sqlite"))
    await store.initialize()
    blocked = _GatedTarget("FXLF")
    healthy = {
        name: FakePresentationAdapter(adapter_id=name)
        for name in ("FXLT", "meshcore", "matrix")
    }
    adapters = {"FXLF": blocked, **healthy}
    route = Route(
        id="fourway",
        source=RouteSource(
            adapter="source", channel=None, event_kinds=("message.created",)
        ),
        targets=[RouteTarget(adapter=name, channel="out") for name in adapters],
    )
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(store, Router(routes=[route]), adapters)
    )
    capacity = CapacityController(RuntimeLimits(max_inflight_deliveries=4))
    runner.set_capacity_controller(capacity)
    ingress = DurableIngressWorker(
        storage=store, pipeline=runner, interval_seconds=0.01
    )
    dispatch = RetryWorker(
        store,
        runner,
        capacity,
        initial_dispatch=True,
        enabled=False,
        interval_seconds=0.01,
    )
    await runner.start()
    await dispatch.start()
    try:
        await runner.admit_ingress(
            make_event(event_id="first", source_adapter="source"), "live"
        )
        await ingress.start()
        await asyncio.wait_for(blocked.entered.wait(), 2)
        assert await wait_until(
            lambda: all(len(a.delivered_payloads) == 1 for a in healthy.values()),
            timeout=2,
        )
        await runner.admit_ingress(
            make_event(event_id="second", source_adapter="source"), "live"
        )
        assert await wait_until(
            lambda: all(len(a.delivered_payloads) == 2 for a in healthy.values()),
            timeout=1,
        )
        assert blocked.calls == 1
        assert not blocked.delivered_payloads
    finally:
        blocked.release.set()
        await ingress.stop(grace_seconds=2)
        await dispatch.stop()
        await runner.stop()
        await store.close()


async def test_stalled_adapter_backlog_stays_durable_and_bounded(
    tmp_path: Path,
) -> None:
    """Later messages use healthy lanes while one adapter retains one native call."""
    blocked = _GatedTarget("FXLF")
    healthy = {
        name: FakePresentationAdapter(adapter_id=name)
        for name in ("FXLT", "matrix", "meshcore")
    }
    app = _runtime(tmp_path, {"FXLF": blocked, **healthy})
    await app.start()
    try:
        publish = app._make_publish_inbound()
        await publish(make_event(event_id="event-0", source_adapter="source"))
        await asyncio.wait_for(blocked.entered.wait(), 2)
        for index in range(1, 12):
            await publish(
                make_event(event_id=f"event-{index}", source_adapter="source")
            )
        assert await wait_until(
            lambda: all(len(a.delivered_payloads) == 12 for a in healthy.values()),
            # Windows CI can spend several seconds on SQLite and adapter
            # scheduling under load. Keep a bounded liveness assertion rather
            # than assuming twelve fanouts fit within three wall-clock seconds.
            timeout=10,
        )
        assert blocked.calls == 1
        assert len(app._retry_worker._active) <= 4

        async def all_healthy_committed() -> bool:
            return (await app.storage.count_outbox_by_status()).get("sent") == 36

        assert await wait_until(all_healthy_committed, timeout=2)
        assert app._capacity_controller.snapshot()["delivery_current"] == 1
        assert await app.storage.count_ingress_work_by_status() == {"completed": 12}
        for index in range(12):
            rows = await app.storage.list_outbox_items_for_event(f"event-{index}")
            radio = next(row for row in rows if row.target_adapter == "FXLF")
            assert radio.attempt_number == 0
            assert radio.active_attempt == (1 if index == 0 else None)
            assert radio.status == ("in_progress" if index == 0 else "pending")
            assert all(
                row.status == "sent" and row.attempt_number == 1
                for row in rows
                if row.target_adapter != "FXLF"
            )
    finally:
        blocked.release.set()
        await app.stop()
