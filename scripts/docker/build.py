"""Build one immutable MEDRE installation; Docker owns dependency resolution."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def command(args: argparse.Namespace) -> list[str]:
    aliases = {
        entry["transport"]
        for entry in json.loads(
            (ROOT / ".github/adapter-compatibility.json").read_text()
        )
    }
    contexts = {}
    for override in args.local:
        name, separator, value = override.partition("=")
        path = Path(value).expanduser().resolve()
        if (
            not separator
            or name not in aliases
            or name in contexts
            or not (path / "pyproject.toml").is_file()
        ):
            raise ValueError(
                "Use --local ALIAS=/path/to/adapter-checkout, once per supported adapter."
            )
        contexts[name] = path
    if args.sources and not args.sources.is_file():
        raise ValueError("The source catalog must exist.")
    result = [
        "docker",
        "buildx",
        "build",
        "--load",
        "--tag",
        args.tag,
        "--build-arg",
        f"MEDRE_ADAPTERS={args.adapters}",
        "--build-arg",
        f"MEDRE_SOURCE={args.source}",
        "--build-arg",
        f"MEDRE_LOCAL_ADAPTERS={' '.join(sorted(contexts))}",
    ]
    for name, path in sorted(contexts.items()):
        result += ["--build-context", f"{name}={path}"]
    if args.sources:
        result += [
            "--secret",
            f"id=sources,src={args.sources.resolve()}",
            "--build-arg",
            "MEDRE_SOURCES_DIGEST="
            + hashlib.sha256(args.sources.read_bytes()).hexdigest(),
        ]
    if args.no_cache:
        result.append("--no-cache")
    if args.platform:
        result += ["--platform", args.platform]
    revision = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if dirty:
        revision += "-dirty"
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    result += [
        "--build-arg",
        f"MEDRE_REVISION={revision}",
        "--build-arg",
        f"MEDRE_VERSION={version}",
        str(ROOT),
    ]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="medre:local")
    parser.add_argument(
        "--adapters", default="all", help="Space-separated aliases, all, or none"
    )
    parser.add_argument("--source", choices=("git", "index"), default="git")
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--local", action="append", default=[], metavar="ALIAS=PATH")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--platform")
    args = parser.parse_args()
    try:
        argv = command(args)
    except ValueError as error:
        parser.error(str(error))
    subprocess.run(argv, check=True)


if __name__ == "__main__":
    main()
