"""Prepare and operate an isolated Docker ecosystem qualification lab."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import LabError
from .budgets import NATIVE_STAGES


def main(argv: list[str] | None = None) -> int:
    """Operate the requested lab action with a private file creation mask.

    Return 0 on success or 1 after reporting an operation failure. Argument
    parsing errors raise SystemExit before the operation starts.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["prepare", "build", "up", "test", "status", "down"]
    )
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument(
        "--profile", type=Path, help="Existing MEDRE development profile"
    )
    parser.add_argument(
        "--mmrelay", type=Path, help="Explicit MMRelay candidate checkout"
    )
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument(
        "--hardware", type=Path, help="Private hardware JSON; absent means Docker-only"
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Record tracked worktree edits in source evidence",
    )
    parser.add_argument(
        "--stage",
        default="all",
        choices=["all", "native", "contracts", "presentation", *NATIVE_STAGES],
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    from .control import operate

    try:
        operate(args)
    except LabError as exc:
        print(f"Lab setup rejected: {exc}")
        return 1
    except (Exception, KeyboardInterrupt) as exc:
        print(
            f"Lab operation failed ({type(exc).__name__}); inspect private logs in {args.directory}"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
