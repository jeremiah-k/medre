"""SDK-only radio peers. Never writes radio configuration or host settings."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .worker import commands, emit, receiver


async def meshtastic(settings: dict) -> None:
    from pubsub import pub

    peer = settings["peer"]
    if "serial_port" in peer:
        from meshtastic.serial_interface import SerialInterface

        interface = await asyncio.to_thread(
            SerialInterface, peer["serial_port"], timeout=15
        )
    else:
        from meshtastic.tcp_interface import TCPInterface

        interface = await asyncio.to_thread(
            TCPInterface,
            hostname=peer["host"],
            portNumber=peer.get("port", 4403),
            timeout=15,
        )
    received = receiver(settings)
    channel = int(peer.get("channel", settings["channel"]))

    def receive(packet, interface=None):
        if interface is not owned_interface:
            return
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") == "TEXT_MESSAGE_APP":
            received(
                text=decoded.get("text", ""),
                sender=str(packet["from"]),
                channel=str(packet.get("channel", 0)),
                native_id=str(packet["id"]),
            )

    owned_interface = interface
    try:
        pub.subscribe(receive, "meshtastic.receive")
        emit("ready", identity=str(interface.myInfo.my_node_num))

        async def send(text):
            packet = await asyncio.to_thread(
                interface.sendText, text, channelIndex=channel, wantAck=False
            )
            return packet.id

        await commands(send)
    finally:
        pub.unsubscribe(receive, "meshtastic.receive")
        await asyncio.to_thread(interface.close)


async def meshcore(settings: dict) -> None:
    from meshcore import EventType, MeshCore

    peer = settings["peer"]
    if "ble_address" in peer:
        connection = await MeshCore.create_ble(
            address=peer["ble_address"], default_timeout=10
        )
    else:
        connection = await MeshCore.create_serial(
            peer["serial_port"], baudrate=peer.get("baudrate", 115200)
        )
    if connection is None:
        raise RuntimeError("MeshCore peer unavailable")
    received = receiver(settings)
    channel = int(peer.get("channel", settings["channel"]))

    async def receive(event):
        payload = event.payload
        text = payload.get("text", "")
        # Native group frames identify their sender in the wire-text prefix.
        sender, separator, _ = text.partition(": ")
        if separator:
            received(text=text, sender=sender, channel=str(payload["channel_idx"]))

    try:
        info = await connection.commands.send_appstart()
        if info.type != EventType.SELF_INFO:
            raise RuntimeError("MeshCore peer metadata unavailable")
        connection.subscribe(EventType.CHANNEL_MSG_RECV, receive)
        await connection.start_auto_message_fetching()
        emit("ready", identity=info.payload["name"])

        async def send(text):
            result = await connection.commands.send_chan_msg(
                channel, text, timestamp=int(time.time())
            )
            if result.type != EventType.OK:
                raise RuntimeError("MeshCore peer rejected send")
            return ""  # Group sends provide no native message ID.

        await commands(send)
    finally:
        await connection.disconnect()


async def lxmf(settings: dict) -> None:
    import LXMF
    import RNS

    peer = settings["peer"]
    identity_path = Path(peer["identity_path"])
    identity = RNS.Identity.from_file(str(identity_path))
    if identity is None:
        raise ValueError("LXMF peer requires an existing identity")
    RNS.Reticulum(
        configdir=peer["reticulum_config_dir"], loglevel=peer.get("loglevel", 2)
    )
    received = receiver(settings)
    router = None
    try:
        interfaces = RNS.Transport.interfaces
        if not interfaces:
            raise RuntimeError("Reticulum peer has no configured interfaces")
        async with asyncio.timeout(20):
            # RNS exposes interface readiness as a synchronous property.
            while not all(iface.online for iface in interfaces):  # noqa: ASYNC110
                await asyncio.sleep(0.2)
        router = LXMF.LXMRouter(
            identity=identity, storagepath=peer["storage_path"], autopeer=False
        )
        destination = router.register_delivery_identity(
            identity, display_name="MEDRE campaign peer", stamp_cost=0
        )

        def receive(message):
            text = message.content.decode(errors="replace")
            if settings["marker_prefix"] not in text:
                return
            emit(
                "native_observed",
                signature_validated=bool(message.signature_validated),
                content_bytes=len(message.content),
            )
            if message.signature_validated:
                received(
                    text=text,
                    sender=message.source_hash.hex(),
                    channel=message.source_hash.hex(),
                    native_id=message.hash.hex(),
                )

        router.register_delivery_callback(receive)

        async def announce():
            while True:
                router.announce(destination.hash)
                await asyncio.sleep(10)

        async def resolve_target():
            target = bytes.fromhex(settings["expected_sender"])
            RNS.Transport.request_path(target)
            async with asyncio.timeout(45):
                # RNS exposes identity discovery as a synchronous cache.
                while (  # noqa: ASYNC110
                    recalled := RNS.Identity.recall(target)
                ) is None:
                    await asyncio.sleep(0.2)
            return recalled

        async def send(text):
            recalled = await resolve_target()
            remote = RNS.Destination(
                recalled,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            message = LXMF.LXMessage(
                remote, destination, text, desired_method=LXMF.LXMessage.DIRECT
            )
            router.handle_outbound(message)
            async with asyncio.timeout(60):
                # LXMF updates this state from its own worker threads.
                while message.state not in (  # noqa: ASYNC110
                    LXMF.LXMessage.DELIVERED,
                    LXMF.LXMessage.FAILED,
                    LXMF.LXMessage.REJECTED,
                    LXMF.LXMessage.CANCELLED,
                ):
                    await asyncio.sleep(0.2)
            if message.state != LXMF.LXMessage.DELIVERED:
                raise RuntimeError("LXMF peer native delivery failed")
            return message.hash.hex()

        async with asyncio.TaskGroup() as tasks:
            announcer = tasks.create_task(announce())
            emit(
                "ready",
                identity=destination.hash.hex(),
                interfaces=[
                    {
                        "type": type(interface).__name__,
                        "online": interface.online,
                        **{
                            field: getattr(interface, field, None)
                            for field in (
                                "r_frequency",
                                "r_bandwidth",
                                "r_txpower",
                                "r_sf",
                                "r_cr",
                                "r_state",
                            )
                        },
                    }
                    for interface in interfaces
                ],
            )
            await commands(send, synchronize=resolve_target)
            announcer.cancel()
    finally:
        RNS.Reticulum.exit_handler()
