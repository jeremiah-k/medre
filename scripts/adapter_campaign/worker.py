"""SDK-only peer subprocess. Never imports MEDRE or injects canonical events."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import traceback

_OUTPUT_LOCK = threading.Lock()


def emit(kind: str, **data) -> None:
    with _OUTPUT_LOCK:
        print(
            "MRPEER " + json.dumps({"kind": kind, "at": time.time(), **data}),
            flush=True,
        )


def receiver(settings: dict):
    def received(
        *,
        text: str,
        sender: str,
        channel: str,
        native_id: str = "",
        formatted_body: str | None = None,
    ):
        # Retain only this campaign's bounded nonces, never unrelated traffic.
        if settings["marker_prefix"] in text:
            emit(
                "received",
                text=text,
                sender=sender,
                channel=channel,
                native_id=native_id,
                **(
                    {"formatted_body": formatted_body}
                    if formatted_body is not None
                    else {}
                ),
            )

    return received


async def commands(send, synchronize=None) -> None:
    while line := await asyncio.to_thread(sys.stdin.readline):
        command = json.loads(line)
        if command["op"] == "stop":
            return
        if command["op"] == "synchronize":
            if synchronize is not None:
                await synchronize()
            emit("sent", command=command["id"], native_id="")
            continue
        if command["op"] != "send":
            raise ValueError("Unknown peer command")
        native_id = await send(command["text"])
        emit("sent", command=command["id"], native_id=str(native_id or ""))
    raise RuntimeError("Supervisor closed before stop")


async def main() -> None:
    settings = json.loads(await asyncio.to_thread(sys.stdin.readline))
    from .ip_peers import chatmail, matrix, xmpp
    from .radio_peers import lxmf, meshcore, meshtastic
    from .simplex_peer import simplexchat

    drivers = {
        "matrix": matrix,
        "xmpp": xmpp,
        "chatmail": chatmail,
        "meshtastic": meshtastic,
        "meshcore": meshcore,
        "lxmf": lxmf,
        "simplexchat": simplexchat,
    }
    await drivers[settings["transport"]](settings)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        # Exception text from SDKs can contain credentials or whole RPC calls.
        # stderr is captured only inside the private evidence directory.
        traceback.print_exc(file=sys.stderr)
        emit("error")
        sys.exit(1)
