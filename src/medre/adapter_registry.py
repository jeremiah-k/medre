"""Registry for separately distributed MEDRE adapter types.

The registry contains *declarative registration metadata only*. It deliberately
stores implementation references as strings so configuration/discovery imports
only SDK-free declarations, config models, and pure destination validators.
It does not import native runtime implementations or optional transport SDKs.

Adapter packages register zero-argument factories through Python entry points
in the ``medre.adapters`` group. Discovery runs once at import time; installing
new packages requires a process restart. The registry never downloads packages.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from medre.adapter_spec import AdapterSpec, DestinationSpec

__all__ = [
    "AdapterDiscoveryFailure",
    "adapter_discovery_failures",
    "AdapterTypeRegistry",
    "get_destination_spec",
    "ADAPTER_REGISTRY",
    "adapter_sdk_packages",
    "get_adapter_spec",
    "iter_adapter_specs",
    "native_detection_specs",
    "registered_transports",
    "valid_transport_name",
]

_TRANSPORT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def valid_transport_name(value: object) -> bool:
    """Whether a value follows the platform-neutral transport token grammar."""
    return isinstance(value, str) and _TRANSPORT_NAME_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class AdapterDiscoveryFailure:
    """A quarantined provider that diagnostics can report without importing it."""

    transport: str
    entry_point: str
    reason: str
    provider_distribution: str | None = None


@dataclass(frozen=True, slots=True, init=False)
class AdapterTypeRegistry:
    """Immutable ordered registry of discovered adapter type specifications."""

    _specs: tuple[AdapterSpec, ...]
    _by_transport: Mapping[str, AdapterSpec]
    _destinations: Mapping[str, DestinationSpec]
    _native_order: tuple[AdapterSpec, ...]
    failures: tuple[AdapterDiscoveryFailure, ...]

    def __init__(
        self,
        specs: Iterable[AdapterSpec],
        *,
        failures: Iterable[AdapterDiscoveryFailure] = (),
    ) -> None:
        """Build and validate an immutable registry from *specs*.

        Raises :class:`ValueError` for invalid or duplicate transport names,
        invalid native-detection priorities, incomplete CLI hook pairs, or
        conflicting support-field declarations.
        """
        object.__setattr__(self, "failures", tuple(failures))
        ordered = tuple(specs)
        by_transport: dict[str, AdapterSpec] = {}
        destinations: dict[str, DestinationSpec] = {}
        for spec in ordered:
            transport = spec.transport
            if not valid_transport_name(transport):
                raise ValueError(f"invalid adapter transport name: {transport!r}")
            if transport in by_transport:
                raise ValueError(f"duplicate adapter transport: {transport!r}")
            if (
                isinstance(spec.native_detection_priority, bool)
                or not isinstance(spec.native_detection_priority, int)
                or spec.native_detection_priority < 0
            ):
                raise ValueError(
                    f"native_detection_priority must be an integer >= 0 for {transport!r}"
                )
            if (spec.cli_register is None) != (spec.cli_dispatch is None):
                raise ValueError(
                    f"CLI contribution for {transport!r} must declare both "
                    "cli_register and cli_dispatch"
                )
            from medre.core.observability.log_levels import VALID_LEVEL_NAMES

            logger_names = set()
            for logger_name, level in spec.logger_levels:
                if (
                    not isinstance(logger_name, str)
                    or not logger_name
                    or logger_name in {"root", "medre"}
                    or logger_name.startswith("medre.")
                    or logger_name in logger_names
                    or level not in VALID_LEVEL_NAMES
                ):
                    raise ValueError(
                        f"invalid dependency logger declaration for {transport!r}"
                    )
                logger_names.add(logger_name)
            endpoint_fields = spec.support_endpoint_fields
            secret_fields = spec.support_secret_fields
            if len(endpoint_fields) != len(set(endpoint_fields)):
                raise ValueError(f"duplicate support endpoint field for {transport!r}")
            if len(secret_fields) != len(set(secret_fields)):
                raise ValueError(f"duplicate support secret field for {transport!r}")
            overlap = set(endpoint_fields) & set(secret_fields)
            if overlap:
                raise ValueError(
                    f"support fields for {transport!r} cannot be both endpoint "
                    f"and secret: {sorted(overlap)}"
                )
            for destination in spec.destinations:
                if not _TRANSPORT_NAME_RE.fullmatch(destination.kind):
                    raise ValueError(f"invalid destination kind: {destination.kind!r}")
                if destination.kind == "channel" or destination.kind in destinations:
                    raise ValueError(
                        f"duplicate or reserved destination kind: {destination.kind!r}"
                    )
                destinations[destination.kind] = destination
            by_transport[transport] = spec
        object.__setattr__(self, "_specs", ordered)
        object.__setattr__(self, "_by_transport", MappingProxyType(by_transport))
        object.__setattr__(self, "_destinations", MappingProxyType(destinations))
        object.__setattr__(
            self,
            "_native_order",
            tuple(sorted(ordered, key=lambda spec: spec.native_detection_priority)),
        )

    def __iter__(self) -> Iterator[AdapterSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def get(self, transport: str) -> AdapterSpec | None:
        """Return the spec for *transport*, or ``None`` when unregistered."""
        return self._by_transport.get(transport)

    def require(self, transport: str) -> AdapterSpec:
        """Return the spec for *transport*.

        Raises :class:`KeyError` naming the known transports when *transport*
        is not registered.
        """
        try:
            return self._by_transport[transport]
        except KeyError as exc:
            raise KeyError(
                f"unknown adapter transport {transport!r}; "
                f"known transports: {', '.join(self.transports())}"
            ) from exc

    def transports(self) -> tuple[str, ...]:
        """Return transport names in registration order."""
        return tuple(spec.transport for spec in self._specs)

    def destination(self, kind: str) -> DestinationSpec | None:
        """Return an installed addressing validator without importing it."""
        return self._destinations.get(kind)

    def native_detection_order(self) -> tuple[AdapterSpec, ...]:
        """Return specs in stable native-metadata ambiguity precedence order."""
        return self._native_order


def _discover_installed_adapters() -> AdapterTypeRegistry:
    """Quarantine broken and conflicting providers while retaining diagnostics.

    No winner is selected for duplicate transport or destination ownership.
    Healthy, unrelated providers remain usable, including by the preflight CLI.
    """
    from collections import Counter
    from importlib.metadata import entry_points

    from medre.core.observability.sanitization import sanitize_error

    candidates = []
    failures = []
    providers = sorted(
        entry_points(group="medre.adapters"), key=lambda ep: (ep.name, ep.value)
    )
    for ep in providers:
        try:
            spec = ep.load()()
            if not isinstance(spec, AdapterSpec):
                raise TypeError(f"expected AdapterSpec, received {type(spec).__name__}")
            if spec.transport != ep.name:
                raise ValueError(
                    f"entry point {ep.name!r} registered transport {spec.transport!r}"
                )
            AdapterTypeRegistry((spec,))
        except Exception as exc:
            failures.append(
                AdapterDiscoveryFailure(
                    ep.name,
                    ep.value,
                    sanitize_error(f"{type(exc).__name__}: {exc}"),
                    getattr(getattr(ep, "dist", None), "name", None),
                )
            )
        else:
            candidates.append((ep, spec))
    transports = Counter(ep.name for ep in providers)
    kinds = Counter(
        destination.kind for _, spec in candidates for destination in spec.destinations
    )
    accepted = []
    for ep, spec in candidates:
        conflicts = sorted(
            destination.kind
            for destination in spec.destinations
            if kinds[destination.kind] > 1
        )
        if transports[spec.transport] > 1 or conflicts:
            failures.append(
                AdapterDiscoveryFailure(
                    ep.name,
                    ep.value,
                    f"Conflicting provider ownership: transport={transports[spec.transport] > 1}, destination kinds={conflicts}",
                    getattr(getattr(ep, "dist", None), "name", None),
                )
            )
        else:
            accepted.append(spec)
    return AdapterTypeRegistry(accepted, failures=failures)


def adapter_discovery_failures() -> tuple[AdapterDiscoveryFailure, ...]:
    """Return installed providers that failed registration."""
    return ADAPTER_REGISTRY.failures


def iter_adapter_specs() -> tuple[AdapterSpec, ...]:
    """Return installed adapter specs in deterministic registration order."""
    return tuple(ADAPTER_REGISTRY)


def native_detection_specs() -> tuple[AdapterSpec, ...]:
    """Return installed adapters in native-metadata precedence order."""
    return ADAPTER_REGISTRY.native_detection_order()


def get_adapter_spec(transport: str) -> AdapterSpec | None:
    """Return the installed spec for *transport*, or ``None``."""
    return ADAPTER_REGISTRY.get(transport)


def registered_transports() -> tuple[str, ...]:
    """Return the installed transport vocabulary."""
    return ADAPTER_REGISTRY.transports()


def adapter_sdk_packages() -> tuple[str, ...]:
    """Return deduplicated SDK import roots declared by installed adapters."""
    seen: set[str] = set()
    ordered: list[str] = []
    for spec in ADAPTER_REGISTRY:
        for package in spec.sdk_import_roots:
            if package not in seen:
                seen.add(package)
                ordered.append(package)
    return tuple(ordered)


def get_destination_spec(kind: str) -> DestinationSpec | None:
    """Return an installed addressing-kind declaration, or None."""
    return ADAPTER_REGISTRY.destination(kind)


# Define the public lookup functions before loading factories: a declarative
# provider may import SDK-free public helpers while discovery is in progress.
# Factories must not query the incomplete registry or import native SDKs.
ADAPTER_REGISTRY = _discover_installed_adapters()
