"""Lifecycle-order regressions for adapters with callback subscriptions."""

from __future__ import annotations

import sys
import types

from medre.adapters.meshcore.session import MeshCoreSession
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig


async def test_meshtastic_stop_unsubscribes_before_client_close(monkeypatch) -> None:
    """Remove SDK callbacks before closing the transport-facing client."""
    calls: list[str] = []

    class _Pub:
        def subscribe(self, callback: object, topic: str) -> None:
            _ = callback
            calls.append(f"subscribe:{topic}")

        def unsubscribe(self, callback: object, topic: str) -> None:
            _ = callback
            calls.append(f"unsubscribe:{topic}")

    pubsub = types.ModuleType("pubsub")
    pubsub.pub = _Pub()
    monkeypatch.setitem(sys.modules, "pubsub", pubsub)

    class _Client:
        myInfo = None

        def close(self) -> None:
            calls.append("close")

    session = MeshtasticSession(
        MeshtasticConfig(
            adapter_id="shutdown-order",
            connection_type="tcp",
            host="127.0.0.1",
        ),
        adapter_id="shutdown-order",
        platform="meshtastic",
    )
    monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
    monkeypatch.setattr(type(session), "_create_client", lambda _self: _Client())

    await session.start()
    calls.clear()
    await session.stop()

    assert calls == [
        "unsubscribe:meshtastic.receive",
        "unsubscribe:meshtastic.connection.lost",
        "close",
    ]


async def test_meshcore_stop_unsubscribes_before_sdk_disconnect() -> None:
    """Drop event subscriptions before stopping fetch and disconnecting the SDK."""
    calls: list[str] = []

    class _MeshCore:
        def unsubscribe(self, subscription: object) -> None:
            calls.append(f"unsubscribe:{subscription}")

        async def stop_auto_message_fetching(self) -> None:
            calls.append("stop_auto_message_fetching")

        async def disconnect(self) -> None:
            calls.append("disconnect")

    session = MeshCoreSession(
        MeshCoreConfig(
            adapter_id="shutdown-order",
            connection_type="tcp",
            host="127.0.0.1",
        ),
        "shutdown-order",
    )
    session._meshcore = _MeshCore()
    session._subscriptions[:] = ["dm", "channel"]
    session._started = True
    session._diag.connected = True

    await session.stop()

    assert calls == [
        "unsubscribe:dm",
        "unsubscribe:channel",
        "stop_auto_message_fetching",
        "disconnect",
    ]
    assert session._subscriptions == []
    assert session._meshcore is None
