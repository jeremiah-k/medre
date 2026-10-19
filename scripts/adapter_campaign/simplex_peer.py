"""Independent official-SDK group peer; no MEDRE adapter/session imports."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .worker import commands, emit, receiver


def group_message(record: dict, group_id: int) -> dict | None:
    """Select complete text from an authenticated member of the fixture group."""
    info, item = record["chatInfo"], record["chatItem"]
    direction, content = item["chatDir"], item["content"]
    scope = info.get("groupChatScope")
    if (
        info.get("type") != "group"
        or info["groupInfo"]["groupId"] != group_id
        or (scope is not None and scope.get("type") != "main")
        or direction.get("type") != "groupRcv"
        or content.get("type") != "rcvMsgContent"
        or content["msgContent"].get("type") != "text"
        or item.get("file") is not None
        or item["meta"].get("itemLive") is True
    ):
        return None
    return {
        "text": content["msgContent"]["text"],
        "sender": str(direction["groupMember"]["groupMemberId"]),
        "channel": f"group:{group_id}",
        "native_id": str(item["meta"]["itemId"]),
    }


async def simplexchat(settings: dict) -> None:
    peer = settings["peer"]
    channel = peer.get("channel", settings["channel"])
    kind, number = channel.split(":", 1)
    if kind != "group" or not number.isdecimal() or int(number) < 1:
        raise ValueError("SimpleX campaign requires a group conversation")
    group_id = int(number)
    prefix = peer["database_prefix"]
    if not Path(prefix + "_chat.db").is_file():
        raise ValueError("Initialize the independent SimpleX peer explicitly")
    if peer.get("libs_dir"):
        os.environ["SIMPLEX_LIBS_DIR"] = peer["libs_dir"]
    if peer.get("native_cache_dir"):
        os.environ["XDG_CACHE_HOME"] = peer["native_cache_dir"]
    # Import lazily, after the peer's native-library environment is configured.
    from simplex_chat import ChatApi, MigrationConfirmation, SqliteDb

    received = receiver(settings)
    api = await ChatApi.init(
        SqliteDb(prefix, peer.get("database_key", "")),
        confirm=MigrationConfirmation.YES_UP,
    )
    try:
        user = await api.api_get_active_user()
        if user is None or user["userId"] != peer.get("user_id", 1):
            raise ValueError("SimpleX peer active user differs from the fixture")
        await api.start_chat()
        history = await api.api_get_chat("group", group_id, 1)
        membership = history["chatInfo"]["groupInfo"]["membership"]
        if membership["memberStatus"] not in {"creator", "connected", "complete"}:
            raise ValueError("SimpleX peer must already belong to the test group")

        async def collect():
            while True:
                event = await api.recv_chat_event(wait_us=100_000)
                if not event or event.get("type") != "newChatItems":
                    continue
                if event["user"]["userId"] != user["userId"]:
                    continue
                for record in event["chatItems"]:
                    message = group_message(record, group_id)
                    if message is not None:
                        received(**message)

        async def send(text):
            rows = await api.api_send_messages(
                {"chatType": "group", "chatId": group_id},
                [{"msgContent": {"type": "text", "text": text}, "mentions": {}}],
            )
            return rows[0]["chatItem"]["meta"]["itemId"]

        async with asyncio.TaskGroup() as tasks:
            collector = tasks.create_task(collect())
            emit("ready", identity=str(membership["groupMemberId"]))
            await commands(send)
            collector.cancel()
    finally:
        try:
            if api.started:
                await api.stop_chat()
        finally:
            await api.close()
