"""Explicit stopped-state maintenance CLI, separate from normal runtime commands.

Usage: python -m medre.maintenance.stopped_cli capture --inventory FILE
       --root COMMON_STATE_ROOT --snapshot NEW_DIR --stopped
       python -m medre.maintenance.stopped_cli verify --snapshot DIR
       python -m medre.maintenance.stopped_cli restore --snapshot DIR
       --destination NEW_DIR --stopped
       python -m medre.maintenance.stopped_cli plan --config FILE
       --root COMMON_STATE_ROOT --inventory NEW_FILE
       python -m medre.maintenance.stopped_cli check --config FILE --root ROOT
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import NoReturn, Sequence

from medre.config.errors import ConfigError
from medre.config.model import RuntimeConfig
from medre.config.paths import MedrePaths
from medre.maintenance.stopped_declare import (
    ExclusionRefused,
    build_stopped_capture_plan,
    retain_local_exclusion,
)
from medre.maintenance.stopped_inventory import StoppedInventory
from medre.maintenance.stopped_snapshot import (
    create_stopped_snapshot,
    restore_stopped_snapshot,
    verify_stopped_snapshot,
)


from medre.runtime.errors import RuntimeConfigError


class _PrivateArgumentParser(argparse.ArgumentParser):
    """Refuse malformed arguments without echoing native profile paths."""

    def error(self, message: str) -> NoReturn:
        # argparse normally prints the raw unknown arguments and their values.
        # Subcommands inherit this parser class, so sanitize both entry points.
        self.exit(2, "medre stopped-state: invalid arguments (use --help)\n")


def _load_configuration(
    config: Path | None,
) -> tuple[RuntimeConfig, MedrePaths]:
    """Load runtime configuration without starting any runtime component."""
    from medre.config.env import apply_env_overrides
    from medre.config.loader import load_config

    runtime_config, _source, paths = load_config(config)
    return apply_env_overrides(runtime_config, paths), paths


def main(argv: Sequence[str] | None = None) -> int:
    """Never infer stop state or overwrite a destination; failures return nonzero."""
    parser = _PrivateArgumentParser(description="MEDRE stopped-state maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser(
        "capture", help="capture a selected stopped installation"
    )
    capture.add_argument("--inventory", type=Path, required=True)
    capture.add_argument("--root", type=Path, required=True)
    capture.add_argument("--snapshot", type=Path, required=True)
    capture.add_argument("--stopped", action="store_true")
    verify = commands.add_parser("verify", help="verify a captured state snapshot")
    verify.add_argument("--snapshot", type=Path, required=True)
    restore = commands.add_parser("restore", help="restore to a new absent directory")
    restore.add_argument("--snapshot", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--stopped", action="store_true")
    plan = commands.add_parser(
        "plan", help="derive the inventory from provider declarations"
    )
    plan.add_argument("--config", type=Path, default=None)
    plan.add_argument("--root", type=Path, required=True)
    plan.add_argument("--inventory", type=Path, required=True)
    check = commands.add_parser(
        "check", help="retain provider ownership primitives without capturing"
    )
    check.add_argument("--config", type=Path, default=None)
    check.add_argument("--root", type=Path, required=True)
    check.add_argument("--lenient", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            entries = StoppedInventory.load(args.inventory).entries
            create_stopped_snapshot(
                args.root, args.snapshot, entries, runtime_stopped=args.stopped
            )
        elif args.command == "restore":
            restore_stopped_snapshot(
                args.snapshot, args.destination, runtime_stopped=args.stopped
            )
        elif args.command == "verify":
            verify_stopped_snapshot(args.snapshot)
        elif args.command == "plan":
            runtime_config, paths = _load_configuration(args.config)
            built = build_stopped_capture_plan(
                runtime_config, paths, common_root=args.root
            )
            built.write_inventory(args.inventory)
            summary = built.summary()
            # Instance labels can echo native profile directory names; keep
            # the shared-log output to provider classes and counts only.
            for row in summary["providers"]:
                row.pop("adapter_id", None)
            print(json.dumps({"operation": "plan", "summary": summary}))
            return 0
        else:
            runtime_config, paths = _load_configuration(args.config)
            build_stopped_capture_plan(runtime_config, paths, common_root=args.root)
            with retain_local_exclusion(
                runtime_config, paths, strict=not args.lenient
            ) as held:
                report = [
                    {
                        "provider": exclusion.provider,
                        "exclusion": exclusion.scope,
                        "method": exclusion.method,
                    }
                    for exclusion in held
                ]
            print(json.dumps({"operation": "check", "providers": report}))
            return 0
        # Do not print paths or secret filenames to shared operational logs.
        print(json.dumps({"operation": args.command, "status": "verified"}))
        return 0
    except (
        ValueError,
        OSError,
        sqlite3.Error,
        ExclusionRefused,
        ConfigError,
        RuntimeConfigError,
    ) as exc:
        # No exception text: underlying OS errors may contain private profile paths.
        print(
            json.dumps(
                {
                    "operation": args.command,
                    "status": "refused",
                    "error_type": type(exc).__name__,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
