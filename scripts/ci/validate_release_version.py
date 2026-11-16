#!/usr/bin/env python3
"""Verify that a GitHub release tag matches the package version exactly."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

# Canonical PEP 440 public versions in the project's three-component namespace.
# In particular, rc/a/b suffixes have no separator; local versions are not
# public PyPI releases and cannot be Docker tags.
_RELEASE_TAG_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?"
    r"(?:\.post(?:0|[1-9][0-9]*))?(?:\.dev(?:0|[1-9][0-9]*))?\Z"
)


def normalize_release_tag(tag: str) -> str:
    """Return the version encoded by a validated release tag."""
    normalized = tag
    if normalized.startswith("v"):
        normalized = normalized[1:]
    if not normalized or _RELEASE_TAG_RE.fullmatch(normalized) is None:
        raise ValueError(f"Release tag {tag!r} is not a supported version tag")
    return normalized


def read_project_version(pyproject_path: Path) -> str:
    """Read the PEP 621 project version from ``pyproject.toml``."""
    with pyproject_path.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    try:
        version = pyproject["project"]["version"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"Unable to read project.version from {pyproject_path}"
        ) from exc
    if not isinstance(version, str) or not version:
        raise ValueError(f"Invalid project.version in {pyproject_path}")
    return version


def validate_release_version(tag: str, pyproject_path: Path) -> str:
    """Validate *tag* against *pyproject_path* and return the package version."""
    tag_version = normalize_release_tag(tag)
    project_version = read_project_version(pyproject_path)
    if tag_version != project_version:
        raise ValueError(
            "Release tag/version mismatch: "
            f"tag {tag!r} resolves to {tag_version!r}, "
            f"but project.version is {project_version!r}"
        )
    return project_version


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="GitHub release tag")
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="Path to pyproject.toml",
    )
    return parser.parse_args()


def main() -> int:
    """Run release-version validation."""
    args = parse_args()
    try:
        version = validate_release_version(args.tag, args.pyproject)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print(f"Release tag {args.tag!r} matches project version {version!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
