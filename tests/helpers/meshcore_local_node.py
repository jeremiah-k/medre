"""Deterministic local MeshCore companion-protocol endpoint for SDK integration tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

_FRAME_FROM_CLIENT = 0x3C
_FRAME_TO_CLIENT = 0x3E


def _frame(marker: int, payload: bytes) -> bytes:
    return bytes([marker]) + len(payload).to_bytes(2, "little") + payload


def _self_info_payload(name: str = "MEDRE local node") -> bytes:
    public_key = bytes(range(32))
    latitude = int(37.7749 * 1_000_000).to_bytes(4, "little", signed=True)
    longitude = int(-122.4194 * 1_000_000).to_bytes(4, "little", signed=True)
    radio_freq = int(869.525 * 1000).to_bytes(4, "little")
    radio_bw = int(62.5 * 1000).to_bytes(4, "little")
    return b"".join(
        [
            b"\x05",  # SELF_INFO
            b"\x01",  # adv_type
            b"\x16",  # tx_power
            b"\x16",  # max_tx_power
            public_key,
            latitude,
            longitude,
            b"\x01",  # multi_acks
            b"\x00",  # adv_loc_policy
            b"\x00",  # telemetry modes
            b"\x00",  # manual_add_contacts
            radio_freq,
            radio_bw,
            b"\x08",  # spreading factor
            b"\x08",  # coding rate
            name.encode("utf-8"),
        ]
    )


def channel_message_payload(
    text: str,
    *,
    channel_index: int = 0,
    sender_timestamp: int = 1_700_000_000,
) -> bytes:
    """Return a standard CHANNEL_MSG_RECV payload for the pinned SDK reader."""
    return b"".join(
        [
            b"\x08",
            channel_index.to_bytes(1, "little"),
            b"\xff",  # direct/flood sentinel used by the reader
            b"\x00",  # txt_type
            sender_timestamp.to_bytes(4, "little"),
            text.encode("utf-8"),
        ]
    )


@dataclass
class LocalMeshCoreNode:
    """Tiny TCP endpoint implementing the protocol subset MEDRE consumes.

    The endpoint intentionally implements only APPSTART, GET_MSG, direct text
    sends and channel text sends. It is not a MeshCore simulator; it exists to
    exercise the real pinned Python SDK and MEDRE session boundary together.
    """

    appstart_error: bool = False
    stall_sends: bool = False
    hold_sends: bool = False
    disconnect_on_next_send: bool = False
    host: str = "127.0.0.1"
    server: asyncio.AbstractServer | None = field(default=None, init=False)
    port: int = field(default=0, init=False)
    connection_count: int = field(default=0, init=False)
    commands: list[bytes] = field(default_factory=list, init=False)
    send_commands: list[bytes] = field(default_factory=list, init=False)
    send_seen: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    release_sends: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _writers: set[asyncio.StreamWriter] = field(default_factory=set, init=False)

    async def __aenter__(self) -> "LocalMeshCoreNode":
        self.server = await asyncio.start_server(self._handle_client, self.host, 0)
        sockets = self.server.sockets or []
        if not sockets:
            raise RuntimeError("local MeshCore server did not bind a socket")
        self.port = int(sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        for writer in list(self._writers):
            writer.close()
        for writer in list(self._writers):
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._writers.clear()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def disconnect_clients(self) -> None:
        writers = list(self._writers)
        for writer in writers:
            writer.close()
        for writer in writers:
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def inject_channel_message(
        self, text: str, *, channel_index: int = 0
    ) -> None:
        writer = next(iter(self._writers), None)
        if writer is None:
            raise RuntimeError("no active MeshCore SDK client")
        writer.write(
            _frame(
                _FRAME_TO_CLIENT,
                channel_message_payload(text, channel_index=channel_index),
            )
        )
        await writer.drain()

    async def inject_malformed_frame_then_channel(self, text: str) -> None:
        """Send an oversized frame header followed by a valid channel message."""
        writer = next(iter(self._writers), None)
        if writer is None:
            raise RuntimeError("no active MeshCore SDK client")
        malformed = bytes([_FRAME_TO_CLIENT]) + (301).to_bytes(2, "little")
        writer.write(
            malformed + _frame(_FRAME_TO_CLIENT, channel_message_payload(text))
        )
        await writer.drain()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.connection_count += 1
        self._writers.add(writer)
        try:
            while True:
                header = await reader.readexactly(3)
                if header[0] != _FRAME_FROM_CLIENT:
                    continue
                size = int.from_bytes(header[1:], "little")
                payload = await reader.readexactly(size)
                self.commands.append(payload)
                await self._handle_command(payload, writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _handle_command(
        self, payload: bytes, writer: asyncio.StreamWriter
    ) -> None:
        if not payload:
            return
        command = payload[0]
        if command == 0x01:  # APPSTART
            response = b"\x01\x06" if self.appstart_error else _self_info_payload()
            await self._respond(writer, response)
            return
        if command == 0x0A:  # GET_MSG
            await self._respond(writer, b"\x0a")  # NO_MORE_MSGS
            return
        if command == 0x02:  # direct text message
            self.send_commands.append(payload)
            self.send_seen.set()
            if self.disconnect_on_next_send:
                self.disconnect_on_next_send = False
                writer.close()
                await writer.wait_closed()
                return
            if self.stall_sends:
                return
            if self.hold_sends:
                await self.release_sends.wait()
            await self._respond(
                writer,
                b"\x06\x00" + b"\x10\x20\x30\x40" + (250).to_bytes(4, "little"),
            )
            return
        if command == 0x03:  # channel text message
            self.send_commands.append(payload)
            self.send_seen.set()
            if self.stall_sends:
                return
            if self.hold_sends:
                await self.release_sends.wait()
            await self._respond(writer, b"\x00")
            return
        await self._respond(writer, b"\x00")

    async def _respond(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        writer.write(_frame(_FRAME_TO_CLIENT, payload))
        await writer.drain()
