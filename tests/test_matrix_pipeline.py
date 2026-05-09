"""Matrix pipeline integration tests: full ingress-to-delivery round-trips
with the Matrix renderer, FakeMatrixAdapter, error isolation, fanout, and
failure classification.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import cast

import pytest

from medre.adapters import FakeMatrixAdapter, FakePresentationAdapter
from medre.adapters.base import AdapterContext, BaseAdapter
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeMessageRef, NativeRef
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
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


def _make_inbound_event(
    event_id: str = "evt-inbound-001",
    source_adapter: str = "matrix-in",
    source_channel_id: str = "!room:server",
    native_message_id: str = "$evt-inbound-001",
    body: str = "hello matrix",
) -> CanonicalEvent:
    """Create a CanonicalEvent with source_native_ref, mimicking MatrixCodec.decode()."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="@alice:example.com",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": body},
        metadata=EventMetadata(),
        source_native_ref=NativeRef(
            adapter=source_adapter,
            native_channel_id=source_channel_id,
            native_message_id=native_message_id,
        ),
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


# ===================================================================
# Platform-aware renderer selection tests
# ===================================================================


def _make_adapter_context_for_pipeline(
    adapter_id: str, runner: PipelineRunner
) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=runner.ingress_handler,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


class TestMatrixPlatformRendererSelection:
    """Prove platform-aware renderer selection works for Matrix
    without relying on adapter-name prefixes or known_adapters."""

    async def test_platform_aware_renderer_selection(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A realistic Matrix adapter ID that does NOT start with 'matrix'
        still selects MatrixRenderer through the pipeline's platform registry.

        This proves:
        - FakeMatrixAdapter.platform == "matrix" drives dispatch
        - The RenderingPipeline platform registry maps adapter_id -> platform
        - MatrixRenderer.can_render matches on target_platform == "matrix"
        - TextRenderer is NOT selected for Matrix routes
        - No known_adapters or prefix-matching required
        """
        # 1. Create adapters with realistic IDs that do NOT start with "matrix"
        in_adapter = FakeMatrixAdapter("chat-source")

        out_adapter = FakeMatrixAdapter("chat-service")

        # 2. Route: chat-source -> chat-service
        route = Route(
            id="platform-registry-route",
            source=RouteSource(
                adapter="chat-source",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="chat-service")],
        )
        router = Router(routes=[route])

        # 3. RenderingPipeline with MatrixRenderer — NO known_adapters (critical!)
        rp = RenderingPipeline()
        rp.register(MatrixRenderer(), priority=50)
        rp.register(TextRenderer(), priority=100)

        # 4. PipelineRunner — start() calls _populate_renderer_platforms()
        runner = PipelineRunner(PipelineConfig(
            storage=cast(StorageBackend, temp_storage),
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"chat-source": in_adapter, "chat-service": out_adapter},
            event_bus=EventBus(),
            rendering_pipeline=rp,
        ))
        await runner.start()

        # 5. Wire inbound adapter
        ctx = _make_adapter_context_for_pipeline("chat-source", runner)
        await in_adapter.start(ctx)

        # 6. Send inbound event through simulate_inbound
        event = _make_event(
            event_id="matrix-platform-001",
            source_adapter="chat-source",
            source_channel_id="ch-0",
            payload={"text": "platform dispatch test"},
        )
        await in_adapter.simulate_inbound(event)

        # 7. Assertions

        # Outbound adapter received the rendered payload
        assert len(out_adapter.delivered_payloads) == 1
        result = out_adapter.delivered_payloads[0]

        # Proves MatrixRenderer was selected (not TextRenderer)
        assert result.metadata["renderer"] == "matrix"

        # Proves Matrix payload shape (msgtype + body)
        assert result.payload["msgtype"] == "m.text"
        assert result.payload["body"] == "platform dispatch test"

        # Outbound delivery returned a deterministic native_message_id
        assert isinstance(result, RenderingResult)
        native_event_id = f"$fake_{result.event_id}"

        # Native ref was persisted in storage
        # FakeMatrixAdapter returns native_channel_id="" which the pipeline
        # stores as None (due to "or target.channel" fallback)
        resolved = await temp_storage.resolve_native_ref(
            adapter="chat-service",
            native_channel_id=None,
            native_message_id=native_event_id,
        )
        assert resolved is not None


# ===================================================================
# Native ref persistence tests
# ===================================================================


class TestMatrixNativeRefPersistence:
    """Pipeline integration tests for native ref persistence with Matrix."""

    async def test_inbound_native_ref_persisted(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inbound Matrix event → pipeline store → NativeMessageRef(direction="inbound")."""
        route = Route(
            id="matrix-inbound-route",
            source=RouteSource(
                adapter="matrix-in",
                event_kinds=("message.created",),
                channel="!room:server",
            ),
            targets=[],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_inbound_event(
            event_id="matrix-inbound-001",
            source_adapter="matrix-in",
            source_channel_id="!room:server",
            native_message_id="$native-inbound-001",
        )

        try:
            await runner.handle_ingress(event)

            resolved = await temp_storage.resolve_native_ref(
                adapter="matrix-in",
                native_channel_id="!room:server",
                native_message_id="$native-inbound-001",
            )
            assert resolved is not None
            assert resolved == event.event_id
        finally:
            await runner.stop()

    async def test_outbound_native_ref_persisted(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Outbound FakeMatrixAdapter deliver → pipeline store → NativeMessageRef(direction="outbound")."""
        out_adapter = FakeMatrixAdapter("matrix-out")

        route = Route(
            id="matrix-outbound-route",
            source=RouteSource(
                adapter="matrix-out-in",
                event_kinds=("message.created",),
                channel="!room:server",
            ),
            targets=[RouteTarget(adapter="matrix-out")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"matrix-out": out_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_inbound_event(
            event_id="matrix-outbound-001",
            source_adapter="matrix-out-in",
            source_channel_id="!room:server",
            native_message_id="$native-outbound-001",
            body="outbound test",
        )

        try:
            await runner.handle_ingress(event)

            # FakeMatrixAdapter.deliver() returns native_message_id=f"$fake_{result.event_id}"
            # With no target channel, native_channel_id resolves to None
            resolved = await temp_storage.resolve_native_ref(
                adapter="matrix-out",
                native_channel_id=None,
                native_message_id=f"$fake_{event.event_id}",
            )
            assert resolved is not None
            assert resolved == event.event_id
        finally:
            await runner.stop()

    async def test_failed_delivery_no_outbound_native_ref(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Failed deliver → no outbound native ref in storage."""

        class _FaultyMatrix:
            adapter_id = "faulty-matrix"

            def __init__(self) -> None:
                self.received_events: list[object] = []

            async def deliver(self, payload: object) -> None:
                raise RuntimeError("faulty adapter exploded")

        faulty = _FaultyMatrix()

        route = Route(
            id="matrix-fail-route",
            source=RouteSource(
                adapter="matrix-fail-in",
                event_kinds=("message.created",),
                channel="!room:server",
            ),
            targets=[RouteTarget(adapter="faulty-matrix")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"faulty-matrix": faulty},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_inbound_event(
            event_id="matrix-fail-001",
            source_adapter="matrix-fail-in",
            source_channel_id="!room:server",
            native_message_id="$native-fail-001",
            body="fail test",
        )

        try:
            await runner.handle_ingress(event)

            # Verify no outbound native ref from failed delivery
            resolved = await temp_storage.resolve_native_ref(
                adapter="faulty-matrix",
                native_channel_id=None,
                native_message_id=f"$fake_{event.event_id}",
            )
            assert resolved is None

            # Inbound ref should still exist
            inbound_resolved = await temp_storage.resolve_native_ref(
                adapter="matrix-fail-in",
                native_channel_id="!room:server",
                native_message_id="$native-fail-001",
            )
            assert inbound_resolved is not None
        finally:
            await runner.stop()

    async def test_duplicate_inbound_native_ref_idempotent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Duplicate inbound native refs are idempotent (INSERT OR IGNORE)."""
        route = Route(
            id="matrix-dup-route",
            source=RouteSource(
                adapter="matrix-dup",
                event_kinds=("message.created",),
                channel="!room:server",
            ),
            targets=[],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_inbound_event(
            event_id="matrix-dup-001",
            source_adapter="matrix-dup",
            source_channel_id="!room:server",
            native_message_id="$native-dup-001",
        )

        try:
            await runner.handle_ingress(event)

            # Manually store a duplicate native ref — should be idempotent
            dup_ref = NativeMessageRef(
                id=f"nref-dup-{uuid.uuid4()}",
                event_id=event.event_id,
                adapter="matrix-dup",
                native_channel_id="!room:server",
                native_message_id="$native-dup-001",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=datetime.now(tz=timezone.utc),
            )
            # This should NOT raise despite the same (adapter, channel, msg_id) triple
            await temp_storage.store_native_ref(dup_ref)

            # Should still resolve to the same event
            resolved = await temp_storage.resolve_native_ref(
                adapter="matrix-dup",
                native_channel_id="!room:server",
                native_message_id="$native-dup-001",
            )
            assert resolved is not None
            assert resolved == event.event_id
        finally:
            await runner.stop()
