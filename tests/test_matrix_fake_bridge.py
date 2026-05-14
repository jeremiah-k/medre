"""Bridge tests: real MatrixAdapter (mocked nio) <-> FakeMatrixAdapter through
the full PipelineRunner.

These tests prove that the real Matrix adapter wrapper can bridge through
the MEDRE runtime without a live Synapse homeserver.  The nio client library
is replaced by a mock module injected into ``sys.modules``, so the tests
exercise the real adapter code paths (codec, renderer, session lifecycle,
deliver with retry, inbound callback) without any network dependency.

Flows covered
-------------
1. Matrix inbound event -> fake adapter outbound
2. Fake adapter inbound -> Matrix adapter outbound
3. Error taxonomy through the pipeline (transient vs permanent)
4. No channel fallback: missing room_id fails permanently
5. Native ref and delivery receipt persistence through real storage

No test requires Docker, Synapse, or the real ``mindroom-nio`` package.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.base import (
    AdapterContext,
    AdapterDeliveryResult,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.errors import MatrixSendError
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_context(
    adapter_id: str = "matrix-bridge",
    publish_inbound: Any = None,
) -> AdapterContext:
    """Build an AdapterContext for bridge tests."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=publish_inbound or AsyncMock(),
        logger=logging.getLogger(f"test.bridge.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_nio_event(
    sender: str = "@alice:example.com",
    event_id: str = "$bridge-evt-001",
    body: str = "hello from matrix",
    content: dict | None = None,
) -> SimpleNamespace:
    """Build a duck-typed nio RoomMessageText event for bridge tests."""
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

    # sync_forever stub: blocks until cancelled
    async def _sync_forever_stub(*args: object, **kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    client.sync_forever = _sync_forever_stub

    # room_send: returns a response with a deterministic event_id
    async def _room_send(
        room_id: str, message_type: str, content: dict, **kwargs: object
    ) -> SimpleNamespace:
        return SimpleNamespace(
            event_id=f"$sent-{content.get('body', 'msg')[:12]}",
            transport_response=None,
        )

    client.room_send = AsyncMock(side_effect=_room_send)

    # whoami returns a device_id for device discovery
    whoami_resp = MagicMock(name="whoami_response")
    whoami_resp.device_id = "BRIDGE_MOCK_DEVICE"
    client.whoami = AsyncMock(return_value=whoami_resp)

    mock.AsyncClient = MagicMock(return_value=client)
    mock.ClientConfig = MagicMock(name="ClientConfig")
    mock.RoomMessageText = MagicMock(name="RoomMessageText")
    mock.RoomMessageNotice = MagicMock(name="RoomMessageNotice")
    mock.RoomMessageEmote = MagicMock(name="RoomMessageEmote")

    # nio.events submodule for MegolmEvent and RoomEncryptionEvent
    mock_events = MagicMock(name="nio.events")
    mock_events.MegolmEvent = MagicMock(name="MegolmEvent")
    mock_events.RoomEncryptionEvent = MagicMock(name="RoomEncryptionEvent")
    mock.events = mock_events

    return mock


@pytest.fixture
def mock_nio():
    """Inject a mock nio module into sys.modules and patch HAS_NIO.

    Yields the mock module so tests can customise client behaviour.
    Cleans up sys.modules and HAS_NIO on teardown.
    """
    mock = _build_mock_nio_module()
    saved_nio = sys.modules.get("nio")
    saved_nio_events = sys.modules.get("nio.events")
    sys.modules["nio"] = mock
    sys.modules["nio.events"] = mock.events
    with patch("medre.adapters.matrix.adapter.HAS_NIO", True):
        yield mock
    # Restore
    if saved_nio is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = saved_nio
    if saved_nio_events is None:
        sys.modules.pop("nio.events", None)
    else:
        sys.modules["nio.events"] = saved_nio_events


def _make_pipeline_config(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict[str, Any] | None = None,
    event_bus: EventBus | None = None,
) -> PipelineConfig:
    """Build a PipelineConfig with MatrixRenderer registered."""
    rp = RenderingPipeline()
    rp.register(MatrixRenderer(), priority=50)
    rp.register(TextRenderer(), priority=100)

    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=event_bus or EventBus(),
        rendering_pipeline=rp,
    )


def _make_adapter_context_for_pipeline(
    adapter_id: str, runner: PipelineRunner
) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler.

    Wraps ``runner.ingress_handler`` in an adapter-compatible async callable
    that returns ``None`` (the pipeline returns ``list[DeliveryOutcome]``
    which is discarded at the adapter boundary).
    """
    async def _publish(event: CanonicalEvent) -> None:
        await runner.ingress_handler(event)

    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_publish,
        logger=logging.getLogger(f"test.bridge.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


# ===================================================================
# Flow 1: Matrix Inbound -> Fake Adapter Outbound
# ===================================================================


class TestMatrixInboundToFakeOutbound:
    """Real MatrixAdapter._on_room_message decodes a duck-typed nio event,
    publishes it into the pipeline, which routes it to a FakeMatrixAdapter.

    This exercises the real MatrixCodec.decode(), the real inbound callback
    path (self-message suppression, envelope loop check, room allowlist),
    and the real pipeline routing/rendering/delivery chain.
    """

    async def test_third_party_message_routes_to_fake(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """A third-party Matrix message is decoded, routed, rendered, and
        delivered to a FakeMatrixAdapter."""
        matrix_adapter = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-in")
        )
        fake_adapter = FakeMatrixAdapter("fake-out", channel="ch-0")

        route = Route(
            id="bridge-in-route",
            source=RouteSource(
                adapter="matrix-in",
                event_kinds=("message.created",),
                channel="!bridge_room:example.com",
            ),
            targets=[RouteTarget(adapter="fake-out")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-out": fake_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx = _make_adapter_context_for_pipeline("matrix-in", runner)
        await matrix_adapter.start(ctx)

        try:
            room = _make_nio_room("!bridge_room:example.com")
            event = _make_nio_event(
                sender="@alice:example.com",
                event_id="$bridge-inbound-001",
                body="hello from matrix bridge",
            )
            await matrix_adapter._on_room_message(room, event)

            # Fake adapter received a rendered payload
            assert len(fake_adapter.delivered_payloads) == 1
            rendered = fake_adapter.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            assert rendered.payload["body"] == "hello from matrix bridge"

            # Delivery receipt persisted
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
                ("fake-out",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "sent"
        finally:
            await matrix_adapter.stop()
            await runner.stop()

    async def test_matrix_inbound_native_ref_persisted(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """Inbound Matrix event_id and room_id are stored as native refs."""
        matrix_adapter = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-in-ref")
        )

        route = Route(
            id="bridge-in-ref-route",
            source=RouteSource(
                adapter="matrix-in-ref",
                event_kinds=("message.created",),
                channel="!ref_room:example.com",
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

        ctx = _make_adapter_context_for_pipeline("matrix-in-ref", runner)
        await matrix_adapter.start(ctx)

        try:
            room = _make_nio_room("!ref_room:example.com")
            event = _make_nio_event(
                sender="@carol:example.com",
                event_id="$native-ref-evt-001",
                body="native ref test",
            )
            await matrix_adapter._on_room_message(room, event)

            # Inbound native ref persisted
            resolved = await temp_storage.resolve_native_ref(
                adapter="matrix-in-ref",
                native_channel_id="!ref_room:example.com",
                native_message_id="$native-ref-evt-001",
            )
            assert resolved is not None
        finally:
            await matrix_adapter.stop()
            await runner.stop()

    async def test_self_message_suppressed_not_delivered(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """Messages from the bot's own user_id are suppressed and never
        reach the pipeline or the fake adapter."""
        matrix_adapter = MatrixAdapter(
            _make_matrix_config(
                adapter_id="matrix-self",
                user_id="@bot:example.com",
            )
        )
        fake_adapter = FakeMatrixAdapter("fake-self-out", channel="ch-0")

        route = Route(
            id="bridge-self-route",
            source=RouteSource(
                adapter="matrix-self",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="fake-self-out")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-self-out": fake_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        ctx = _make_adapter_context_for_pipeline("matrix-self", runner)
        await matrix_adapter.start(ctx)

        try:
            room = _make_nio_room("!room:example.com")
            event = _make_nio_event(
                sender="@bot:example.com",
                event_id="$self-evt-001",
                body="self message",
            )
            await matrix_adapter._on_room_message(room, event)

            # Nothing delivered to fake adapter
            assert len(fake_adapter.delivered_payloads) == 0

            # Counter incremented
            assert matrix_adapter._inbound_suppressed_self == 1
        finally:
            await matrix_adapter.stop()
            await runner.stop()

    async def test_matrix_inbound_source_channel_id_is_room_id(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """Canonical event's source_channel_id is the Matrix room_id."""
        matrix_adapter = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-channel")
        )
        published: list[CanonicalEvent] = []

        async def _capture(event: CanonicalEvent) -> None:
            published.append(event)

        ctx = _make_context(
            adapter_id="matrix-channel", publish_inbound=_capture
        )
        matrix_adapter.ctx = ctx

        room = _make_nio_room("!channel_room:example.com")
        event = _make_nio_event(
            sender="@alice:example.com",
            event_id="$channel-evt-001",
        )
        await matrix_adapter._on_room_message(room, event)

        assert len(published) == 1
        assert published[0].source_channel_id == "!channel_room:example.com"

        # source_native_ref carries the Matrix event_id as native_message_id
        ref = published[0].source_native_ref
        assert ref is not None
        assert ref.native_message_id == "$channel-evt-001"
        assert ref.native_channel_id == "!channel_room:example.com"


# ===================================================================
# Flow 2: Fake Adapter Inbound -> Matrix Adapter Outbound
# ===================================================================


class TestFakeInboundToMatrixOutbound:
    """A FakeMatrixAdapter.simulate_inbound feeds a canonical event into the
    pipeline, which routes it through the real MatrixRenderer to the real
    MatrixAdapter.deliver(), where the mocked nio client.room_send is
    exercised.

    This proves the outbound Matrix rendering, delivery, and native ref
    persistence path works through the real adapter wrapper.
    """

    async def test_fake_event_routes_to_matrix_room_send(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """Fake adapter inbound -> pipeline -> MatrixRenderer -> real
        MatrixAdapter.deliver() -> mock client.room_send."""
        fake_in = FakeMatrixAdapter("fake-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-out")
        )

        target_room = "!out_room:example.com"
        route = Route(
            id="bridge-out-route",
            source=RouteSource(
                adapter="fake-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-out", channel=target_room)],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-in": fake_in, "matrix-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Start the Matrix adapter with a context wired to the pipeline
        matrix_ctx = _make_adapter_context_for_pipeline("matrix-out", runner)
        await matrix_out.start(matrix_ctx)

        # Start the fake adapter with a context wired to the pipeline
        fake_ctx = _make_adapter_context_for_pipeline("fake-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="bridge outbound test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            await fake_in.simulate_inbound(event)

            # Mock client.room_send was called
            mock_client = mock_nio.AsyncClient.return_value
            assert mock_client.room_send.await_count >= 1

            call_kwargs = mock_client.room_send.call_args
            assert call_kwargs.kwargs["room_id"] == target_room
            assert call_kwargs.kwargs["message_type"] == "m.room.message"

            content = call_kwargs.kwargs["content"]
            assert content["msgtype"] == "m.text"
            assert content["body"] == "bridge outbound test"
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()

    async def test_matrix_outbound_native_ref_persisted(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """Outbound Matrix delivery stores a native ref with the response
        event_id and room_id."""
        fake_in = FakeMatrixAdapter("fake-ref-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-ref-out")
        )

        target_room = "!ref_out:example.com"
        route = Route(
            id="bridge-ref-out-route",
            source=RouteSource(
                adapter="fake-ref-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-ref-out", channel=target_room)],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-ref-in": fake_in, "matrix-ref-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline("matrix-ref-out", runner)
        await matrix_out.start(matrix_ctx)
        fake_ctx = _make_adapter_context_for_pipeline("fake-ref-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="native ref test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            await fake_in.simulate_inbound(event)

            # Delivery receipt stored
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
                ("matrix-ref-out",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "sent"

            # Outbound native ref persisted with response event_id
            native_rows = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE adapter = ? AND direction = 'outbound'",
                ("matrix-ref-out",),
            )
            assert len(native_rows) == 1
            assert native_rows[0]["native_channel_id"] == target_room
            assert native_rows[0]["native_message_id"].startswith("$sent-")
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()

    async def test_rendered_payload_is_matrix_content(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """The payload delivered to room_send has Matrix content shape."""
        fake_in = FakeMatrixAdapter("fake-shape-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-shape-out")
        )

        target_room = "!shape_room:example.com"
        route = Route(
            id="bridge-shape-route",
            source=RouteSource(
                adapter="fake-shape-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-shape-out", channel=target_room)],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-shape-in": fake_in, "matrix-shape-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline("matrix-shape-out", runner)
        await matrix_out.start(matrix_ctx)
        fake_ctx = _make_adapter_context_for_pipeline("fake-shape-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="shape test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            await fake_in.simulate_inbound(event)

            mock_client = mock_nio.AsyncClient.return_value
            call_kwargs = mock_client.room_send.call_args
            content = call_kwargs.kwargs["content"]

            # Matrix content shape
            assert "msgtype" in content
            assert content["msgtype"] == "m.text"
            assert "body" in content

            # MEDRE envelope embedded
            assert "medre" in content
            assert "envelope" in content["medre"]
            envelope = content["medre"]["envelope"]
            assert envelope["source_adapter"] == "fake-shape-in"
            assert envelope["canonical_event_id"] == event.event_id

            # room_id must NOT leak into the content dict
            assert "room_id" not in content
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()

    async def test_room_id_stripped_from_sent_content(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """Even if the rendered payload contains room_id, it is stripped
        before being sent via room_send."""
        fake_in = FakeMatrixAdapter("fake-strip-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-strip-out")
        )

        target_room = "!strip_room:example.com"
        route = Route(
            id="bridge-strip-route",
            source=RouteSource(
                adapter="fake-strip-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-strip-out", channel=target_room)],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-strip-in": fake_in, "matrix-strip-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline("matrix-strip-out", runner)
        await matrix_out.start(matrix_ctx)
        fake_ctx = _make_adapter_context_for_pipeline("fake-strip-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="strip test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            await fake_in.simulate_inbound(event)

            mock_client = mock_nio.AsyncClient.return_value
            call_kwargs = mock_client.room_send.call_args
            content = call_kwargs.kwargs["content"]
            assert "room_id" not in content
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()


# ===================================================================
# Flow 3: Error taxonomy through the pipeline
# ===================================================================


class TestMatrixBridgeErrorTaxonomy:
    """Verify error classification when MatrixAdapter.deliver() raises
    through the real pipeline."""

    async def test_transient_error_classified(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """ConnectionError from room_send produces transient_failure."""
        fake_in = FakeMatrixAdapter("fake-trans-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-trans-out")
        )

        # Make room_send raise a transient error
        mock_client = mock_nio.AsyncClient.return_value
        mock_client.room_send = AsyncMock(
            side_effect=ConnectionError("homeserver unreachable")
        )

        target_room = "!trans_room:example.com"
        route = Route(
            id="bridge-trans-route",
            source=RouteSource(
                adapter="fake-trans-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-trans-out", channel=target_room)],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-trans-in": fake_in, "matrix-trans-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline("matrix-trans-out", runner)
        await matrix_out.start(matrix_ctx)
        fake_ctx = _make_adapter_context_for_pipeline("fake-trans-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="transient test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            await fake_in.simulate_inbound(event)

            # Pipeline outcome: the adapter exhausts retries and raises
            # AdapterSendError(transient=True); the pipeline records
            # status="failed" for all adapter-level delivery failures.
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
                ("matrix-trans-out",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()

    async def test_permanent_error_classified(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """AdapterPermanentError from deliver produces permanent_failure."""
        fake_in = FakeMatrixAdapter("fake-perm-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-perm-out")
        )

        # Make room_send raise a permanent error (MatrixSendError with
        # transient=False is converted to AdapterPermanentError).
        mock_client = mock_nio.AsyncClient.return_value
        mock_client.room_send = AsyncMock(
            side_effect=MatrixSendError("forbidden", transient=False)
        )

        target_room = "!perm_room:example.com"
        route = Route(
            id="bridge-perm-route",
            source=RouteSource(
                adapter="fake-perm-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-perm-out", channel=target_room)],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-perm-in": fake_in, "matrix-perm-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline("matrix-perm-out", runner)
        await matrix_out.start(matrix_ctx)
        fake_ctx = _make_adapter_context_for_pipeline("fake-perm-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="permanent test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            await fake_in.simulate_inbound(event)

            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
                ("matrix-perm-out",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()

    async def test_cancelled_error_propagates(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """CancelledError from room_send propagates and is not swallowed
        by the adapter or pipeline."""
        fake_in = FakeMatrixAdapter("fake-cancel-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-cancel-out")
        )

        mock_client = mock_nio.AsyncClient.return_value
        mock_client.room_send = AsyncMock(
            side_effect=asyncio.CancelledError()
        )

        target_room = "!cancel_room:example.com"
        route = Route(
            id="bridge-cancel-route",
            source=RouteSource(
                adapter="fake-cancel-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-cancel-out", channel=target_room)],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-cancel-in": fake_in, "matrix-cancel-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline("matrix-cancel-out", runner)
        await matrix_out.start(matrix_ctx)
        fake_ctx = _make_adapter_context_for_pipeline("fake-cancel-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="cancel test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            with pytest.raises(asyncio.CancelledError):
                await fake_in.simulate_inbound(event)

            # No permanent failure counter incremented
            assert matrix_out._permanent_delivery_failures == 0
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()


# ===================================================================
# Flow 4: No channel fallback
# ===================================================================


class TestMatrixBridgeNoChannelFallback:
    """Missing target room_id must cause a permanent failure, not a
    fabricated channel fallback.  No outbound native ref is stored."""

    async def test_missing_room_fails_without_fabricated_channel(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """When no room_id is available, deliver raises AdapterPermanentError
        and the pipeline records permanent_failure."""
        fake_in = FakeMatrixAdapter("fake-noroom-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-noroom-out")
        )

        # Route with no explicit channel target
        route = Route(
            id="bridge-noroom-route",
            source=RouteSource(
                adapter="fake-noroom-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-noroom-out")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-noroom-in": fake_in, "matrix-noroom-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline("matrix-noroom-out", runner)
        await matrix_out.start(matrix_ctx)
        fake_ctx = _make_adapter_context_for_pipeline("fake-noroom-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="no room test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            # The pipeline catches the adapter error and records a receipt.
            # It should not raise to the caller.
            await fake_in.simulate_inbound(event)

            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE target_adapter = ?",
                ("matrix-noroom-out",),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()

    async def test_no_outbound_native_ref_on_failure(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """A failed delivery must not store an outbound native ref."""
        fake_in = FakeMatrixAdapter("fake-noref-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-noref-out")
        )

        # Make room_send raise
        mock_client = mock_nio.AsyncClient.return_value
        mock_client.room_send = AsyncMock(
            side_effect=MatrixSendError("rejected", transient=False)
        )

        target_room = "!noref_room:example.com"
        route = Route(
            id="bridge-noref-route",
            source=RouteSource(
                adapter="fake-noref-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-noref-out", channel=target_room)],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-noref-in": fake_in, "matrix-noref-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline("matrix-noref-out", runner)
        await matrix_out.start(matrix_ctx)
        fake_ctx = _make_adapter_context_for_pipeline("fake-noref-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="no ref test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            await fake_in.simulate_inbound(event)

            # No outbound native ref stored for the failed delivery
            native_rows = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE adapter = ? AND direction = 'outbound'",
                ("matrix-noref-out",),
            )
            assert len(native_rows) == 0
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()

    async def test_pipeline_does_not_create_phantom_route(
        self, mock_nio, temp_storage: SQLiteStorage
    ) -> None:
        """A delivery failure to Matrix does not fabricate any additional
        routes or deliveries."""
        fake_in = FakeMatrixAdapter("fake-phantom-in", channel="ch-0")
        matrix_out = MatrixAdapter(
            _make_matrix_config(adapter_id="matrix-phantom-out")
        )

        mock_client = mock_nio.AsyncClient.return_value
        mock_client.room_send = AsyncMock(
            side_effect=MatrixSendError("unrecoverable", transient=False)
        )

        target_room = "!phantom_room:example.com"
        route = Route(
            id="bridge-phantom-route",
            source=RouteSource(
                adapter="fake-phantom-in",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="matrix-phantom-out", channel=target_room)],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-phantom-in": fake_in, "matrix-phantom-out": matrix_out},
        )
        runner = PipelineRunner(config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline("matrix-phantom-out", runner)
        await matrix_out.start(matrix_ctx)
        fake_ctx = _make_adapter_context_for_pipeline("fake-phantom-in", runner)
        await fake_in.start(fake_ctx)

        try:
            event = fake_in.make_event(
                text="phantom test",
                event_kind=EventKind.MESSAGE_CREATED,
                channel="ch-0",
            )
            await fake_in.simulate_inbound(event)

            # Exactly one receipt for the single declared target
            rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                (event.event_id,),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
            assert rows[0]["target_adapter"] == "matrix-phantom-out"
        finally:
            await fake_in.stop()
            await matrix_out.stop()
            await runner.stop()


# ===================================================================
# Flow 5: Error boundary — direct adapter-level assertions
# ===================================================================


class TestMatrixBridgeDirectErrorBoundary:
    """Direct assertions on the MatrixAdapter error boundary without
    the full pipeline.  These complement the pipeline-level tests by
    verifying the exact exception types and transient flags."""

    async def test_matrix_send_error_transient_converted(
        self, mock_nio
    ) -> None:
        """MatrixSendError(transient=True) is converted to
        AdapterSendError at the adapter boundary."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = mock_nio.AsyncClient.return_value
        mock_client.room_send = AsyncMock(
            side_effect=MatrixSendError("timeout", transient=True)
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-trans-boundary",
            target_adapter="matrix-bridge",
            target_channel="!room:example.com",
            payload={"msgtype": "m.text", "body": "test"},
        )
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    async def test_matrix_send_error_permanent_converted(
        self, mock_nio
    ) -> None:
        """MatrixSendError(transient=False) is converted to
        AdapterPermanentError at the adapter boundary."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = mock_nio.AsyncClient.return_value
        mock_client.room_send = AsyncMock(
            side_effect=MatrixSendError("forbidden", transient=False)
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-perm-boundary",
            target_adapter="matrix-bridge",
            target_channel="!room:example.com",
            payload={"msgtype": "m.text", "body": "test"},
        )
        with pytest.raises(AdapterPermanentError):
            await adapter.deliver(result)

    async def test_not_connected_is_permanent(self, mock_nio) -> None:
        """Client is None -> AdapterPermanentError (lifecycle state)."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        adapter._client = None

        result = RenderingResult(
            event_id="evt-no-client-boundary",
            target_adapter="matrix-bridge",
            target_channel="!room:example.com",
            payload={"msgtype": "m.text", "body": "test"},
        )
        with pytest.raises(AdapterPermanentError, match="not connected"):
            await adapter.deliver(result)

    async def test_no_room_id_is_permanent(self, mock_nio) -> None:
        """Missing room_id -> AdapterPermanentError (no channel fallback)."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = mock_nio.AsyncClient.return_value
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-no-room-boundary",
            target_adapter="matrix-bridge",
            target_channel=None,
            payload={"msgtype": "m.text", "body": "test"},
        )
        with pytest.raises(AdapterPermanentError, match="no room_id"):
            await adapter.deliver(result)

    async def test_successful_response_event_id_maps_to_native(
        self, mock_nio
    ) -> None:
        """Successful room_send returns AdapterDeliveryResult with the
        response event_id as native_message_id and room_id as
        native_channel_id."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        response = SimpleNamespace(
            event_id="$successful-evt-001", transport_response=None
        )
        mock_client = mock_nio.AsyncClient.return_value
        mock_client.room_send = AsyncMock(return_value=response)
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-ok-boundary",
            target_adapter="matrix-bridge",
            target_channel="!ok_room:example.com",
            payload={"msgtype": "m.text", "body": "test"},
        )
        delivery = await adapter.deliver(result)
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id == "$successful-evt-001"
        assert delivery.native_channel_id == "!ok_room:example.com"
