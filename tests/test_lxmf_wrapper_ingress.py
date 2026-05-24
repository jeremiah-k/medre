"""LXMF wrapper callback bridge ingress tests.

Proves that ``LxmfAdapter._on_packet`` and ``simulate_inbound`` can receive an
inbound LXMF payload, decode it, and route through the pipeline to a fake
outbound adapter.  Closes the evidence gap for LXMF wrapper callback ingress.

Uses ``LxmfAdapter`` in ``"fake"`` connection mode — no Reticulum or LXMF
packages required.
"""

from __future__ import annotations

import asyncio

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.bridge import make_adapter_context, make_pipeline_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_lxmf_text_packet(
    content: str = "hello from lxmf",
    source_hash: str = "aabbccdd11223344",
    message_id: str = "msg-0001",
    title: str = "",
    fields: dict | None = None,
    destination_hash: str = "eeff001122334455",
    delivery_method: str = "direct",
    timestamp: int | None = 1700000000,
) -> dict:
    """Build a valid LXMF text packet dict matching codec/classifier expectations."""
    packet: dict = {
        "source_hash": source_hash,
        "content": content,
        "message_id": message_id,
        "destination_hash": destination_hash,
        "delivery_method": delivery_method,
    }
    if title:
        packet["title"] = title
    if fields is not None:
        packet["fields"] = fields
    if timestamp is not None:
        packet["timestamp"] = timestamp
    return packet


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLxmfWrapperCallbackIngress:
    """LxmfAdapter callback ingress → pipeline → fake target."""

    async def test_text_message_routes_to_fake_target(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """simulate_inbound decodes LXMF text packet, routes through pipeline,
        and delivers to fake Meshtastic target."""
        lxmf_adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lxmf-cb", connection_type="fake")
        )
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="fake-cb-target")
        )

        route = Route(
            id="lxmf-to-fake",
            source=RouteSource(
                adapter="lxmf-cb",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="fake-cb-target", channel="0")],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"lxmf-cb": lxmf_adapter, "fake-cb-target": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await lxmf_adapter.start(make_adapter_context("lxmf-cb", runner))
        await fake_target.start(make_adapter_context("fake-cb-target", runner))

        try:
            packet = make_lxmf_text_packet(
                content="callback bridge test",
                source_hash="aa11bb22",
                message_id="cb-evt-001",
            )
            await lxmf_adapter.simulate_inbound(packet)

            # Fake target received rendered payload
            assert len(fake_target.delivered_payloads) == 1
            rendered = fake_target.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)

            # Delivery receipt persisted with status "sent"
            receipts = await temp_storage._read_all(
                "SELECT target_adapter, status FROM delivery_receipts"
            )
            assert len(receipts) == 1
            assert receipts[0]["status"] == "sent"

            # Accounting tracked the outbound delivery
            snapshot = accounting.snapshot()
            assert snapshot["outbound_delivered"] >= 1

            # Canonical event stored with correct source
            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )
            assert len(events) == 1
            assert events[0]["source_adapter"] == "lxmf-cb"
        finally:
            await lxmf_adapter.stop()
            await fake_target.stop()
            await runner.stop()

    async def test_unsupported_payload_ignored(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Packets classified as non-text produce no canonical event and no
        delivery."""
        lxmf_adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lxmf-ignore", connection_type="fake")
        )

        route = Route(
            id="lxmf-ignore-route",
            source=RouteSource(
                adapter="lxmf-ignore",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"lxmf-ignore": lxmf_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await lxmf_adapter.start(make_adapter_context("lxmf-ignore", runner))

        try:
            # Empty dict — no content, no fields → category "unknown"
            await lxmf_adapter.simulate_inbound({})

            # ACK-like: fields-only packet with no content → "unsupported"
            await lxmf_adapter.simulate_inbound({"fields": {"ack": True}})

            # No canonical events stored
            events = await temp_storage._read_all("SELECT * FROM canonical_events")
            assert len(events) == 0
        finally:
            await lxmf_adapter.stop()
            await runner.stop()

    async def test_duplicate_native_ref_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Sending the same message_id twice stores exactly one canonical
        event.  The second is suppressed by the native-ref deduplication
        inside the pipeline."""
        lxmf_adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lxmf-dedup", connection_type="fake")
        )

        route = Route(
            id="lxmf-dedup-route",
            source=RouteSource(
                adapter="lxmf-dedup",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"lxmf-dedup": lxmf_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await lxmf_adapter.start(make_adapter_context("lxmf-dedup", runner))

        try:
            packet = make_lxmf_text_packet(
                content="dedup test",
                source_hash="cc33dd44",
                message_id="dedup-msg-001",
            )

            # First send — accepted
            await lxmf_adapter.simulate_inbound(packet)

            events_after_first = await temp_storage._read_all(
                "SELECT event_id FROM canonical_events"
            )
            assert len(events_after_first) == 1

            # Second send — same message_id → suppressed by native ref
            # deduplication in the pipeline's ingress handler.
            await lxmf_adapter.simulate_inbound(packet)

            events_after_second = await temp_storage._read_all(
                "SELECT event_id FROM canonical_events"
            )
            # Exactly one event — the duplicate was suppressed.
            assert len(events_after_second) == 1
        finally:
            await lxmf_adapter.stop()
            await runner.stop()

    async def test_malformed_packet_does_not_crash(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Malformed payloads do not raise; adapter stays alive and can
        process a valid packet afterward."""
        lxmf_adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lxmf-malformed", connection_type="fake")
        )

        route = Route(
            id="lxmf-malformed-route",
            source=RouteSource(
                adapter="lxmf-malformed",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"lxmf-malformed": lxmf_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await lxmf_adapter.start(make_adapter_context("lxmf-malformed", runner))

        try:
            # Malformed: content is an int (unsupported type by classifier)
            # The classifier's normalize_lxmf_text will raise LxmfCodecError,
            # but simulate_inbound catches nothing — the codec call happens
            # after classification.  With no content, classify returns
            # category "unknown", so simulate_inbound returns early.
            # Let's test something that passes classify but fails decode.
            # A packet with valid content but message_id as a non-string/
            # non-bytes value still works through classify and decode.
            # Instead, test _on_packet directly with a truly broken payload
            # that could cause unexpected errors.
            lxmf_adapter._on_packet(None)  # type: ignore[arg-type]

            # Adapter still works after malformed input
            packet = make_lxmf_text_packet(
                content="still alive",
                source_hash="ee55ff66",
                message_id="recover-msg-001",
            )
            await lxmf_adapter.simulate_inbound(packet)

            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )
            assert len(events) == 1
            assert events[0]["source_adapter"] == "lxmf-malformed"
        finally:
            await lxmf_adapter.stop()
            await runner.stop()

    async def test_lxmf_packet_metadata_maps_correctly(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Canonical event metadata fields are populated correctly from
        the LXMF packet."""
        lxmf_adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lxmf-meta", connection_type="fake")
        )

        route = Route(
            id="lxmf-meta-route",
            source=RouteSource(
                adapter="lxmf-meta",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={"lxmf-meta": lxmf_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await lxmf_adapter.start(make_adapter_context("lxmf-meta", runner))

        try:
            packet = make_lxmf_text_packet(
                content="metadata test",
                source_hash="99887766aabb",
                message_id="meta-msg-001",
                title="Test Title",
            )
            await lxmf_adapter.simulate_inbound(packet)

            # Resolve via native ref
            resolved = await temp_storage.resolve_native_ref(
                adapter="lxmf-meta",
                native_channel_id=None,
                native_message_id="meta-msg-001",
            )
            assert resolved is not None

            stored = await temp_storage.get(resolved)
            assert stored is not None

            # source_adapter matches the adapter
            assert stored.source_adapter == "lxmf-meta"

            # source_channel_id is None (LXMF has no channel concept)
            assert stored.source_channel_id is None

            # event_kind is MESSAGE_CREATED
            assert stored.event_kind == EventKind.MESSAGE_CREATED

            # source_transport_id is the sender hash
            assert stored.source_transport_id == "99887766aabb"

            # source_native_ref has the message_id
            assert stored.source_native_ref is not None
            assert stored.source_native_ref.native_message_id == "meta-msg-001"

            # Payload contains the body text
            assert stored.payload.get("body") == "metadata test"
            assert stored.payload.get("title") == "Test Title"

            # Native metadata preserved
            native_data = stored.metadata.native.data
            assert native_data.get("source_hash") == "99887766aabb"
            assert native_data.get("message_id") == "meta-msg-001"
        finally:
            await lxmf_adapter.stop()
            await runner.stop()

    async def test_on_packet_sync_routes_through_pipeline(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """The synchronous ``_on_packet`` callback spawns an async task that
        routes through the full pipeline to a fake target."""
        lxmf_adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lxmf-sync-cb", connection_type="fake")
        )
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="fake-sync-target")
        )

        route = Route(
            id="lxmf-sync-route",
            source=RouteSource(
                adapter="lxmf-sync-cb",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="fake-sync-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = make_pipeline_config(
            temp_storage,
            router,
            adapters={
                "lxmf-sync-cb": lxmf_adapter,
                "fake-sync-target": fake_target,
            },
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await lxmf_adapter.start(make_adapter_context("lxmf-sync-cb", runner))
        await fake_target.start(make_adapter_context("fake-sync-target", runner))

        try:
            packet = make_lxmf_text_packet(
                content="sync callback test",
                source_hash="1234abcd",
                message_id="sync-evt-001",
            )

            # Call the sync _on_packet callback directly
            lxmf_adapter._on_packet(packet)

            # Wait for the background task to complete
            for _ in range(50):
                if fake_target.delivered_payloads:
                    break
                await asyncio.sleep(0.01)

            # Fake target received the delivery
            assert len(fake_target.delivered_payloads) == 1
            rendered = fake_target.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)

            # Canonical event stored
            events = await temp_storage._read_all(
                "SELECT source_adapter FROM canonical_events"
            )
            assert len(events) == 1
            assert events[0]["source_adapter"] == "lxmf-sync-cb"

            # Delivery receipt
            receipts = await temp_storage._read_all(
                "SELECT status FROM delivery_receipts"
            )
            assert len(receipts) == 1
            assert receipts[0]["status"] == "sent"
        finally:
            await lxmf_adapter.stop()
            await fake_target.stop()
            await runner.stop()
