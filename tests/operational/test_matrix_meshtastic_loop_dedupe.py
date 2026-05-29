"""Matrix <-> Meshtastic loop prevention and dedup operational tests.

Tests exercising the PipelineRunner self-loop guard, route-trace loop
prevention, and native-ref dedup via the full ingress path.  Also
contains byte-budget truncation characterization tests.

All tests use fakes -- no real Matrix homeserver, no real Meshtastic radio.
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.core.engine.pipeline.runner import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats

# Reuse helpers from the flow module to avoid duplication.
from tests.operational.test_matrix_meshtastic_flow import (
    _FakeStorage,
    _make_meshtastic_config,
    _matrix_inbound_event,
    _mesh_rendering_context,
    _meshtastic_inbound_event,
)


def _build_selfloop_runner(
    storage: _FakeStorage | None = None,
) -> tuple[PipelineRunner, _FakeStorage]:
    """Build a PipelineRunner with a self-loop route (matrix -> matrix)."""
    from medre.adapters.fakes.matrix import FakeMatrixAdapter
    from medre.adapters.matrix.renderer import MatrixRenderer

    store = storage or _FakeStorage()
    event_bus = EventBus()

    matrix_adapter = FakeMatrixAdapter("test_matrix")
    adapters: dict[str, Any] = {
        "test_matrix": matrix_adapter,
    }

    rendering_pipeline = RenderingPipeline()
    rendering_pipeline.register(MatrixRenderer(), priority=10)

    router = Router()
    route = Route(
        id="route-self-loop",
        source=RouteSource(adapter="test_matrix", event_kinds=(), channel=None),
        targets=[RouteTarget(adapter="test_matrix", channel="!test:example.com")],
    )
    router.add_route(route)

    config = PipelineConfig(
        storage=store,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=store),
        adapters=adapters,
        event_bus=event_bus,
        rendering_pipeline=rendering_pipeline,
        route_stats=RouteStats(),
    )
    runner = PipelineRunner(config)
    return runner, store


def _build_matrix_to_mesh_runner(
    storage: _FakeStorage | None = None,
) -> tuple[PipelineRunner, _FakeStorage]:
    """Build a PipelineRunner with a matrix->mesh route for dedup tests."""
    from medre.adapters.fakes.matrix import FakeMatrixAdapter
    from medre.adapters.matrix.renderer import MatrixRenderer

    store = storage or _FakeStorage()
    event_bus = EventBus()

    matrix_adapter = FakeMatrixAdapter("test_matrix")
    mesh_config = _make_meshtastic_config()
    mesh_adapter = FakeMeshtasticAdapter(mesh_config)

    adapters: dict[str, Any] = {
        "test_matrix": matrix_adapter,
        "test_mesh": mesh_adapter,
    }

    rendering_pipeline = RenderingPipeline()
    rendering_pipeline.register(MatrixRenderer(), priority=10)
    rendering_pipeline.register(
        MeshtasticRenderer(configs={"test_mesh": mesh_config}), priority=10
    )

    router = Router()
    route = Route(
        id="route-mx-mesh",
        source=RouteSource(adapter="test_matrix", event_kinds=(), channel=None),
        targets=[RouteTarget(adapter="test_mesh", channel="0")],
    )
    router.add_route(route)

    config = PipelineConfig(
        storage=store,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=store),
        adapters=adapters,
        event_bus=event_bus,
        rendering_pipeline=rendering_pipeline,
        route_stats=RouteStats(),
    )
    runner = PipelineRunner(config)
    return runner, store


def _build_mesh_to_matrix_runner(
    storage: _FakeStorage | None = None,
) -> tuple[PipelineRunner, _FakeStorage]:
    """Build a PipelineRunner with a mesh->matrix route for dedup tests."""
    from medre.adapters.fakes.matrix import FakeMatrixAdapter
    from medre.adapters.matrix.renderer import MatrixRenderer

    store = storage or _FakeStorage()
    event_bus = EventBus()

    matrix_adapter = FakeMatrixAdapter("test_matrix")
    mesh_config = _make_meshtastic_config()
    mesh_adapter = FakeMeshtasticAdapter(mesh_config)

    adapters: dict[str, Any] = {
        "test_matrix": matrix_adapter,
        "test_mesh": mesh_adapter,
    }

    rendering_pipeline = RenderingPipeline()
    rendering_pipeline.register(MatrixRenderer(), priority=10)
    rendering_pipeline.register(
        MeshtasticRenderer(configs={"test_mesh": mesh_config}), priority=10
    )

    router = Router()
    route = Route(
        id="route-mesh-mx",
        source=RouteSource(adapter="test_mesh", event_kinds=(), channel=None),
        targets=[RouteTarget(adapter="test_matrix", channel="!test:example.com")],
    )
    router.add_route(route)

    config = PipelineConfig(
        storage=store,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=store),
        adapters=adapters,
        event_bus=event_bus,
        rendering_pipeline=rendering_pipeline,
        route_stats=RouteStats(),
    )
    runner = PipelineRunner(config)
    return runner, store


# ===========================================================================
# C. Self-loop prevention via PipelineRunner
# ===========================================================================


class TestSelfLoopPrevention:
    """PipelineRunner self-loop guard suppresses delivery back to source."""

    @pytest.mark.asyncio
    async def test_self_loop_suppresses_delivery_via_pipeline(self) -> None:
        """Exercise the PipelineRunner self-loop guard: a route targeting
        the source adapter is suppressed at Phase 1."""
        runner, storage = _build_selfloop_runner()
        await runner.start()
        try:
            event = _matrix_inbound_event(body="Loop test")

            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.failure_kind == DeliveryFailureKind.LOOP_SUPPRESSED
            assert "loop_prevented" in (outcome.error or "")

            # No adapter delivery occurred.
            matrix_adapter = runner._config.adapters["test_matrix"]
            assert len(matrix_adapter.delivered_payloads) == 0

            # Route stats show loop_prevented.
            stats = runner._config.route_stats
            assert stats is not None
            snap = stats.snapshot()
            assert snap["route-self-loop"]["loop_prevented"] == 1
            assert snap["route-self-loop"]["delivered"] == 0

            # Suppression receipt was persisted.
            receipts = await storage.list_receipts_for_event(event.event_id)
            suppressed = [r for r in receipts if r.status == "suppressed"]
            assert len(suppressed) == 1
            assert suppressed[0].failure_kind == "loop_suppressed"
        finally:
            await runner.stop()


# ===========================================================================
# D. Duplicate suppression via PipelineRunner
# ===========================================================================


class TestDuplicateSuppression:
    """PipelineRunner dedup guard suppresses events with duplicate
    native refs."""

    @pytest.mark.asyncio
    async def test_duplicate_matrix_event_suppressed_via_pipeline(self) -> None:
        """Feed the same Matrix native ref twice: first delivery succeeds,
        second is suppressed at Stage 1.5 dedup."""
        runner, storage = _build_matrix_to_mesh_runner()
        await runner.start()
        try:
            # First event: normal delivery.
            event1 = _matrix_inbound_event(body="First", event_id="$dup001")
            outcomes1 = await runner.handle_ingress(event1)
            assert len(outcomes1) == 1
            assert outcomes1[0].status == "success"

            mesh_adapter = runner._config.adapters["test_mesh"]
            assert len(mesh_adapter.delivered_payloads) == 1

            # Second event with same native ref: suppressed.
            event2 = _matrix_inbound_event(body="Duplicate", event_id="$dup001")
            outcomes2 = await runner.handle_ingress(event2)
            assert outcomes2 == []

            # No additional adapter delivery.
            assert len(mesh_adapter.delivered_payloads) == 1

            # Only one receipt stored (from first delivery).
            receipts = await storage.list_receipts_for_event(event1.event_id)
            sent_receipts = [r for r in receipts if r.status == "sent"]
            assert len(sent_receipts) == 1

            # No receipt for the second event at all.
            receipts2 = await storage.list_receipts_for_event(event2.event_id)
            assert len(receipts2) == 0
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_duplicate_meshtastic_packet_suppressed_via_pipeline(self) -> None:
        """Feed the same Meshtastic packet ID twice: second suppressed."""
        runner, storage = _build_mesh_to_matrix_runner()
        await runner.start()
        try:
            # First event: normal delivery.
            event1 = _meshtastic_inbound_event(body="First mesh", packet_id=42)
            outcomes1 = await runner.handle_ingress(event1)
            assert len(outcomes1) == 1
            assert outcomes1[0].status == "success"

            matrix_adapter = runner._config.adapters["test_matrix"]
            assert len(matrix_adapter.delivered_payloads) == 1

            # Second event with same native ref: suppressed.
            event2 = _meshtastic_inbound_event(body="Duplicate mesh", packet_id=42)
            snr2 = event2.source_native_ref
            assert snr2 is not None  # Verify source_native_ref is present for the dedup lookup.

            outcomes2 = await runner.handle_ingress(event2)
            assert outcomes2 == []

            # No additional adapter delivery.
            assert len(matrix_adapter.delivered_payloads) == 1
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_different_native_refs_both_deliver(self) -> None:
        """Events with different native refs both deliver normally."""
        runner, storage = _build_matrix_to_mesh_runner()
        await runner.start()
        try:
            event1 = _matrix_inbound_event(body="A", event_id="$a001")
            event2 = _matrix_inbound_event(body="B", event_id="$b002")

            outcomes1 = await runner.handle_ingress(event1)
            outcomes2 = await runner.handle_ingress(event2)

            assert len(outcomes1) == 1
            assert outcomes1[0].status == "success"
            assert len(outcomes2) == 1
            assert outcomes2[0].status == "success"

            mesh_adapter = runner._config.adapters["test_mesh"]
            assert len(mesh_adapter.delivered_payloads) == 2
        finally:
            await runner.stop()


# ===========================================================================
# Byte-budget truncation (characterization)
# ===========================================================================


class TestByteBudgetTruncation:
    """UTF-8 byte-budget truncation behavior."""

    @pytest.mark.asyncio
    async def test_long_text_truncated_to_byte_budget(self) -> None:
        config = _make_meshtastic_config(max_text_bytes=20)
        renderer = MeshtasticRenderer(configs={"test_mesh": config})
        event = _matrix_inbound_event(body="A" * 30)
        ctx = _mesh_rendering_context(max_text_bytes=20)

        result = await renderer.render(event, ctx)
        text = result.payload["text"]
        assert len(text.encode("utf-8")) <= 20
        assert result.truncated is True

    @pytest.mark.asyncio
    async def test_multibyte_utf8_truncation_safe(self) -> None:
        config = _make_meshtastic_config(max_text_bytes=10)
        renderer = MeshtasticRenderer(configs={"test_mesh": config})
        event = _matrix_inbound_event(body="\U0001f389\U0001f38a\U0001f381\U0001f388")
        ctx = _mesh_rendering_context(max_text_bytes=10)

        result = await renderer.render(event, ctx)
        text = result.payload["text"]
        decoded = text.encode("utf-8").decode("utf-8")
        assert decoded == text
        assert len(text.encode("utf-8")) <= 10

    @pytest.mark.asyncio
    async def test_truncation_metadata_in_result(self) -> None:
        config = _make_meshtastic_config(max_text_bytes=15)
        renderer = MeshtasticRenderer(configs={"test_mesh": config})
        event = _matrix_inbound_event(body="Hello world, this is too long")
        ctx = _mesh_rendering_context(max_text_bytes=15)

        result = await renderer.render(event, ctx)
        assert result.metadata["truncated"] is True
        assert result.metadata["original_text_bytes"] > 15
        assert result.metadata["rendered_text_bytes"] <= 15
        assert result.metadata["max_text_bytes"] == 15
