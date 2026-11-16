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


def register_source_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register source inspection and explicit uv-owned source switching."""
    sources_p = subparsers.add_parser(
        "sources",
        help="Inspect installed package sources or switch an owned tool installation",
    )
    source_actions = sources_p.add_subparsers(dest="source_action", required=True)
    show_sources = source_actions.add_parser(
        "show", help="Show configured, requested and installed sources"
    )
    show_sources.add_argument(
        "--sources", metavar="PATH", help="Source catalog override"
    )
    show_sources.add_argument(
        "--json", action="store_true", help="Print machine-readable source inventory"
    )
    switch_sources = source_actions.add_parser(
        "switch", help="Switch installed core or adapter sources through uv"
    )
    switch_sources.add_argument(
        "packages",
        nargs="*",
        help="core, adapter aliases or installed distribution names, optionally with extras",
    )
    switch_sources.add_argument(
        "--all",
        dest="all_packages",
        action="store_true",
        help="Select core and installed first-party adapters; retain community packages",
    )
    source_choice = switch_sources.add_mutually_exclusive_group(required=True)
    source_choice.add_argument(
        "--from",
        dest="source_kind",
        choices=("index", "pypi", "git", "local"),
        help="Index (PyPI by default), Git or local checkout; pypi is an alias for index",
    )
    source_choice.add_argument(
        "--requirement",
        help="Explicit named Python requirement, including ranges or direct URLs",
    )
    switch_sources.add_argument("--version", help="Exact index version for one package")
    switch_sources.add_argument(
        "--git-url", help="Fork or community Git repository URL"
    )
    switch_sources.add_argument(
        "--ref",
        help="Git branch, tag or full commit; default main for built-in repositories, remote HEAD for custom URLs",
    )
    switch_sources.add_argument("--path", help="Local project directory")
    switch_sources.add_argument(
        "--allow-refresh",
        action="store_true",
        help="Explicitly allow uv to refresh unselected moving Git/local sources",
    )
    switch_sources.add_argument(
        "--editable",
        action="store_true",
        help="Use the local checkout directly instead of installing a snapshot",
    )
    switch_sources.add_argument(
        "--sources", metavar="PATH", help="Source catalog used to resolve aliases"
    )
    switch_sources.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the complete request without changing packages or catalogs",
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
        switching = args.command == "sources"
        if switching:
            from medre.package_sources import source_inventory

            catalog = load_source_catalog(Path(args.sources) if args.sources else None)
            if args.source_action == "show":
                import json

                report = source_inventory(catalog)
                if args.json:
                    print(json.dumps(report, indent=2))
                else:
                    print(
                        "Configured catalog choices and actual installed sources are separate."
                    )
                    for row in report["packages"]:
                        print(
                            f"{row['package']}: {row['installed_version'] or 'not installed'}"
                        )
                        print(
                            f"  configured: {'; '.join(row['configured']) or '(none)'}"
                        )
                        print(
                            f"  requested: {row['requested'] or '(no managed receipt)'}"
                        )
                        print(f"  installed source: {json.dumps(row['source'])}")
                    if report["receipt_error"]:
                        print(report["receipt_error"])
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
        if switching:
            from medre.package_sources import plan_source_switch

            plan = plan_source_switch(
                installation,
                catalog,
                tuple(args.packages),
                all_packages=args.all_packages,
                allow_refresh=args.allow_refresh,
                source=args.source_kind,
                version=args.version,
                git_url=args.git_url,
                ref=args.ref,
                path=args.path,
                editable=args.editable,
                requirement=args.requirement,
            )
            print(f"Tool environment: {installation.prefix}")
            for note in plan.notes:
                print(note)
            command = list(plan.command)
        elif resetting:
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

        if not (upgrading or resetting or switching):
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
