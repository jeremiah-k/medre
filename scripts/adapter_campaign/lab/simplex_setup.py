"""Official SimpleX SDK provisioning worker; one native controller per process."""

from __future__ import annotations

import asyncio
import json
import sys


async def main() -> None:
    from simplex_chat import ChatApi, MigrationConfirmation, SqliteDb

    settings = json.loads(await asyncio.to_thread(sys.stdin.readline))
    api = await ChatApi.init(
        SqliteDb(settings["prefix"], ""), confirm=MigrationConfirmation.YES_UP
    )
    user = await api.api_get_active_user()
    if user is None:
        user = await api.api_create_active_user(
            {"displayName": settings["name"], "fullName": ""}
        )
    await api.start_chat()
    # The configuration command requires a started controller. No contacts or
    # queues exist yet, and the lab network has no external egress.
    try:
        response = await api.send_chat_cmd("/smp " + settings["smp"])
    except Exception as exc:
        print(repr(vars(exc)), file=sys.stderr, flush=True)
        await api.close()
        raise
    if response.get("type") in {"chatCmdError", "chatError"}:
        raise RuntimeError("Local SimpleX SMP configuration rejected")

    def emit(value):
        print("MRSETUP " + json.dumps(value), flush=True)

    async def drain():
        while True:
            await api.recv_chat_event(wait_us=100_000)

    collector = asyncio.create_task(drain())
    emit({"ready": user["userId"]})
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["op"] == "stop":
                break
            try:
                result = await getattr(api, command["op"])(*command.get("args", []))
                emit({"result": result})
            except Exception as exc:
                emit({"error": type(exc).__name__})
    finally:
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)
        await api.stop_chat()
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
