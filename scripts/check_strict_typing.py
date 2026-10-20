"""Run reproducible strict-mypy checks across MEDRE distributions.

Usage: python scripts/check_strict_typing.py [--workspace PATH] [--all]

The default is a passing gate over every MEDRE source tree: core and the
seven adapters. --all is the same whole-tree audit spelled explicitly.
Every repository declares its typing semantics in its own pyproject.toml
under [tool.mypy] — strictness, the 3.11 target, and per-module
exemptions for exactly that repository's optional transport SDKs — and
the checker runs mypy from each repository root so that configuration
applies. Any other unresolved import — including a misspelled
MEDRE-owned module — fails the gate instead of degrading to Any.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_READY: dict[str, tuple[str, ...]] = {
    "medre": ("src/medre",),
    "medre-adapter-lxmf": ("src/medre",),
    "medre-adapter-meshcore": ("src/medre",),
    "medre-adapter-meshtastic": ("src/medre",),
    "medre-adapter-matrix": ("src/medre",),
    "medre-adapter-xmpp": ("src/medre",),
    "medre-adapter-chatmail": ("src/medre",),
    "medre-adapter-simplexchat": ("src/medre",),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--all", action="store_true", help="audit whole source trees")
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    core = workspace / "medre"
    targets = {name: ("src/medre",) for name in _READY} if args.all else _READY
    result = 0
    for name, paths in targets.items():
        repo = workspace / name
        if not repo.is_dir():
            print(f"{name}: repository missing at {repo}", file=sys.stderr)
            result = 1
            continue
        src_paths = [str(repo / "src")]
        if repo != core:
            src_paths.append(str(core / "src"))
        env = os.environ.copy()
        # Workspace roots come first so caller-provided stub paths cannot
        # shadow the trees under check; caller entries remain searchable.
        caller_paths = env.get("MYPYPATH", "")
        if caller_paths:
            src_paths.append(caller_paths)
        env["MYPYPATH"] = os.pathsep.join(src_paths)
        print(f"\n== {name} ==", flush=True)
        # The repository's own [tool.mypy] supplies the semantics; mypy
        # discovers it from the working directory.
        check = subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--no-incremental",
                *paths,
            ],
            cwd=repo,
            env=env,
            check=False,
        )
        result |= int(check.returncode != 0)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
