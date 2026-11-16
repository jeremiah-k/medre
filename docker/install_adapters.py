"""Resolve image packages through MEDRE's existing source-catalog grammar."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from medre.tool_management import (
    SourceCatalog,
    ToolManagementError,
    load_source_catalog,
    selected_source,
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
    if not local <= set(aliases):
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
    return result


def main() -> None:
    manifest = json.loads(Path("/core/.github/adapter-compatibility.json").read_text())
    catalog = Path("/run/secrets/sources")
    if catalog.exists() and hashlib.sha256(
        catalog.read_bytes()
    ).hexdigest() != os.environ.get("MEDRE_SOURCES_DIGEST"):
        raise ToolManagementError(
            "Source catalog cache key is missing or stale; build with scripts/docker/build.py."
        )
    selected = requirements(
        manifest,
        os.environ["MEDRE_ADAPTERS"],
        os.environ["MEDRE_SOURCE"],
        set(os.environ.get("MEDRE_LOCAL_ADAPTERS", "").split()),
        catalog if catalog.exists() else None,
    )
    path = Path("/tmp/adapter-requirements.txt")
    path.write_text("\n".join(selected) + "\n")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            "/opt/venv/bin/python",
            "/core",
            "-r",
            str(path),
        ],
        check=True,
    )
    subprocess.run(
        ["uv", "pip", "check", "--python", "/opt/venv/bin/python"], check=True
    )


if __name__ == "__main__":
    main()
