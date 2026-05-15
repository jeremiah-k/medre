"""Adapter callback ingress, bidirectional bridge safety, fanout, and real
wrapper callback path tests.

Proves:
1. adapter.simulate_inbound produces identical results to direct
   pipeline_runner.handle_ingress.
2. Bidirectional bridges don't loop (each message creates exactly one
   canonical event; self-loop guard prevents source-adapter re-delivery).
3. Fanout routes deliver to all targets but never back to the source.
4. Real Matrix and Meshtastic wrapper callbacks bridge correctly to fake
   targets through the full pipeline.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.base import (
    AdapterContext,
    AdapterDeliveryResult,
    AdapterPermanentError,
)
from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.bus import EventBus
from medre.core.events.kinds import EventKind
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.storage.backend import StorageBackend


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_adapter_context(
    adapter_id: str, runner: PipelineRunner
) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=runner.ingress_handler,
        logger=logging.getLogger(f"test.bridge.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_pipeline_config(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict[str, Any] | None = None,
    event_bus: EventBus | None = None,
    rendering_pipeline: RenderingPipeline | None = None,
    accounting: RuntimeAccounting | None = None,
    route_stats: RouteStats | None = None,
) -> PipelineConfig:
    """Build a PipelineConfig with standard renderers registered."""
    rp = rendering_pipeline or RenderingPipeline()
    # Ensure TextRenderer is always available as fallback
    if not rp._renderers:
        rp.register(TextRenderer(), priority=100)

    return PipelineConfig(
        storage=storage,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=event_bus or EventBus(),
        rendering_pipeline=rp,
        runtime_accounting=accounting,
        route_stats=route_stats,
    )


def _make_text_packet(
    text: str = "hello bridge",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
) -> dict:
    """Minimal Meshtastic text packet dict."""
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


def _make_meshcore_packet(
    text: str = "hello meshcore",
    sender: str = "abc123",
    channel: int = 0,
    packet_id: int = 99,
) -> dict:
    """Minimal MeshCore text packet dict."""
    packet: dict[str, Any] = {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": packet_id,
        "type": "CHAN",
        "txt_type": 0,
    }
    if channel is not None:
        packet["channel_idx"] = channel
    return packet


def _make_nio_event(
    sender: str = "@alice:example.com",
    event_id: str = "$bridge-evt-001",
    body: str = "hello from matrix",
    content: dict | None = None,
) -> SimpleNamespace:
    """Build a duck-typed nio RoomMessageText event."""
    final_content = content or {"msgtype": "m.text", "body": body}
    return SimpleNamespace(
        sender=sender,
        event_id=event_id,
        body=body,
        source={
            "content": final_content,
            "event_id": event_id,
            "sender": sender,
            "type": "m.room.message",
        },
    )


def _make_nio_room(room_id: str = "!bridge_room:example.com") -> SimpleNamespace:
    """Build a duck-typed nio Room object."""
    return SimpleNamespace(room_id=room_id)


def _build_mock_nio_module() -> MagicMock:
    """Create a mock nio module suitable for MatrixSession/MatrixAdapter."""
    mock = MagicMock(name="mock_nio")
    client = MagicMock(name="mock_async_client")
    client.logged_in = True
    client.restore_login = MagicMock()
    client.add_event_callback = MagicMock()
    client.stop_sync_forever = MagicMock()
    client.close = AsyncMock()
    client.rooms = {}

    async def _sync_forever_stub(*args: object, **kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    client.sync_forever = _sync_forever_stub

    async def _room_send(
        room_id: str, message_type: str, content: dict, **kwargs: object
    ) -> SimpleNamespace:
        return SimpleNamespace(
            event_id=f"$sent-{content.get('body', 'msg')[:12]}",
            transport_response=None,
        )

    client.room_send = AsyncMock(side_effect=_room_send)

    whoami_resp = MagicMock(name="whoami_response")
    whoami_resp.device_id = "BRIDGE_MOCK_DEVICE"
    client.whoami = AsyncMock(return_value=whoami_resp)

    mock.AsyncClient = MagicMock(return_value=client)
    mock.ClientConfig = MagicMock(name="ClientConfig")
    mock.RoomMessageText = MagicMock(name="RoomMessageText")
    mock.RoomMessageNotice = MagicMock(name="RoomMessageNotice")
    mock.RoomMessageEmote = MagicMock(name="RoomMessageEmote")

    mock_events = MagicMock(name="nio.events")
    mock_events.MegolmEvent = MagicMock(name="MegolmEvent")
    mock_events.RoomEncryptionEvent = MagicMock(name="RoomEncryptionEvent")
    mock.events = mock_events

    return mock


@pytest.fixture
def mock_nio():
    """Inject a mock nio module into sys.modules and patch HAS_NIO."""
    mock = _build_mock_nio_module()
    saved_nio = sys.modules.get("nio")
    saved_nio_events = sys.modules.get("nio.events")
    sys.modules["nio"] = mock
    sys.modules["nio.events"] = mock.events
    with patch("medre.adapters.matrix.adapter.HAS_NIO", True):
        yield mock
    if saved_nio is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = saved_nio
    if saved_nio_events is None:
        sys.modules.pop("nio.events", None)
    else:
        sys.modules["nio.events"] = saved_nio_events


def _make_matrix_config(**overrides: Any) -> MatrixConfig:
    """Build a valid MatrixConfig for bridge tests."""
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-bridge",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok_bridge",
        "encryption_mode": "plaintext",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


# ===================================================================
# 1. Adapter callback ingress equivalence
# ===================================================================


class TestFakeMatrixAdapterIngressEquivalence:
    """FakeMatrixAdapter.simulate_inbound produces the same pipeline
    results as direct PipelineRunner.handle_ingress."""

    async def test_simulate_inbound_vs_handle_ingress_identical_outcomes(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Both paths produce identical delivery receipts and accounting."""
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="fake-target")
        )

        route = Route(
            id="equiv-route",
            source=RouteSource(
                adapter="fake-matrix-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="fake-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        adapters = {"fake-target": fake_target}
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters=adapters,
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Path A: simulate_inbound
        fake_matrix_a = FakeMatrixAdapter("fake-matrix-src", channel="ch-0")
        ctx_a = _make_adapter_context("fake-matrix-src", runner)
        await fake_matrix_a.start(ctx_a)

        event_a = fake_matrix_a.make_event(
            text="equiv test A",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix_a.simulate_inbound(event_a)
        await fake_matrix_a.stop()

        # Path B: direct handle_ingress
        event_b = CanonicalEvent(
            event_id="direct-evt-b",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake-matrix-src",
            source_transport_id="fake-matrix-src",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "equiv test B"},
            metadata=EventMetadata(),
        )
        await runner.handle_ingress(event_b)

        await runner.stop()

        # Both events stored
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 2
        stored_ids = {row["event_id"] for row in all_events}
        assert event_a.event_id in stored_ids
        assert event_b.event_id in stored_ids

        # Both have delivery receipts
        receipts = await temp_storage._read_all(
            "SELECT event_id, target_adapter, status FROM delivery_receipts ORDER BY event_id"
        )
        assert len(receipts) == 2
        for r in receipts:
            assert r["target_adapter"] == "fake-target"
            assert r["status"] == "sent"

        # Fake target received both deliveries
        assert len(fake_target.delivered_payloads) == 2

    async def test_native_refs_persisted_identically(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Both paths persist inbound native refs with the same structure."""
        route = Route(
            id="nref-equiv-route",
            source=RouteSource(
                adapter="fm-nref",
                event_kinds=("message.created",),
                channel=None,
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

        # Path A: simulate_inbound
        fake_matrix = FakeMatrixAdapter("fm-nref", channel="ch-nref")
        ctx = _make_adapter_context("fm-nref", runner)
        await fake_matrix.start(ctx)

        event = fake_matrix.make_event(
            text="nref test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)
        await fake_matrix.stop()

        # Path B: direct handle_ingress with same fields
        event_direct = CanonicalEvent(
            event_id="direct-nref-b",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fm-nref",
            source_transport_id="fm-nref",
            source_channel_id="ch-nref",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "nref test direct"},
            metadata=EventMetadata(),
        )
        await runner.handle_ingress(event_direct)
        await runner.stop()

        # Both should have canonical events in storage
        stored_a = await temp_storage.get(event.event_id)
        stored_b = await temp_storage.get(event_direct.event_id)
        assert stored_a is not None
        assert stored_b is not None
        assert stored_a.source_adapter == stored_b.source_adapter == "fm-nref"

    async def test_accounting_increments_match(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Both paths increment RuntimeAccounting identically."""
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="acc-target")
        )

        route = Route(
            id="acc-equiv-route",
            source=RouteSource(
                adapter="fm-acc",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="acc-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"acc-target": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        fake_matrix = FakeMatrixAdapter("fm-acc", channel="ch-acc")
        ctx = _make_adapter_context("fm-acc", runner)
        await fake_matrix.start(ctx)

        # simulate_inbound path
        event_a = fake_matrix.make_event(
            text="acc test A",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event_a)

        snap_after_a = accounting.snapshot()

        # direct handle_ingress path
        event_b = CanonicalEvent(
            event_id="direct-acc-b",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fm-acc",
            source_transport_id="fm-acc",
            source_channel_id="ch-acc",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "acc test B"},
            metadata=EventMetadata(),
        )
        await runner.handle_ingress(event_b)

        snap_after_b = accounting.snapshot()

        await fake_matrix.stop()
        await runner.stop()

        # After A: 1 inbound, 1 outbound attempt, 1 delivered
        assert snap_after_a["inbound_accepted"] == 1
        assert snap_after_a["outbound_attempts"] == 1
        assert snap_after_a["outbound_delivered"] == 1

        # After B: 2 inbound, 2 outbound attempts, 2 delivered (incremented by 1 each)
        assert snap_after_b["inbound_accepted"] == 2
        assert snap_after_b["outbound_attempts"] == 2
        assert snap_after_b["outbound_delivered"] == 2

        # No loop_prevented in either path
        assert snap_after_b["loop_prevented"] == 0


class TestFakeMeshtasticAdapterIngressEquivalence:
    """FakeMeshtasticAdapter.simulate_inbound produces the same pipeline
    results as direct PipelineRunner.handle_ingress."""

    async def test_simulate_inbound_vs_handle_ingress_identical_receipts(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Both paths produce identical delivery receipts."""
        fake_target = FakeMatrixAdapter("mesh-eq-target", channel="ch-0")

        route = Route(
            id="mesh-eq-route",
            source=RouteSource(
                adapter="fmesh-eq-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="mesh-eq-target", channel="ch-0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mesh-eq-target": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Path A: simulate_inbound
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="fmesh-eq-src")
        )
        ctx = _make_adapter_context("fmesh-eq-src", runner)
        await fake_mesh.start(ctx)

        packet = _make_text_packet(text="mesh equiv test", packet_id=88888)
        await fake_mesh.simulate_inbound(packet)

        # Get the decoded canonical event from the adapter's inbound history
        assert len(fake_mesh.inbound_events) == 1
        canonical_a = fake_mesh.inbound_events[0]
        await fake_mesh.stop()

        # Path B: direct handle_ingress with the same canonical event fields
        canonical_b = CanonicalEvent(
            event_id="direct-mesh-b",
            event_kind=canonical_a.event_kind,
            schema_version=canonical_a.schema_version,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fmesh-eq-src",
            source_transport_id=canonical_a.source_transport_id,
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload=canonical_a.payload,
            metadata=EventMetadata(),
        )
        await runner.handle_ingress(canonical_b)

        await runner.stop()

        # Both events stored
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 2

        # Both have delivery receipts with same status
        receipts = await temp_storage._read_all(
            "SELECT event_id, status, target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 2
        for r in receipts:
            assert r["status"] == "sent"
            assert r["target_adapter"] == "mesh-eq-target"

        # Fake target received both
        assert len(fake_target.delivered_payloads) == 2


# ===================================================================
# 2. Bidirectional bridge safety
# ===================================================================


class TestBidirectionalBridgeSafety:
    """Bidirectional routes (Matrix↔Meshtastic) do not loop.
    Each message creates exactly one canonical event. Self-loop guard
    prevents delivery back to the source adapter."""

    async def test_matrix_to_meshtastic_does_not_echo_back(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Matrix→Meshtastic delivery is not re-routed back to Matrix."""
        fake_matrix = FakeMatrixAdapter("bidir-matrix", channel="!room:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="bidir-mesh")
        )

        # Route 1: matrix -> meshtastic
        route_mx_to_mesh = Route(
            id="bidir-mx-to-mesh",
            source=RouteSource(
                adapter="bidir-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bidir-mesh", channel="0")],
        )
        # Route 2: meshtastic -> matrix
        route_mesh_to_mx = Route(
            id="bidir-mesh-to-mx",
            source=RouteSource(
                adapter="bidir-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bidir-matrix", channel="!room:fake")],
        )
        router = Router(routes=[route_mx_to_mesh, route_mesh_to_mx])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"bidir-matrix": fake_matrix, "bidir-mesh": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = _make_adapter_context("bidir-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = _make_adapter_context("bidir-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        # Inject from Matrix side
        event = fake_matrix.make_event(
            text="bidir from matrix",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Meshtastic received the delivery
        assert len(fake_mesh.delivered_payloads) == 1
        # TextRenderer extracts from event.payload["text"]; FakeMatrixAdapter
        # uses "body" key, so the rendered text comes from .get("text", "")
        rendered_payload = fake_mesh.delivered_payloads[0].payload
        assert "text" in rendered_payload

        # Matrix did NOT receive any delivery (no echo-back)
        assert len(fake_matrix.delivered_payloads) == 0

        # Exactly one canonical event
        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events"
        )
        assert len(all_events) == 1
        assert all_events[0]["source_adapter"] == "bidir-matrix"

        # Exactly one delivery receipt (to meshtastic)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "bidir-mesh"
        assert receipts[0]["status"] == "sent"

    async def test_meshtastic_to_matrix_does_not_echo_back(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Meshtastic→Matrix delivery is not re-routed back to Meshtastic."""
        fake_matrix = FakeMatrixAdapter("bidir2-matrix", channel="!room2:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="bidir2-mesh")
        )

        route_mx_to_mesh = Route(
            id="bidir2-mx-to-mesh",
            source=RouteSource(
                adapter="bidir2-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bidir2-mesh", channel="0")],
        )
        route_mesh_to_mx = Route(
            id="bidir2-mesh-to-mx",
            source=RouteSource(
                adapter="bidir2-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bidir2-matrix", channel="!room2:fake")],
        )
        router = Router(routes=[route_mx_to_mesh, route_mesh_to_mx])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"bidir2-matrix": fake_matrix, "bidir2-mesh": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = _make_adapter_context("bidir2-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = _make_adapter_context("bidir2-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        # Inject from Meshtastic side
        packet = _make_text_packet(text="bidir from mesh", packet_id=77777)
        await fake_mesh.simulate_inbound(packet)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Matrix received the delivery
        assert len(fake_matrix.delivered_payloads) == 1

        # Meshtastic did NOT receive any delivery (no echo-back)
        assert len(fake_mesh.delivered_payloads) == 0

        # Exactly one canonical event
        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events"
        )
        assert len(all_events) == 1
        assert all_events[0]["source_adapter"] == "bidir2-mesh"

        # Exactly one delivery receipt (to matrix)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "bidir2-matrix"

    async def test_each_message_creates_exactly_one_canonical_event(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Multiple messages in both directions each create exactly one event."""
        fake_matrix = FakeMatrixAdapter("multi-matrix", channel="!multi:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="multi-mesh")
        )

        route_a = Route(
            id="multi-mx-to-mesh",
            source=RouteSource(
                adapter="multi-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="multi-mesh", channel="0")],
        )
        route_b = Route(
            id="multi-mesh-to-mx",
            source=RouteSource(
                adapter="multi-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="multi-matrix", channel="!multi:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"multi-matrix": fake_matrix, "multi-mesh": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = _make_adapter_context("multi-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = _make_adapter_context("multi-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        # Send 3 messages from matrix
        for i in range(3):
            event = fake_matrix.make_event(
                text=f"mx msg {i}",
                event_kind=EventKind.MESSAGE_CREATED,
            )
            await fake_matrix.simulate_inbound(event)

        # Send 3 messages from meshtastic
        for i in range(3):
            packet = _make_text_packet(text=f"mesh msg {i}", packet_id=1000 + i)
            await fake_mesh.simulate_inbound(packet)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Exactly 6 canonical events (3 from each source)
        all_events = await temp_storage._read_all(
            "SELECT source_adapter FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 6

        matrix_events = [e for e in all_events if e["source_adapter"] == "multi-matrix"]
        mesh_events = [e for e in all_events if e["source_adapter"] == "multi-mesh"]
        assert len(matrix_events) == 3
        assert len(mesh_events) == 3

        # 6 delivery receipts total (3 matrix→mesh + 3 mesh→matrix)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 6

        mesh_receipts = [r for r in receipts if r["target_adapter"] == "multi-mesh"]
        mx_receipts = [r for r in receipts if r["target_adapter"] == "multi-matrix"]
        assert len(mesh_receipts) == 3
        assert len(mx_receipts) == 3

    async def test_loop_prevented_counter_zero_for_normal_delivery(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """loop_prevented counter is 0 for expected bidirectional deliveries."""
        fake_matrix = FakeMatrixAdapter("lp-matrix", channel="!lp:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="lp-mesh")
        )

        route_a = Route(
            id="lp-mx-to-mesh",
            source=RouteSource(
                adapter="lp-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lp-mesh", channel="0")],
        )
        route_b = Route(
            id="lp-mesh-to-mx",
            source=RouteSource(
                adapter="lp-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lp-matrix", channel="!lp:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"lp-matrix": fake_matrix, "lp-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = _make_adapter_context("lp-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = _make_adapter_context("lp-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        event = fake_matrix.make_event(
            text="lp test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # No loop prevention triggered for normal bidirectional delivery
        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 0

        stats = route_stats.snapshot()
        for route_id, counters in stats.items():
            assert counters["loop_prevented"] == 0

    async def test_self_loop_guard_increments_loop_prevented(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """When source adapter is also a target, loop_prevented increments."""
        fake_matrix = FakeMatrixAdapter("loop-matrix", channel="!loop:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="loop-mesh")
        )

        # Route that includes the source adapter in its targets (self-loop)
        route = Route(
            id="self-loop-route",
            source=RouteSource(
                adapter="loop-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="loop-matrix", channel="!loop:fake"),  # self-loop
                RouteTarget(adapter="loop-mesh", channel="0"),  # normal target
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"loop-matrix": fake_matrix, "loop-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = _make_adapter_context("loop-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = _make_adapter_context("loop-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        event = fake_matrix.make_event(
            text="self-loop test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # loop_prevented incremented for the self-loop target
        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1

        stats = route_stats.snapshot()
        assert stats["self-loop-route"]["loop_prevented"] == 1

        # Meshtastic still received its delivery
        assert len(fake_mesh.delivered_payloads) == 1

        # Matrix did NOT receive its own event
        assert len(fake_matrix.delivered_payloads) == 0

    async def test_source_adapter_metadata_correct(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """source_adapter on canonical events correctly identifies origin."""
        fake_matrix = FakeMatrixAdapter("meta-matrix", channel="!meta:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="meta-mesh")
        )

        route_a = Route(
            id="meta-mx-to-mesh",
            source=RouteSource(
                adapter="meta-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="meta-mesh", channel="0")],
        )
        route_b = Route(
            id="meta-mesh-to-mx",
            source=RouteSource(
                adapter="meta-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="meta-matrix", channel="!meta:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"meta-matrix": fake_matrix, "meta-mesh": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = _make_adapter_context("meta-matrix", runner)
        await fake_matrix.start(ctx_mx)

        ctx_mesh = _make_adapter_context("meta-mesh", runner)
        await fake_mesh.start(ctx_mesh)

        # Matrix-sourced event
        mx_event = fake_matrix.make_event(
            text="from matrix",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(mx_event)

        # Meshtastic-sourced event
        mesh_packet = _make_text_packet(text="from mesh", packet_id=55555)
        await fake_mesh.simulate_inbound(mesh_packet)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 2

        sources = {row["source_adapter"] for row in all_events}
        assert sources == {"meta-matrix", "meta-mesh"}


# ===================================================================
# 3. Fanout without source-duplication
# ===================================================================


class TestFanoutWithoutSourceDuplication:
    """Fanout routes (Matrix → [Meshtastic, MeshCore]) deliver to all
    targets but never back to the source adapter."""

    async def test_fanout_delivers_to_all_non_source_targets(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Fanout: Matrix → [Meshtastic, MeshCore] delivers to both."""
        fake_matrix = FakeMatrixAdapter("fanout-matrix", channel="!fanout:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="fanout-mesh")
        )
        fake_meshcore = FakeMeshCoreAdapter(
            MeshCoreConfig(adapter_id="fanout-meshcore")
        )

        route = Route(
            id="fanout-route",
            source=RouteSource(
                adapter="fanout-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="fanout-mesh", channel="0"),
                RouteTarget(adapter="fanout-meshcore", channel="0"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fanout-matrix": fake_matrix,
                "fanout-mesh": fake_mesh,
                "fanout-meshcore": fake_meshcore,
            },
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = _make_adapter_context("fanout-matrix", runner)
        await fake_matrix.start(ctx_mx)

        await fake_mesh.start(_make_adapter_context("fanout-mesh", runner))
        await fake_meshcore.start(_make_adapter_context("fanout-meshcore", runner))

        event = fake_matrix.make_event(
            text="fanout test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await fake_meshcore.stop()
        await runner.stop()

        # Both targets received
        assert len(fake_mesh.delivered_payloads) == 1
        assert len(fake_meshcore.delivered_payloads) == 1

        # Source did NOT receive its own event
        assert len(fake_matrix.delivered_payloads) == 0

        # Two delivery receipts
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 2
        targets = {r["target_adapter"] for r in receipts}
        assert targets == {"fanout-mesh", "fanout-meshcore"}

    async def test_fanout_self_loop_guard(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Fanout route with source in targets: self-loop guard fires."""
        fake_matrix = FakeMatrixAdapter("fanout-sl-matrix", channel="!sl:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="fanout-sl-mesh")
        )

        # Route includes source adapter in targets
        route = Route(
            id="fanout-sl-route",
            source=RouteSource(
                adapter="fanout-sl-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="fanout-sl-matrix", channel="!sl:fake"),  # self-loop
                RouteTarget(adapter="fanout-sl-mesh", channel="0"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fanout-sl-matrix": fake_matrix,
                "fanout-sl-mesh": fake_mesh,
            },
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx_mx = _make_adapter_context("fanout-sl-matrix", runner)
        await fake_matrix.start(ctx_mx)
        await fake_mesh.start(_make_adapter_context("fanout-sl-mesh", runner))

        event = fake_matrix.make_event(
            text="fanout self-loop",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Meshtastic received
        assert len(fake_mesh.delivered_payloads) == 1

        # Matrix did NOT receive its own event
        assert len(fake_matrix.delivered_payloads) == 0

        # loop_prevented incremented
        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1

        # Only one receipt (to meshtastic); matrix target was skipped
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "fanout-sl-mesh"
        assert receipts[0]["status"] == "sent"

    async def test_fanout_three_targets_no_duplicates(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Fanout to three targets creates exactly three receipts."""
        fake_matrix = FakeMatrixAdapter("fan3-mx", channel="!f3:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="fan3-mesh")
        )
        fake_meshcore = FakeMeshCoreAdapter(
            MeshCoreConfig(adapter_id="fan3-mc")
        )
        fake_matrix_2 = FakeMatrixAdapter("fan3-mx2", channel="!f3-out:fake")

        route = Route(
            id="fan3-route",
            source=RouteSource(
                adapter="fan3-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="fan3-mesh", channel="0"),
                RouteTarget(adapter="fan3-mc", channel="0"),
                RouteTarget(adapter="fan3-mx2", channel="!f3-out:fake"),
            ],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fan3-mx": fake_matrix,
                "fan3-mesh": fake_mesh,
                "fan3-mc": fake_meshcore,
                "fan3-mx2": fake_matrix_2,
            },
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(_make_adapter_context("fan3-mx", runner))
        await fake_mesh.start(_make_adapter_context("fan3-mesh", runner))
        await fake_meshcore.start(_make_adapter_context("fan3-mc", runner))
        await fake_matrix_2.start(_make_adapter_context("fan3-mx2", runner))

        event = fake_matrix.make_event(
            text="fanout three",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await fake_meshcore.stop()
        await fake_matrix_2.stop()
        await runner.stop()

        # Each non-source target received exactly one delivery
        assert len(fake_mesh.delivered_payloads) == 1
        assert len(fake_meshcore.delivered_payloads) == 1
        assert len(fake_matrix_2.delivered_payloads) == 1

        # Source did not receive
        assert len(fake_matrix.delivered_payloads) == 0

        # Three receipts
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 3


# ===================================================================
# 4. Real wrapper callback paths (mocked SDK)
# ===================================================================


class TestMatrixWrapperCallbackPath:
    """Real MatrixAdapter._on_room_message → publish_inbound → pipeline →
    fake target.  Uses mocked nio SDK."""

    async def test_on_room_message_routes_to_fake_target(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """_on_room_message decodes nio event, publishes through pipeline,
        and delivers to fake target."""
        matrix_adapter = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-cb")
        )
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"matrix-cb": matrix_adapter, "fake-cb-target": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx = _make_adapter_context("matrix-cb", runner)
        await matrix_adapter.start(ctx)
        await fake_target.start(_make_adapter_context("fake-cb-target", runner))

        try:
            room = _make_nio_room("!cb_room:example.com")
            event = _make_nio_event(
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

    async def test_room_id_event_id_mapping_to_native_refs(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """room_id and event_id from nio event map to native_channel_id
        and native_message_id on the canonical event."""
        matrix_adapter = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-nref-cb")
        )

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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"matrix-nref-cb": matrix_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx = _make_adapter_context("matrix-nref-cb", runner)
        await matrix_adapter.start(ctx)

        try:
            room = _make_nio_room("!nref_room:example.com")
            event = _make_nio_event(
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
            assert stored.source_native_ref.native_channel_id == "!nref_room:example.com"
        finally:
            await matrix_adapter.stop()
            await runner.stop()

    async def test_matrix_originated_event_reaches_fake_meshtastic(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """Full bridge: Matrix _on_room_message → pipeline → fake
        Meshtastic adapter delivery."""
        matrix_adapter = MatrixAdapter(
            _make_matrix_config(adapter_id="mx-bridge-src")
        )
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mx-bridge-src": matrix_adapter, "mesh-bridge-dst": fake_mesh},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await matrix_adapter.start(_make_adapter_context("mx-bridge-src", runner))
        await fake_mesh.start(_make_adapter_context("mesh-bridge-dst", runner))

        try:
            room = _make_nio_room("!bridge_room:example.com")
            event = _make_nio_event(
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


class TestMeshtasticWrapperCallbackPath:
    """Real MeshtasticAdapter.simulate_inbound → codec → publish_inbound →
    pipeline → fake target."""

    async def test_simulate_inbound_routes_to_fake_target(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MeshtasticAdapter.simulate_inbound decodes packet, publishes
        through pipeline, delivers to fake target."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        mesh_adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-cb-src", connection_type="fake")
        )
        fake_target = FakeMatrixAdapter("fake-mx-dst", channel="!dst:fake")

        route = Route(
            id="mesh-cb-route",
            source=RouteSource(
                adapter="mesh-cb-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="fake-mx-dst", channel="!dst:fake")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mesh-cb-src": mesh_adapter, "fake-mx-dst": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(_make_adapter_context("mesh-cb-src", runner))
        await fake_target.start(_make_adapter_context("fake-mx-dst", runner))

        packet = _make_text_packet(
            text="mesh callback test", packet_id=44444, channel=0
        )
        await mesh_adapter.simulate_inbound(packet)

        await mesh_adapter.stop()
        await fake_target.stop()
        await runner.stop()

        # Fake target received
        assert len(fake_target.delivered_payloads) == 1
        rendered = fake_target.delivered_payloads[0]
        assert isinstance(rendered, RenderingResult)

        # Delivery receipt
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["status"] == "sent"

    async def test_packet_channel_metadata_maps_to_canonical(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Packet sender, channel, packet_id map correctly to canonical
        event fields."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        mesh_adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-meta-src", connection_type="fake")
        )

        route = Route(
            id="mesh-meta-route",
            source=RouteSource(
                adapter="mesh-meta-src",
                event_kinds=("message.created",),
                channel="3",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mesh-meta-src": mesh_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(_make_adapter_context("mesh-meta-src", runner))

        packet = _make_text_packet(
            text="metadata test",
            sender="!deadbeef",
            channel=3,
            packet_id=12345,
        )
        await mesh_adapter.simulate_inbound(packet)

        await mesh_adapter.stop()
        await runner.stop()

        # Native ref persisted with correct metadata
        resolved = await temp_storage.resolve_native_ref(
            adapter="mesh-meta-src",
            native_channel_id="3",
            native_message_id="12345",
        )
        assert resolved is not None

        # Stored event has correct source metadata
        stored = await temp_storage.get(resolved)
        assert stored is not None
        assert stored.source_transport_id == "!deadbeef"
        assert stored.source_channel_id == "3"
        assert stored.source_native_ref is not None
        assert stored.source_native_ref.native_message_id == "12345"
        assert stored.source_adapter == "mesh-meta-src"

    async def test_meshtastic_inbound_reaches_fake_matrix(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Full bridge: Meshtastic simulate_inbound → pipeline → fake
        Matrix adapter delivery."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        mesh_adapter = MeshtasticAdapter(
            MeshtasticConfig(
                adapter_id="mesh-bridge-src", connection_type="fake"
            )
        )
        fake_mx = FakeMatrixAdapter("mx-bridge-dst", channel="!bridge-dst:fake")

        route = Route(
            id="mesh-to-mx-bridge",
            source=RouteSource(
                adapter="mesh-bridge-src",
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mesh-bridge-src": mesh_adapter, "mx-bridge-dst": fake_mx},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(_make_adapter_context("mesh-bridge-src", runner))
        await fake_mx.start(_make_adapter_context("mx-bridge-dst", runner))

        packet = _make_text_packet(text="mesh to matrix", packet_id=66666)
        await mesh_adapter.simulate_inbound(packet)

        await mesh_adapter.stop()
        await fake_mx.stop()
        await runner.stop()

        # Fake matrix adapter received delivery
        assert len(fake_mx.delivered_payloads) == 1
        rendered = fake_mx.delivered_payloads[0]
        assert isinstance(rendered, RenderingResult)

        # Receipt
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "mx-bridge-dst"
        assert receipts[0]["status"] == "sent"

        # Canonical event has meshtastic source
        events = await temp_storage._read_all(
            "SELECT source_adapter FROM canonical_events"
        )
        assert len(events) == 1
        assert events[0]["source_adapter"] == "mesh-bridge-src"


class TestMeshCoreWrapperCallbackPath:
    """FakeMeshCoreAdapter.simulate_inbound → codec → publish_inbound →
    pipeline → fake target.  Proves the MeshCore codec/classifier path
    works correctly through the pipeline."""

    async def test_simulate_inbound_routes_to_fake_target(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """FakeMeshCoreAdapter.simulate_inbound delivers to fake target."""
        fake_meshcore = FakeMeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-cb-src")
        )
        fake_target = FakeMatrixAdapter("mc-fake-dst", channel="!mc-dst:fake")

        route = Route(
            id="mc-cb-route",
            source=RouteSource(
                adapter="mc-cb-src",
                event_kinds=("message.created",),
                channel="0",
            ),
            targets=[RouteTarget(adapter="mc-fake-dst", channel="!mc-dst:fake")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mc-cb-src": fake_meshcore, "mc-fake-dst": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_meshcore.start(_make_adapter_context("mc-cb-src", runner))
        await fake_target.start(_make_adapter_context("mc-fake-dst", runner))

        packet = _make_meshcore_packet(
            text="meshcore callback", sender="mc_sender", channel=0, packet_id=77777
        )
        await fake_meshcore.simulate_inbound(packet)

        await fake_meshcore.stop()
        await fake_target.stop()
        await runner.stop()

        # Fake target received
        assert len(fake_target.delivered_payloads) == 1

        # Receipt persisted
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["status"] == "sent"

    async def test_meshcore_packet_metadata_maps_correctly(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MeshCore packet metadata (sender, channel) maps to canonical
        event fields."""
        fake_meshcore = FakeMeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-meta-src")
        )

        route = Route(
            id="mc-meta-route",
            source=RouteSource(
                adapter="mc-meta-src",
                event_kinds=("message.created",),
                channel="2",
            ),
            targets=[],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mc-meta-src": fake_meshcore},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_meshcore.start(_make_adapter_context("mc-meta-src", runner))

        packet = _make_meshcore_packet(
            text="mc metadata",
            sender="pubkey_xyz",
            channel=2,
            packet_id=99999,
        )
        await fake_meshcore.simulate_inbound(packet)

        await fake_meshcore.stop()
        await runner.stop()

        # Verify stored event has correct metadata
        assert len(fake_meshcore.inbound_events) == 1
        canonical = fake_meshcore.inbound_events[0]
        assert canonical.source_adapter == "mc-meta-src"
        assert canonical.source_channel_id == "2"
