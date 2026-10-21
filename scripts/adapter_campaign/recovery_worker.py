"""Actual runtime subprocess with harness-owned, precisely placed fault barriers."""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path

from medre.config._yaml import parse_yaml_config
from medre.config.loader import parse_runtime_config
from medre.config.paths import resolve
from medre.core.observability.logging import setup_logging
from medre.runtime.builder import RuntimeBuilder

from .tcp_gate import TcpGate
from .worker import emit

_RECOVERY_BACKOFF_BASE_SECONDS = 10
_RECOVERY_MAX_DELAY_SECONDS = 20


async def run(settings: dict) -> None:
    barrier = settings.get("barrier")
    if barrier not in (None, "before_handoff", "after_handoff"):
        raise ValueError("Unknown recovery barrier")
    source, target = settings["source"], settings["target"]
    path = Path(settings["runtime_file"])
    data = parse_yaml_config(path.read_text(), str(path))
    destination = data["adapters"]["xmpp"][target]
    if (
        not isinstance(destination.get("host"), str)
        or not destination["host"]
        or type(destination.get("port")) is not int
        or not 1 <= destination["port"] <= 65535
    ):
        raise ValueError(
            "The recovery TCP gate requires an explicit XMPP host and port"
        )
    gate = TcpGate(destination["host"], destination["port"])
    app = None
    try:
        host, port = await gate.start()
        destination.update(
            host=host,
            port=port,
            reconnect_delay_seconds=0.2,
            connect_timeout_seconds=10,
        )
        data["adapters"] = {
            "matrix": {source: data["adapters"]["matrix"][source]},
            "xmpp": {target: destination},
        }
        data["storage"] = {
            "backend": "sqlite",
            "path": settings["database"],
        }
        data["retry"] = {"enabled": True, "interval_seconds": 0.1, "max_attempts": 3}
        data["logging"] = {"level": "INFO"}
        data["routes"] = {
            "recovery_route": {
                "source_adapters": [source],
                "dest_adapters": [target],
                "source_channel": settings["source_channel"],
                "dest_channel": settings["target_channel"],
                "policy": {"allowed_event_types": ["message.created", "message.text"]},
                "retry": {
                    "enabled": True,
                    "max_attempts": 3,
                    "backoff_base": _RECOVERY_BACKOFF_BASE_SECONDS,
                    "max_delay_seconds": _RECOVERY_MAX_DELAY_SECONDS,
                    "jitter": False,
                },
            }
        }
        paths = resolve(environ={"MEDRE_HOME": settings["directory"]})
        app = RuntimeBuilder(parse_runtime_config(data, paths), paths).build()
        if barrier is not None:
            original = app.adapters[target].deliver

            async def paused(rendered):
                if settings["marker"] not in str(rendered.payload.get("body", "")):
                    return await original(rendered)
                if barrier == "before_handoff":
                    emit("barrier", stage=barrier, event_id=rendered.event_id)
                    await asyncio.Event().wait()
                result = await original(rendered)
                emit("barrier", stage=barrier, event_id=rendered.event_id)
                await asyncio.Event().wait()
                return result

            app.adapters[target].deliver = paused
        await app.start()
        if app.build_failures or set(app.started_adapter_ids) != {source, target}:
            raise RuntimeError("Every recovery adapter must start")
        async with asyncio.timeout(30):
            while True:
                healthy = [
                    (await adapter.health_check()).health == "healthy"
                    for adapter in app.adapters.values()
                ]
                if all(healthy):
                    break
                await asyncio.sleep(0.1)
        emit("ready", identity="recovery-runtime")
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            operation = command["op"]
            if operation == "stop":
                return
            if operation == "block":
                await gate.block()
            elif operation == "restore":
                gate.restore()
            elif operation != "status":
                raise ValueError("Unknown runtime control operation")
            health = await app.adapters[target].health_check()
            emit(
                "sent",
                command=command["id"],
                native_id="",
                target_health=health.health,
                gate=gate.snapshot(),
            )
        raise RuntimeError("Recovery supervisor closed without stop")
    finally:
        try:
            if app is not None:
                await app.stop()
        finally:
            await gate.close()


if __name__ == "__main__":
    setup_logging(level="INFO")
    try:
        asyncio.run(run(json.loads(sys.stdin.readline())))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        emit("error")
        raise SystemExit(1) from None
