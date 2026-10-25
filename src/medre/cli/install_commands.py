"""Operator-facing package management, separate from runtime configuration."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from medre.tool_management import (
    ToolInstallation,
    ToolManagementError,
    initialize_sources,
    load_source_catalog,
    load_sources,
    sources_path,
)


def _manage_installation(args: argparse.Namespace) -> None:
    """Render source choices or execute a verified uv operation."""
    try:
        if args.command == "init":
            target = Path(args.sources) if args.sources else sources_path()
            try:
                target, created = initialize_sources(target)
            except OSError:
                raise ToolManagementError(
                    f"Cannot create source catalog at {target}; check the parent directory path and permissions."
                ) from None
            print(f"{'Created' if created else 'Kept existing catalog:'} {target}")
            print("Edit adapter sources here, then run 'medre adapters install ALIAS'.")
            print("Run 'medre upgrade' to update core and installed adapters.")
            print("Use --sync-sources to apply deliberate catalog changes.")
            return
        upgrading = args.command in ("upgrade", "update") or (
            args.command == "adapters" and args.action in ("upgrade", "update")
        )
        resetting = args.command == "adapters" and args.action == "reset"
        if args.command == "adapters" and not (upgrading or resetting):
            if args.action == "sources":
                import yaml

                catalog = load_source_catalog(
                    Path(args.sources) if args.sources else None
                )
                document: dict[str, object] = {"adapters": catalog.adapters}
                if catalog.core is not None:
                    document["core"] = catalog.core
                print(
                    f"# Package sources; override in {args.sources or sources_path()}"
                )
                print(yaml.safe_dump(document, sort_keys=False), end="")
                return
            sources = load_sources(Path(args.sources) if args.sources else None)
        installation = ToolInstallation.inspect()
        if resetting:
            from medre.self_upgrade import plan_adapter_reset

            plan = plan_adapter_reset(
                installation,
                selections=tuple(args.packages),
                include_core=args.include_core,
            )
            print(f"Tool environment: {installation.prefix}")
            for note in plan.notes:
                print(note)
            if not plan.command:
                return
            print("Reset package sources: " + ", ".join(plan.packages))
            command = list(plan.command)
        elif upgrading:
            from medre.self_upgrade import plan_upgrade

            upgrade_catalog = (
                load_source_catalog(Path(args.sources) if args.sources else None)
                if args.packages or args.sync_sources
                else None
            )
            plan = plan_upgrade(
                installation,
                selections=tuple(args.packages),
                core_only=getattr(args, "core_only", False),
                adapters_only=args.command == "adapters"
                or getattr(args, "adapters_only", False),
                catalog=upgrade_catalog,
                sync_sources=args.sync_sources,
            )
            print(f"Tool environment: {installation.prefix}")
            for note in plan.notes:
                print(note)
            if not plan.command:
                return
            print("Update: " + ", ".join(plan.packages))
            command = list(plan.command)
        else:
            command = installation.change_adapters(
                args.packages, sources, remove=args.action == "remove"
            )
        from .bridge_commands import _command_example

        if not (upgrading or resetting):
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
            if resetting and not args.include_core:
                retry = _command_example(
                    "medre", "adapters", "reset", *args.packages, "--include-core"
                )
                print(
                    "Adapter reset retained the saved core source. If uv cannot fetch "
                    "a retired core ref, explicitly restore core to main with "
                    f"'{retry}'. Preview it first with --dry-run.",
                    file=sys.stderr,
                )
            raise SystemExit(result.returncode if result.returncode > 0 else 1)
        print(
            f"{'Upgrade' if upgrading else 'Installation'} complete. Run 'medre adapters' to inspect providers, then 'medre config check --config PATH' before restarting your bridge."
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
