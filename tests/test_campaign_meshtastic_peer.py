"""Native campaign peers select one transport and retire their owned interface."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.adapter_campaign import radio_peers


@pytest.mark.parametrize(
    "peer,module,symbol,expected_args,expected_kwargs",
    [
        (
            {"serial_port": "/dev/radio"},
            "serial_interface",
            "SerialInterface",
            ("/dev/radio",),
            {},
        ),
        (
            {"ble_address": "AA:BB:CC:DD:EE:FF"},
            "ble_interface",
            "BLEInterface",
            (),
            {"address": "AA:BB:CC:DD:EE:FF"},
        ),
        (
            {"host": "localhost", "port": 4404},
            "tcp_interface",
            "TCPInterface",
            (),
            {"hostname": "localhost", "portNumber": 4404},
        ),
    ],
)
@pytest.mark.parametrize("unsubscribe_error", [False, True])
async def test_peer_transport_owns_subscription_and_cleanup(
    monkeypatch, peer, module, symbol, expected_args, expected_kwargs, unsubscribe_error
):
    """Serial/BLE/TCP differ only in construction; all use native peer evidence."""
    interface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=42), close=Mock())
    factory = Mock(return_value=interface)
    pub = Mock()
    if unsubscribe_error:
        pub.unsubscribe.side_effect = RuntimeError("Unsubscribe failed")
    monkeypatch.setitem(sys.modules, "pubsub", SimpleNamespace(pub=pub))
    monkeypatch.setitem(
        sys.modules, f"meshtastic.{module}", SimpleNamespace(**{symbol: factory})
    )
    emitted = Mock()
    monkeypatch.setattr(radio_peers, "emit", emitted)

    async def commands(send):
        raise RuntimeError("Supervisor ended the fixture")

    monkeypatch.setattr(radio_peers, "commands", commands)
    with pytest.raises(
        RuntimeError,
        match="Unsubscribe failed" if unsubscribe_error else "Supervisor ended",
    ):
        await radio_peers.meshtastic(
            {"peer": peer, "channel": "0", "marker_prefix": "fixture"}
        )
    factory.assert_called_once_with(*expected_args, timeout=15, **expected_kwargs)
    emitted.assert_called_once_with("ready", identity="42")
    pub.unsubscribe.assert_called_once_with(
        pub.subscribe.call_args.args[0], "meshtastic.receive"
    )
    interface.close.assert_called_once_with()


@pytest.mark.parametrize(
    "peer", [{}, {"serial_port": "/dev/radio", "ble_address": "AA:BB:CC:DD:EE:FF"}]
)
async def test_ambiguous_or_missing_peer_transport_fails_before_construction(
    monkeypatch, peer
):
    """An old address cannot silently override the transport selected for a run."""
    monkeypatch.setitem(sys.modules, "pubsub", SimpleNamespace(pub=Mock()))
    with pytest.raises(ValueError, match="exactly one"):
        await radio_peers.meshtastic({"peer": peer, "channel": "0"})


@pytest.mark.parametrize("failure", ["receiver", "channel"])
async def test_observer_setup_errors_never_open_a_radio(monkeypatch, failure):
    """Invalid evidence or channel settings fail before acquiring native resources."""
    monkeypatch.setitem(sys.modules, "pubsub", SimpleNamespace(pub=Mock()))
    factory = Mock()
    monkeypatch.setattr(radio_peers, "open_owned_interface", factory)
    if failure == "receiver":
        monkeypatch.setattr(
            radio_peers, "receiver", Mock(side_effect=ValueError("observer"))
        )
    with pytest.raises(ValueError):
        await radio_peers.meshtastic(
            {
                "peer": {"ble_address": "AA:BB:CC:DD:EE:FF"},
                "channel": "invalid" if failure == "channel" else "0",
                "marker_prefix": "fixture",
            }
        )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "bad", [None, "", " ", "bad\x00marker", "bad\nmarker", 23, "x" * 129]
)
async def test_missing_or_invalid_marker_rejected_without_radio(monkeypatch, bad):
    """An observer without a trustworthy marker must never acquire a radio."""
    from scripts.adapter_campaign import meshtastic_connection

    monkeypatch.setitem(sys.modules, "pubsub", SimpleNamespace(pub=Mock()))
    acquired = Mock()
    monkeypatch.setattr(meshtastic_connection, "open_interface", acquired)
    with pytest.raises(ValueError, match="marker_prefix"):
        await radio_peers.meshtastic(
            {"peer": {"serial_port": "/dev/radio"}, "channel": 0, "marker_prefix": bad}
        )
    acquired.assert_not_called()


async def test_cancelled_constructor_late_client_is_disposed_in_native_thread(
    monkeypatch,
):
    """Asyncio cancellation must not discard a native result returned afterward."""
    import asyncio
    import threading

    from scripts.adapter_campaign import meshtastic_connection

    entered = threading.Event()
    finish = threading.Event()
    closed = threading.Event()
    cancellation_events = []
    interface = SimpleNamespace(close=Mock(side_effect=closed.set))

    def construct(settings, *, timeout, cancel_event=None):
        assert settings == {"ble_address": "AA:BB:CC:DD:EE:FF"}
        cancellation_events.append(cancel_event)
        entered.set()
        assert finish.wait(3), "native constructor did not finish"
        return interface

    monkeypatch.setattr(meshtastic_connection, "open_interface", construct)
    task = asyncio.create_task(
        meshtastic_connection.open_owned_interface(
            {"ble_address": "AA:BB:CC:DD:EE:FF"}, timeout=3
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancellation_events[0].is_set()
    finally:
        finish.set()
    assert await asyncio.to_thread(closed.wait, 3)
    interface.close.assert_called_once_with()


@pytest.mark.parametrize("selector", ["serial_port", "ble_address", "host"])
def test_optional_sdk_constructor_cancellation_is_explicit(monkeypatch, selector):
    """Only SDK constructors declaring cancelEvent receive the native event."""
    import threading

    from scripts.adapter_campaign import meshtastic_connection

    event = threading.Event()
    captured = []

    def cancellable_constructor(*args, timeout, cancelEvent=None, **kwargs):
        captured.append(cancelEvent)
        return object()

    fake = SimpleNamespace()
    for name in ("SerialInterface", "BLEInterface", "TCPInterface"):
        setattr(fake, name, cancellable_constructor)
    monkeypatch.setitem(sys.modules, "meshtastic.serial_interface", fake)
    monkeypatch.setitem(sys.modules, "meshtastic.ble_interface", fake)
    monkeypatch.setitem(sys.modules, "meshtastic.tcp_interface", fake)
    value = {
        "serial_port": "/dev/radio",
        "ble_address": "radio-1",
        "host": "localhost",
    }[selector]
    meshtastic_connection.open_interface(
        {selector: value}, timeout=3, cancel_event=event
    )
    assert captured == [event]


@pytest.mark.parametrize("name", ["meshcore", "lxmf"])
async def test_radio_peer_marker_validated_before_sdk_constructor(monkeypatch, name):
    """All radio witnesses reject invalid evidence before creating native SDKs."""
    sdk = SimpleNamespace(
        EventType=object(), MeshCore=Mock(), Identity=Mock(), Reticulum=Mock()
    )
    monkeypatch.setitem(sys.modules, "meshcore", sdk)
    monkeypatch.setitem(sys.modules, "LXMF", sdk)
    monkeypatch.setitem(sys.modules, "RNS", sdk)
    with pytest.raises(ValueError, match="marker_prefix"):
        await getattr(radio_peers, name)({"peer": {}, "marker_prefix": ""})
    sdk.MeshCore.assert_not_called()
    sdk.Reticulum.assert_not_called()
