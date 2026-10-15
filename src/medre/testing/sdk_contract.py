"""Opt-in SDK pin qualification against installed provider metadata.

The adapter distribution and selected extras are explicit inputs. No provider
registry, repository manifest, import-name convention, or checkout is required.
Install ``packaging`` in the test environment to use this module.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from importlib import metadata

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


@dataclass(frozen=True)
class DeclaredSdkPin:
    """An exact dependency version owned by an adapter distribution."""

    distribution: str
    version: str


def declared_sdk_pins(
    adapter_distribution: str, *, extras: Iterable[str] = ()
) -> dict[str, DeclaredSdkPin]:
    """Read active exact SDK pins from installed distribution metadata."""
    dist = metadata.distribution(adapter_distribution)
    selected = ("", *extras)
    pins: dict[str, DeclaredSdkPin] = {}
    for raw in dist.requires or ():
        requirement = Requirement(raw)
        if requirement.marker is not None and not any(
            requirement.marker.evaluate({"extra": extra}) for extra in selected
        ):
            continue
        name = canonicalize_name(requirement.name)
        if name == "medre":
            continue
        specifiers = list(requirement.specifier)
        if (
            requirement.url
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise AssertionError(
                f"{adapter_distribution} dependency is not exact-pinned: {raw}"
            )
        pin = DeclaredSdkPin(requirement.name, specifiers[0].version)
        if name in pins and Version(pins[name].version) != Version(pin.version):
            raise AssertionError(f"conflicting SDK requirement: {name}")
        pins[name] = pin
    if not pins:
        raise AssertionError(f"{adapter_distribution} declares no active SDK pins")
    return pins


def assert_installed_sdk_pins(
    adapter_distribution: str,
    distributions: Iterable[str],
    *,
    extras: Iterable[str] = (),
) -> None:
    """Assert requested installed SDKs match the provider's own exact pins."""
    pins = declared_sdk_pins(adapter_distribution, extras=extras)
    requested = {canonicalize_name(name) for name in distributions}
    missing = requested - pins.keys()
    assert (
        not missing
    ), f"SDK distributions not declared by {adapter_distribution}: {sorted(missing)}"
    for name in sorted(requested):
        pin = pins[name]
        installed = metadata.version(pin.distribution)
        assert Version(installed) == Version(pin.version), (
            f"installed {pin.distribution} {installed} does not match "
            f"{adapter_distribution} pin {pin.version}"
        )
