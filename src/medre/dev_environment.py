"""Checkout development orchestration; uv retains dependency resolution ownership.

The tracked compatibility snapshot supplies repeatable defaults. Local profiles
and command-line overrides select experiments without editing release metadata.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from medre.config._yaml import StrictYAMLError, parse_yaml_config
from medre.tool_management import (
    ToolManagementError,
    load_sources,
    package_name,
    selected_source,
)


def checkout_root(project: str | None = None) -> Path:
    """Locate a MEDRE checkout, independently of the executing installation."""
    start = Path(project).expanduser().resolve() if project else Path.cwd()
    candidates = [start] if project else [start, *start.parents]
    for root in candidates:
        metadata = root / "pyproject.toml"
        if not metadata.is_file():
            continue
        document = tomllib.loads(metadata.read_text(encoding="utf-8"))
        if (
            document.get("project", {}).get("name") == "medre"
            and (root / "uv.lock").is_file()
            and (root / ".github/adapter-compatibility.json").is_file()
        ):
            for parent in root.parents:
                parent_metadata = parent / "pyproject.toml"
                if parent_metadata.is_file():
                    settings = tomllib.loads(
                        parent_metadata.read_text(encoding="utf-8")
                    )
                    if "workspace" in settings.get("tool", {}).get("uv", {}):
                        raise ToolManagementError(
                            "Use a standalone MEDRE checkout outside a parent uv workspace; uv workspaces share environment and lock ownership."
                        )
            if (root / ".venv").resolve() != root / ".venv":
                raise ToolManagementError(
                    "The checkout's .venv must not be a symlink to another environment."
                )
            return root
        # Do not walk through another project's metadata into an unrelated root.
        break
    raise ToolManagementError(
        "Run from a MEDRE checkout, or pass --project PATH to that checkout."
    )


def _local_path(value: str, relative_to: Path) -> Path:
    path = Path(value).expanduser()
    return (relative_to / path).resolve() if not path.is_absolute() else path.resolve()


def _snapshot_sources(root: Path) -> dict[str, str]:
    entries = json.loads((root / ".github/adapter-compatibility.json").read_text())
    result = {}
    for entry in entries:
        alias, distribution, repo, ref = (
            entry["transport"],
            entry["distribution"],
            entry["repository"],
            entry["ref"],
        )
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]*", alias)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not re.fullmatch(r"[0-9a-f]{40}", ref)
            or alias in result
        ):
            raise ToolManagementError(
                "The compatibility snapshot requires unique aliases and full commit SHAs."
            )
        package_name(distribution)
        result[alias] = f"{distribution} @ git+https://github.com/{repo}.git@{ref}"
    if not result:
        raise ToolManagementError("The compatibility snapshot must contain adapters.")
    return result


@dataclass(frozen=True)
class DevEnvironment:
    """A fully validated local selection, ready to turn into uv commands."""

    root: Path
    sources: dict[str, str]
    adapters: list[str]
    editable: dict[str, Path]
    sdk: bool

    @property
    def python(self) -> Path:
        return (
            self.root
            / ".venv"
            / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        )

    @classmethod
    def inspect(cls, args: argparse.Namespace) -> DevEnvironment:
        root = checkout_root(args.project)
        profile_path = (
            _local_path(args.profile, Path.cwd())
            if args.profile
            else root / ".medre-dev.yaml"
        )
        profile = {}
        if args.profile or profile_path.exists():
            try:
                profile = parse_yaml_config(
                    profile_path.read_text(encoding="utf-8"), source=str(profile_path)
                )
            except StrictYAMLError as exc:
                raise ToolManagementError(str(exc)) from None

        if profile.keys() - {"adapters", "editable", "sdk", "sources"}:
            raise ToolManagementError(
                "Unknown development profile field; use adapters, editable, sdk, or sources."
            )
        adapters = profile.get("adapters")
        if "adapters" in profile and (
            not isinstance(adapters, list)
            or not all(isinstance(item, str) for item in adapters)
        ):
            raise ToolManagementError(
                "Profile adapters must be a list of source aliases."
            )
        sdk = profile.get("sdk", False)
        if not isinstance(sdk, bool):
            raise ToolManagementError("Profile sdk must be true or false.")
        if args.sdk is not None:
            sdk = args.sdk
        sources = _snapshot_sources(root)
        catalog = args.sources or profile.get("sources")
        if catalog is not None:
            if not isinstance(catalog, str) or not catalog:
                raise ToolManagementError(
                    "Profile sources must name a source catalog file."
                )
            base = Path.cwd() if args.sources else profile_path.parent
            sources = load_sources(_local_path(catalog, base), defaults=sources)
        selections = (
            [args.adapter]
            if getattr(args, "adapter", None)
            else (
                list(args.adapters)
                if getattr(args, "adapters", None)
                else (adapters if adapters is not None else list(sources))
            )
        )
        editable = profile.get("editable", {})
        if not isinstance(editable, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in editable.items()
        ):
            raise ToolManagementError(
                "Profile editable must map aliases to checkout paths."
            )
        paths = {
            key: _local_path(value, profile_path.parent)
            for key, value in editable.items()
        }
        overrides = set()
        for override in args.editable:
            alias, separator, path = override.partition("=")
            if not separator or not alias or not path or alias in overrides:
                raise ToolManagementError("Use --editable ALIAS=PATH once per alias.")
            overrides.add(alias)
            paths[alias] = _local_path(path, Path.cwd())
        # Validate all profile paths before the first environment mutation.
        for alias, path in paths.items():
            if "[" in alias or "]" in alias:
                raise ToolManagementError(
                    "Editable keys are source aliases without extras; select extras in adapters instead."
                )
            name, _ = selected_source(alias, sources)
            metadata = tomllib.loads(
                (path / "pyproject.toml").read_text(encoding="utf-8")
            )
            project = metadata.get("project", {})
            if package_name(project.get("name")) != name or not project.get(
                "entry-points", {}
            ).get("medre.adapters"):
                raise ToolManagementError(
                    f"Editable checkout for {alias} has the wrong distribution or no medre.adapters entry point: {path}"
                )
        seen = set()
        for selection in selections:
            name, _ = selected_source(selection, sources)
            if name in seen:
                raise ToolManagementError(
                    f"Duplicate adapter distribution selected: {name}"
                )
            seen.add(name)
        unused = overrides - {selection.split("[", 1)[0] for selection in selections}
        if unused:
            raise ToolManagementError(
                f"Editable overrides must be selected: {', '.join(sorted(unused))}"
            )
        return cls(root, sources, selections, paths, sdk)

    def sync_commands(self, *, python: str | None = None) -> list[list[str]]:
        uv = shutil.which("uv")
        if uv is None:
            raise ToolManagementError(
                "Install uv and put it on PATH before synchronizing a checkout."
            )
        sync = [uv, "sync", "--project", str(self.root), "--locked", "--extra", "dev"]
        if python:
            sync += ["--python", python]
        commands = [sync]
        if not self.adapters:
            return commands
        install = [uv, "pip", "install", "--python", str(self.python)]
        if self.sdk:
            constraints = self.root / ".venv/medre-core-constraints.txt"
            commands.append(
                [
                    uv,
                    "export",
                    "--project",
                    str(self.root),
                    "--locked",
                    "--extra",
                    "dev",
                    "--no-emit-project",
                    "--no-hashes",
                    "--output-file",
                    str(constraints),
                ]
            )
            install += ["--constraint", constraints.as_uri()]
        else:
            install += ["--no-deps"]
        # Keep core editable at this checkout even if an adapter requires a
        # different core version: resolution must fail instead of replacing it.
        install += ["--editable", self.root.as_uri()]
        for selection in self.adapters:
            _, requirement = selected_source(selection, self.sources)
            alias = selection.split("[", 1)[0]
            if alias in self.editable:
                match = re.match(r"[A-Za-z0-9._-]+(\[[A-Za-z0-9,._-]+\])?", requirement)
                assert match is not None
                extras = match[1] or ""
                install += ["--editable", self.editable[alias].as_uri() + extras]
            else:
                install.append(requirement)
        commands.append(install)
        return commands

    def test_command(self, alias: str, arguments: list[str]) -> tuple[list[str], Path]:
        name, _ = selected_source(alias, self.sources)
        if alias not in self.editable:
            raise ToolManagementError(
                f"Testing {alias} requires an editable checkout; set --editable {alias}=/path/to/checkout."
            )
        if not self.python.is_file():
            raise ToolManagementError(
                "The checkout environment is missing; run medre dev sync first."
            )
        return [
            str(self.python),
            "-m",
            "medre.dev_environment",
            name,
            self.editable[alias].as_uri(),
            self.root.as_uri(),
            *(arguments or ["-q"]),
        ], self.editable[alias]

    def record_adapter_evidence(self, alias: str, directory: Path) -> None:
        """Retain the selected checkout's Git state before its tests execute."""
        checkout = self.editable[alias]
        artifacts = {"adapter-checkout.txt": (str(checkout) + "\n").encode("utf-8")}
        for filename, arguments in (
            ("adapter-commit.txt", ["rev-parse", "HEAD"]),
            ("adapter-working-tree.patch", ["diff", "--binary", "HEAD"]),
            ("adapter-working-tree-status.txt", ["status", "--short"]),
        ):
            result = subprocess.run(
                ["git", "-C", str(checkout), *arguments],
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise ToolManagementError(
                    f"Cannot record adapter Git state at {checkout}; check that this is a Git checkout."
                )
            artifacts[filename] = result.stdout
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in artifacts.items():
            with (directory / filename).open("xb") as stream:
                stream.write(content)


def development_process_environment() -> dict[str, str]:
    """Keep explicit checkout targeting independent of the caller's environment."""
    environment = os.environ.copy()
    for key in (
        "UV_PROJECT_ENVIRONMENT",
        "UV_PROJECT",
        "UV_WORKING_DIRECTORY",
        "UV_WORKING_DIR",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "PYTHONPATH",
        "PYTHONHOME",
    ):
        environment.pop(key, None)
    return environment


def _test_installed_adapter(
    name: str, expected: str, core_expected: str, arguments: list[str]
) -> None:
    """Verify target-environment metadata before launching pytest."""
    import importlib.metadata
    import site
    from urllib.parse import unquote

    # The adapter cwd can contain build-generated egg-info without direct_url.
    # Restrict discovery to this interpreter's installed site-packages.
    installed = {}
    for distribution in importlib.metadata.distributions(path=site.getsitepackages()):
        # ``PackageMetadata`` is a message-style mapping at runtime but the
        # stdlib protocol types only ``__getitem__``; read through the
        # mapping view it actually implements.
        metadata = cast(Mapping[str, str], distribution.metadata)
        raw_name = metadata.get("Name")
        if not isinstance(raw_name, str):
            continue
        try:
            normalized = package_name(raw_name)
        except ToolManagementError:
            continue
        if normalized not in ("medre", name):
            continue
        try:
            direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        except (ValueError, OSError):
            direct = {}
        installed[normalized] = direct
    for distribution_name, source, label in (
        ("medre", core_expected, "Core"),
        (name, expected, "Adapter"),
    ):
        direct = installed.get(distribution_name, {})
        if (
            not isinstance(direct, dict)
            or not isinstance(direct.get("dir_info"), dict)
            or direct["dir_info"].get("editable") is not True
            or not isinstance(direct.get("url"), str)
            or unquote(direct["url"]).rstrip("/") != unquote(source).rstrip("/")
        ):
            print(
                f"{label} checkout does not match the installed editable distribution. Run medre dev sync with the same profile and overrides first.",
                file=sys.stderr,
            )
            raise SystemExit(2)
    command = [sys.executable, "-m", "pytest", *arguments]
    if sys.platform == "win32":
        raise SystemExit(subprocess.run(command, check=False).returncode)
    os.execv(sys.executable, command)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("Use medre dev test ALIAS -- PYTEST_ARGUMENTS.")
    _test_installed_adapter(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
