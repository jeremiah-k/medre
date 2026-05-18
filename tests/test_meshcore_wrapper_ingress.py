"""Real MeshCoreAdapter wrapper callback ingress tests.

Proves that the real ``MeshCoreAdapter.simulate_inbound`` callback decodes
MeshCore channel packets, publishes them through the pipeline, and delivers
to fake targets.  Uses a fake connection type — no live MeshCore hardware
required.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage.sqlite import SQLiteStorage

from tests.helpers.bridge import (
    make_adapter_context,
    make_meshcore_packet,
    make_pipeline_config,
)


class TestMeshCoreWrapperCallbackPath:
    """Real MeshCoreAdapter.simulate_inbound → codec → publish_inbound →
    pipeline → fake target."""

    async def test_text_message_routes_to_fake_target(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MeshCoreAdapter.simulate_inbound decodes a channel text packet,
        publishes through pipeline, and delivers to fake target."""
        meshcore_adapter = MeshCoreAdapter(
            MeshCoreConfig(
                adapter_id="mc-cb-src",
                connection_type="fake",
            )
        )
        fake_target = FakeMatrixAdapter(
            "fake-mx-dst", channel="!dst:fake"
        )

        route = Route(
            id="mc-cb-route",
            source=RouteSource(
                adapter="mc-cb-src",
                event_kinds=("message.created",),
                channel="1",
            ),
            targets=[RouteTarget(adapter="fake-mx-dst", channel="!dst:fake")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mc-cb-src": meshcore_adapter, "fake-mx-dst": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await meshcore_adapter.start(make_adapter_context("mc-cb-src", runner))
        await fake_target.start(make_adapter_context("fake-mx-dst", runner))

        try:
            packet = make_meshcore_packet(
                text="hello meshcore bridge",
                sender="abc123",
                channel=1,
                packet_id=1001,
            )
            await meshcore_adapter.simulate_inbound(packet)

            # Canonical event persisted in storage
            events = await temp_storage._read_all(
                "SELECT source_adapter, source_channel_id FROM canonical_events"
            )
            assert len(events) == 1
            assert events[0]["source_adapter"] == "mc-cb-src"
            assert events[0]["source_channel_id"] == "1"

            # Fake target received delivery
            assert len(fake_target.delivered_payloads) == 1
            rendered = fake_target.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            assert rendered.payload.get("text") is not None

            # Delivery receipt status == "sent"
            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts"
            )
            assert len(receipts) == 1
            assert receipts[0]["status"] == "sent"

            # Accounting: outbound_delivered >= 1
            assert accounting.counters().outbound_delivered >= 1

            # Native ref persisted (sender_timestamp maps to native_message_id)
            resolved = await temp_storage.resolve_native_ref(
                adapter="mc-cb-src",
                native_channel_id="1",
                native_message_id="1001",
            )
            assert resolved is not None
        finally:
            await meshcore_adapter.stop()
            await fake_target.stop()
            await runner.stop()

    async def test_unsupported_packet_ignored(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """An ACK packet (with ``code`` key) is filtered out by the
        classifier and never enters the pipeline."""
        meshcore_adapter = MeshCoreAdapter(
            MeshCoreConfig(
                adapter_id="mc-ack-src",
                connection_type="fake",
            )
        )

        route = Route(
            id="mc-ack-route",
            source=RouteSource(
                adapter="mc-ack-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mc-ack-src": meshcore_adapter},
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await meshcore_adapter.start(make_adapter_context("mc-ack-src", runner))

        try:
            # ACK packet with ``code`` key — classifier returns is_ack=True
            ack_packet: dict = {
                "code": 3,
                "pubkey_prefix": "abc123",
                "sender_timestamp": 2002,
            }
            await meshcore_adapter.simulate_inbound(ack_packet)

            # No canonical event stored
            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )
            assert len(events) == 0

            # No delivery receipts
            receipts = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts"
            )
            assert len(receipts) == 0

            # inbound_accepted == 0 (packet was filtered before codec)
            assert accounting.counters().inbound_accepted == 0
        finally:
            await meshcore_adapter.stop()
            await runner.stop()

    async def test_duplicate_native_ref_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Sending the same packet twice (same sender_timestamp) results in
        only one canonical event — the second is suppressed by the duplicate
        native ref guard."""
        meshcore_adapter = MeshCoreAdapter(
            MeshCoreConfig(
                adapter_id="mc-dup-src",
                connection_type="fake",
            )
        )

        route = Route(
            id="mc-dup-route",
            source=RouteSource(
                adapter="mc-dup-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mc-dup-src": meshcore_adapter},
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await meshcore_adapter.start(make_adapter_context("mc-dup-src", runner))

        try:
            packet = make_meshcore_packet(
                text="dup test",
                sender="dup123",
                channel=0,
                packet_id=3003,
            )

            # First send: accepted
            await meshcore_adapter.simulate_inbound(packet)

            # Second send: same sender_timestamp — suppressed
            await meshcore_adapter.simulate_inbound(packet)

            # Only 1 canonical event in storage
            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )
            assert len(events) == 1
            assert events[0]["source_adapter"] == "mc-dup-src"

            # loop_prevented >= 1
            assert accounting.counters().loop_prevented >= 1

            # inbound_accepted == 1 (only first accepted)
            assert accounting.counters().inbound_accepted == 1
        finally:
            await meshcore_adapter.stop()
            await runner.stop()

    async def test_malformed_packet_does_not_crash(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A malformed packet (missing required fields like pubkey_prefix)
        is handled gracefully — no crash, no canonical event stored for
        unclassifiable packets."""
        meshcore_adapter = MeshCoreAdapter(
            MeshCoreConfig(
                adapter_id="mc-malformed-src",
                connection_type="fake",
            )
        )

        route = Route(
            id="mc-malformed-route",
            source=RouteSource(
                adapter="mc-malformed-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mc-malformed-src": meshcore_adapter},
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await meshcore_adapter.start(
            make_adapter_context("mc-malformed-src", runner)
        )

        try:
            # Empty dict — classifier returns "unknown" category, not "text"
            # so simulate_inbound returns early before codec
            await meshcore_adapter.simulate_inbound({})

            # Packet with text but missing pubkey_prefix — classifier says
            # "text" so it passes to codec.  Codec tolerates missing
            # pubkey_prefix (sender defaults to "").
            minimal_packet = {"text": "just text"}
            await meshcore_adapter.simulate_inbound(minimal_packet)

            # Neither should crash the adapter.  The empty dict is filtered
            # by classifier (category="unknown").  The text-only dict passes
            # classifier and codec (sender defaults to ""), producing an event.
            # The key assertion: no exception was raised.
            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )

            # The empty dict produced no event; the text-only dict produced
            # one event (codec tolerates missing pubkey_prefix).
            assert len(events) == 1

            # Only the text-only packet incremented inbound_accepted.
            assert accounting.counters().inbound_accepted == 1
        finally:
            await meshcore_adapter.stop()
            await runner.stop()

    async def test_meshcore_inbound_reaches_fake_matrix(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Full bridge: MeshCore simulate_inbound → pipeline → fake
        Matrix adapter delivery with receipt verification."""
        meshcore_adapter = MeshCoreAdapter(
            MeshCoreConfig(
                adapter_id="mc-bridge-src",
                connection_type="fake",
            )
        )
        fake_mx = FakeMatrixAdapter(
            "mx-bridge-dst", channel="!bridge-dst:fake"
        )

        route = Route(
            id="mc-to-mx-bridge",
            source=RouteSource(
                adapter="mc-bridge-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[
                RouteTarget(adapter="mx-bridge-dst", channel="!bridge-dst:fake")
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"mc-bridge-src": meshcore_adapter, "mx-bridge-dst": fake_mx},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await meshcore_adapter.start(
            make_adapter_context("mc-bridge-src", runner)
        )
        await fake_mx.start(make_adapter_context("mx-bridge-dst", runner))

        try:
            packet = make_meshcore_packet(
                text="meshcore to matrix",
                sender="bridge_peer",
                channel=0,
                packet_id=55555,
            )
            await meshcore_adapter.simulate_inbound(packet)

            # Fake matrix adapter received delivery
            assert len(fake_mx.delivered_payloads) == 1
            rendered = fake_mx.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            assert rendered.event_id is not None

            # Receipt
            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts"
            )
            assert len(receipts) == 1
            assert receipts[0]["target_adapter"] == "mx-bridge-dst"
            assert receipts[0]["status"] == "sent"

            # Canonical event has meshcore source
            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )
            assert len(events) == 1
            assert events[0]["source_adapter"] == "mc-bridge-src"
        finally:
            await meshcore_adapter.stop()
            await fake_mx.stop()
            await runner.stop()
