"""LXMF synchronization announces identity after the bridge becomes reachable."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

from scripts.adapter_campaign import radio_peers


async def test_synchronization_announces_peer_before_measured_sends(monkeypatch):
    identity = object()
    destination = SimpleNamespace(hash=b"p" * 16)
    router = Mock()
    router.register_delivery_identity.return_value = destination
    events = []
    router.announce.side_effect = lambda target: events.append(("announce", target))
    reticulum = Mock()
    transport = SimpleNamespace(
        interfaces=[SimpleNamespace(online=True)],
        request_path=lambda target: events.append(("request", target)),
    )
    monkeypatch.setitem(
        sys.modules,
        "RNS",
        SimpleNamespace(
            Identity=SimpleNamespace(
                from_file=lambda _: identity, recall=lambda _: identity
            ),
            Transport=transport,
            Reticulum=reticulum,
        ),
    )
    monkeypatch.setitem(
        sys.modules, "LXMF", SimpleNamespace(LXMRouter=lambda **_: router)
    )
    monkeypatch.setattr(radio_peers, "emit", Mock())

    async def commands(_send, *, synchronize):
        # No event-loop turn has run the periodic announcer. Readiness must
        # explicitly publish identity without relying on its timer.
        await synchronize()
        assert events == [("request", b"b" * 16), ("announce", destination.hash)]

    monkeypatch.setattr(radio_peers, "commands", commands)
    await radio_peers.lxmf(
        {
            "expected_sender": (b"b" * 16).hex(),
            "marker_prefix": "fixture",
            "peer": {
                "identity_path": "fixture-identity",
                "reticulum_config_dir": "fixture-config",
                "storage_path": "fixture-state",
            },
        }
    )
    reticulum.exit_handler.assert_called_once()
