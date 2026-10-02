"""Process-isolated RNS reconnect/reload evidence over a loopback TCP endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
from contextlib import suppress
from pathlib import Path

from tests.helpers.async_utils import wait_until


async def _run(base: Path) -> dict[str, bool]:
    import RNS
    from RNS.Interfaces.TCPInterface import HDLC

    from medre.adapters.lxmf.session import LxmfSession
    from medre.config.adapters.lxmf import LxmfConfig

    loop = asyncio.get_running_loop()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    connections: list[socket.socket] = []
    session = None
    reticulum = None
    name = "medre-loopback-reconnect"

    async def accept() -> socket.socket:
        connection, _ = await asyncio.wait_for(loop.sock_accept(listener), 10)
        connection.setblocking(False)
        connections.append(connection)
        return connection

    async def observe_announce(connection: socket.socket) -> None:
        destination_hash = session._delivery_destination_hash
        session._router.announce(destination_hash)
        received = bytearray()

        async def seen() -> bool:
            while HDLC.escape(destination_hash) not in received:
                packet = await loop.sock_recv(connection, 4096)
                if not packet:
                    raise AssertionError("loopback endpoint disconnected during announce")
                received.extend(packet)
            return True

        assert await asyncio.wait_for(seen(), 10), "announce did not reach the endpoint"

    def active_interface():
        interfaces = [iface for iface in RNS.Transport.interfaces if iface.name == name]
        assert len(interfaces) == 1, "interface registry lost ownership or duplicated it"
        return interfaces[0]

    try:
        config_dir = base / "reticulum"
        config_dir.mkdir(parents=True)
        (config_dir / "config").write_text(
            "[reticulum]\n"
            "enable_transport = No\n"
            "share_instance = No\n"
            "[logging]\n"
            "loglevel = 1\n"
            "[interfaces]\n"
            f"  [[{name}]]\n"
            "    type = TCPClientInterface\n"
            "    enabled = Yes\n"
            "    target_host = 127.0.0.1\n"
            f"    target_port = {listener.getsockname()[1]}\n",
            encoding="utf-8",
        )
        session = LxmfSession(
            adapter_id="rns-local-reconnect",
            config=LxmfConfig(
                adapter_id="rns-local-reconnect",
                connection_type="reticulum",
                storage_path=str(base / "router"),
                reticulum_config_dir=str(config_dir),
                announce_interval_seconds=0,
                message_delay_seconds=0,
            ),
            logger=logging.getLogger("test.rns.connection"),
            reticulum_config_dir=str(config_dir),
        )
        await session.start(lambda _payload: None)
        assert RNS.Reticulum.configpath == str(config_dir / "config")
        connection = await accept()
        reticulum = session._reticulum
        router = session._router
        destination_hash = session._delivery_destination_hash
        interface = active_interface()
        await observe_announce(connection)

        # Closing the far endpoint exercises the SDK's read-loop reconnect,
        # without invoking MEDRE's private, unwired reconnect helper.
        with suppress(OSError):
            connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        assert await wait_until(
            lambda: interface.reconnecting and not interface.online, timeout=5
        ), "RNS did not detect the disconnected endpoint"
        assert session.connected and session.router_running
        connection = await accept()
        assert await wait_until(
            lambda: interface.online and not interface.reconnecting, timeout=10
        ), "RNS did not reconnect its interface"
        assert active_interface() is interface
        await observe_announce(connection)

        assert reticulum.detach_interface(name) is True
        assert interface.detached and not interface.online
        assert interface not in RNS.Transport.interfaces
        assert reticulum.attach_interface(name) is True
        connection = await accept()
        attached = active_interface()
        assert attached is not interface and attached.online
        await observe_announce(connection)

        assert reticulum.reload_interface(name) is True
        connection = await accept()
        reloaded = active_interface()
        assert reloaded is not attached and reloaded.online
        assert attached.detached and attached not in RNS.Transport.interfaces
        await observe_announce(connection)

        assert session._router is router and session._reticulum is reticulum
        assert session._delivery_destination_hash == destination_hash
        await session.stop()
        assert active_interface() is reloaded and reloaded.online
        assert RNS.Reticulum.get_instance() is reticulum
        return {
            "automatic_reconnect": True,
            "detach_attach": True,
            "live_reload": True,
            "announce_after_each_recovery": True,
            "stable_router_and_destination": True,
            "session_stop_preserves_transport": True,
        }
    finally:
        if session is not None:
            await session.stop()
        if reticulum is not None:
            reticulum.detach_interface(name)
        for connection in connections:
            connection.close()
        listener.close()


def main() -> int:
    base = Path(sys.argv[1]).resolve()
    base.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(_run(base))
    print("MEDRE_RNS_CONNECTION_RESULT=" + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
