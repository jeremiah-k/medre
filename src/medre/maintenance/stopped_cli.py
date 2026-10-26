"""Explicit stopped-state maintenance CLI, separate from normal runtime commands.

Usage: python -m medre.maintenance.stopped_cli capture --inventory FILE
       --root COMMON_STATE_ROOT --snapshot NEW_DIR --stopped
       python -m medre.maintenance.stopped_cli verify --snapshot DIR
       python -m medre.maintenance.stopped_cli restore --snapshot DIR
       --destination NEW_DIR --stopped
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import NoReturn, Sequence

from medre.maintenance.stopped_inventory import StoppedInventory
from medre.maintenance.stopped_snapshot import (
    create_stopped_snapshot,
    restore_stopped_snapshot,
    verify_stopped_snapshot,
)


class _PrivateArgumentParser(argparse.ArgumentParser):
    """Refuse malformed arguments without echoing native profile paths."""

    def error(self, message: str) -> NoReturn:
        # argparse normally prints the raw unknown arguments and their values.
        # Subcommands inherit this parser class, so sanitize both entry points.
        self.exit(2, "medre stopped-state: invalid arguments (use --help)\n")


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
        else:
            verify_stopped_snapshot(args.snapshot)
        # Do not print paths or secret filenames to shared operational logs.
        print(json.dumps({"operation": args.command, "status": "verified"}))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
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
