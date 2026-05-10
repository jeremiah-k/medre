"""MEDRE command-line interface.

Usage::

    medre run [--config PATH]       Start the MEDRE runtime
    medre config check [--config]   Validate config file
    medre config sample             Print a sample TOML config
    medre paths                     Print resolved MEDRE paths
    medre version                   Print MEDRE version

The module also supports ``python -m medre.cli`` via the ``__main__`` block
at the bottom of this file.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import logging
import signal
import sys
from typing import NoReturn

from medre.config.loader import load_config, ConfigSource
from medre.config.sample import generate_sample_config
from medre.config.paths import resolve
from medre.config.env import apply_env_overrides

logger = logging.getLogger("medre")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _version() -> None:
    """Print the MEDRE version."""
    try:
        version = importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:
        version = "0.1.0"
    print(f"medre {version}")


def _paths() -> None:
    """Resolve and print all MEDRE paths."""
    paths = resolve()
    print(f"Config file:  {paths.config_file}")
    print(f"State dir:    {paths.state_dir}")
    print(f"Data dir:     {paths.data_dir}")
    print(f"Cache dir:    {paths.cache_dir}")
    print(f"Log dir:      {paths.log_dir}")
    print(f"Database:     {paths.database_path}")
    print(f"Matrix store: {paths.matrix_store_path}")


def _config_check(config_path: str | None) -> None:
    """Load and validate the config, printing a summary."""
    try:
        config, source, paths = load_config(config_path)
        print(f"Config OK — source: {source.value}")
        print(f"Config path: {paths.config_file}")
        print(f"State dir:   {paths.state_dir}")
        # Adapter summary
        for transport, adapters in [
            ("matrix", config.adapters.matrix),
            ("meshtastic", config.adapters.meshtastic),
            ("meshcore", config.adapters.meshcore),
            ("lxmf", config.adapters.lxmf),
        ]:
            for name, ac in adapters.items():
                status = "enabled" if ac.enabled else "disabled"
                print(f"  {transport}.{name}: {status}")
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Run command (async)
# ---------------------------------------------------------------------------

shutdown_requested: bool = False


def _request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested  # noqa: PLW0603
    shutdown_requested = True
    logger.info("Received signal %s — requesting shutdown", signal.Signals(signum).name)


async def _run(config_path: str | None) -> None:
    """Load config, build the runtime, and run until interrupted."""
    from medre.runtime.builder import RuntimeBuilder

    config, source, paths = load_config(config_path)
    config = apply_env_overrides(config, paths)

    # Configure logging from the loaded config
    _setup_logging(config)

    logger.info("MEDRE starting — config source: %s", source.value)
    logger.info("Config path: %s", paths.config_file)
    logger.info("State dir:   %s", paths.state_dir)

    builder = RuntimeBuilder(config, paths)
    app = builder.build()

    # Install signal handlers for clean shutdown
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    await app.start()

    try:
        while not shutdown_requested:
            await app.wait_for_shutdown(timeout=1.0)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
    finally:
        await app.stop()
        logger.info("MEDRE stopped")


def _setup_logging(config: object) -> None:
    """Apply logging configuration from the parsed config."""
    log_cfg = getattr(config, "logging", None)
    if log_cfg is None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        return

    level = getattr(log_cfg, "level", "INFO")
    fmt = getattr(log_cfg, "format", None) or "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medre",
        description="Modular Event-driven Routing Engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Start the MEDRE runtime")
    run_p.add_argument("--config", default=None, help="Path to config file")

    # config (with sub-subcommands)
    config_p = sub.add_parser("config", help="Config management commands")
    config_sub = config_p.add_subparsers(dest="config_command", required=True)
    check_p = config_sub.add_parser("check", help="Validate config file")
    check_p.add_argument("--config", default=None, help="Path to config file")
    config_sub.add_parser("sample", help="Print sample config")

    # paths
    sub.add_parser("paths", help="Print resolved MEDRE paths")

    # version
    sub.add_parser("version", help="Print MEDRE version")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to the appropriate command handler."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        import asyncio

        try:
            asyncio.run(_run(args.config))
        except KeyboardInterrupt:
            pass
    elif args.command == "config":
        if args.config_command == "check":
            _config_check(args.config)
        elif args.config_command == "sample":
            print(generate_sample_config())
    elif args.command == "paths":
        _paths()
    elif args.command == "version":
        _version()


if __name__ == "__main__":
    main()
