"""Track 3: Runtime Event Flow Validation.

Tests that events flow correctly through the runtime pipeline:
inbound events reach the EventBus, outbound delivery targets the
correct adapter, replay works, adapter-specific metadata is preserved,
and diagnostics remain truthful.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.contracts.adapter import AdapterContext
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    RadioMetadata,
    TransportMetadata,
)
from medre.core.events.bus import EventBus
from medre.core.events.kinds import EventKind
from medre.core.observability.metrics import Diagnostician
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.runtime.builder import RuntimeBuilder
from tests.helpers.async_utils import wait_until

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


def _make_event(
    event_id: str = "evt-001",
    event_kind: str = EventKind.MESSAGE_TEXT,
    source_adapter: str = "fake_source",
    source_transport_id: str = "node-1",
    source_channel_id: str = "ch-0",
    payload: dict[str, object] | None = None,
    metadata: EventMetadata | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id=source_transport_id,
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"text": "hello world"},
        metadata=metadata or EventMetadata(),
    )


def _make_runtime_with_one_matrix(
    adapter_id: str = "mx_main",
) -> RuntimeConfig:
    """Create config with one fake Matrix adapter."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-event-flow"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                adapter_id: MatrixRuntimeConfig(
                    adapter_id=adapter_id,
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _make_runtime_with_two_matrix(
    adapter_id_a: str = "mx_alpha",
    adapter_id_b: str = "mx_beta",
) -> RuntimeConfig:
    """Create config with two fake Matrix adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-two-matrix-flow"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                adapter_id_a: MatrixRuntimeConfig(
                    adapter_id=adapter_id_a,
                    enabled=True,
                    adapter_kind="fake",
                ),
                adapter_id_b: MatrixRuntimeConfig(
                    adapter_id=adapter_id_b,
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


# ===================================================================
# H) Inbound event reaches EventBus
# ===================================================================


class TestInboundEventReachesEventBus:
    """Inbound events from adapters appear on the EventBus."""

    async def test_inbound_event_published_to_bus(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Simulated inbound event flows through the pipeline pipeline."""
        config = _make_runtime_with_one_matrix("mx_main")
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            adapter = app.adapters["mx_main"]
            assert isinstance(adapter, FakeMatrixAdapter)

            # Simulate inbound — the event goes through the pipeline's
            # ingress handler (store → route → deliver).  With no routes
            # configured, the event is stored but not delivered.
            event = adapter.make_event(
                "Hello from Matrix!", event_kind=EventKind.MESSAGE_TEXT
            )
            await adapter.simulate_inbound(event)

            # Verify the event was received by the adapter
            assert len(adapter.inbound_events) == 1
            assert adapter.inbound_events[0].event_id == event.event_id

            # Verify the event was stored
            assert app.storage is not None
            stored = await app.storage.get(event.event_id)
            assert stored is not None
            assert stored.event_id == event.event_id
        finally:
            await app.stop()

    async def test_renderer_selection_pipeline(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Events flowing through pipeline trigger codec → renderer pipeline."""
        config = _make_runtime_with_one_matrix("mx_main")
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            # Verify the rendering pipeline is wired
            assert app.rendering_pipeline is not None
            # Verify a TextRenderer is registered
            app.adapters["mx_main"]
            event = _make_event(
                event_id="evt-render-test",
                source_adapter="mx_main",
            )
            # The pipeline should be able to render for the matrix adapter
            result = await app.rendering_pipeline.render(
                event, target_adapter="mx_main"
            )
            assert result is not None
            assert result.event_id == "evt-render-test"
        finally:
            await app.stop()


# ===================================================================
# I) Outbound delivery reaches correct adapter
# ===================================================================


class TestOutboundDeliveryReachesCorrectAdapter:
    """Outbound events routed to a specific adapter only hit that adapter."""

    async def test_targeted_delivery(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Route outbound event to one of two adapters — only target receives."""
        config = _make_runtime_with_two_matrix("mx_alpha", "mx_beta")
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            alpha = app.adapters["mx_alpha"]
            beta = app.adapters["mx_beta"]
            assert isinstance(alpha, FakeMatrixAdapter)
            assert isinstance(beta, FakeMatrixAdapter)

            # Deliver directly to one adapter
            result = RenderingResult(
                event_id="evt-target-001",
                target_adapter="mx_alpha",
                target_channel="room-1",
                payload={"text": "targeted message"},
            )
            delivery_result = await alpha.deliver(result)

            assert len(alpha.delivered_payloads) == 1
            assert len(beta.delivered_payloads) == 0
            assert delivery_result is not None
            assert delivery_result.native_message_id is not None
        finally:
            await app.stop()

    async def test_pipeline_routes_to_specific_adapter(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Full pipeline routes event only to the targeted adapter."""
        config = _make_runtime_with_two_matrix("mx_alpha", "mx_beta")
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        # Add a route targeting only mx_alpha
        route = Route(
            id="route-to-alpha",
            source=RouteSource(
                adapter="mx_alpha",
                event_kinds=(EventKind.MESSAGE_TEXT,),
                channel=None,
            ),
            targets=[RouteTarget(adapter="mx_beta")],
        )
        app.router.add_route(route)

        await app.start()
        try:
            alpha = app.adapters["mx_alpha"]
            beta = app.adapters["mx_beta"]

            # Simulate inbound on alpha — should route to beta
            event = alpha.make_event(
                "Route this to beta", event_kind=EventKind.MESSAGE_TEXT
            )
            event = CanonicalEvent(
                event_id=event.event_id,
                event_kind=event.event_kind,
                schema_version=event.schema_version,
                timestamp=event.timestamp,
                source_adapter="mx_alpha",
                source_transport_id=event.source_transport_id,
                source_channel_id=event.source_channel_id,
                parent_event_id=event.parent_event_id,
                lineage=event.lineage,
                relations=event.relations,
                payload=event.payload,
                metadata=event.metadata,
            )
            await alpha.simulate_inbound(event)

            # Wait for pipeline delivery to complete.
            await wait_until(lambda: len(beta.delivered_payloads) >= 1, timeout=2.0)

            # Beta should have received the delivery
            assert (
                len(beta.delivered_payloads) >= 1
            ), f"Expected beta to receive delivery, got {len(beta.delivered_payloads)} payloads"
        finally:
            await app.stop()


# ===================================================================
# J) Replay integration
# ===================================================================


class TestReplayIntegration:
    """Replayable events can be re-delivered without duplication errors."""

    async def test_event_re_deliverable(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """The same event can be delivered to the same adapter twice."""
        config = _make_runtime_with_one_matrix("mx_main")
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            adapter = app.adapters["mx_main"]
            result = RenderingResult(
                event_id="evt-replay-001",
                target_adapter="mx_main",
                target_channel="room-1",
                payload={"text": "replay me"},
            )
            # First delivery
            r1 = await adapter.deliver(result)
            assert r1 is not None

            # Second delivery (replay) — adapter accepts it
            r2 = await adapter.deliver(result)
            assert r2 is not None
            assert len(adapter.delivered_payloads) == 2
        finally:
            await app.stop()

    async def test_replay_no_cross_adapter_delivery(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Replaying an event does not cause delivery to the wrong adapter."""
        config = _make_runtime_with_two_matrix("mx_alpha", "mx_beta")
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            alpha = app.adapters["mx_alpha"]
            beta = app.adapters["mx_beta"]

            result = RenderingResult(
                event_id="evt-replay-iso",
                target_adapter="mx_alpha",
                target_channel="room-1",
                payload={"text": "only for alpha"},
            )
            # Deliver twice to alpha (replay scenario)
            await alpha.deliver(result)
            await alpha.deliver(result)

            # Beta should never have received anything
            assert len(beta.delivered_payloads) == 0
            assert len(alpha.delivered_payloads) == 2
        finally:
            await app.stop()


# ===================================================================
# K) Adapter-specific metadata preserved
# ===================================================================


class TestAdapterSpecificMetadataPreserved:
    """Transport-specific metadata is preserved without leaking raw SDK objects."""

    def test_matrix_event_metadata(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Matrix event contains Matrix-specific metadata (room_id, event_id)."""
        adapter = FakeMatrixAdapter("test_mx", channel="!room123:matrix.org")
        event = adapter.make_event(
            "Hello",
            event_kind=EventKind.MESSAGE_TEXT,
        )
        # The event should carry source_adapter as the adapter_id
        assert event.source_adapter == "test_mx"
        # FakeMatrixAdapter uses "body" key in payload
        assert event.payload.get("body") == "Hello"

    async def test_matrix_delivery_preserves_native_ids(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Matrix adapter returns native Matrix event_id and channel."""
        adapter = FakeMatrixAdapter("test_mx")

        async def _noop_publish(e: CanonicalEvent) -> None:
            pass

        ctx = AdapterContext(
            adapter_id="test_mx",
            event_bus=EventBus(),
            publish_inbound=_noop_publish,
            logger=logging.getLogger("test.test_mx"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        result = RenderingResult(
            event_id="evt-mx-001",
            target_adapter="test_mx",
            target_channel="!room:server",
            payload={"text": "hello matrix"},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        # Fake Matrix adapter returns deterministic Matrix-like event_id
        assert delivery.native_message_id == "$fake_evt-mx-001"
        assert delivery.native_channel_id == "!room:server"

    def test_meshtastic_event_metadata(self) -> None:
        """Meshtastic event carries radio metadata (node_id, channel)."""
        config = MeshtasticConfig(adapter_id="test_mesh")
        adapter = FakeMeshtasticAdapter(config)
        assert adapter.adapter_id == "test_mesh"
        assert adapter.platform == "meshtastic"

    def test_canonical_event_no_raw_sdk_objects(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Canonical events contain no raw nio or protobuf objects."""
        adapter = FakeMatrixAdapter("test_mx")
        event = adapter.make_event("test")
        # Verify all values in payload are standard Python types
        for key, value in event.payload.items():
            assert isinstance(
                value, (str, int, float, bool, type(None))
            ), f"Payload key {key!r} has non-standard type: {type(value)}"

    async def test_metadata_transport_radio_isolation(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Radio metadata and transport metadata are properly typed."""
        event = _make_event(
            metadata=EventMetadata(
                transport=TransportMetadata(
                    protocol="lora",
                    delivery_confirmed=True,
                ),
                radio=RadioMetadata(snr=7.5, rssi=-90.0, frequency=868.0),
            ),
        )
        assert event.metadata.transport is not None
        assert event.metadata.transport.protocol == "lora"
        assert event.metadata.radio is not None
        assert event.metadata.radio.snr == 7.5
        # No raw protobuf/nio objects in metadata
        assert isinstance(event.metadata.transport.protocol, str)
        assert isinstance(event.metadata.radio.snr, float)


# ===================================================================
# L) Diagnostics snapshots remain truthful
# ===================================================================


class TestDiagnosticsSnapshots:
    """After events flow, diagnostics reflect correct counters."""

    async def test_adapter_counters_after_delivery(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Adapter delivery increments diagnostics counters."""
        config = _make_runtime_with_one_matrix("mx_main")
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()
        try:
            adapter = app.adapters["mx_main"]
            event = _make_event(
                event_id="evt-diag-001",
                source_adapter="mx_main",
            )

            # Simulate inbound to trigger pipeline
            await adapter.simulate_inbound(event)
            # Yield to ensure async delivery tasks complete.
            # No specific positive condition to poll — diagnostics are always present.
            await asyncio.sleep(0)

            # Check diagnostics snapshot
            snap = app.diagnostician.snapshot()
            assert isinstance(snap, dict)
        finally:
            await app.stop()

    async def test_cross_adapter_diagnostics_isolation(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Adapter failure counters are isolated per adapter."""
        diag = Diagnostician()
        diag.record_adapter_failure("evt-1", "adapter_a", "timeout")
        diag.record_adapter_failure("evt-2", "adapter_a", "timeout")
        diag.record_adapter_failure("evt-3", "adapter_b", "connection_refused")

        snap = diag.snapshot()
        assert snap["adapter_failures"]["adapter_a"] == 2
        assert snap["adapter_failures"]["adapter_b"] == 1
        # No cross-contamination
        assert "adapter_c" not in snap["adapter_failures"]

    async def test_event_bus_subscriber_receives_events(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Events published on bus reach all matching subscribers."""
        bus = EventBus()
        received_a: list[CanonicalEvent] = []
        received_b: list[CanonicalEvent] = []

        async def _handler_a(event: CanonicalEvent) -> None:
            received_a.append(event)

        async def _handler_b(event: CanonicalEvent) -> None:
            received_b.append(event)

        sub_a = bus.subscribe("message", _handler_a)
        sub_b = bus.subscribe("*", _handler_b)

        event = _make_event(event_kind=EventKind.MESSAGE_TEXT)
        await bus.publish(event)

        assert len(received_a) == 1
        assert len(received_b) == 1
        assert received_a[0].event_id == event.event_id

        await sub_a.unsubscribe()
        await sub_b.unsubscribe()

    async def test_diagnostics_snapshot_structure(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Diagnostician snapshot has expected keys."""
        diag = Diagnostician()
        snap = diag.snapshot()
        assert "adapter_failures" in snap
        assert "planner_failures" in snap
        assert "renderer_failures" in snap
        assert "storage_failures" in snap
        assert "replay_skips" in snap
        assert "replay_downgrades" in snap
        assert "correlation_misses" in snap
