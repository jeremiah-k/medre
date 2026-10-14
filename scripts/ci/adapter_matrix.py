#!/usr/bin/env python3
"""Resolve the curated CI compatibility set once; runtime discovery is unrelated.

Moving refs are resolved to full commits before any test job runs. Each job
uses the same snapshot, which is printed in the plan log for reproduction.
This is CI orchestration, not an adapter allowlist or package installer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import tomllib
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
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository
        ):
            raise ValueError(f"invalid compatibility repository: {repository!r}")
        if not isinstance(entry["local_integration"], bool):
            raise ValueError("local_integration must be a boolean")
        if entry["extras"] and not re.fullmatch(r"[A-Za-z0-9_-]+", entry["extras"]):
            raise ValueError("extras must be a single package extra")
        distribution = entry.get("distribution")
        if distribution is not None and (
            not isinstance(distribution, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", distribution)
        ):
            raise ValueError("distribution must be a valid Python distribution name")
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


def reconcile_local(
    entries: list[dict[str, Any]], *, repo_parent: Path, update: bool = False
) -> list[dict[str, Any]]:
    """Compare pinned commits with clean sibling adapter checkouts.

    The compatibility manifest remains the CI authority. Local checkouts are
    only used on explicit request; the operation never fetches or installs.
    Validate every checkout *before* returning any updated refs.
    """
    updated: list[dict[str, Any]] = []
    mismatches: list[str] = []
    for entry in entries:
        repository = entry["repository"]
        transport = entry["transport"]
        checkout = (repo_parent / repository.split("/")[1]).resolve()
        if not checkout.is_dir():
            raise ValueError(f"missing adapter checkout for {transport}: {checkout}")

        def git(*args: str, checkout: Path = checkout) -> str:
            result = subprocess.run(
                ["git", "-C", str(checkout), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.stdout.strip()

        if Path(git("rev-parse", "--show-toplevel")).resolve() != checkout:
            raise ValueError(f"{transport}: expected a separate adapter repository")
        metadata = tomllib.loads(
            (checkout / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = metadata.get("project", {})
        distribution = project.get("name")
        expected = entry.get("distribution")
        providers = project.get("entry-points", {}).get("medre.adapters", {})
        if (
            not isinstance(distribution, str)
            or not distribution.strip()
            or (
                expected is not None
                and re.sub(r"[-_.]+", "-", distribution).lower()
                != re.sub(r"[-_.]+", "-", expected).lower()
            )
            or transport not in providers
        ):
            raise ValueError(
                f"{transport}: checkout distribution or entry point mismatch"
            )
        if git("status", "--porcelain", "--untracked-files=normal"):
            raise ValueError(f"{transport}: checkout contains uncommitted changes")
        head = git("rev-parse", "--verify", "HEAD")
        if not _SHA.fullmatch(head):
            raise ValueError(f"{transport}: cannot resolve a full local commit")
        if not update and entry["ref"] != head:
            mismatches.append(f"{transport}: pinned {entry['ref']}, local {head}")
        updated.append({**entry, "ref": head} if update else dict(entry))
    if mismatches:
        raise ValueError(
            "adapter compatibility pins differ from local checkouts:\n"
            + "\n".join(mismatches)
            + "\nRun --sync-local only after all adapter commits are finalized."
        )
    return updated


def write_manifest_atomic(path: Path, entries: list[dict[str, Any]]) -> None:
    """Publish a fully validated compatibility snapshot in one file replace."""
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temp:
            temp_name = temp.name
            json.dump(entries, temp, indent=2)
            temp.write("\n")
        os.replace(temp_name, path)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def requirements(entries: list[dict[str, Any]]) -> str:
    """Produce ordinary commit-pinned pip requirements from a CI snapshot."""
    lines = []
    for entry in entries:
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
            entry["repository"],
        ):
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
    local = parser.add_mutually_exclusive_group()
    local.add_argument(
        "--check-local",
        action="store_true",
        help="Fail if clean sibling adapter HEADs differ from pinned refs",
    )
    local.add_argument(
        "--sync-local",
        action="store_true",
        help="Atomically update manifest pins from clean sibling adapter HEADs",
    )
    parser.add_argument(
        "--repo-parent",
        type=Path,
        default=_MANIFEST.parents[1].parent,
        help="Directory containing MEDRE and sibling adapter checkouts",
    )
    args = parser.parse_args()
    if (args.check_local or args.sync_local) and (
        args.resolve or args.github_output or args.requirements
    ):
        parser.error(
            "local verification/synchronization cannot be combined "
            "with CI output or resolution"
        )
    entries = resolve_adapters(args.manifest, resolve=args.resolve)
    if args.check_local or args.sync_local:
        entries = reconcile_local(
            entries, repo_parent=args.repo_parent, update=args.sync_local
        )
        if args.sync_local:
            write_manifest_atomic(args.manifest, entries)
        message = (
            "Adapter compatibility snapshot matches local commits."
            if args.check_local
            else "Adapter compatibility snapshot updated from clean local commits."
        )
        print(message)
        return
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
