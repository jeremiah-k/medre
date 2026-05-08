"""Matrix pipeline integration tests: full ingress-to-delivery round-trips
with the Matrix renderer, FakeMatrixAdapter, error isolation, fanout, and
failure classification.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest

from medre.adapters import FakeMatrixAdapter, FakePresentationAdapter
from medre.adapters.base import BaseAdapter
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline_config(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict | None = None,
    event_bus: EventBus | None = None,
    use_matrix_renderer: bool = True,
) -> PipelineConfig:
    """Build a PipelineConfig with sensible defaults for Matrix tests."""
    pipeline = RenderingPipeline()
    if use_matrix_renderer:
        pipeline.register(MatrixRenderer())
    from medre.core.rendering.text import TextRenderer
    pipeline.register(TextRenderer())

    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=event_bus or EventBus(),
        rendering_pipeline=pipeline,
    )


def _make_event(
    event_id: str = "evt-001",
    event_kind: str = "message.created",
    source_adapter: str = "src",
    source_channel_id: str | None = "ch-0",
    payload: dict | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# Tests
# ===================================================================


class TestMatrixPipelineIntegration:
    """Pipeline integration with FakeMatrixAdapter and MatrixRenderer."""

    async def test_ingress_to_delivery_with_matrix(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Full round-trip: ingress → routing → Matrix rendering → delivery → receipt."""
        adapter = FakeMatrixAdapter("fake_matrix")
        route = Route(
            id="matrix-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="fake_matrix")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake_matrix": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
            event_id="matrix-001",
            payload={"text": "hello matrix"},
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Adapter received a rendered payload
            assert len(adapter.delivered_payloads) == 1
            rendered = adapter.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)

            # Receipt stored in database
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("matrix-001",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "sent"
            assert rows[0]["target_adapter"] == "fake_matrix"
        finally:
            await runner.stop()

    async def test_matrix_adapter_receives_rendering_result(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Pipeline delivers RenderingResult to Matrix adapter, not raw CanonicalEvent."""
        adapter = FakeMatrixAdapter("fake_matrix")
        route = Route(
            id="matrix-boundary-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="fake_matrix")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake_matrix": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
            event_id="matrix-boundary-001",
            payload={"text": "boundary test"},
        )

        try:
            await runner.handle_ingress(event)

            # Adapter received a RenderingResult, not a raw CanonicalEvent.
            assert len(adapter.delivered_payloads) == 1
            assert isinstance(adapter.delivered_payloads[0], RenderingResult)

            # No raw CanonicalEvents in received_events
            assert len(adapter.received_events) == 0
        finally:
            await runner.stop()

    async def test_fanout_matrix_and_presentation(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Fanout delivers to both FakeMatrixAdapter and FakePresentationAdapter."""
        matrix_adapter = FakeMatrixAdapter("fake_matrix")
        pres_adapter = FakePresentationAdapter("fake_presentation")

        route = Route(
            id="fanout-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[
                RouteTarget(adapter="fake_matrix"),
                RouteTarget(adapter="fake_presentation"),
            ],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fake_matrix": matrix_adapter,
                "fake_presentation": pres_adapter,
            },
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
            event_id="fanout-001",
            payload={"text": "fanout test"},
        )

        try:
            await runner.handle_ingress(event)

            # Both adapters received a rendered payload
            assert len(matrix_adapter.delivered_payloads) == 1
            assert len(pres_adapter.delivered_payloads) == 1

            # Two delivery receipts stored
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("fanout-001",),
            )
            assert len(rows) == 2
            adapter_names = {r["target_adapter"] for r in rows}
            assert adapter_names == {"fake_matrix", "fake_presentation"}
        finally:
            await runner.stop()

    async def test_error_isolation_matrix_fails(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """One failing adapter does not prevent Matrix adapter from succeeding."""

        class _FaultyPresentation:
            adapter_id = "faulty"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("faulty adapter exploded")

        matrix_adapter = FakeMatrixAdapter("fake_matrix")
        faulty = _FaultyPresentation()

        route = Route(
            id="error-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[
                RouteTarget(adapter="fake_matrix"),
                RouteTarget(adapter="faulty"),
            ],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake_matrix": matrix_adapter, "faulty": faulty},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
            event_id="error-001",
            payload={"text": "error isolation"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            by_adapter = {o.target_adapter: o for o in outcomes}
            assert by_adapter["fake_matrix"].status == "success"
            assert by_adapter["faulty"].status == "permanent_failure"

            # Matrix adapter got its payload
            assert len(matrix_adapter.delivered_payloads) == 1

            # Faulty adapter did not receive anything
            assert len(faulty.received_events) == 0
        finally:
            await runner.stop()

    async def test_matrix_transient_failure_classified(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ConnectionError in adapter is classified as transient_failure."""

        class _FlakyMatrix:
            adapter_id = "flaky_matrix"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise ConnectionError("matrix homeserver unreachable")

        flaky = _FlakyMatrix()

        route = Route(
            id="transient-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel="ch-0"
            ),
            targets=[RouteTarget(adapter="flaky_matrix")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"flaky_matrix": flaky},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(
            event_id="transient-001",
            payload={"text": "transient test"},
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"
        finally:
            await runner.stop()
