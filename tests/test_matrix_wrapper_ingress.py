"""Real MatrixAdapter wrapper callback ingress tests.

Proves that the real ``MatrixAdapter._on_room_message`` callback decodes nio
events, publishes them through the pipeline, and delivers to fake targets.
Uses mocked nio SDK — no live Matrix connection required.

No Docker, no live transports, no SDK dependencies required.
"""

# ruff: noqa: F811

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.bridge import make_adapter_context, make_pipeline_config
from tests.helpers.matrix import (  # noqa: F401
    make_matrix_config,
    make_nio_event,
    make_nio_room,
    mock_nio,
)


class TestMatrixWrapperCallbackPath:
    """Real MatrixAdapter._on_room_message → publish_inbound → pipeline →
    fake target.  Uses mocked nio SDK."""

    async def test_on_room_message_routes_to_fake_target(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """_on_room_message decodes nio event, publishes through pipeline,
        and delivers to fake target."""
        matrix_adapter = MatrixAdapter(make_matrix_config(adapter_id="matrix-cb"))
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="fake-cb-target")
        )

        route = Route(
            id="matrix-cb-route",
            source=RouteSource(
                adapter="matrix-cb",
                event_kinds=("message.created",),
                channel="!cb_room:example.com",
            ),
            targets=[RouteTarget(adapter="fake-cb-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"matrix-cb": matrix_adapter, "fake-cb-target": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx = make_adapter_context("matrix-cb", runner)
        await matrix_adapter.start(ctx)
        await fake_target.start(make_adapter_context("fake-cb-target", runner))
        matrix_adapter._session._live_sync_started = True

        try:
            room = make_nio_room("!cb_room:example.com")
            event = make_nio_event(
                sender="@alice:example.com",
                event_id="$cb-evt-001",
                body="callback test",
            )
            await matrix_adapter._on_room_message(room, event)

            # Fake target received rendered payload
            assert len(fake_target.delivered_payloads) == 1
            rendered = fake_target.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            # TextRenderer extracts from payload["text"]; MatrixCodec puts
            # text in payload["body"], so the rendered text key is "text"
            assert rendered.payload.get("text") is not None

            # Delivery receipt persisted
            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts"
            )
            assert len(receipts) == 1
            assert receipts[0]["status"] == "sent"
        finally:
            await matrix_adapter.stop()
            await fake_target.stop()
            await runner.stop()

    async def test_pre_start_matrix_event_is_dropped_before_pipeline(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """Historical Matrix sync events must not be stored, deduped, or routed."""
        matrix_adapter = MatrixAdapter(make_matrix_config(adapter_id="matrix-stale-cb"))
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="fake-stale-target")
        )

        route = Route(
            id="matrix-stale-route",
            source=RouteSource(
                adapter="matrix-stale-cb",
                event_kinds=("message.created",),
                channel="!stale_room:example.com",
            ),
            targets=[RouteTarget(adapter="fake-stale-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={
                "matrix-stale-cb": matrix_adapter,
                "fake-stale-target": fake_target,
            },
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await matrix_adapter.start(make_adapter_context("matrix-stale-cb", runner))
        await fake_target.start(make_adapter_context("fake-stale-target", runner))
        matrix_adapter._session._live_sync_started = True

        try:
            room = make_nio_room("!stale_room:example.com")
            event = make_nio_event(
                sender="@alice:example.com",
                event_id="$stale-evt-001",
                body="historical message",
            )
            event.source["origin_server_ts"] = int(
                datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
            )

            await matrix_adapter._on_room_message(room, event)

            assert fake_target.delivered_payloads == []
            assert await temp_storage.count_events() == 0
            assert matrix_adapter._stale_events_dropped == 1
        finally:
            await matrix_adapter.stop()
            await fake_target.stop()
            await runner.stop()

    async def test_room_id_event_id_mapping_to_native_refs(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """room_id and event_id from nio event map to native_channel_id
        and native_message_id on the canonical event."""
        matrix_adapter = MatrixAdapter(make_matrix_config(adapter_id="matrix-nref-cb"))

        route = Route(
            id="nref-cb-route",
            source=RouteSource(
                adapter="matrix-nref-cb",
                event_kinds=("message.created",),
                channel="!nref_room:example.com",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"matrix-nref-cb": matrix_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx = make_adapter_context("matrix-nref-cb", runner)
        await matrix_adapter.start(ctx)
        matrix_adapter._session._live_sync_started = True

        try:
            room = make_nio_room("!nref_room:example.com")
            event = make_nio_event(
                sender="@bob:example.com",
                event_id="$nref-cb-evt-001",
                body="native ref mapping test",
            )
            await matrix_adapter._on_room_message(room, event)

            # Inbound native ref persisted
            resolved = await temp_storage.resolve_native_ref(
                adapter="matrix-nref-cb",
                native_channel_id="!nref_room:example.com",
                native_message_id="$nref-cb-evt-001",
            )
            assert resolved is not None

            # Stored event has correct source metadata
            stored = await temp_storage.get(resolved)
            assert stored is not None
            assert stored.source_channel_id == "!nref_room:example.com"
            assert stored.source_native_ref is not None
            assert stored.source_native_ref.native_message_id == "$nref-cb-evt-001"
            assert (
                stored.source_native_ref.native_channel_id == "!nref_room:example.com"
            )
        finally:
            await matrix_adapter.stop()
            await runner.stop()

    async def test_matrix_originated_event_reaches_fake_meshtastic(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """Full bridge: Matrix _on_room_message → pipeline → fake
        Meshtastic adapter delivery."""
        matrix_adapter = MatrixAdapter(make_matrix_config(adapter_id="mx-bridge-src"))
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-bridge-dst")
        )

        route = Route(
            id="mx-to-mesh-bridge",
            source=RouteSource(
                adapter="mx-bridge-src",
                event_kinds=("message.created",),
                channel="!bridge_room:example.com",
            ),
            targets=[RouteTarget(adapter="mesh-bridge-dst", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mx-bridge-src": matrix_adapter, "mesh-bridge-dst": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await matrix_adapter.start(make_adapter_context("mx-bridge-src", runner))
        await fake_mesh.start(make_adapter_context("mesh-bridge-dst", runner))
        matrix_adapter._session._live_sync_started = True

        try:
            room = make_nio_room("!bridge_room:example.com")
            event = make_nio_event(
                sender="@carol:example.com",
                event_id="$bridge-evt-002",
                body="bridge to mesh",
            )
            await matrix_adapter._on_room_message(room, event)

            # Fake meshtastic adapter received delivery
            assert len(fake_mesh.delivered_payloads) == 1
            rendered = fake_mesh.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            assert rendered.event_id is not None

            # Receipt
            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts"
            )
            assert len(receipts) == 1
            assert receipts[0]["target_adapter"] == "mesh-bridge-dst"
            assert receipts[0]["status"] == "sent"

            # Canonical event stored with matrix source
            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )
            assert len(events) == 1
            assert events[0]["source_adapter"] == "mx-bridge-src"
        finally:
            await matrix_adapter.stop()
            await fake_mesh.stop()
            await runner.stop()
