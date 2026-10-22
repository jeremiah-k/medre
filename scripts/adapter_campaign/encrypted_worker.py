"""Own two production Matrix adapters with persistent, exclusive crypto stores."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import traceback

from medre.config._yaml import parse_yaml_config
from medre.config.loader import parse_runtime_config
from medre.config.paths import resolve
from medre.core.observability.logging import setup_logging
from medre.runtime.builder import RuntimeBuilder

from .worker import emit


async def run(settings: dict) -> None:
    """Start the production builder, require healthy adapters, and drain on stop.

    Generations share durable core state and distinct per-device SDK stores.
    No canonical messages or adapter handles are exposed to the supervisor.
    """
    path = Path(settings["runtime_file"])
    paths = resolve(environ={"MEDRE_HOME": settings["directory"]})
    config = parse_runtime_config(parse_yaml_config(path.read_text(), str(path)), paths)
    app = RuntimeBuilder(config, paths).build()
    try:
        await app.start()
        if app.build_failures or set(app.started_adapter_ids) != {
            "matrix",
            "matrix_target",
        }:
            raise RuntimeError("Both encrypted Matrix adapters must start")
        async with asyncio.timeout(60):
            while True:
                healthy = all(
                    [
                        (await adapter.health_check()).health == "healthy"
                        for adapter in app.adapters.values()
                    ]
                )
                if healthy:
                    break
                await asyncio.sleep(0.1)
        emit("ready", identity="encrypted-runtime")
        while line := await asyncio.to_thread(sys.stdin.readline):
            if json.loads(line)["op"] == "stop":
                return
            raise ValueError("Unknown encrypted runtime command")
        raise RuntimeError("Encrypted runtime supervisor closed without stop")
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
