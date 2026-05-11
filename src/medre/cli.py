"""MEDRE command-line interface.

Usage::

    medre run [--config PATH]       Start the MEDRE runtime
    medre config check [--config]   Validate config file
    medre config sample             Print a sample TOML config
    medre paths                     Print resolved MEDRE paths
    medre version                   Print MEDRE version
    medre adapters                  List available and configured adapters

The module also supports ``python -m medre.cli`` via the ``__main__`` block
at the bottom of this file.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import logging
import os
import platform
import signal
import sys
import time
from typing import NoReturn

from medre.config.loader import load_config, ConfigSource
from medre.config.sample import generate_sample_config
from medre.config.paths import resolve, MedrePaths
from medre.config.env import apply_env_overrides
from medre.logging import (
    adapter_logger,
    format_duration_ms,
    sanitize_for_log,
    startup_summary,
    shutdown_summary,
)

logger = logging.getLogger("medre")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Transport adapter types that medre supports.
_TRANSPORTS: list[tuple[str, str, tuple[str, ...]]] = [
    ("matrix", "mindroom-nio", ("mindroom_nio", "nio")),
    ("meshtastic", "mtjk", ("mtjk", "meshtastic")),
    ("meshcore", "meshcore", ("meshcore",)),
    ("lxmf", "lxmf", ("lxmf", "RNS")),
]


def _get_version() -> str:
    """Return the MEDRE version string."""
    try:
        return importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _version() -> None:
    """Print version, Python, and platform information."""
    version = _get_version()
    print(f"medre {version}")
    print(f"Python  {platform.python_version()}")
    print(f"Platform {platform.system()} {platform.release()} ({platform.machine()})")


def _dir_status(p: object) -> str:
    """Return a status indicator for a path — exists / will be created."""
    path = p if isinstance(p, str) else str(p)
    if os.path.isdir(path):
        return "exists"
    return "will be created"


def _paths() -> None:
    """Resolve and print all MEDRE paths with status indicators."""
    paths = resolve()
    medre_home = os.environ.get("MEDRE_HOME", "").strip()

    if medre_home:
        print(f"Mode:     MEDRE_HOME")
        print(f"MEDRE_HOME: {medre_home}")
    else:
        print("Mode:     XDG")
        xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
        xdg_state = os.environ.get("XDG_STATE_HOME", "")
        xdg_data = os.environ.get("XDG_DATA_HOME", "")
        xdg_cache = os.environ.get("XDG_CACHE_HOME", "")
        if xdg_config:
            print(f"XDG_CONFIG_HOME:  {xdg_config}")
        if xdg_state:
            print(f"XDG_STATE_HOME:   {xdg_state}")
        if xdg_data:
            print(f"XDG_DATA_HOME:    {xdg_data}")
        if xdg_cache:
            print(f"XDG_CACHE_HOME:   {xdg_cache}")

    print()
    print(f"Config file:  {paths.config_file}")
    print(f"State dir:    {paths.state_dir}  [{_dir_status(paths.state_dir)}]")
    print(f"Data dir:     {paths.data_dir}  [{_dir_status(paths.data_dir)}]")
    print(f"Cache dir:    {paths.cache_dir}  [{_dir_status(paths.cache_dir)}]")
    print(f"Log dir:      {paths.log_dir}  [{_dir_status(paths.log_dir)}]")
    print(f"Global DB:    {paths.database_path}")

    # Try to load config for adapter state roots.
    try:
        config, _source, _paths = load_config(None)
        adapter_roots = []
        for transport, adapter_id, rtc in config.adapters.all_configs():
            if rtc.enabled:
                adapter_roots.append(
                    f"{transport}.{adapter_id}: {paths.adapter_state_dir(adapter_id)}"
                )
        if adapter_roots:
            print()
            print("Adapter state roots:")
            for line in adapter_roots:
                print(f"  {line}")
    except Exception:
        pass  # No config available — skip adapter roots.


def _config_check(config_path: str | None) -> None:
    """Load and validate the config, printing a rich summary."""
    try:
        config, source, paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Config file info ---
    print(f"Config file: {paths.config_file}")
    print(f"Source:      {source.value}")

    # --- Resolved paths ---
    print()
    print("Resolved paths:")
    print(f"  State dir:  {paths.state_dir}")
    print(f"  Data dir:   {paths.data_dir}")
    print(f"  Cache dir:  {paths.cache_dir}")
    print(f"  Log dir:    {paths.log_dir}")
    print(f"  Global DB:  {paths.database_path}")

    # --- Adapter inventory ---
    print()
    print("Adapter inventory:")
    total = 0
    enabled_count = 0
    validation_errors: list[str] = []

    for transport, adapters in [
        ("matrix", config.adapters.matrix),
        ("meshtastic", config.adapters.meshtastic),
        ("meshcore", config.adapters.meshcore),
        ("lxmf", config.adapters.lxmf),
    ]:
        for name, ac in adapters.items():
            total += 1
            status = "enabled" if ac.enabled else "disabled"
            kind = getattr(ac, "adapter_kind", "real")
            print(f"  {transport}.{name}: {status}  (transport={transport}, adapter_kind={kind})")
            if ac.enabled:
                enabled_count += 1
            # Try adapter-specific validation.
            adapter_conf = getattr(ac, "config", None)
            if adapter_conf is not None and hasattr(adapter_conf, "validate"):
                try:
                    adapter_conf.validate()
                except Exception as exc:
                    msg = f"  \u26a0 {transport}.{name}: validation error — {exc}"
                    validation_errors.append(msg)

    if total == 0:
        print("  (no adapters configured)")

    # --- Validation errors ---
    if validation_errors:
        print()
        print("Validation errors:")
        for err in validation_errors:
            print(err)

    # --- Adapter state roots ---
    enabled_adapters = config.adapters.all_configs()
    enabled_for_roots = [(t, aid, rtc) for t, aid, rtc in enabled_adapters if rtc.enabled]
    if enabled_for_roots:
        print()
        print("Adapter state roots:")
        for transport, adapter_id, _rtc in enabled_for_roots:
            root = paths.adapter_state_dir(adapter_id)
            print(f"  {transport}.{adapter_id}: {root}")

    # --- Global DB ---
    print()
    print(f"Global DB: {paths.database_path}")

    # --- Summary ---
    print()
    if validation_errors:
        print(f"Config has {len(validation_errors)} error(s)")
    else:
        print("Config valid")
    print(f"  {enabled_count}/{total} adapter(s) enabled")


def _adapters() -> None:
    """List available adapter types, SDK availability, and configured adapters."""
    print("Adapter types:")

    # Check SDK availability
    for transport, dist_name, import_names in _TRANSPORTS:
        installed = False
        for mod_name in import_names:
            try:
                importlib.import_module(mod_name)
                installed = True
                break
            except ImportError:
                pass
        status = "installed" if installed else "not installed"
        print(f"  {transport:14s} SDK ({dist_name}): {status}")

    # Configured adapters from config
    print()
    try:
        config, _source, _paths = load_config(None)
        configured = config.adapters.all_configs()
        if not configured:
            print("No adapters configured.")
            return
        print("Configured adapters:")
        for transport, adapter_id, rtc in configured:
            status = "enabled" if rtc.enabled else "disabled"
            kind = getattr(rtc, "adapter_kind", "real")
            print(f"  {transport}.{adapter_id}: {status} (kind={kind})")
    except Exception:
        print("No config found — run 'medre config sample' to generate one.")


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

    # Check for enabled adapters *before* building runtime.
    enabled_adapters = config.adapters.all_enabled()
    if not enabled_adapters:
        print(
            "Error: no adapters enabled. Set at least one adapter enabled = true in config.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Configure logging from the loaded config
    _setup_logging(config)

    adapter_ids = [aid for aid, _rtc in enabled_adapters]
    logger.info("MEDRE starting — config source: %s", source.value)
    logger.info("Config path: %s", paths.config_file)
    logger.info("State dir:   %s", paths.state_dir)

    # Print startup header to console
    print(f"Runtime starting with {len(enabled_adapters)} adapter(s): {', '.join(adapter_ids)}")

    builder = RuntimeBuilder(config, paths)
    app = builder.build()

    # Install signal handlers for clean shutdown
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    # Track per-adapter startup timing
    run_start = time.monotonic()
    startup_results: list[tuple[str, str, bool, float, str | None]] = []

    try:
        await app.start()
        # After app.start() succeeds, all adapters started.
        # Build startup summary from adapter list.
        for adapter_id in app.adapters:
            dur = time.monotonic() - run_start
            startup_results.append((adapter_id, _transport_for_adapter(adapter_id, config), True, dur, None))

    except Exception as exc:
        # If startup partially succeeded, report which adapters started.
        # The exception typically carries the adapter that failed.
        from medre.runtime.errors import AdapterStartupError
        failed_id: str | None = None
        failed_msg: str | None = None
        if isinstance(exc, AdapterStartupError):
            failed_id = exc.adapter_id
            failed_msg = str(exc)

        # Report successful adapters
        for adapter_id in app.adapters:
            dur = time.monotonic() - run_start
            startup_results.append(
                (adapter_id, _transport_for_adapter(adapter_id, config), True, dur, None)
            )

        # Report failed adapter
        if failed_id:
            dur = time.monotonic() - run_start
            startup_results.append(
                (failed_id, _transport_for_adapter(failed_id, config), False, dur, failed_msg)
            )
        else:
            logger.error("Runtime startup failed: %s", exc)
            raise

        # Print startup summary with failures
        summary = startup_summary(startup_results)
        print(summary)
        raise

    # Print startup summary
    summary = startup_summary(startup_results)
    print(summary)

    # Log structured startup
    logger.info(
        "Runtime started — %d adapter(s) in %s",
        len(app.adapters),
        format_duration_ms(run_start),
        extra=sanitize_for_log({"adapter_count": len(app.adapters)}),
    )

    try:
        while not shutdown_requested:
            try:
                await asyncio.wait_for(app.wait_for_shutdown(), timeout=1.0)
            except asyncio.TimeoutError:
                pass  # expected — poll loop
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")
    finally:
        # Shutdown
        print("Runtime shutting down")
        logger.info("Runtime shutting down")

        shutdown_errors: list[tuple[str, str]] = []
        try:
            await app.stop()
        except Exception as exc:
            logger.error("Shutdown error: %s", exc)
            shutdown_errors.append(("runtime", str(exc)))

        # Per-adapter shutdown messages
        for adapter_id in app.adapters:
            alog = adapter_logger("medre.adapters", adapter_id, _transport_for_adapter(adapter_id, config))
            alog.info("Adapter %s stopped", adapter_id)
            print(f"  stopped {adapter_id}")

        summary = shutdown_summary(list(app.adapters.keys()), shutdown_errors or None)
        print(summary)
        logger.info("MEDRE stopped")


def _transport_for_adapter(adapter_id: str, config: object) -> str:
    """Look up the transport type for an adapter_id from config."""
    adapters = getattr(config, "adapters", None)
    if adapters is None:
        return "unknown"
    for transport in ("matrix", "meshtastic", "meshcore", "lxmf"):
        group = getattr(adapters, transport, {})
        for _name, rtc in group.items():
            if rtc.adapter_id == adapter_id:
                return transport
    return "unknown"


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

    # adapters
    sub.add_parser("adapters", help="List available and configured adapters")

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
    elif args.command == "adapters":
        _adapters()


if __name__ == "__main__":
    main()
