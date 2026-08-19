"""Executable Meshtastic behavior requirements derived from the MMRelay corpus.

MMRelay supplies a mature corpus of transport edge cases.  These tests encode
those transport expectations at MEDRE's session, adapter, and renderer seams
without importing or depending on MMRelay at runtime.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.rendering.renderer import RenderingContext


def _config(**overrides: Any) -> MeshtasticConfig:
    values: dict[str, Any] = {
        "adapter_id": "mesh-reference",
        "radio_relay_prefix": "",
    }
    values.update(overrides)
    return MeshtasticConfig(**values)


def _session(**overrides: Any) -> MeshtasticSession:
    return MeshtasticSession(
        _config(**overrides),
        adapter_id="mesh-reference",
        platform="meshtastic",
    )


def _event_with_reply() -> CanonicalEvent:
    relation = EventRelation(
        relation_type="reply",
        target_event_id="canonical-original",
        target_native_ref=NativeRef(
            adapter="mesh-reference",
            native_channel_id="0",
            native_message_id="4242",
        ),
        key=None,
        fallback_text="original",
    )
    return CanonicalEvent(
        event_id="reply-event",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="matrix-reference",
        source_transport_id="@alice:example.test",
        source_channel_id="!room:example.test",
        parent_event_id=None,
        lineage=(),
        relations=(relation,),
        payload={"body": "native reply"},
        metadata=EventMetadata(),
    )


def test_stale_receive_callback_is_rejected_after_interface_replacement() -> None:
    session = _session()
    active = object()
    stale = object()
    received: list[dict[str, Any]] = []
    session._client = active
    session._started = True
    session._message_callback = received.append

    session._on_receive({"id": 1}, interface=stale)
    session._on_receive({"id": 2}, interface=active)

    assert received == [{"id": 2}]
    diagnostics = session.diagnostics()
    assert diagnostics.stale_receive_callbacks == 1


def test_stale_disconnect_callback_does_not_start_reconnect() -> None:
    session = _session()
    active = object()
    stale = object()
    session._client = active
    session._started = True

    with patch.object(MeshtasticSession, "notify_connection_lost") as notify:
        session._on_connection_lost(interface=stale)
        notify.assert_not_called()
        session._on_connection_lost(interface=active)
        notify.assert_called_once_with()

    assert session.diagnostics().stale_disconnect_callbacks == 1


def test_sdk_connection_event_is_authoritative_when_available() -> None:
    session = _session()
    state = MagicMock()
    state.is_set.return_value = False
    session._client = SimpleNamespace(isConnected=state)
    session._started = True

    assert session.connected is False
    state.is_set.return_value = True
    assert session.connected is True


def test_sdk_connection_state_failure_is_treated_as_disconnected() -> None:
    session = _session()
    state = MagicMock()
    state.is_set.side_effect = RuntimeError("SDK state unavailable")
    session._client = SimpleNamespace(isConnected=state)
    session._started = True

    assert session.connected is False


def test_connection_loss_crosses_reader_thread_boundary_with_call_soon_threadsafe() -> (
    None
):
    session = _session()
    loop = MagicMock()
    loop.is_closed.return_value = False
    session._loop = loop
    session._started = True
    session._client = object()

    session.notify_connection_lost()

    loop.call_soon_threadsafe.assert_called_once_with(session._start_reconnect_task)
    assert session.diagnostics().last_error == "Connection lost"


async def test_health_check_uses_normal_reconnect_boundary_when_sdk_reports_down() -> (
    None
):
    adapter = MeshtasticAdapter(_config(connection_type="tcp", host="127.0.0.1"))
    session = MagicMock()
    session.connected = False
    session.reconnecting = False
    adapter._session = session
    adapter._started = True

    info = await adapter.health_check()

    assert info.health == "degraded"
    session.notify_connection_lost.assert_called_once_with()


def test_receive_callback_adapter_boundary_uses_threadsafe_coroutine_submission() -> (
    None
):
    adapter = MeshtasticAdapter(_config())
    loop = MagicMock()
    loop.is_closed.return_value = False
    adapter._loop = loop
    adapter._started = True
    adapter.ctx = AdapterContext(
        adapter_id="mesh-reference",
        event_bus=None,
        publish_inbound=MagicMock(),
        logger=logging.getLogger("test.meshtastic.reference"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )
    adapter._session = SimpleNamespace(node_id=None, get_node_info=lambda _node: None)
    future = MagicMock()

    packet = {
        "fromId": "!00000001",
        "toId": "^all",
        "channel": 0,
        "id": 42,
        "rxTime": int(datetime.now(UTC).timestamp()),
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
    }

    with patch(
        "medre.adapters.meshtastic.adapter.asyncio.run_coroutine_threadsafe",
        return_value=future,
    ) as submit:
        adapter._on_packet(packet)

    submit.assert_called_once()
    assert submit.call_args.args[1] is loop
    future.add_done_callback.assert_called_once()
    # Avoid an unawaited-coroutine warning from the mocked submission boundary.
    submitted = submit.call_args.args[0]
    submitted.close()


async def test_shutdown_unsubscribes_before_closing_active_interface() -> None:
    session = _session()
    client = MagicMock()
    session._client = client
    session._started = True
    session._loop = asyncio.get_running_loop()
    ordering: list[str] = []

    def _unsubscribe() -> None:
        ordering.append("unsubscribe")

    def _close() -> None:
        ordering.append("close")

    client.close.side_effect = _close
    with patch.object(
        MeshtasticSession, "_unsubscribe_callbacks", side_effect=_unsubscribe
    ):
        await session.stop()

    assert ordering == ["unsubscribe", "close"]


def test_post_stop_receive_callback_is_ignored() -> None:
    session = _session()
    received = MagicMock()
    session._message_callback = received
    session._started = False
    session._stop_requested = True
    session._client = object()

    session._on_receive({"id": 7}, interface=session._client)

    received.assert_not_called()


async def test_native_reply_renderer_uses_meshtastic_packet_id() -> None:
    renderer = MeshtasticRenderer(configs={"mesh-reference": _config()})

    result = await renderer.render(
        _event_with_reply(),
        RenderingContext(
            target_adapter="mesh-reference",
            target_platform="meshtastic",
            target_channel="0",
            delivery_strategy="direct",
        ),
    )

    assert result.payload["reply_id"] == 4242
    assert result.payload["text"] == "native reply"
