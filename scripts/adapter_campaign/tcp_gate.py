"""A bounded, container-local TCP interruption seam for native campaigns.

TLS and XMPP bytes pass untouched. Only the selected adapter uses this gate;
independent peers keep their direct server connections.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress


class TcpGate:
    def __init__(self, host: str, port: int, *, max_connections: int = 8) -> None:
        """Configure an opaque relay to host/port without opening a listener.

        max_connections bounds concurrent relays; nonpositive integers and
        non-integer values, including bool, raise ValueError.
        """
        if type(max_connections) is not int or max_connections < 1:
            raise ValueError("max_connections must be a positive integer")
        self.host = host
        self.port = port
        self.max_connections = max_connections
        self.blocked = False
        self.accepted = 0
        self.rejected = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> tuple[str, int]:
        """Listen on an ephemeral IPv4 loopback port and return its host/port.

        Raise RuntimeError if already listening; socket binding errors propagate.
        """
        if self._server is not None:
            raise RuntimeError("TCP gate is already running")
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        address = self._server.sockets[0].getsockname()
        return str(address[0]), int(address[1])

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Reject blocked or excess clients; otherwise count and own a new relay."""
        if self.blocked or len(self._tasks) >= self.max_connections:
            self.rejected += 1
            writer.close()
            return
        self.accepted += 1
        self._writers.add(writer)
        task = asyncio.create_task(self._relay(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Forward bytes until EOF, propagating stream read and write errors."""
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()

    async def _relay(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Relay bytes in both directions, closing both sides when either finishes.

        Upstream connection errors and its five-second timeout close the client.
        Copy-task failures are collected during cleanup rather than propagated.
        """
        upstream: asyncio.StreamWriter | None = None
        copies: list[asyncio.Task[None]] = []
        try:
            async with asyncio.timeout(5):
                remote, upstream = await asyncio.open_connection(self.host, self.port)
            copies = [
                asyncio.create_task(self._copy(reader, upstream)),
                asyncio.create_task(self._copy(remote, writer)),
            ]
            await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, TimeoutError):
            # A refused upstream is the fault under test, not gate success.
            pass
        finally:
            for task in copies:
                task.cancel()
            await asyncio.gather(*copies, return_exceptions=True)
            for owned in (writer, upstream):
                if owned is not None:
                    owned.close()
                    with suppress(OSError, TimeoutError):
                        async with asyncio.timeout(2):
                            await owned.wait_closed()
            self._writers.discard(writer)

    async def block(self) -> None:
        """Reject new clients and close and drain all currently owned relays.

        Keep the listener open. Suppress connection-close errors and stop waiting
        for client closure after two seconds.
        """
        self.blocked = True
        writers = tuple(self._writers)
        for writer in writers:
            writer.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first step cannot run its own finally.
        self._writers.difference_update(writers)
        with suppress(OSError, TimeoutError):
            async with asyncio.timeout(2):
                await asyncio.gather(
                    *(writer.wait_closed() for writer in writers),
                    return_exceptions=True,
                )

    def restore(self) -> None:
        """Allow new clients without reopening connections closed by block()."""
        self.blocked = False

    def snapshot(self) -> dict[str, int | bool]:
        """Return blocked state, active relay count, and cumulative client counts."""
        return {
            "blocked": self.blocked,
            "active_connections": len(self._tasks),
            "accepted": self.accepted,
            "rejected": self.rejected,
        }

    async def close(self) -> None:
        """Stop listening and block clients, leaving the gate blocked for reuse.

        Raise TimeoutError if the listener takes more than five seconds to close.
        """
        server = self._server
        if self._server is not None:
            self._server.close()
            self._server = None
        # Server.wait_closed also waits for active clients on Python 3.12+.
        # Close owned relays before waiting for the listener to finish.
        await self.block()
        if server is not None:
            async with asyncio.timeout(5):
                await server.wait_closed()
