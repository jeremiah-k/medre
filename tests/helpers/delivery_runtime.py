"""Controlled native targets and real runtime assembly for delivery tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.config.model import (
    RetryConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import resolve
from medre.core.contracts.adapter import AdapterHandoffResult
from medre.core.engine.pipeline import PipelineRunner
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.capacity import CapacityController
from medre.runtime.app import MedreApp
from tests.helpers.pipeline import make_pipeline_config_for_pipeline


class _GatedTarget(FakePresentationAdapter):
    """Hold a handoff without accepting it until the test releases the link."""

    def __init__(self, adapter_id: str) -> None:
        super().__init__(adapter_id=adapter_id)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return await super().deliver(result)


def _runtime(tmp_path: Path, adapters: dict[str, FakePresentationAdapter]) -> MedreApp:
    """Assemble the real runtime with controlled native boundaries and SQLite."""
    paths = resolve(environ={"MEDRE_HOME": str(tmp_path)})
    store = SQLiteStorage(str(paths.database_path))
    route = Route(
        id="fourway",
        source=RouteSource("source", ("message.created",), None),
        targets=[RouteTarget(name, "out") for name in adapters],
    )
    pipeline_config = make_pipeline_config_for_pipeline(
        store, Router([route]), adapters
    )
    runner = PipelineRunner(pipeline_config)
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="controlled-fourway"),
        storage=StorageConfig(backend="sqlite"),
        limits=RuntimeLimits(
            max_inflight_deliveries=4, shutdown_drain_timeout_seconds=0.2
        ),
        retry=RetryConfig(enabled=False),
    )
    app = MedreApp(
        config=config,
        paths=paths,
        storage=store,
        event_bus=pipeline_config.event_bus,
        rendering_pipeline=runner._rendering_pipeline,
        router=pipeline_config.router,
        fallback_resolver=pipeline_config.fallback_resolver,
        relation_resolver=pipeline_config.relation_resolver,
        pipeline_runner=runner,
        diagnostician=runner._diagnostician,
        adapters=adapters,
        shutdown_event=asyncio.Event(),
    )
    app._capacity_controller = CapacityController(config.limits)
    runner.set_capacity_controller(app._capacity_controller)
    return app
