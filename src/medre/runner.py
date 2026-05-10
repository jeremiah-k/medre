"""Config-driven MEDRE runner.

Bootstraps the full runtime stack from a TOML configuration file with
MEDRE_ environment overrides.

Usage::

    python -m medre.runner [--config PATH]

Or via the ``medre`` CLI::

    medre run [--config PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

from medre.config.loader import load_config
from medre.config.env import apply_env_overrides
from medre.config.paths import MedrePaths
from medre.runtime.builder import RuntimeBuilder


async def run(config_path: str | None = None) -> None:
    """Load config, apply env overrides, build runtime, and run."""
    # 1. Load config
    config, source, paths = load_config(config_path)

    # 2. Set up logging per config
    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        stream=sys.stderr,
    )

    # 3. Apply env overrides
    config = apply_env_overrides(config, paths)

    # 4. Build runtime
    builder = RuntimeBuilder(config, paths)
    app = builder.build()

    # 5. Signal handling
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.shutdown_event.set)

    # 6. Start
    await app.start()

    # 7. Wait for shutdown
    await app.wait_for_shutdown()

    # 8. Stop cleanly
    await app.stop()


def main(argv: list[str] | None = None) -> None:
    """Synchronous entry point for ``python -m medre.runner``."""
    parser = argparse.ArgumentParser(prog="python -m medre.runner")
    parser.add_argument("--config", help="Path to TOML configuration file")
    args = parser.parse_args(argv)
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
