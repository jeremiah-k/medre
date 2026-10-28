"""Reusable staged initial-outbox fixture for dispatcher regressions."""

from __future__ import annotations

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.ingress import DurableIngressWorker
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


def _pipeline(store: SQLiteStorage):
    adapters = {
        name: FakePresentationAdapter(adapter_id=name) for name in ("radio", "matrix")
    }
    route = Route(
        id="mapping",
        source=RouteSource("source", ("message.created",), None, "Original mesh"),
        targets=[RouteTarget(name, "out") for name in adapters],
    )
    config = make_pipeline_config_for_pipeline(store, Router([route]), adapters)
    config.route_retry_policies[route.id] = RetryPolicy(max_attempts=4, jitter=False)
    return PipelineRunner(config), route, adapters


async def _stage(store: SQLiteStorage, runner: PipelineRunner, event_id: str = "event"):
    await runner.admit_ingress(
        make_event(event_id=event_id, source_adapter="source"), "live"
    )
    ingress = DurableIngressWorker(storage=store, pipeline=runner, batch_size=1)
    assert await ingress.run_once() == 1
    return await store.list_outbox_items_for_event(event_id)
