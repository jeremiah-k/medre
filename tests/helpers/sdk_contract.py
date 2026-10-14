"""Helpers for installed optional-SDK contract tests.

Each installed adapter distribution is the authority for its SDK versions.
SDK contract tests verify that CI installed those declared pins without copying
version literals into a second place that Renovate must update by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Iterable

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


@dataclass(frozen=True)
class DeclaredSdkPin:
    """One exact SDK pin declared by its adapter distribution."""

    distribution: str
    version: str


def declared_extra_pins(extra: str) -> dict[str, DeclaredSdkPin]:
    """Read authoritative SDK pins from the installed adapter's metadata.

    Source provenance (PyPI, Git, or local checkout) is intentionally irrelevant.
    MEDRE core does not declare transport SDK dependencies.
    """
    entries = json.loads(
        (
            Path(__file__).resolve().parents[2] / ".github/adapter-compatibility.json"
        ).read_text()
    )
    entry = next(entry for entry in entries if entry["transport"] == extra)
    dist = metadata.distribution(entry["distribution"])
    selected_extra = entry["extras"]
    pins: dict[str, DeclaredSdkPin] = {}
    for raw in dist.requires or ():
        requirement = Requirement(raw)
        if requirement.marker is not None and not requirement.marker.evaluate(
            {"extra": selected_extra}
        ):
            continue
        canonical = canonicalize_name(requirement.name)
        if canonical == "medre":
            continue
        specifiers = list(requirement.specifier)
        if (
            requirement.url
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise AssertionError(f"{extra} SDK dependency is not exact-pinned: {raw}")
        pin = DeclaredSdkPin(requirement.name, specifiers[0].version)
        if canonical in pins and pins[canonical] != pin:
            raise AssertionError(f"conflicting SDK requirement: {canonical}")
        pins[canonical] = pin
    if not pins:
        raise AssertionError(f"{dist.metadata['Name']} declares no {extra} SDK pins")
    return pins


def assert_installed_extra_matches_declared_pins(
    extra: str,
    distributions: Iterable[str],
) -> None:
    """Assert installed SDK distributions match their adapter's exact pins."""
    pins = declared_extra_pins(extra)
    requested = {canonicalize_name(name) for name in distributions}
    missing = requested - pins.keys()
    if missing:
        raise AssertionError(
            f"{extra} SDK contract distributions are not declared by the adapter: "
            f"{sorted(missing)}"
        )

    for key in sorted(requested):
        pin = pins[key]
        installed = metadata.version(pin.distribution)
        assert installed == pin.version, (
            f"installed {pin.distribution} {installed} does not match "
            f"adapter {extra} pin {pin.version}"
        )
