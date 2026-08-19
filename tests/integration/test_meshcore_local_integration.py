"""Local real-SDK integration tests for the MeshCore session boundary."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from medre.adapters.meshcore import session as session_mod
from medre.adapters.meshcore.errors import MeshCoreConnectionError
from medre.adapters.meshcore.session import MeshCoreSession
from medre.config.adapters.meshcore import MeshCoreConfig
from tests.helpers.async_utils import wait_until
from tests.helpers.meshcore_local_node import LocalMeshCoreNode

pytestmark = [pytest.mark.local_integration, pytest.mark.meshcore_sdk]


def _config(node: LocalMeshCoreNode) -> MeshCoreConfig:
    return MeshCoreConfig(
        adapter_id="meshcore-local-integration",
        connection_type="tcp",
        host=node.host,
        port=node.port,
        message_delay_seconds=0,
    )


def _session(node: LocalMeshCoreNode) -> MeshCoreSession:
    return MeshCoreSession(
        adapter_id="meshcore-local-integration",
        config=_config(node),
        logger=logging.getLogger("test.meshcore.local"),
    )


async def test_real_sdk_start_inbound_send_and_stop() -> None:
    received: list[dict[str, Any]] = []
    async with LocalMeshCoreNode() as node:
        session = _session(node)
        await session.start(received.append)
        assert session.connected is True
        assert session.diagnostics()["device_name"] == "MEDRE local node"

        await node.inject_channel_message("local inbound", channel_index=2)
        assert await wait_until(lambda: len(received) == 1)
        assert received[0]["text"] == "local inbound"
        assert received[0]["channel_idx"] == 2

        native_id = await session.send_text("001122334455", "local outbound")
        assert native_id == "10203040"
        assert len(node.send_commands) == 1

        await session.stop()
        assert session.connected is False


async def test_real_sdk_repeated_start_stop_reuses_local_endpoint() -> None:
    async with LocalMeshCoreNode() as node:
        session = _session(node)
        for _ in range(3):
            await session.start(lambda _payload: None)
            assert session.connected is True
            await session.stop()
            assert session.connected is False
        assert node.connection_count == 3


async def test_real_sdk_startup_error_cleans_partial_client() -> None:
    async with LocalMeshCoreNode(appstart_error=True) as node:
        session = _session(node)
        with pytest.raises(MeshCoreConnectionError, match="No response"):
            await session.start(lambda _payload: None)
        assert session.connected is False
        assert session.diagnostics()["reconnecting"] is False
        assert session._meshcore is None


async def test_real_sdk_disconnect_during_inbound_reconnects() -> None:
    received: list[dict[str, Any]] = []
    async with LocalMeshCoreNode() as node:
        session = _session(node)
        await session.start(received.append)
        await node.inject_channel_message("before disconnect")
        assert await wait_until(lambda: len(received) == 1)

        await node.disconnect_clients()
        assert await wait_until(lambda: node.connection_count >= 2, timeout=5.0)
        assert await wait_until(lambda: session.connected, timeout=5.0)

        await node.inject_channel_message("after reconnect")
        assert await wait_until(lambda: len(received) == 2)
        assert [item["text"] for item in received] == [
            "before disconnect",
            "after reconnect",
        ]
        await session.stop()


async def test_real_sdk_malformed_frame_resynchronizes() -> None:
    received: list[dict[str, Any]] = []
    async with LocalMeshCoreNode() as node:
        session = _session(node)
        await session.start(received.append)

        await node.inject_malformed_frame_then_channel("after malformed")
        assert await wait_until(lambda: len(received) == 1)
        assert received[0]["text"] == "after malformed"
        assert session.connected is True
        await session.stop()


async def test_real_sdk_disconnect_during_outbound_recovers_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with LocalMeshCoreNode(disconnect_on_next_send=True) as node:
        session = _session(node)
        # Make the ownership transition deterministic without weakening the
        # real SDK command/response path exercised by this test.
        monkeypatch.setattr(session_mod, "_RECONNECT_BASE_DELAY", 0.0)
        monkeypatch.setattr(session_mod, "_RECONNECT_JITTER_FRACTION", 0.0)
        await session.start(lambda _payload: None)
        assert session._meshcore is not None
        session._meshcore.default_timeout = 0.2

        native_id = await session.send_text("001122334455", "disconnect once")
        assert native_id == "10203040"
        assert await wait_until(lambda: node.connection_count >= 2)
        assert session.connected is True
        assert session.transient_delivery_failures == 1
        await session.stop()


async def test_real_sdk_stalled_send_is_cancellable() -> None:
    async with LocalMeshCoreNode(stall_sends=True) as node:
        session = _session(node)
        await session.start(lambda _payload: None)
        task = asyncio.create_task(session.send_text("001122334455", "stalled"))
        await asyncio.wait_for(node.send_seen.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session.stop()


async def test_real_sdk_send_lock_applies_backpressure() -> None:
    async with LocalMeshCoreNode(hold_sends=True) as node:
        session = _session(node)
        await session.start(lambda _payload: None)

        first = asyncio.create_task(session.send_text("001122334455", "first"))
        await asyncio.wait_for(node.send_seen.wait(), timeout=2.0)
        second = asyncio.create_task(session.send_text("001122334455", "second"))
        await asyncio.sleep(0)
        assert len(node.send_commands) == 1

        node.release_sends.set()
        assert await first == "10203040"
        assert await second == "10203040"
        assert len(node.send_commands) == 2
        await session.stop()


@pytest.mark.soak
async def test_real_sdk_local_soak_repeated_lifecycle_and_send() -> None:
    async with LocalMeshCoreNode() as node:
        session = _session(node)
        for index in range(10):
            await session.start(lambda _payload: None)
            native_id = await session.send_text(
                "001122334455",
                f"local soak {index}",
            )
            assert native_id == "10203040"
            await session.stop()
        assert node.connection_count == 10
        assert len(node.send_commands) == 10
