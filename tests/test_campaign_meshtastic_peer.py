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
    monkeypatch.setattr(radio_peers, "open_interface", factory)
    if failure == "receiver":
        monkeypatch.setattr(
            radio_peers, "receiver", Mock(side_effect=ValueError("observer"))
        )
    with pytest.raises(ValueError):
        await radio_peers.meshtastic(
            {
                "peer": {"ble_address": "AA:BB:CC:DD:EE:FF"},
                "channel": "invalid" if failure == "channel" else "0",
            }
        )
    factory.assert_not_called()
