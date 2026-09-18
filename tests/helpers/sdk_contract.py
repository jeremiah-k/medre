"""Helpers for installed optional-SDK contract tests.

The project metadata is the single source of truth for exact dependency pins.
SDK contract tests verify that CI installed those declared pins without copying
version literals into a second place that Renovate must update by hand.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_EXACT_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==(?P<version>[^;\s]+)\s*$"
)


@dataclass(frozen=True)
class DeclaredSdkPin:
    """One exact optional-dependency pin declared by MEDRE."""

    distribution: str
    version: str


def _canonical_distribution_name(name: str) -> str:
    """Normalize a distribution name using the PEP 503 comparison form."""
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_extra_pins(extra: str) -> dict[str, DeclaredSdkPin]:
    """Return exact pins for one project optional-dependency group.

    Contract-tier extras are intentionally exact-pinned.  Reject a non-exact
    requirement here so a future dependency-policy change cannot silently turn
    an installed-SDK contract into a floating-version check.
    """
    project = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]
    optional = project.get("optional-dependencies", {})
    requirements = optional.get(extra)
    if requirements is None:
        raise AssertionError(f"unknown optional dependency group: {extra}")

    pins: dict[str, DeclaredSdkPin] = {}
    for requirement in requirements:
        match = _EXACT_REQUIREMENT.fullmatch(requirement)
        if match is None:
            raise AssertionError(
                f"{extra} SDK contract dependency is not an exact pin: {requirement!r}"
            )
        distribution = match.group("name")
        key = _canonical_distribution_name(distribution)
        if key in pins:
            raise AssertionError(
                f"duplicate {extra} SDK contract distribution: {distribution}"
            )
        pins[key] = DeclaredSdkPin(
            distribution=distribution,
            version=match.group("version"),
        )
    return pins


def assert_installed_extra_matches_declared_pins(
    extra: str,
    distributions: Iterable[str],
) -> None:
    """Assert installed SDK distributions match MEDRE's current exact pins."""
    pins = declared_extra_pins(extra)
    requested = {_canonical_distribution_name(name) for name in distributions}
    missing = requested - pins.keys()
    if missing:
        raise AssertionError(
            f"{extra} SDK contract distributions are not declared in pyproject.toml: "
            f"{sorted(missing)}"
        )

    for key in sorted(requested):
        pin = pins[key]
        installed = metadata.version(pin.distribution)
        assert installed == pin.version, (
            f"installed {pin.distribution} {installed} does not match "
            f"pyproject.toml {extra} pin {pin.version}"
        )
