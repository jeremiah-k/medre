"""Executable contract checks for the pinned meshcore_py SDK."""

from __future__ import annotations

import inspect
from importlib import import_module, metadata
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.meshcore_sdk


def _load_sdk() -> tuple[object, object, object, object]:
    """Import the pinned MeshCore modules or fail this contract tier."""
    try:
        root = import_module("meshcore")
        events = import_module("meshcore.events")
        packets = import_module("meshcore.packets")
        reader = import_module("meshcore.reader")
    except ImportError as exc:  # pragma: no cover - CI dependency contract
        pytest.fail(f"meshcore_sdk tier requires medre[meshcore]: {exc}")
    return root, events, packets, reader


def test_pinned_meshcore_version_is_exact() -> None:
    """The contract tier must execute against MEDRE's exact meshcore pin."""
    assert metadata.version("meshcore") == "2.3.8"


def test_connection_factory_shapes_keep_sdk_reconnect_disabled_by_default() -> None:
    """MEDRE owns reconnect; every SDK connection factory must default it off."""
    root, _, _, _ = _load_sdk()
    for name in ("create_tcp", "create_serial", "create_ble"):
        factory = getattr(root.MeshCore, name)
        assert inspect.iscoroutinefunction(factory)
        parameters = inspect.signature(factory).parameters
        assert "auto_reconnect" in parameters
        assert parameters["auto_reconnect"].default is False
        assert "max_reconnect_attempts" in parameters
        assert parameters["max_reconnect_attempts"].default == 3


def test_subscription_and_disconnect_lifecycle_shapes_are_frozen() -> None:
    """Pin sync subscription management and async disconnect semantics."""
    root, _, _, _ = _load_sdk()
    assert callable(getattr(root.MeshCore, "subscribe", None))
    assert callable(getattr(root.MeshCore, "unsubscribe", None))
    assert not inspect.iscoroutinefunction(root.MeshCore.subscribe)
    assert not inspect.iscoroutinefunction(root.MeshCore.unsubscribe)
    assert inspect.iscoroutinefunction(root.MeshCore.start_auto_message_fetching)
    assert inspect.iscoroutinefunction(root.MeshCore.stop_auto_message_fetching)
    assert inspect.iscoroutinefunction(root.MeshCore.disconnect)


def test_required_event_values_are_frozen() -> None:
    """Pin the event names MEDRE subscribes to and interprets."""
    _, events, _, _ = _load_sdk()
    event_type = events.EventType
    assert event_type.CONTACT_MSG_RECV.value == "contact_message"
    assert event_type.CHANNEL_MSG_RECV.value == "channel_message"
    assert event_type.MSG_SENT.value == "message_sent"
    assert event_type.ACK.value == "acknowledgement"
    assert event_type.CONTACTS.value == "contacts"
    assert event_type.SELF_INFO.value == "self_info"
    assert event_type.DISCONNECTED.value == "disconnected"


async def test_send_appstart_executes_once_on_initial_and_sdk_reconnect_paths() -> None:
    """Execute initial and SDK-owned reconnect handshakes exactly once each."""
    root, events, _, _ = _load_sdk()
    timeline: list[str] = []

    async def _dispatcher_start() -> None:
        timeline.append("dispatcher.start")

    async def _connection_connect() -> object:
        timeline.append("connection.connect")
        return object()

    async def _send_appstart() -> object:
        timeline.append("send_appstart")
        return SimpleNamespace(type=events.EventType.SELF_INFO)

    client = object.__new__(root.MeshCore)
    client.dispatcher = SimpleNamespace(
        start=AsyncMock(side_effect=_dispatcher_start),
        stop=AsyncMock(),
    )
    client.connection_manager = SimpleNamespace(
        connect=AsyncMock(side_effect=_connection_connect),
    )
    client.commands = SimpleNamespace(
        send_appstart=AsyncMock(side_effect=_send_appstart),
    )

    await client.connect()
    await client._on_reconnect()

    assert timeline == [
        "dispatcher.start",
        "connection.connect",
        "send_appstart",
        "send_appstart",
    ]
    assert client.connection_manager.connect.await_count == 1
    assert client.commands.send_appstart.await_count == 2


@pytest.mark.parametrize(
    ("factory_name", "connection_name", "args", "kwargs"),
    [
        ("create_tcp", "TCPConnection", ("127.0.0.1", 4000), {}),
        ("create_serial", "SerialConnection", ("/dev/ttyUSB0",), {}),
        ("create_ble", "BLEConnection", (), {"address": "00:11:22:33:44:55"}),
    ],
)
async def test_connection_factories_await_connect_once(
    monkeypatch: pytest.MonkeyPatch,
    factory_name: str,
    connection_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    """Every pinned SDK factory must await one client connect before return."""
    root, _, _, _ = _load_sdk()
    sdk_module = import_module("meshcore.meshcore")
    connection = MagicMock()
    connection_ctor = MagicMock(return_value=connection)
    monkeypatch.setattr(sdk_module, connection_name, connection_ctor)
    timeline: list[str] = []

    class ProbeMeshCore(root.MeshCore):
        def __init__(self, cx: object, **init_kwargs: object) -> None:
            self.cx = cx
            self.init_kwargs = init_kwargs

        async def connect(self) -> object:
            timeline.append("connect")
            return object()

    client = await getattr(ProbeMeshCore, factory_name)(*args, **kwargs)

    assert isinstance(client, ProbeMeshCore)
    assert timeline == ["connect"]
    connection_ctor.assert_called_once()


async def test_msg_sent_parser_exposes_four_byte_ack_and_millisecond_timeout() -> None:
    """Execute the pinned reader against a synthetic firmware MSG_SENT frame."""
    _, events, packets, reader_module = _load_sdk()
    dispatcher = AsyncMock()
    reader = reader_module.MessageReader(dispatcher)
    expected_ack = b"\x10\x20\x30\x40"
    suggested_timeout_ms = 4321
    frame = bytearray(
        [packets.PacketType.MSG_SENT.value, 1]
        + list(expected_ack)
        + list(suggested_timeout_ms.to_bytes(4, "little"))
    )

    await reader.handle_rx(frame)

    dispatched = dispatcher.dispatch.await_args.args[0]
    assert dispatched.type is events.EventType.MSG_SENT
    assert dispatched.payload["expected_ack"] == expected_ack
    assert len(dispatched.payload["expected_ack"]) == 4
    assert dispatched.payload["suggested_timeout"] == suggested_timeout_ms
    assert dispatched.attributes["expected_ack"] == expected_ack.hex()
