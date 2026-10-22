"""Independent Matrix Client API, Slixmpp MUC, and native Chatmail peers."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import ssl
import subprocess
from urllib.parse import quote
from uuid import uuid4
from xml.etree.ElementTree import SubElement

from .worker import commands, emit, receiver


async def matrix(settings: dict) -> None:
    import aiohttp

    peer = settings["peer"]
    channel = peer.get("channel", settings["channel"])
    received = receiver(settings)
    async with aiohttp.ClientSession(
        headers={"Authorization": "Bearer " + peer["access_token"]},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as http:

        async def request(method, path, **kwargs):
            async with http.request(
                method, peer["homeserver"] + "/_matrix/client/v3/" + path, **kwargs
            ) as response:
                response.raise_for_status()
                return await response.json()

        identity = (await request("GET", "account/whoami"))["user_id"]
        if identity != peer["user_id"]:
            raise ValueError("Matrix peer identity mismatch")
        rooms = (await request("GET", "joined_rooms"))["joined_rooms"]
        if channel not in rooms:
            raise ValueError("Matrix peer must already belong to the test room")
        # Establish a fresh sync token before readiness; discard prior history.
        baseline = await request("GET", "sync", params={"timeout": "0"})
        since = baseline["next_batch"]

        async def collect():
            nonlocal since
            while True:
                data = await request(
                    "GET", "sync", params={"since": since, "timeout": "1000"}
                )
                since = data["next_batch"]
                for event in (
                    data.get("rooms", {})
                    .get("join", {})
                    .get(channel, {})
                    .get("timeline", {})
                    .get("events", [])
                ):
                    if event.get("type") == "m.room.message":
                        received(
                            text=event.get("content", {}).get("body", ""),
                            sender=event["sender"],
                            channel=channel,
                            native_id=event["event_id"],
                            native_details={
                                "reply_to": event.get("content", {})
                                .get("m.relates_to", {})
                                .get("m.in_reply_to", {})
                                .get("event_id")
                            },
                            formatted_body=event.get("content", {}).get(
                                "formatted_body"
                            ),
                        )

        async def send(text, target=None, presentation=None):
            content = {"msgtype": "m.text", "body": text}
            if target is not None:
                content["m.relates_to"] = {"m.in_reply_to": {"event_id": target}}
            if presentation is not None and "html" in presentation:
                content.update(
                    format="org.matrix.custom.html", formatted_body=presentation["html"]
                )
            result = await request(
                "PUT",
                f"rooms/{quote(channel, safe='')}/send/m.room.message/{uuid4().hex}",
                json=content,
            )
            return result["event_id"]

        async with asyncio.TaskGroup() as tasks:
            collector = tasks.create_task(collect())
            emit("ready", identity=identity)

            async def styled(text, presentation):
                return await send(text, presentation=presentation)

            await commands(send, reply=send, styled=styled)
            collector.cancel()


async def xmpp(settings: dict) -> None:
    import slixmpp

    peer = settings["peer"]
    room = peer.get("channel", settings["channel"])
    received = receiver(settings)
    client = slixmpp.ClientXMPP(peer["jid"], peer["password"])
    client.ssl_context = ssl.create_default_context()
    client.enable_plaintext = False
    client.register_plugin("xep_0030")
    client.register_plugin("xep_0045")
    ready = asyncio.get_running_loop().create_future()
    disconnected = asyncio.Event()
    trusted_room = False

    async def session(_):
        nonlocal trusted_room
        try:
            client.send_presence()
            await client.plugin["xep_0045"].join_muc_wait(
                room, peer["nickname"], maxstanzas=0, timeout=20
            )
            info = await client.plugin["xep_0030"].get_info(jid=room, timeout=20)
            trusted_room = "urn:xmpp:sid:0" in info["disco_info"]["features"]
            ready.set_result(None)
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)

    def message(stanza):
        if str(stanza["type"]) == "groupchat" and str(stanza["from"].bare) == room:
            ids = {
                entry.get("id")
                for entry in stanza.xml.findall("{urn:xmpp:sid:0}stanza-id")
                if entry.get("by") == room and entry.get("id")
            }
            reply = stanza.xml.find("{urn:xmpp:reply:0}reply")
            origins = stanza.xml.findall("{urn:xmpp:sid:0}origin-id")
            region = stanza.xml.find(
                "{urn:xmpp:fallback:0}fallback[@for='urn:xmpp:reply:0']/{urn:xmpp:fallback:0}body"
            )
            received(
                text=str(stanza["body"]),
                sender=str(stanza["from"]),
                channel=room,
                native_id=str(stanza["id"]),
                native_details={
                    "unstyled": stanza.xml.find("{urn:xmpp:styling:0}unstyled")
                    is not None,
                    "origin_id": origins[0].get("id") if len(origins) == 1 else None,
                    "room_stanza_id": (
                        next(iter(ids)) if trusted_room and len(ids) == 1 else None
                    ),
                    "reply_to": reply.get("id") if reply is not None else None,
                    "fallback_start": (
                        region.get("start") if region is not None else None
                    ),
                    "fallback_end": region.get("end") if region is not None else None,
                },
            )

    def failed(_):
        if not ready.done():
            ready.set_exception(RuntimeError("XMPP authentication failed"))

    client.add_event_handler("session_start", session)
    client.add_event_handler("failed_all_auth", failed)
    client.add_event_handler("message", message)
    client.add_event_handler("disconnected", lambda _: disconnected.set())
    connection = client.connect(peer.get("host"), peer.get("port", 5222))
    try:
        await asyncio.wait_for(asyncio.shield(ready), 30)
        emit("ready", identity=f"{room}/{peer['nickname']}")

        async def send(text, target=None, presentation=None):
            stanza = client.make_message(mto=room, mtype="groupchat", mbody=text)
            SubElement(
                stanza.xml, "{urn:xmpp:sid:0}origin-id", {"id": str(stanza["id"])}
            )
            if target is not None:
                SubElement(stanza.xml, "{urn:xmpp:reply:0}reply", {"id": target})
            if presentation is not None and presentation.get("unstyled") is True:
                SubElement(stanza.xml, "{urn:xmpp:styling:0}unstyled")
            stanza.send()
            return str(stanza["id"])

        async def styled(text, presentation):
            return await send(text, presentation=presentation)

        async with asyncio.TaskGroup() as tasks:
            command_task = tasks.create_task(commands(send, reply=send, styled=styled))
            closed = tasks.create_task(disconnected.wait())
            done, _ = await asyncio.wait(
                [command_task, closed], return_when=asyncio.FIRST_COMPLETED
            )
            if closed in done:
                raise RuntimeError("XMPP peer disconnected")
            closed.cancel()
    finally:
        await client.disconnect(wait=2)
        if not connection.done():
            connection.cancel()
        await asyncio.gather(connection, return_exceptions=True)


async def chatmail(settings: dict) -> None:
    from deltachat_rpc_client import Rpc

    peer = settings["peer"]
    account, chat = peer["account_id"], int(peer["channel"])
    received = receiver(settings)

    class PeerRpc(Rpc):
        def events_loop(self):
            # Native storage is the oracle; do not retain unbounded SDK queues.
            with contextlib.suppress(Exception):
                while self.get_next_event_batch() and not self.closing:
                    pass

    rpc = PeerRpc(
        peer["accounts_dir"],
        rpc_server_path=peer.get("rpc_server_path")
        or shutil.which("deltachat-rpc-server"),
        stderr=subprocess.DEVNULL,
        process_group=None,  # Keep the native server in the supervisor-owned group.
    )

    async def call(method, *args):
        return await asyncio.to_thread(getattr(rpc, method), *args)

    try:
        await asyncio.to_thread(rpc.start)
        info = await call("get_basic_chat_info", account, chat)
        if (
            info["chatType"] != "Group"
            or info["isContactRequest"]
            or info["isSelfTalk"]
            or info["isDeviceChat"]
        ):
            raise ValueError("Chatmail campaign requires an accepted group")
        seen = set(await call("get_message_ids", account, chat, False, False))
        await call("start_io", account)

        async def collect():
            while True:
                for message_id in await call(
                    "get_message_ids", account, chat, False, False
                ):
                    if message_id in seen:
                        continue
                    message = await call("get_message", account, message_id)
                    if message.get("downloadState") != "Done":
                        continue
                    seen.add(message_id)
                    if message.get("state") in (10, 13, 16) and not message.get(
                        "isInfo"
                    ):
                        received(
                            text=message.get("text", ""),
                            sender=str(message["fromId"]),
                            channel=str(chat),
                            native_id=str(message_id),
                        )
                await asyncio.sleep(0.2)

        async def send(text):
            return await call("send_msg", account, chat, {"text": text})

        async with asyncio.TaskGroup() as tasks:
            collector = tasks.create_task(collect())
            emit("ready", identity="1")
            await commands(send)
            collector.cancel()
    finally:
        # Inspect __dict__: Rpc synthesizes methods for missing attributes.
        process = vars(rpc).get("process")
        if process is not None:
            try:
                await asyncio.wait_for(asyncio.to_thread(rpc.close), 10)
            finally:
                if process.poll() is None:
                    process.kill()
                await asyncio.to_thread(process.wait)
