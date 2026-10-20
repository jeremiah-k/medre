"""Run reproducible incremental strict-mypy checks across MEDRE distributions.

Usage: python scripts/check_strict_typing.py [--workspace PATH] [--all]

The default is a passing gate for the contracts and transport packages already
migrated. --all audits every package and fails until the remaining migrations
are complete. Missing optional SDK stubs are exempt, not local type errors.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_READY: dict[str, tuple[str, ...]] = {
    "medre": (
        "src/medre/core/contracts",
        "src/medre/adapter_spec.py",
        "src/medre/adapter_registry.py",
    ),
    "medre-adapter-lxmf": ("src/medre",),
    "medre-adapter-meshcore": ("src/medre",),
    "medre-adapter-meshtastic": ("src/medre",),
}
_REMAINING = (
    "medre-adapter-matrix",
    "medre-adapter-xmpp",
    "medre-adapter-chatmail",
    "medre-adapter-simplexchat",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--all", action="store_true", help="audit all source files and adapters"
    )
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    core = workspace / "medre"
    targets = (
        {name: ("src/medre",) for name in (*_READY, *_REMAINING)}
        if args.all
        else _READY
    )
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
        env["MYPYPATH"] = os.pathsep.join(src_paths)
        print(f"\n== {name} ==", flush=True)
        check = subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--strict",
                "--python-version=3.11",
                "--explicit-package-bases",
                "--follow-imports=silent",
                "--ignore-missing-imports",
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
