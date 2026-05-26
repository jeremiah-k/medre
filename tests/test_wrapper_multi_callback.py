"""Real Matrix/Meshtastic adapter wrapper callbacks with multiple messages.

Proves that:
1. MatrixAdapter._on_room_message processes 5 distinct nio events into 5
   canonical events with stable event_id->room_id mapping and no duplicates.
2. MatrixAdapter suppresses messages from its own user_id (self-message).
3. MeshtasticAdapter.simulate_inbound processes 5 packets into 5 canonical
   events with consistent packet metadata.
4. Detailed metadata consistency checks across 5 Meshtastic packets.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
    make_text_packet,
)
from tests.helpers.matrix import (
    make_matrix_config,
    make_nio_event,
    make_nio_room,
    to_event_dict,
)


class TestMatrixWrapperMultiCallback:
    """Real MatrixAdapter._on_room_message with 5 messages via mocked nio.
    Assert exact event count, receipt count, stable event_id->room_id
    mapping, no duplicates."""

    async def test_five_messages_via_on_room_message(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """5 distinct nio events -> 5 canonical events, 5 receipts, stable mapping."""
        matrix_adapter = MatrixAdapter(
            make_matrix_config(
                adapter_id="mx-multi",
                room_allowlist=["!multi_room:example.com"],
            )
        )
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="mx-multi-target")
        )

        route = Route(
            id="mx-multi-route",
            source=RouteSource(
                adapter="mx-multi",
                event_kinds=("message.created",),
                channel="!multi_room:example.com",
            ),
            targets=[RouteTarget(adapter="mx-multi-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mx-multi": matrix_adapter, "mx-multi-target": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await matrix_adapter.start(make_adapter_context("mx-multi", runner))
        await fake_target.start(make_adapter_context("mx-multi-target", runner))
        matrix_adapter._session._live_sync_started = True

        room = make_nio_room("!multi_room:example.com")

        try:
            # Inject 5 distinct messages
            for i in range(5):
                nio_event = make_nio_event(
                    sender=f"@user{i}:example.com",
                    event_id=f"$multi-evt-{i:03d}",
                    body=f"multi message {i}",
                )
                await matrix_adapter._on_room_message(to_event_dict(room, nio_event))

            # Exactly 5 canonical events
            all_events = await temp_storage._read_all(
                "SELECT event_id, source_channel_id FROM canonical_events ORDER BY event_id"
            )
            assert len(all_events) == 5

            # All events map to the same room
            for row in all_events:
                assert row["source_channel_id"] == "!multi_room:example.com"

            # All event IDs are unique (no duplicates)
            event_ids = [row["event_id"] for row in all_events]
            assert len(set(event_ids)) == 5

            # Exactly 5 delivery receipts
            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts ORDER BY sequence"
            )
            assert len(receipts) == 5
            for r in receipts:
                assert r["target_adapter"] == "mx-multi-target"
                assert r["status"] == "sent"

            # Fake target received exactly 5
            assert len(fake_target.delivered_payloads) == 5

            # Verify event_id -> room_id mapping via native refs
            for i in range(5):
                resolved = await temp_storage.resolve_native_ref(
                    adapter="mx-multi",
                    native_channel_id="!multi_room:example.com",
                    native_message_id=f"$multi-evt-{i:03d}",
                )
                assert resolved is not None, f"Native ref for event {i} not found"
        finally:
            await matrix_adapter.stop()
            await fake_target.stop()
            await runner.stop()

    async def test_self_message_suppressed_by_matrix_adapter(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """MatrixAdapter suppresses messages from its own user_id."""
        matrix_adapter = MatrixAdapter(make_matrix_config(adapter_id="mx-self"))

        route = Route(
            id="mx-self-route",
            source=RouteSource(
                adapter="mx-self",
                event_kinds=("message.created",),
                channel="!self_room:example.com",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mx-self": matrix_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await matrix_adapter.start(make_adapter_context("mx-self", runner))
        matrix_adapter._session._live_sync_started = True

        try:
            room = make_nio_room("!self_room:example.com")
            # Event from the bot's own user_id
            self_event = make_nio_event(
                sender="@bot:example.com",  # matches config.user_id
                event_id="$self-evt-001",
                body="this is from myself",
            )
            await matrix_adapter._on_room_message(to_event_dict(room, self_event))

            # No canonical event stored
            all_events = await temp_storage._read_all(
                "SELECT event_id FROM canonical_events"
            )
            assert len(all_events) == 0

            # Adapter counter incremented
            assert matrix_adapter._inbound_suppressed_self == 1
        finally:
            await matrix_adapter.stop()
            await runner.stop()


class TestMeshtasticWrapperMultiCallback:
    """Real MeshtasticAdapter.simulate_inbound with 5 packets.
    Assert exact event count, packet metadata consistency."""

    async def test_five_packets_via_simulate_inbound(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """5 distinct packets -> 5 canonical events, 5 receipts, consistent metadata."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        mesh_adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-multi-src", connection_type="fake")
        )
        fake_target = FakeMatrixAdapter("mesh-multi-dst", channel="!dst:fake")

        route = Route(
            id="mesh-multi-route",
            source=RouteSource(
                adapter="mesh-multi-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="mesh-multi-dst", channel="!dst:fake")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mesh-multi-src": mesh_adapter, "mesh-multi-dst": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(make_adapter_context("mesh-multi-src", runner))
        await fake_target.start(make_adapter_context("mesh-multi-dst", runner))

        try:
            # Inject 5 distinct packets
            for i in range(5):
                packet = make_text_packet(
                    text=f"multi mesh {i}",
                    sender=f"!node{i}",
                    channel=0,
                    packet_id=4000 + i,
                )
                await mesh_adapter.simulate_inbound(packet)

            # Exactly 5 canonical events
            all_events = await temp_storage._read_all(
                "SELECT event_id, source_transport_id, source_channel_id "
                "FROM canonical_events ORDER BY event_id"
            )
            assert len(all_events) == 5

            # Packet metadata consistency: all from channel 0
            for row in all_events:
                assert row["source_channel_id"] == "0"

            # All source_transport_ids are unique (one per node)
            transport_ids = {row["source_transport_id"] for row in all_events}
            assert len(transport_ids) == 5

            # Exactly 5 delivery receipts
            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts ORDER BY sequence"
            )
            assert len(receipts) == 5
            for r in receipts:
                assert r["target_adapter"] == "mesh-multi-dst"
                assert r["status"] == "sent"

            # Fake target received exactly 5
            assert len(fake_target.delivered_payloads) == 5

            # Native refs persisted for all 5
            for i in range(5):
                resolved = await temp_storage.resolve_native_ref(
                    adapter="mesh-multi-src",
                    native_channel_id="0",
                    native_message_id=str(4000 + i),
                )
                assert resolved is not None, f"Native ref for packet {i} not found"
        finally:
            await mesh_adapter.stop()
            await fake_target.stop()
            await runner.stop()

    async def test_packet_metadata_across_all_five(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Detailed metadata consistency checks across 5 packets."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        mesh_adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-meta-multi", connection_type="fake")
        )

        route = Route(
            id="mesh-meta-multi-route",
            source=RouteSource(
                adapter="mesh-meta-multi",
                event_kinds=("message.created",),
                channel="1",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mesh-meta-multi": mesh_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(make_adapter_context("mesh-meta-multi", runner))

        try:
            sender_id = "!consistent_node"
            for i in range(5):
                packet = make_text_packet(
                    text=f"consistent msg {i}",
                    sender=sender_id,
                    channel=1,
                    packet_id=5000 + i,
                )
                await mesh_adapter.simulate_inbound(packet)

            # Verify all 5 events stored in storage
            all_events = await temp_storage._read_all(
                "SELECT event_id, source_adapter, source_transport_id, "
                "source_channel_id FROM canonical_events ORDER BY event_id"
            )
            assert len(all_events) == 5

            # All have the same source adapter and transport ID
            for row in all_events:
                assert row["source_adapter"] == "mesh-meta-multi"
                assert row["source_transport_id"] == sender_id
                assert row["source_channel_id"] == "1"

            # All have unique event IDs
            event_ids = [row["event_id"] for row in all_events]
            assert len(set(event_ids)) == 5

            # All native refs persisted
            for i in range(5):
                resolved = await temp_storage.resolve_native_ref(
                    adapter="mesh-meta-multi",
                    native_channel_id="1",
                    native_message_id=str(5000 + i),
                )
                assert resolved is not None
        finally:
            await mesh_adapter.stop()
            await runner.stop()
