"""Matrix boundary enforcement tests: architectural separation between
core, rendering, and adapter layers; inbound/outbound correlation;
reply resolution; and delivery contract.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from medre.adapters import FakeMatrixAdapter, FakePresentationAdapter
from medre.adapters.base import AdapterDeliveryResult
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.rendering.renderer import RenderingResult


class TestMatrixBoundaries:
    """Architectural boundary enforcement for Matrix components."""

    def test_core_does_not_import_matrix(self) -> None:
        """medre.core should not import medre.adapters.matrix at module level."""
        # Import core and check matrix adapter modules are not loaded
        import medre.core  # noqa: F401

        # No core module should contain "matrix" in its name at all.
        # MatrixRenderer is now owned by the adapter package.
        core_modules = [k for k in sys.modules if k.startswith("medre.core.") and "matrix" in k]
        assert len(core_modules) == 0, (
            f"Core modules must not reference matrix: {core_modules}"
        )

    def test_matrix_does_not_import_other_adapters(self) -> None:
        """Matrix adapter package does not import other adapter modules."""
        from medre.adapters import matrix as matrix_pkg  # noqa: F401

        # Verify the MatrixAdapter class exists but doesn't trigger
        # imports of meshtastic or other adapter packages
        other_adapters = [
            k for k in sys.modules
            if "medre.adapters" in k
            and "meshtastic" in k
        ]
        assert len(other_adapters) == 0, (
            f"Matrix adapter triggered import of other adapters: {other_adapters}"
        )

    def test_matrix_adapter_does_not_route(self) -> None:
        """FakeMatrixAdapter has no route matching or routing methods."""
        adapter = FakeMatrixAdapter("m")
        assert not hasattr(adapter, "match")
        assert not hasattr(adapter, "route")

    def test_matrix_renderer_does_not_deliver(self) -> None:
        """MatrixRenderer has no deliver method."""
        renderer = MatrixRenderer()
        assert not hasattr(renderer, "deliver")

    def test_matrix_codec_does_not_route_or_plan(self) -> None:
        """MatrixCodec has decode/encode but no route/match/plan methods."""
        config = MatrixConfig(
            adapter_id="test",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            access_token="tok",
        )
        codec = MatrixCodec("test", config)
        assert hasattr(codec, "decode")
        assert hasattr(codec, "encode")
        assert not hasattr(codec, "route")
        assert not hasattr(codec, "match")
        assert not hasattr(codec, "plan")

    def test_matrix_renderer_lives_outside_core(self) -> None:
        """MatrixRenderer is in the adapter package, not core."""
        from medre.adapters.matrix.renderer import MatrixRenderer
        assert "adapters.matrix" in MatrixRenderer.__module__

    def test_matrix_codec_does_not_render_outbound_payloads(self) -> None:
        """MatrixCodec.encode() raises NotImplementedError."""
        config = MatrixConfig(
            adapter_id="test",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            access_token="tok",
        )
        codec = MatrixCodec("test", config)
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="transport",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        with pytest.raises(NotImplementedError, match="MatrixRenderer"):
            codec.encode(event, target=None)

    async def test_fake_matrix_rejects_raw_canonical_event(self) -> None:
        """FakeMatrixAdapter.deliver raises TypeError for CanonicalEvent."""
        adapter = FakeMatrixAdapter("m")
        event = CanonicalEvent(
            event_id="evt-raw",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "raw"},
            metadata=EventMetadata(),
        )
        with pytest.raises(TypeError, match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_fake_presentation_rejects_raw_canonical_event(self) -> None:
        """FakePresentationAdapter.deliver raises TypeError for CanonicalEvent."""
        adapter = FakePresentationAdapter("p")
        event = CanonicalEvent(
            event_id="evt-raw",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "raw"},
            metadata=EventMetadata(),
        )
        with pytest.raises(TypeError, match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_outbound_native_ref_uses_adapter_result_id(self) -> None:
        """Outbound native ref uses the adapter's actual result ID, not synthetic."""
        adapter = FakeMatrixAdapter("fake_matrix")
        result = RenderingResult(
            event_id="evt-out-001",
            target_adapter="fake_matrix",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
            metadata={"renderer": "matrix"},
        )
        delivery = await adapter.deliver(result)
        assert isinstance(delivery, AdapterDeliveryResult)
        # Must use the adapter-provided ID, not a synthetic one
        assert delivery.native_message_id == "$fake_evt-out-001"
        assert delivery.native_channel_id == "!room:server"

    async def test_failed_matrix_delivery_does_not_store_outbound_ref(
        self,
        temp_storage,
    ) -> None:
        """Failed Matrix delivery must not store an outbound native ref."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing import Route, RouteSource, RouteTarget, Router

        adapter = FaultyPresentationAdapter(
            adapter_id="failing_matrix", failure_mode="always_fail",
        )
        route = Route(
            id="fail-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="failing_matrix")],
        )
        router = Router(routes=[route])
        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"failing_matrix": adapter},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = CanonicalEvent(
            event_id="fail-out-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "will fail"},
            metadata=EventMetadata(),
        )

        try:
            await runner.handle_ingress(event)
            # No outbound native ref stored
            rows = await temp_storage._read_all(
                "SELECT * FROM native_message_refs WHERE event_id = ? AND direction = 'outbound'",
                ("fail-out-001",),
            )
            assert len(rows) == 0
        finally:
            await runner.stop()
