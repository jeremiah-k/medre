"""Exercise native fault controls through their bounded loopback interface."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from scripts.adapter_campaign.tcp_gate import TcpGate
from tests.helpers.async_utils import wait_until


@pytest.fixture
async def upstream():
    tasks = set()

    async def echo(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[:2]
    finally:
        server.close()
        pending = tuple(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()


async def closed(reader):
    try:
        return await asyncio.wait_for(reader.read(1), 2) == b""
    except ConnectionResetError:
        return True


async def close(writer):
    writer.close()
    with suppress(ConnectionError):
        await writer.wait_closed()


async def test_gate_preserves_arbitrary_bytes_without_protocol_parsing(upstream):
    gate = TcpGate(*upstream)
    address = await gate.start()
    reader, writer = await asyncio.open_connection(*address)
    try:
        data = b"\x00\xffTLS\x00" + "café ✓".encode()
        writer.write(data)
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(len(data)), 2) == data
    finally:
        await close(writer)
        await gate.close()
    assert gate.snapshot()["active_connections"] == 0


async def test_block_closes_existing_connection_and_restore_accepts_new_one(upstream):
    gate = TcpGate(*upstream)
    address = await gate.start()
    reader, writer = await asyncio.open_connection(*address)
    rejected = restored = None
    try:
        assert await wait_until(lambda: gate.snapshot()["active_connections"] == 1)
        await gate.block()
        assert await closed(reader)
        denied, rejected = await asyncio.open_connection(*address)
        assert await closed(denied)
        assert gate.snapshot()["rejected"] == 1
        gate.restore()
        active, restored = await asyncio.open_connection(*address)
        restored.write(b"restored")
        await restored.drain()
        assert await asyncio.wait_for(active.readexactly(8), 2) == b"restored"
    finally:
        for owned in (writer, rejected, restored):
            if owned is not None:
                await close(owned)
        await gate.close()


async def test_gate_limits_owned_connections(upstream):
    gate = TcpGate(*upstream, max_connections=1)
    address = await gate.start()
    reader, first = await asyncio.open_connection(*address)
    second = None
    try:
        assert await wait_until(lambda: gate.snapshot()["active_connections"] == 1)
        denied, second = await asyncio.open_connection(*address)
        assert await closed(denied)
        assert gate.snapshot()["active_connections"] == 1
        await gate.close()
        assert await closed(reader)
        assert gate.snapshot()["active_connections"] == 0
    finally:
        await close(first)
        if second is not None:
            await close(second)
        await gate.close()


@pytest.mark.parametrize("limit", [0, -1, True])
def test_gate_rejects_invalid_connection_limits(limit):
    with pytest.raises(ValueError, match="positive integer"):
        TcpGate("127.0.0.1", 5222, max_connections=limit)
