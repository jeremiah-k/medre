"""Opt-in SDK constraint qualification against installed provider metadata.

The adapter distribution and selected extras are explicit inputs. No provider
registry, repository manifest, import-name convention, or checkout is required.
Install ``packaging`` in the test environment to use this module.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from importlib import metadata

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

_LOWER_BOUND_OPERATORS = frozenset({">=", ">", "~=", "=="})
_UPPER_BOUND_OPERATORS = frozenset({"<", "<="})
_OPERATOR_BOUND = _LOWER_BOUND_OPERATORS | _UPPER_BOUND_OPERATORS


@dataclass(frozen=True)
class DeclaredSdkPin:
    """An adapter-owned SDK constraint: an exact pin or a bounded range.

    ``version`` is the pinned version, or the declared floor of a bounded
    range; ``specifier`` carries the full constraint text for ranges and
    stays empty for exact pins.
    """

    distribution: str
    version: str
    specifier: str = ""


def declared_sdk_pins(
    adapter_distribution: str, *, extras: Iterable[str] = ()
) -> dict[str, DeclaredSdkPin]:
    """Read active SDK constraints from installed distribution metadata.

    Each active dependency is either a single exact pin or a bounded range
    with at least one lower-bound and one upper-bound operator; unbounded,
    wildcard, and URL requirements remain qualification errors.
    """
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
        operators = {spec.operator for spec in specifiers}
        exact = (
            len(specifiers) == 1
            and specifiers[0].operator == "=="
            and "*" not in specifiers[0].version
        )
        bounded = (
            not requirement.url
            and bool(operators)
            and operators <= _OPERATOR_BOUND
            and bool(operators & _LOWER_BOUND_OPERATORS)
            and bool(operators & _UPPER_BOUND_OPERATORS)
            and "*" not in "".join(spec.version for spec in specifiers)
        )
        if not exact and not bounded:
            raise AssertionError(
                f"{adapter_distribution} dependency is neither an exact pin "
                f"nor a bounded range: {raw}"
            )
        if exact:
            pin = DeclaredSdkPin(requirement.name, specifiers[0].version)
        else:
            floor = max(
                (
                    spec.version
                    for spec in specifiers
                    if spec.operator in _LOWER_BOUND_OPERATORS
                ),
                key=lambda value: (Version(value), value),
            )
            pin = DeclaredSdkPin(requirement.name, floor, str(requirement.specifier))
        existing = pins.get(name)
        if existing is not None and (
            Version(existing.version) != Version(pin.version)
            or SpecifierSet(existing.specifier) != SpecifierSet(pin.specifier)
        ):
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
    """Assert requested installed SDKs satisfy the provider's own constraints."""
    pins = declared_sdk_pins(adapter_distribution, extras=extras)
    requested = {canonicalize_name(name) for name in distributions}
    missing = requested - pins.keys()
    assert (
        not missing
    ), f"SDK distributions not declared by {adapter_distribution}: {sorted(missing)}"
    for name in sorted(requested):
        pin = pins[name]
        installed = metadata.version(pin.distribution)
        if pin.specifier:
            constraint = SpecifierSet(pin.specifier)
            assert constraint.contains(
                Version(installed), prereleases=bool(constraint.prereleases)
            ), (
                f"installed {pin.distribution} {installed} is outside "
                f"{adapter_distribution} range {pin.specifier}"
            )
        else:
            assert Version(installed) == Version(pin.version), (
                f"installed {pin.distribution} {installed} does not match "
                f"{adapter_distribution} pin {pin.version}"
            )
