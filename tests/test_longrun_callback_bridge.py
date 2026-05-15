"""Long-running bidirectional callback bridge, self-message/duplicate
prevention, multi-message wrapper callbacks, and loop prevention hardening.

Proves:
1. Long-run bidirectional bridge (fake_matrix <-> fake_meshtastic) handles
   10 messages without loops, with exact accounting.
2. Self-message / existing-native-ref deduplication at the pipeline level.
3. Real Matrix and Meshtastic wrapper multi-message callback correctness.
4. Loop prevention hardening: source==target, route-trace guard, native-ref
   cycle detection.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.base import (
    AdapterContext,
    AdapterDeliveryResult,
)
from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.bus import EventBus
from medre.core.events.canonical import NativeMessageRef
from medre.core.events.kinds import EventKind
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage.sqlite import SQLiteStorage


# ---------------------------------------------------------------------------
# Shared helpers (adapted from test_adapter_callback_bridge.py)
# ---------------------------------------------------------------------------


def _make_adapter_context(
    adapter_id: str, runner: PipelineRunner
) -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=runner.ingress_handler,
        logger=logging.getLogger(f"test.longrun.{adapter_id}"),
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
    rp = rendering_pipeline or RenderingPipeline()
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


def _make_nio_event(
    sender: str = "@alice:example.com",
    event_id: str = "$bridge-evt-001",
    body: str = "hello from matrix",
    content: dict | None = None,
) -> SimpleNamespace:
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
    return SimpleNamespace(room_id=room_id)


def _build_mock_nio_module() -> MagicMock:
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
# 1. Long-run bidirectional callback bridge
# ===================================================================


class TestLongRunBidirectionalCallbackBridge:
    """Bidirectional fake_matrix <-> fake_meshtastic bridge handling N=10
    messages without loops, with exact accounting and clean shutdown."""

    async def test_ten_messages_bidirectional_no_loops(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inject 5 messages from each side. Assert exact counts, no loops."""
        fake_matrix = FakeMatrixAdapter("lr-matrix", channel="!lr-room:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="lr-mesh")
        )

        route_mx_to_mesh = Route(
            id="lr-mx-to-mesh",
            source=RouteSource(
                adapter="lr-matrix",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lr-mesh", channel="0")],
        )
        route_mesh_to_mx = Route(
            id="lr-mesh-to-mx",
            source=RouteSource(
                adapter="lr-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="lr-matrix", channel="!lr-room:fake")],
        )
        router = Router(routes=[route_mx_to_mesh, route_mesh_to_mx])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"lr-matrix": fake_matrix, "lr-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(_make_adapter_context("lr-matrix", runner))
        await fake_mesh.start(_make_adapter_context("lr-mesh", runner))

        # Inject 5 from Matrix side
        for i in range(5):
            event = fake_matrix.make_event(
                text=f"lr matrix msg {i}",
                event_kind=EventKind.MESSAGE_CREATED,
            )
            await fake_matrix.simulate_inbound(event)

        # Inject 5 from Meshtastic side
        for i in range(5):
            packet = _make_text_packet(
                text=f"lr mesh msg {i}", packet_id=2000 + i
            )
            await fake_mesh.simulate_inbound(packet)

        # Clean stop
        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        # Exactly 10 canonical events persisted
        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events ORDER BY event_id"
        )
        assert len(all_events) == 10, f"Expected 10 events, got {len(all_events)}"

        # 5 from each source
        matrix_events = [e for e in all_events if e["source_adapter"] == "lr-matrix"]
        mesh_events = [e for e in all_events if e["source_adapter"] == "lr-mesh"]
        assert len(matrix_events) == 5
        assert len(mesh_events) == 5

        # Exactly 10 delivery receipts (5 matrix->mesh + 5 mesh->matrix)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter, status FROM delivery_receipts ORDER BY sequence"
        )
        assert len(receipts) == 10, f"Expected 10 receipts, got {len(receipts)}"
        for r in receipts:
            assert r["status"] == "sent"

        mesh_receipts = [r for r in receipts if r["target_adapter"] == "lr-mesh"]
        mx_receipts = [r for r in receipts if r["target_adapter"] == "lr-matrix"]
        assert len(mesh_receipts) == 5
        assert len(mx_receipts) == 5

        # Accounting: inbound_accepted == 10, outbound_delivered == 10
        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 10
        assert snap["outbound_delivered"] == 10
        assert snap["outbound_attempts"] == 10

        # loop_prevented == 0 (bidirectional routes don't echo)
        assert snap["loop_prevented"] == 0

        # Route stats match expected
        stats = route_stats.snapshot()
        assert stats["lr-mx-to-mesh"]["delivered"] == 5
        assert stats["lr-mx-to-mesh"]["loop_prevented"] == 0
        assert stats["lr-mesh-to-mx"]["delivered"] == 5
        assert stats["lr-mesh-to-mx"]["loop_prevented"] == 0

        # Fake adapters received correct number of deliveries
        assert len(fake_mesh.delivered_payloads) == 5
        assert len(fake_matrix.delivered_payloads) == 5

    async def test_snapshot_reflects_totals(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Final accounting snapshot reflects exact totals after bridge run."""
        fake_matrix = FakeMatrixAdapter("snap-mx", channel="!snap:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="snap-mesh")
        )

        route_a = Route(
            id="snap-mx-mesh",
            source=RouteSource(
                adapter="snap-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="snap-mesh", channel="0")],
        )
        route_b = Route(
            id="snap-mesh-mx",
            source=RouteSource(
                adapter="snap-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="snap-mx", channel="!snap:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"snap-mx": fake_matrix, "snap-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(_make_adapter_context("snap-mx", runner))
        await fake_mesh.start(_make_adapter_context("snap-mesh", runner))

        # 3 from matrix, 3 from mesh
        for i in range(3):
            evt = fake_matrix.make_event(
                text=f"snap mx {i}", event_kind=EventKind.MESSAGE_CREATED
            )
            await fake_matrix.simulate_inbound(evt)
        for i in range(3):
            pkt = _make_text_packet(text=f"snap mesh {i}", packet_id=3000 + i)
            await fake_mesh.simulate_inbound(pkt)

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 6
        assert snap["outbound_delivered"] == 6
        assert snap["outbound_attempts"] == 6
        assert snap["outbound_failed"] == 0
        assert snap["loop_prevented"] == 0
        assert snap["capacity_rejections"] == 0

        # All values are deterministic ints (no floats, no None)
        for key, value in snap.items():
            assert isinstance(value, int), f"{key}={value!r} is not int"


# ===================================================================
# 2. Self-message / existing-native-ref deduplication
# ===================================================================


class TestSelfMessagePrevention:
    """Pipeline-level dedup: if an inbound event's native ref already maps
    to an existing canonical event, the pipeline skips store + delivery."""

    async def test_duplicate_native_ref_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inject event with native ref already in storage → no second event."""
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="dedup-target")
        )

        route = Route(
            id="dedup-route",
            source=RouteSource(
                adapter="dedup-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dedup-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"dedup-target": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Pre-store a canonical event and its native ref
        original_event_id = f"orig-{uuid.uuid4()}"
        original_event = CanonicalEvent(
            event_id=original_event_id,
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="dedup-src",
            source_transport_id="dedup-src",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "original message"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="dedup-src",
                native_channel_id="ch-0",
                native_message_id="native-msg-001",
            ),
        )
        await runner.handle_ingress(original_event)

        # Verify the first event was stored and delivered
        assert accounting.snapshot()["inbound_accepted"] == 1
        assert accounting.snapshot()["outbound_delivered"] == 1
        assert len(fake_target.delivered_payloads) == 1

        # Now inject a SECOND event with the SAME native ref
        duplicate_event = CanonicalEvent(
            event_id=f"dup-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="dedup-src",
            source_transport_id="dedup-src",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "duplicate message"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="dedup-src",
                native_channel_id="ch-0",
                native_message_id="native-msg-001",
            ),
        )
        outcomes = await runner.handle_ingress(duplicate_event)

        # Pipeline suppressed the duplicate
        assert outcomes == []

        # No second canonical event stored
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events"
        )
        assert len(all_events) == 1
        assert all_events[0]["event_id"] == original_event_id

        # No second delivery receipt
        receipts = await temp_storage._read_all(
            "SELECT event_id FROM delivery_receipts"
        )
        assert len(receipts) == 1

        # Still only one delivered payload
        assert len(fake_target.delivered_payloads) == 1

        # Accounting: inbound_accepted still 1, loop_prevented incremented
        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 1, "Duplicate should not increment inbound_accepted"
        assert snap["loop_prevented"] == 1, "Duplicate should increment loop_prevented"

        await runner.stop()

    async def test_no_dedup_when_native_ref_absent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Events without source_native_ref are never deduplicated."""
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="nodup-target")
        )

        route = Route(
            id="nodup-route",
            source=RouteSource(
                adapter="nodup-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="nodup-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"nodup-target": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Two events with NO source_native_ref — both should go through
        for i in range(2):
            event = CanonicalEvent(
                event_id=f"nodup-{i}",
                event_kind=EventKind.MESSAGE_CREATED,
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="nodup-src",
                source_transport_id="nodup-src",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"body": f"msg {i}"},
                metadata=EventMetadata(),
                # No source_native_ref
            )
            await runner.handle_ingress(event)

        await runner.stop()

        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 2
        assert snap["outbound_delivered"] == 2
        assert snap["loop_prevented"] == 0
        assert len(fake_target.delivered_payloads) == 2


class TestLoopPreventionExistingRef:
    """Outbound produces native ref. Simulate that native ref reappearing
    inbound (echo). Verify loop_prevented increments and no new delivery."""

    async def test_echo_native_ref_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Outbound delivery creates native ref. Same ref inbound → suppressed."""
        fake_matrix = FakeMatrixAdapter("echo-mx", channel="!echo:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="echo-mesh")
        )

        route_a = Route(
            id="echo-mx-mesh",
            source=RouteSource(
                adapter="echo-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="echo-mesh", channel="0")],
        )
        route_b = Route(
            id="echo-mesh-mx",
            source=RouteSource(
                adapter="echo-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="echo-mx", channel="!echo:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"echo-mx": fake_matrix, "echo-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(_make_adapter_context("echo-mx", runner))
        await fake_mesh.start(_make_adapter_context("echo-mesh", runner))

        # Step 1: Inject a message from Matrix → Meshtastic
        event = fake_matrix.make_event(
            text="echo test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        # After delivery, meshtastic adapter creates an outbound native ref.
        # Now simulate that native ref coming back inbound (echo).
        # The outbound delivery to mesh creates a native ref with
        # adapter="echo-mesh", native_message_id=1, native_channel_id="0"
        echo_event = CanonicalEvent(
            event_id=f"echo-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="echo-mesh",
            source_transport_id="!node1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "echo test"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="echo-mesh",
                native_channel_id="0",
                native_message_id="1",  # matches outbound ref from fake_mesh
            ),
        )
        outcomes = await runner.handle_ingress(echo_event)

        # Echo was suppressed
        assert outcomes == []

        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 1, "Only the first message accepted"
        assert snap["outbound_delivered"] == 1, "Only one outbound delivery"
        assert snap["loop_prevented"] == 1, "Echo suppressed as loop"

        # Only one canonical event stored
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events"
        )
        assert len(all_events) == 1

        # Only one delivery receipt
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 1

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()


# ===================================================================
# 3. Real wrapper multi-message callback tests
# ===================================================================


class TestMatrixWrapperMultiCallback:
    """Real MatrixAdapter._on_room_message with 5 messages via mocked nio.
    Assert exact event count, receipt count, stable event_id→room_id
    mapping, no duplicates."""

    async def test_five_messages_via_on_room_message(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """5 distinct nio events → 5 canonical events, 5 receipts, stable mapping."""
        matrix_adapter = MatrixAdapter(
            _make_matrix_config(
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mx-multi": matrix_adapter, "mx-multi-target": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await matrix_adapter.start(_make_adapter_context("mx-multi", runner))
        await fake_target.start(_make_adapter_context("mx-multi-target", runner))

        room = _make_nio_room("!multi_room:example.com")

        try:
            # Inject 5 distinct messages
            for i in range(5):
                nio_event = _make_nio_event(
                    sender=f"@user{i}:example.com",
                    event_id=f"$multi-evt-{i:03d}",
                    body=f"multi message {i}",
                )
                await matrix_adapter._on_room_message(room, nio_event)

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

            # Verify event_id → room_id mapping via native refs
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
        matrix_adapter = MatrixAdapter(
            _make_matrix_config(adapter_id="mx-self")
        )

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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mx-self": matrix_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await matrix_adapter.start(_make_adapter_context("mx-self", runner))

        try:
            room = _make_nio_room("!self_room:example.com")
            # Event from the bot's own user_id
            self_event = _make_nio_event(
                sender="@bot:example.com",  # matches config.user_id
                event_id="$self-evt-001",
                body="this is from myself",
            )
            await matrix_adapter._on_room_message(room, self_event)

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
        """5 distinct packets → 5 canonical events, 5 receipts, consistent metadata."""
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mesh-multi-src": mesh_adapter, "mesh-multi-dst": fake_target},
            rendering_pipeline=rp,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(_make_adapter_context("mesh-multi-src", runner))
        await fake_target.start(_make_adapter_context("mesh-multi-dst", runner))

        try:
            # Inject 5 distinct packets
            for i in range(5):
                packet = _make_text_packet(
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

        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"mesh-meta-multi": mesh_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        await mesh_adapter.start(_make_adapter_context("mesh-meta-multi", runner))

        try:
            sender_id = "!consistent_node"
            for i in range(5):
                packet = _make_text_packet(
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


# ===================================================================
# 4. Loop prevention hardening
# ===================================================================


class TestLoopPreventionHardening:
    """Hardened loop prevention: source==target, route-trace guard,
    native-ref cycle detection."""

    async def test_source_equals_target_adapter(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inject event whose source_adapter == target_adapter.
        Assert loop_prevented increments, no delivery."""
        fake_adapter = FakeMatrixAdapter("loop-src-tgt", channel="!loop:fake")

        # Route where source adapter is also the only target
        route = Route(
            id="loop-self-route",
            source=RouteSource(
                adapter="loop-src-tgt",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="loop-src-tgt", channel="!loop:fake")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"loop-src-tgt": fake_adapter},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_adapter.start(_make_adapter_context("loop-src-tgt", runner))

        event = fake_adapter.make_event(
            text="self-loop hardening",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        outcomes = await runner.handle_ingress(event)

        await fake_adapter.stop()
        await runner.stop()

        # loop_prevented incremented
        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1

        # Route stats show loop_prevented
        stats = route_stats.snapshot()
        assert stats["loop-self-route"]["loop_prevented"] == 1
        assert stats["loop-self-route"]["delivered"] == 0

        # Outcome status is "skipped"
        assert len(outcomes) == 1
        assert outcomes[0].status == "skipped"
        assert "loop_prevented" in (outcomes[0].error or "")

        # No delivery to the adapter
        assert len(fake_adapter.delivered_payloads) == 0

        # No delivery receipt persisted
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 0

    async def test_route_trace_guard_fires(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inject event with route-trace showing it already traversed a route.
        Assert route-trace guard fires."""
        fake_matrix = FakeMatrixAdapter("trace-mx", channel="!trace:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="trace-mesh")
        )

        route = Route(
            id="trace-route",
            source=RouteSource(
                adapter="trace-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="trace-mesh", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"trace-mx": fake_matrix, "trace-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Create event with a pre-existing route_trace showing it already
        # traversed this route (simulating re-routing from a multi-hop)
        from medre.core.events.metadata import RoutingMetadata

        event = CanonicalEvent(
            event_id=f"trace-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="trace-mx",
            source_transport_id="trace-mx",
            source_channel_id="!trace:fake",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "trace guard test"},
            metadata=EventMetadata(
                routing=RoutingMetadata(
                    matched_routes=("trace-route",),
                    route_trace=("trace-route", "other-route", "trace-route"),
                ),
            ),
        )
        outcomes = await runner.handle_ingress(event)

        await runner.stop()

        # Route-trace guard fired
        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1, "Route-trace guard should increment loop_prevented"

        stats = route_stats.snapshot()
        assert stats["trace-route"]["loop_prevented"] == 1

        # Outcome is skipped
        assert len(outcomes) == 1
        assert outcomes[0].status == "skipped"
        assert "route already traversed" in (outcomes[0].error or "")

        # No delivery
        assert len(fake_mesh.delivered_payloads) == 0

    async def test_native_ref_cycle_detection(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inject event from adapter A whose native ref points back to
        adapter A's previously stored outbound ref → cycle detected via
        dedup."""
        fake_matrix = FakeMatrixAdapter("cycle-mx", channel="!cycle:fake")
        fake_mesh = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="cycle-mesh")
        )

        route_a = Route(
            id="cycle-mx-mesh",
            source=RouteSource(
                adapter="cycle-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="cycle-mesh", channel="0")],
        )
        route_b = Route(
            id="cycle-mesh-mx",
            source=RouteSource(
                adapter="cycle-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="cycle-mx", channel="!cycle:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"cycle-mx": fake_matrix, "cycle-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(_make_adapter_context("cycle-mx", runner))
        await fake_mesh.start(_make_adapter_context("cycle-mesh", runner))

        # Step 1: Message from matrix → delivered to mesh, creating outbound
        # native ref for cycle-mesh adapter
        event_1 = fake_matrix.make_event(
            text="cycle test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event_1)

        # After delivery, fake_mesh creates an outbound native ref:
        #   adapter="cycle-mesh", native_message_id="1", native_channel_id="0"
        # Step 2: Simulate that native ref coming back inbound from mesh side
        # with a native_ref pointing back to adapter A (cycle-mesh)
        cycle_event = CanonicalEvent(
            event_id=f"cycle-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="cycle-mesh",
            source_transport_id="!meshnode",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "cycle echo"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="cycle-mesh",
                native_channel_id="0",
                native_message_id="1",  # matches outbound from fake_mesh delivery
            ),
        )
        outcomes = await runner.handle_ingress(cycle_event)

        # Cycle detected — event suppressed
        assert outcomes == []

        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1
        assert snap["inbound_accepted"] == 1, "Only the first message accepted"

        # Only one canonical event
        all_events = await temp_storage._read_all(
            "SELECT event_id, source_adapter FROM canonical_events"
        )
        assert len(all_events) == 1
        assert all_events[0]["source_adapter"] == "cycle-mx"

        # Only one delivery receipt (to mesh)
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 1
        assert receipts[0]["target_adapter"] == "cycle-mesh"

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()

    async def test_all_three_guards_independent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Fire all three guards independently, verify each increments
        loop_prevented correctly."""
        fake_a = FakeMatrixAdapter("guard-a", channel="!ga:fake")
        fake_b = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="guard-b")
        )

        route = Route(
            id="guard-route",
            source=RouteSource(
                adapter="guard-a",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="guard-a", channel="!ga:fake"),  # self-loop
                RouteTarget(adapter="guard-b", channel="0"),
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
            adapters={"guard-a": fake_a, "guard-b": fake_b},
            rendering_pipeline=rp,
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_a.start(_make_adapter_context("guard-a", runner))
        await fake_b.start(_make_adapter_context("guard-b", runner))

        # Guard 1: source==target (self-loop)
        event_1 = fake_a.make_event(
            text="guard 1 self-loop",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        outcomes_1 = await runner.handle_ingress(event_1)
        # One target is self-loop (skipped), one is normal (delivered)
        assert len(outcomes_1) == 2
        skipped = [o for o in outcomes_1 if o.status == "skipped"]
        succeeded = [o for o in outcomes_1 if o.status == "success"]
        assert len(skipped) == 1
        assert len(succeeded) == 1
        assert snap_value(accounting, "loop_prevented") == 1

        # Guard 2: route-trace guard (event already traversed this route)
        from medre.core.events.metadata import RoutingMetadata

        event_2 = CanonicalEvent(
            event_id=f"guard2-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="guard-a",
            source_transport_id="guard-a",
            source_channel_id="!ga:fake",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "guard 2 trace"},
            metadata=EventMetadata(
                routing=RoutingMetadata(
                    route_trace=("guard-route", "guard-route"),
                ),
            ),
        )
        outcomes_2 = await runner.handle_ingress(event_2)
        # Both targets skipped by route-trace guard
        assert len(outcomes_2) == 2
        for o in outcomes_2:
            assert o.status == "skipped"
        assert snap_value(accounting, "loop_prevented") == 3  # +2 from route-trace

        # Guard 3: native-ref dedup (same native ref seen before)
        # First, create an event with a known native ref
        event_3 = CanonicalEvent(
            event_id=f"guard3a-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="guard-a",
            source_transport_id="guard-a",
            source_channel_id="!ga:fake",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "guard 3 first"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="guard-a",
                native_channel_id="!ga:fake",
                native_message_id="guard3-native-001",
            ),
        )
        await runner.handle_ingress(event_3)
        lp_after_first = snap_value(accounting, "loop_prevented")

        # Now send the duplicate
        event_3_dup = CanonicalEvent(
            event_id=f"guard3b-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="guard-a",
            source_transport_id="guard-a",
            source_channel_id="!ga:fake",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "guard 3 dup"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="guard-a",
                native_channel_id="!ga:fake",
                native_message_id="guard3-native-001",
            ),
        )
        outcomes_3 = await runner.handle_ingress(event_3_dup)
        assert outcomes_3 == []
        assert snap_value(accounting, "loop_prevented") == lp_after_first + 1

        await fake_a.stop()
        await fake_b.stop()
        await runner.stop()


def snap_value(accounting: RuntimeAccounting, key: str) -> int:
    """Helper to read a single counter from accounting snapshot."""
    return accounting.snapshot()[key]
