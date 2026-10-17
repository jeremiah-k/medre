"""Explicit developer operations against a chosen source checkout."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from medre.dev_environment import DevEnvironment, development_process_environment


def _development(args: argparse.Namespace) -> None:
    from .bridge_commands import _command_example

    try:
        environment = DevEnvironment.inspect(args)
        if args.dev_command == "sync":
            commands = environment.sync_commands(python=args.python)
            cwd = environment.root
        else:
            arguments = args.pytest_args
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
            command, cwd = environment.test_command(args.adapter, arguments)
            commands = [command]
        print(f"Checkout environment: {environment.root / '.venv'}")
        print(f"Working directory: {cwd}")
        if args.dev_command == "sync":
            print(
                "When running the printed commands manually, unset UV_PROJECT_ENVIRONMENT and UV_PROJECT first."
            )
        for command in commands:
            print(_command_example(*command), flush=True)
        if args.dry_run:
            return
        if args.dev_command == "sync":
            if (
                sys.platform == "win32"
                and Path(sys.prefix).resolve() == (environment.root / ".venv").resolve()
            ):
                print(
                    "Run the commands above after medre.exe exits; Windows can lock its running executable.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            print(
                "Stop tests and bridges using this checkout before syncing. Rerun the same sync after an interrupted installation.",
                flush=True,
            )
        elif args.evidence_dir:
            environment.record_adapter_evidence(
                args.adapter, Path(args.evidence_dir).expanduser().resolve()
            )
        for command in commands:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=development_process_environment(),
                stdout=(
                    subprocess.DEVNULL
                    if args.dev_command == "sync" and command[1] == "export"
                    else None
                ),
                check=False,
            )
            if result.returncode:
                if args.dev_command == "sync":
                    print(
                        "Synchronization may be incomplete. Correct the failure and rerun sync before testing.",
                        file=sys.stderr,
                    )
                raise SystemExit(result.returncode if result.returncode > 0 else 1)
        if args.dev_command == "sync":
            print(
                "Checkout ready. Run 'uv run --no-sync medre adapters' or 'uv run --no-sync pytest -q'."
            )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Development error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print(
            "Development command interrupted. Rerun sync before testing if installation was interrupted.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
