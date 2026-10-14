#!/usr/bin/env python3
"""Resolve the curated CI compatibility set once; runtime discovery is unrelated.

Moving refs are resolved to full commits before any test job runs. Each job
uses the same snapshot, which is printed in the plan log for reproduction.
This is CI orchestration, not an adapter allowlist or package installer.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_MANIFEST = Path(__file__).resolve().parents[2] / ".github/adapter-compatibility.json"
_SHA = re.compile(r"[0-9a-f]{40}")


def resolve_adapters(path: Path, *, resolve: bool) -> list[dict[str, Any]]:
    """Validate CI inputs and optionally snapshot repository refs."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("compatibility manifest must be a nonempty list")
    seen: set[str] = set()
    for entry in entries:
        transport = entry["transport"]
        repository = entry["repository"]
        ref = entry["ref"]
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", transport) or transport in seen:
            raise ValueError(
                f"invalid or duplicate compatibility transport: {transport!r}"
            )
        seen.add(transport)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError(f"invalid compatibility repository: {repository!r}")
        if not isinstance(entry["local_integration"], bool):
            raise ValueError("local_integration must be a boolean")
        if entry["extras"] and not re.fullmatch(r"[A-Za-z0-9_-]+", entry["extras"]):
            raise ValueError("extras must be a single package extra")
        if _SHA.fullmatch(ref):
            continue
        if not ref.startswith(("refs/heads/", "refs/tags/")) or any(
            c.isspace() for c in ref
        ):
            raise ValueError(
                "compatibility refs must be full SHAs or explicit branch/tag refs"
            )
        if resolve:
            result = subprocess.run(
                [
                    "git",
                    "ls-remote",
                    "--exit-code",
                    f"https://github.com/{repository}.git",
                    ref,
                    ref + "^{}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            refs = dict(
                line.split(maxsplit=1)[::-1] for line in result.stdout.splitlines()
            )
            commit = refs.get(ref + "^{}", refs.get(ref, ""))
            if not _SHA.fullmatch(commit):
                raise ValueError(
                    f"cannot resolve compatibility ref: {repository}@{ref}"
                )
            entry["ref"] = commit
    return entries


def requirements(entries: list[dict[str, Any]]) -> str:
    """Produce ordinary commit-pinned pip requirements from a CI snapshot."""
    lines = []
    for entry in entries:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", entry["repository"]):
            raise ValueError("invalid compatibility repository")
        if not _SHA.fullmatch(entry["ref"]):
            raise ValueError("install requirements need resolved full commit SHAs")
        # The repository is an installable Python project; egg-name assumptions
        # and distribution naming conventions are unnecessary.
        lines.append(f"git+https://github.com/{entry['repository']}.git@{entry['ref']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=_MANIFEST)
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--requirements", action="store_true")
    args = parser.parse_args()
    entries = resolve_adapters(args.manifest, resolve=args.resolve)
    if args.requirements:
        print(requirements(entries), end="")
        return
    outputs = {
        "adapters": {"include": entries},
        "by_transport": {entry["transport"]: entry for entry in entries},
        "has_local": any(entry["local_integration"] for entry in entries),
        "local_adapters": {"include": [e for e in entries if e["local_integration"]]},
    }
    print(json.dumps(outputs, indent=2))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in outputs.items():
                output.write(f"{key}={json.dumps(value, separators=(',', ':'))}\n")


if __name__ == "__main__":
    main()
