"""Own a native typing runtime and expose only explicit campaign controls."""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from dataclasses import replace
from pathlib import Path

from medre.core.observability.logging import setup_logging
from medre.runtime.builder import RuntimeBuilder

from .plan import Plan, runtime_config
from .worker import emit


async def run(settings: dict) -> None:
    """Start production assembly; withdraw configured grants without new ingress."""
    plan = Plan(Path(settings["runtime_file"]), settings["endpoints"], 60, 0, 1)
    config, paths = runtime_config(plan, Path(settings["directory"]))
    if type(settings["forward_typing"]) is not bool:
        raise ValueError("forward_typing requires an explicit boolean")
    config = replace(
        config,
        routes=replace(
            config.routes,
            routes=tuple(
                replace(route, forward_typing=settings["forward_typing"])
                for route in config.routes.routes
            ),
        ),
    )
    app = RuntimeBuilder(config, paths).build()
    try:
        await app.start()
        if app.build_failures or set(app.started_adapter_ids) != set(plan.endpoints):
            raise RuntimeError("Every typing adapter must start")
        emit("ready", identity="typing-runtime")
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["op"] == "stop":
                return
            if command["op"] != "withdraw":
                raise ValueError("Unknown typing runtime command")
            for name, endpoint in plan.endpoints.items():
                for route in app.router.typing_routes(name, endpoint["channel"]):
                    route.forward_typing = False
            emit("sent", command=command["id"], native_id="")
        raise RuntimeError("Typing supervisor closed without stop")
    finally:
        await app.stop()
        relay = app._typing_relay
        if relay is not None:
            emit(
                "activity_summary",
                accepted=relay.accepted,
                refused=relay.refused,
                send_failures=relay.send_failures,
            )


if __name__ == "__main__":
    setup_logging(level="INFO")
    try:
        asyncio.run(run(json.loads(sys.stdin.readline())))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        emit("error")
        raise SystemExit(1) from None
