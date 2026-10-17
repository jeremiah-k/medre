"""Operator-facing package management, separate from runtime configuration."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from medre.tool_management import (
    ToolInstallation,
    ToolManagementError,
    load_sources,
    sources_path,
)


def _manage_installation(args: argparse.Namespace) -> None:
    """Render source choices or execute a verified uv operation."""
    try:
        if args.command == "adapters":
            sources = load_sources(Path(args.sources) if args.sources else None)
            if args.action == "sources":
                import yaml

                print(
                    f"# Adapter sources; override in {args.sources or sources_path()}"
                )
                print(yaml.safe_dump({"adapters": sources}, sort_keys=False), end="")
                return
        installation = ToolInstallation.inspect()
        if args.command == "update":
            command = installation.update()
        else:
            command = installation.change_adapters(
                args.packages, sources, remove=args.action == "remove"
            )
        from .bridge_commands import _command_example

        print(f"Tool environment: {installation.prefix}")
        print(_command_example(*command), flush=True)
        if args.dry_run:
            return
        if sys.platform == "win32":
            print(
                "No packages changed. Run the command above after medre.exe exits; Windows can lock its running executable.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        print(
            "Stop running bridges before changing packages; restart them after this command completes.",
            flush=True,
        )
        result = subprocess.run(command, check=False)
        if result.returncode:
            raise SystemExit(result.returncode if result.returncode > 0 else 1)
        print(
            "Installation complete. Run 'medre adapters' to inspect providers, then 'medre config check --config PATH' before restarting your bridge."
        )
    except ToolManagementError as exc:
        print(f"Installation error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except OSError:
        print(
            "Installation error: cannot execute uv; check its installation and permissions.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print(
            "Installation interrupted. Inspect 'uv tool list' before retrying.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
