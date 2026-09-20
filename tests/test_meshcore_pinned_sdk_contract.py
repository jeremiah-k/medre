"""Executable contract checks for the pinned meshcore_py SDK."""

from __future__ import annotations

import inspect
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.helpers.sdk_contract import assert_installed_extra_matches_declared_pins

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


def test_installed_meshcore_matches_declared_extra() -> None:
    """The contract tier executes against MEDRE's current declared SDK pin."""
    assert_installed_extra_matches_declared_pins("meshcore", ("meshcore",))


def test_connection_factory_shapes_accept_medre_reconnect_control() -> None:
    """Consumed SDK factories accept MEDRE's explicit reconnect control.

    ``create_serial`` is deliberately absent: MEDRE does not consume the
    serial factory because its no-response retry inverts DTR (see
    :doc:`adapter-sdk-parity`); the serial surface MEDRE consumes is
    pinned by the construction tests below.
    """
    root, _, _, _ = _load_sdk()
    for name in ("create_tcp", "create_ble"):
        factory = getattr(root.MeshCore, name)
        assert inspect.iscoroutinefunction(factory)
        signature = inspect.signature(factory)
        auto_reconnect = signature.parameters.get("auto_reconnect")
        assert auto_reconnect is not None
        assert auto_reconnect.kind is not inspect.Parameter.POSITIONAL_ONLY


def test_medre_serial_construction_surface_is_frozen() -> None:
    """Pin the SDK surface MEDRE uses to build deasserted serial clients.

    MEDRE constructs ``SerialConnection(port, baudrate, cx_dly=..., rts=...,
    dtr=...)`` itself and hands it to ``MeshCore(connection,
    auto_reconnect=False)``; both constructor shapes plus the awaited
    connect/disconnect semantics are consumed API.  A pin bump that
    renames or removes any of them must fail this tier before a lab run.
    """
    root, _, _, _ = _load_sdk()

    serial_init = inspect.signature(root.SerialConnection.__init__)
    for name in ("port", "baudrate", "cx_dly", "rts", "dtr"):
        assert name in serial_init.parameters, f"SerialConnection.{name}"

    meshcore_init = inspect.signature(root.MeshCore.__init__)
    for name in (
        "cx",
        "debug",
        "only_error",
        "default_timeout",
        "auto_reconnect",
        "max_reconnect_attempts",
    ):
        assert name in meshcore_init.parameters, f"MeshCore.{name}"

    assert inspect.iscoroutinefunction(root.MeshCore.connect)
    assert inspect.iscoroutinefunction(root.MeshCore.disconnect)


async def test_serial_connection_latches_requested_deasserted_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute the pinned ``SerialConnection`` against a simulated port.

    ``connection_made`` applies the requested DTR/RTS state when the port
    opens — before any protocol handshake.  That latch timing is exactly
    why a post-hoc line check could never satisfy MEDRE's never-assert
    contract: whatever construction path MEDRE uses must apply the
    deasserted state at open.  The fake serial object records the writes;
    the firmware-side effect (bootloader entry on assertion) is modelled,
    not observed on hardware.
    """
    root, _, _, _ = _load_sdk()
    serial_cx = import_module("meshcore.serial_cx")
    written: dict[str, bool] = {}

    class FakeSerial:
        dtr = True  # SDK default state starts asserted
        rts = True

        def __setattr__(self, name: str, value: object) -> None:
            if name in ("dtr", "rts"):
                written[name] = value  # type: ignore[assignment]
            object.__setattr__(self, name, value)

    class FakeTransport(serial_cx.serial_asyncio.SerialTransport):
        def __init__(self, serial_obj: FakeSerial) -> None:
            # Skip the real transport init (no loop/reader); the read-only
            # ``serial`` property reads ``_serial``.
            self._serial = serial_obj
            self.closed = False

        def write(self, data: object) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    opened: dict[str, object] = {}

    async def fake_create_serial_connection(
        loop: object, factory: object, port: str, **kwargs: object
    ) -> tuple[FakeTransport, object]:
        protocol = factory()  # type: ignore[misc]
        transport = FakeTransport(FakeSerial())
        protocol.connection_made(transport)
        opened["port"] = port
        opened["kwargs"] = kwargs
        return transport, protocol

    monkeypatch.setattr(
        serial_cx.serial_asyncio,
        "create_serial_connection",
        fake_create_serial_connection,
    )

    connection = root.SerialConnection(
        "/dev/ttyUSB0", 57600, cx_dly=0.1, rts=False, dtr=False
    )
    result = await connection.connect(timeout=5.0)

    assert result == "/dev/ttyUSB0"
    assert written == {"dtr": False, "rts": False}
    assert opened["port"] == "/dev/ttyUSB0"
    assert opened["kwargs"] == {"baudrate": 57600}


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
    appstart_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def _dispatcher_start() -> None:
        timeline.append("dispatcher.start")

    async def _connection_connect() -> object:
        timeline.append("connection.connect")
        return object()

    async def _send_appstart(*args: object, **kwargs: object) -> object:
        # MeshCore may add handshake tuning kwargs (for example ``timeout``)
        # without changing the contract MEDRE depends on: one APP_START for
        # initial connect and one for an SDK-owned reconnect.  Record the exact
        # call shape so it can be checked against the real command method
        # below; a permissive double alone could hide a handshake TypeError.
        appstart_calls.append((args, kwargs))
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

    # The SDK's own connect/reconnect code must call ``send_appstart`` with a
    # shape the real command method accepts; otherwise production would raise
    # ``TypeError`` during the APP_START handshake.
    commands = import_module("meshcore.commands")
    real_send_appstart = inspect.signature(commands.CommandHandler.send_appstart)
    assert appstart_calls, "expected the SDK to perform APP_START handshakes"
    for args, kwargs in appstart_calls:
        real_send_appstart.bind(object(), *args, **kwargs)


@pytest.mark.parametrize(
    ("factory_name", "connection_name", "args", "kwargs"),
    [
        ("create_tcp", "TCPConnection", ("127.0.0.1", 4000), {"auto_reconnect": False}),
        (
            "create_ble",
            "BLEConnection",
            (),
            {"address": "00:11:22:33:44:55", "auto_reconnect": False},
        ),
    ],
)
async def test_connection_factories_await_connect_once(
    monkeypatch: pytest.MonkeyPatch,
    factory_name: str,
    connection_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    """Every consumed SDK factory must await one client connect before return.

    ``create_serial`` is not consumed (MEDRE constructs the serial client
    directly to keep DTR/RTS deasserted); its single-connect behavior is
    covered indirectly by the SDK itself and the construction tests above.
    """
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
