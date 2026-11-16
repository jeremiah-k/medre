"""Resolve image packages through MEDRE's existing source-catalog grammar."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from source_policy import validate_source_url

from medre.tool_management import (
    SourceCatalog,
    ToolManagementError,
    load_source_catalog,
    selected_source,
    split_requirement_marker,
)


def requirements(
    manifest: list[dict[str, str]],
    selection: str,
    source: str,
    local: set[str],
    catalog: Path | None = None,
) -> list[str]:
    """Select published packages, committed compatibility refs, or local contexts."""
    if source not in {"git", "index"}:
        raise ToolManagementError("Image source must be 'index' or 'git'.")
    entries = {entry["transport"]: entry for entry in manifest}
    defaults = {}
    for alias, entry in entries.items():
        name = entry["distribution"]
        if extras := entry.get("extras"):
            name += f"[{extras}]"
        if source == "git":
            name += (
                f" @ git+https://github.com/{entry['repository']}.git@{entry['ref']}"
            )
        defaults[alias] = name
    sources = (
        load_source_catalog(catalog, defaults=defaults)
        if catalog
        else SourceCatalog(None, defaults)
    )
    if sources.core is not None:
        raise ToolManagementError(
            "The image builds core from its build context; omit 'core' from the adapter catalog."
        )
    aliases = (
        list(sources.adapters)
        if selection == "all"
        else ([] if selection == "none" else selection.split())
    )
    if len(aliases) != len(set(aliases)):
        raise ToolManagementError("Select each image adapter once.")
    if not local <= set(aliases) or not local <= entries.keys():
        raise ToolManagementError("Local overrides must name selected adapter aliases.")
    result = []
    for alias in aliases:
        if alias in local:
            entry = entries[alias]
            name = entry["distribution"]
            if extras := entry.get("extras"):
                name += f"[{extras}]"
            result.append(f"{name} @ file:///sources/{alias}")
        else:
            _, requirement = selected_source(alias, sources.adapters)
            result.append(requirement)
    for requirement in result:
        body, _ = split_requirement_marker(requirement)
        if "@" in body:
            try:
                validate_source_url(body.split("@", 1)[1].strip())
            except ValueError as error:
                raise ToolManagementError(str(error)) from None
    return result


def install_command(path: Path, selected: list[str]) -> list[str]:
    """Build directory requirements without uv's packaging-file cache heuristic."""
    command = ["uv", "pip", "install", "--python", "/opt/venv/bin/python"]
    bodies = [split_requirement_marker(requirement)[0] for requirement in selected]
    if any(
        "@" in body and body.split("@", 1)[1].strip().lower().startswith("file:")
        for body in bodies
    ):
        command.append("--no-cache")
    return command + ["/core", "-r", str(path)]


def checked_catalog(catalog: Path, digest: str) -> Path | None:
    """Do not silently fall back to defaults when a requested secret is absent."""
    if not catalog.exists() and digest == "none":
        return None
    if (
        not catalog.is_file()
        or hashlib.sha256(catalog.read_bytes()).hexdigest() != digest
    ):
        raise ToolManagementError(
            "Source catalog or its cache key is missing or stale; build with scripts/docker/build.py."
        )
    return catalog


def main() -> None:
    manifest = json.loads(Path("/core/.github/adapter-compatibility.json").read_text())
    catalog = checked_catalog(
        Path("/run/secrets/sources"), os.environ.get("MEDRE_SOURCES_DIGEST", "none")
    )
    selected = requirements(
        manifest,
        os.environ["MEDRE_ADAPTERS"],
        os.environ["MEDRE_SOURCE"],
        set(os.environ.get("MEDRE_LOCAL_ADAPTERS", "").split()),
        catalog,
    )
    path = Path("/tmp/adapter-requirements.txt")
    path.write_text("\n".join(selected) + "\n")
    subprocess.run(install_command(path, selected), check=True)
    subprocess.run(
        ["uv", "pip", "check", "--python", "/opt/venv/bin/python"], check=True
    )


if __name__ == "__main__":
    main()
