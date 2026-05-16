"""Docker-backed meshtasticd SDK-boundary bridge smoke tests.

These tests prove the MEDRE MeshtasticAdapter can operate against a real
meshtasticd daemon running in Docker, exercising genuine SDK-boundary
lifecycle, outbound send, and pipeline bridging paths.

Running locally::

    # Prerequisites: Docker running, mtjk installed
    pip install -e ".[meshtastic]"
    pytest tests/integration/test_meshtasticd_sdk_bridge.py -m docker -v

To include in a broader integration run::

    pytest tests/integration/ -m docker -v

What these tests prove
----------------------
1. **Real SDK session lifecycle.** The adapter creates a real
   ``TCPInterface`` to meshtasticd, subscribes to the
   ``meshtastic.receive`` pubsub topic, reports ``healthy``, and stops
   cleanly with no orphaned state.

2. **Real outbound SDK boundary.** ``deliver()`` enqueues a payload
   locally; ``send_one()`` dequeues it and calls the real
   ``sendText()`` through the ``TCPInterface``.  meshtasticd accepts
   the send and returns a real packet ID.

3. **Pipeline bridge with real session.** While a real meshtasticd
   session is active, ``simulate_inbound()`` exercises the real
   codec/classifier/publish path through ``PipelineRunner`` to a
   ``FakeMeshtasticAdapter`` outbound target.  Delivery receipts and
   native refs are persisted in SQLite.

What these tests do NOT prove
-----------------------------
- **Real inbound packet reception via pubsub.** Inbound packets are
  injected through ``simulate_inbound()``, not received from meshtasticd
  via the ``meshtastic.receive`` pubsub callback.  Test 4 attempts
  two-client real injection but is non-blocking (xfail).

- **Real LoRa radio interaction.** meshtasticd runs in simulation mode
  (``-s``), not against real hardware.

- **Sustained throughput or reconnect resilience.** These are smoke
  tests, not reliability tests.

Evidence classification
-----------------------
Each bridge test produces a compact ``report`` dict containing:

- ``transport``: ``"meshtastic"``
- ``evidence_level``: ``"docker_sdk_boundary"``
- ``inbound_path``: ``"simulate_inbound"`` (always — real pubsub
  delivery is not exercised by these smoke tests)
- ``source_adapter``, ``target_adapter``, ``native_event_id``,
  ``route_id``, ``receipt_status``
- ``accounting``: relevant counters
- ``limitations``: list of what this run does not prove
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from medre.adapters.base import AdapterContext
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.core.events.canonical import CanonicalEvent
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.compat import HAS_MESHTASTIC
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.core.events.bus import EventBus
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner

from .conftest import MeshtasticdEnvironment, _write_artifact_json, _RUN_ARTIFACT_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gating — docker marker + mtjk dependency
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.docker

if not HAS_MESHTASTIC:
    pytestmark = [
        pytest.mark.docker,
        pytest.mark.skip(reason="mtjk not installed; run: pip install '.[meshtastic]'"),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    env: MeshtasticdEnvironment,
    adapter_id: str = "sdk-bridge",
) -> MeshtasticConfig:
    """Build a TCP MeshtasticConfig pointing at the Docker meshtasticd."""
    return MeshtasticConfig(
        adapter_id=adapter_id,
        connection_type="tcp",
        host=env.host,
        port=env.port,
        meshnet_name="MEDRE SDK Bridge CI",
        message_delay_seconds=0.0,
    ).validate()


def _make_context(adapter_id: str = "sdk-bridge") -> AdapterContext:
    """Build an AdapterContext wired to a mock publish_inbound."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.sdk_bridge.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_text_packet(
    text: str = "sdk bridge test",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
) -> dict:
    """Minimal Meshtastic text packet for bridge tests."""
    return {
        "fromId": sender,
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {
            "portnum": "text_message",
            "text": text,
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMeshtasticdSdkBridge:
    """Docker-backed meshtasticd SDK-boundary bridge smoke tests.

    Proves the MeshtasticAdapter works with a real meshtasticd instance
    across lifecycle, outbound send, and pipeline bridging paths.
    """

    @pytest.mark.asyncio
    async def test_real_session_lifecycle_and_health(
        self,
        meshtasticd_env: MeshtasticdEnvironment,
    ) -> None:
        """Real TCPInterface lifecycle: start, health, diagnostics, stop.

        Proves the adapter creates a real ``TCPInterface``, subscribes to
        ``meshtastic.receive``, reports ``healthy``, and stops cleanly.
        """
        config = _make_config(meshtasticd_env, adapter_id="sdk-lifecycle")
        adapter = MeshtasticAdapter(config)
        ctx = _make_context("sdk-lifecycle")

        await adapter.start(ctx)
        try:
            # Session is wired.
            assert adapter._started is True
            assert adapter._session is not None
            assert adapter._session.connected is True
            assert adapter._client is not None

            # Health reports healthy with real session.
            info = await adapter.health_check()
            assert info.health == "healthy"
            assert info.adapter_id == "sdk-lifecycle"
            assert info.platform == "meshtastic"

            # Diagnostics expose real session state.
            diag = adapter.diagnostics()
            assert diag["started"] is True
            assert diag["connection_type"] == "tcp"
            assert "session" in diag
            session_diag = diag["session"]
            assert session_diag["connected"] is True
            assert session_diag["reconnect_attempts"] == 0
            assert session_diag["last_error"] is None
        finally:
            await adapter.stop()

        # After stop: clean state.
        info = await adapter.health_check()
        assert info.health == "unknown"
        assert adapter._session is None
        assert adapter._client is None

    @pytest.mark.asyncio
    async def test_outbound_send_one_through_real_session(
        self,
        meshtasticd_env: MeshtasticdEnvironment,
    ) -> None:
        """Outbound SDK boundary: deliver enqueues, send_one sends via real
        sendText to meshtasticd.

        Proves the full outbound path: ``deliver()`` enqueues locally with
        ``native_message_id=None``, then ``send_one()`` dequeues and calls
        real ``sendText()`` through the ``TCPInterface``, returning a real
        packet ID from meshtasticd.
        """
        config = _make_config(meshtasticd_env, adapter_id="sdk-outbound")
        adapter = MeshtasticAdapter(config)
        ctx = _make_context("sdk-outbound")

        await adapter.start(ctx)
        try:
            result = RenderingResult(
                event_id="evt-sdk-outbound",
                target_adapter="sdk-outbound",
                target_channel="0",
                payload={
                    "text": "docker sdk bridge outbound",
                    "channel_index": 0,
                    "meshnet_name": "",
                },
            )
            delivery = await adapter.deliver(result)

            # Local enqueue accepted; no native_message_id yet.
            assert delivery is not None
            assert delivery.native_message_id is None
            assert delivery.delivery_note == "locally enqueued"
            assert adapter.queue.pending_count == 1

            # send_one dequeues and sends via real sendText.
            send_result = await adapter.send_one()
            assert send_result is not None
            assert send_result.native_message_id is not None
            assert send_result.native_channel_id == "0"
            assert adapter.queue.pending_count == 0

            # Queue diagnostics reflect the successful send.
            assert adapter.queue.total_sent == 1
            assert adapter.queue.total_failed == 0

            # Persist outbound-only artifact when artifact collection is enabled.
            if _RUN_ARTIFACT_DIR is not None:
                _write_artifact_json(
                    "meshtasticd-outbound-send-report.json",
                    {
                        "scenario": "meshtastic_outbound_send_one",
                        "transport": "meshtastic",
                        "evidence_level": "docker_sdk_boundary",
                        "outbound_path": "real_sendText",
                        "inbound_path": "none",
                        "cross_transport_proof": "partial",
                        "native_message_id": send_result.native_message_id,
                        "queue_sent": adapter.queue.total_sent,
                        "queue_failed": adapter.queue.total_failed,
                        "limitations": [
                            "Outbound only (no inbound path exercised).",
                            "meshtasticd simulation mode (-s), not real LoRa hardware.",
                            "Fire-and-forget: remote receipt not confirmed.",
                        ],
                    },
                )
        finally:
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_simulate_inbound_bridge_to_fake_outbound(
        self,
        meshtasticd_env: MeshtasticdEnvironment,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Pipeline bridge: real meshtasticd session + simulate_inbound ->
        PipelineRunner -> FakeMeshtasticAdapter.

        Proves that with a real meshtasticd session active, the full
        codec/classifier/publish path works through PipelineRunner to a fake
        outbound adapter.  Delivery receipts and native refs are persisted.

        **Limitation**: The inbound packet is injected via
        ``simulate_inbound()``, not received from meshtasticd through the
        ``meshtastic.receive`` pubsub callback.  This test proves the
        codec/pipeline/accounting path works while the real session is
        active, but does NOT prove real pubsub packet delivery.
        """
        # Real adapter against meshtasticd as pipeline source.
        mesh_config = _make_config(meshtasticd_env, adapter_id="sdk-bridge-in")
        mesh_adapter = MeshtasticAdapter(mesh_config)

        # Fake adapter as outbound target.
        fake_config = MeshtasticConfig(adapter_id="sdk-bridge-fake-out")
        fake_adapter = FakeMeshtasticAdapter(fake_config)

        route = Route(
            id="sdk-bridge-route",
            source=RouteSource(
                adapter="sdk-bridge-in",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="sdk-bridge-fake-out", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(MeshtasticRenderer(), priority=50)
        rp.register_adapter_platform("sdk-bridge-fake-out", "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={
                    "sdk-bridge-in": mesh_adapter,
                    "sdk-bridge-fake-out": fake_adapter,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        ctx = AdapterContext(
            adapter_id="sdk-bridge-in",
            event_bus=None,
            publish_inbound=runner.ingress_handler,
            logger=logging.getLogger("test.sdk_bridge.sdk-bridge-in"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await mesh_adapter.start(ctx)

        try:
            # Inject a packet through the real adapter's codec path.
            packet = _make_text_packet(
                text="sdk bridge pipeline",
                packet_id=88888,
            )
            await mesh_adapter.simulate_inbound(packet)

            # Fake adapter received the rendered payload via pipeline.
            assert len(fake_adapter.delivered_payloads) == 1
            result = fake_adapter.delivered_payloads[0]
            assert isinstance(result, RenderingResult)
            assert result.payload["text"] == "sdk bridge pipeline"
            # MeshtasticRenderer was selected, not TextRenderer.
            assert result.metadata["renderer"] == "meshtastic"
            assert "channel_index" in result.payload
            assert "meshnet_name" in result.payload

            # Inbound native ref persisted in storage (public API).
            resolved = await temp_storage.resolve_native_ref(
                adapter="sdk-bridge-in",
                native_channel_id="0",
                native_message_id="88888",
            )
            assert resolved is not None, (
                "Expected inbound native ref for packet_id 88888 on channel 0"
            )
            canonical_id = resolved

            # Delivery receipt persisted as 'sent' (public API).
            receipts = await temp_storage.list_receipts_for_event(
                canonical_id,
            )
            assert len(receipts) == 1, (
                f"Expected exactly 1 receipt for event {canonical_id!r}, "
                f"got {len(receipts)}"
            )
            receipt_status = receipts[0].status
            assert receipt_status == "sent"

            # Outbound native ref exists from FakeMeshtasticAdapter
            # (which generates deterministic IDs).
            resolved_out = await temp_storage.resolve_native_ref(
                adapter="sdk-bridge-fake-out",
                native_channel_id="0",
                native_message_id="1",
            )
            assert resolved_out is not None

            # Build compact evidence report.
            report: dict[str, object] = {
                "transport": "meshtastic",
                "evidence_level": "docker_sdk_boundary",
                "inbound_path": "simulate_inbound",
                "source_adapter": "sdk-bridge-in",
                "target_adapter": "sdk-bridge-fake-out",
                "native_event_id": "88888",
                "route_id": route.id,
                "receipt_status": receipt_status,
                "limitations": [
                    "Inbound injected via simulate_inbound (not real pubsub).",
                    "meshtasticd simulation mode (-s), not real LoRa hardware.",
                    "Not a throughput or reconnect resilience test.",
                ],
            }
            assert report["evidence_level"] == "docker_sdk_boundary"
            assert report["receipt_status"] == "sent"
            assert report["inbound_path"] == "simulate_inbound"

            logger.info(
                "Meshtastic bridge smoke report: inbound_path=%s "
                "native_event_id=%s receipt_status=%s",
                report["inbound_path"],
                report["native_event_id"],
                report["receipt_status"],
            )

            # Persist structured artifact when MEDRE_DOCKER_ARTIFACT_RUN_DIR is set.
            if _RUN_ARTIFACT_DIR is not None:
                _write_artifact_json(
                    "meshtasticd-sdk-bridge-report.json",
                    {
                        "scenario": "meshtastic_simulate_inbound_bridge",
                        "transport": "meshtastic",
                        "evidence_level": "docker_sdk_boundary",
                        "inbound_path": "simulate_inbound",
                        "inbound_note": (
                            "Inbound injected via simulate_inbound; "
                            "real pubsub delivery not exercised"
                        ),
                        "outbound_path": "fake_adapter",
                        "cross_transport_proof": "partial",
                        "cross_transport_note": (
                            "Outbound target is FakeMeshtasticAdapter; "
                            "real radio delivery not proven"
                        ),
                        "event_id": resolved,
                        "native_event_id": report["native_event_id"],
                        "receipt_count": len(receipts),
                        "native_ref_count_inbound": 1,
                        "native_ref_count_outbound": 1,
                        "limitations": report["limitations"],
                    },
                )
        finally:
            await mesh_adapter.stop()
            await runner.stop()

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=False,
        reason=(
            "meshtasticd simulation mode may not relay packets between "
            "TCP clients; this test provides bonus evidence when it passes"
        ),
    )
    async def test_two_client_real_packet_injection(
        self,
        meshtasticd_env: MeshtasticdEnvironment,
    ) -> None:
        """Attempt real packet injection via a second TCPInterface.

        Connects a second ``TCPInterface`` (injector) to the same
        meshtasticd instance, sends a text message, and checks whether
        the adapter's ``publish_inbound`` callback fires.

        **If this test passes**: meshtasticd simulation mode relays packets
        between TCP clients.  This proves the full ``meshtastic.receive``
        pubsub callback path: daemon → pubsub → ``_on_receive`` →
        ``_on_packet`` → codec → ``publish_inbound``.

        **If this test fails (xfail)**: meshtasticd does not relay between
        TCP clients in this version/configuration.  The other tests in this
        module still prove the SDK-boundary lifecycle, outbound send, and
        codec/pipeline path with a real session active.
        """
        import meshtastic.tcp_interface

        config = _make_config(meshtasticd_env, adapter_id="sdk-inject-recv")
        adapter = MeshtasticAdapter(config)

        received_events: list[CanonicalEvent] = []

        async def capture_inbound(event: CanonicalEvent) -> None:
            received_events.append(event)

        ctx = AdapterContext(
            adapter_id="sdk-inject-recv",
            event_bus=None,
            publish_inbound=capture_inbound,
            logger=logging.getLogger("test.sdk_bridge.sdk-inject-recv"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        await adapter.start(ctx)
        injector = meshtastic.tcp_interface.TCPInterface(
            hostname=meshtasticd_env.host,
            portNumber=meshtasticd_env.port,
        )
        loop = asyncio.get_event_loop()

        try:
            # Wait for injector to connect.
            await loop.run_in_executor(None, injector.waitForConfig)
            assert injector.isConnected.is_set()

            # Give the daemon a moment to settle both connections.
            await asyncio.sleep(2)

            # Send a text message from the injector.
            await loop.run_in_executor(
                None,
                lambda: injector.sendText(
                    "sdk inject test", channelIndex=0,
                ),
            )

            # Wait for the adapter to receive the packet via pubsub.
            # Use a generous timeout; meshtasticd may need time to
            # process and relay.
            for _ in range(20):
                if received_events:
                    break
                await asyncio.sleep(0.5)

            assert len(received_events) >= 1, (
                "Adapter did not receive the injected packet within "
                "the expected window.  meshtasticd simulation mode may "
                "not relay packets between TCP clients."
            )

            # Verify the received event has the expected content.
            event = received_events[0]
            assert "body" in event.payload
            assert event.payload["body"] == "sdk inject test"
        finally:
            try:
                await loop.run_in_executor(None, injector.close)
            except Exception:
                pass
            await adapter.stop()
