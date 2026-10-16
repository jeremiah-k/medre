#!/usr/bin/env python3
"""Install a checked-out compatibility adapter with manifest-declared SDK extras.

Used by Docker integration and standalone adapter CI. Fake-only jobs continue
using --no-deps. This is ordinary pip installation, with no runtime installer.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from adapter_matrix import resolve_adapters
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def checkout_requirement(checkout: Path, manifest: Path, *, test: bool) -> str:
    """Validate provider identity and select its single authoritative SDK extra."""
    project = tomllib.loads((checkout / "pyproject.toml").read_text())["project"]

    def normalize(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    providers = project.get("entry-points", {}).get("medre.adapters", {})
    entries = [
        entry
        for entry in resolve_adapters(manifest, resolve=False)
        if entry["transport"] in providers
        and normalize(entry["distribution"]) == normalize(project["name"])
    ]
    if len(entries) != 1:
        raise ValueError("checkout must match exactly one compatibility manifest entry")
    extra = entries[0]["extras"]
    extras = ([extra] if extra else []) + (["test"] if test else [])
    if not set(extras) <= project.get("optional-dependencies", {}).keys():
        raise ValueError("compatibility extras are not declared by the adapter")
    return str(checkout.resolve()) + ("[" + ",".join(extras) + "]" if extras else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / ".github/adapter-compatibility.json",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    requirement = checkout_requirement(args.adapter, args.manifest, test=args.test)
    project = tomllib.loads(
        (args.adapter / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    selected = requirement[len(str(args.adapter.resolve())) :].strip("[]")
    dependencies = list(project.get("dependencies", []))
    for extra in selected.split(",") if selected else ():
        dependencies.extend(project["optional-dependencies"][extra])
    # Qualify the selected core checkout even when prerelease versions move.
    # Resolving the adapter itself with dependencies can replace it with a
    # published MEDRE wheel. Install declared SDK/test requirements explicitly,
    # then install the adapter without resolving its core dependency again.
    dependencies = [
        raw
        for raw in dependencies
        if canonicalize_name(Requirement(raw).name) != "medre"
    ]
    if dependencies:
        subprocess.run(
            ["uv", "pip", "install", "--python", args.python, *dependencies], check=True
        )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            args.python,
            "--no-deps",
            "-e",
            requirement,
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
