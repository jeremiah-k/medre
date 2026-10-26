"""Own a real bidirectional runtime without injecting canonical test messages."""

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
    plan = Plan(Path(settings["runtime_file"]), settings["endpoints"], 60, 0, 1)
    config, paths = runtime_config(plan, Path(settings["directory"]))
    config = replace(config, logging=replace(config.logging, level="INFO"))
    app = RuntimeBuilder(config, paths).build()
    try:
        await app.start()
        if app.build_failures or set(app.started_adapter_ids) != set(plan.endpoints):
            raise RuntimeError("Every fidelity adapter must start")
        async with asyncio.timeout(40):
            while True:
                healthy = [
                    (await adapter.health_check()).health == "healthy"
                    for adapter in app.adapters.values()
                ]
                if all(healthy):
                    break
                await asyncio.sleep(0.1)
        emit("ready", identity="fidelity-runtime")
        while line := await asyncio.to_thread(sys.stdin.readline):
            if json.loads(line)["op"] == "stop":
                return
            raise ValueError("Unknown fidelity runtime command")
        raise RuntimeError("Fidelity supervisor closed without stop")
    finally:
        await app.stop()


if __name__ == "__main__":
    setup_logging(level="INFO")
    try:
        asyncio.run(run(json.loads(sys.stdin.readline())))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        emit("error")
        raise SystemExit(1) from None
