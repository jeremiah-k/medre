"""Registry for separately distributed MEDRE adapter types.

The registry contains *declarative registration metadata only*. It deliberately
stores implementation references as strings so importing configuration code
never imports adapter implementations or optional transport SDKs.

Adapter packages register zero-argument factories through Python entry points
in the ``medre.adapters`` group. Discovery runs once at import time; installing
new packages requires a process restart. The registry never downloads packages.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

__all__ = [
    "AdapterSpec",
    "AdapterTypeRegistry",
    "BUILTIN_ADAPTER_REGISTRY",
    "SymbolRef",
    "adapter_sdk_packages",
    "get_adapter_spec",
    "iter_adapter_specs",
    "native_detection_specs",
    "registered_transports",
]

_TRANSPORT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class SymbolRef:
    """Lazy reference to a Python symbol.

    Keeping references lazy is important: config parsing and CLI discovery
    must not import optional SDK-backed adapter modules merely to learn which
    transports MEDRE supports.
    """

    module: str
    name: str

    def load(self) -> Any:
        """Import and return the referenced symbol."""
        module = importlib.import_module(self.module)
        return getattr(module, self.name)


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """Declarative assembly metadata for one installed adapter type.

    ``distribution`` and ``import_names`` describe an optional Python SDK when
    one exists. They may be empty for sidecar-backed adapters that use only a
    MEDRE-owned client.
    """

    transport: str
    config: SymbolRef
    runtime_config: SymbolRef | None
    adapter: SymbolRef
    fake_adapter: SymbolRef
    renderer_factory: SymbolRef | None
    dependency_probe: SymbolRef | None
    distribution: str | None
    import_names: tuple[str, ...]
    sdk_import_roots: tuple[str, ...]
    native_namespace_reader: SymbolRef
    versioned_namespace_reader: SymbolRef
    attribution_projector: SymbolRef
    runtime_config_preparer: SymbolRef | None = None
    runtime_directories: SymbolRef | None = None
    cli_register: SymbolRef | None = None
    cli_dispatch: SymbolRef | None = None
    support_endpoint_fields: tuple[str, ...] = ()
    support_secret_fields: tuple[str, ...] = ()
    optional_extra: str | None = None
    traits: frozenset[str] = frozenset()
    native_detection_priority: int = 100

    @property
    def install_extra(self) -> str:
        """Return the optional-dependency extra used in operator guidance."""
        return self.optional_extra or self.transport

    @property
    def has_python_sdk(self) -> bool:
        """Whether this adapter declares a separately importable Python SDK."""
        return bool(self.distribution or self.import_names or self.sdk_import_roots)


@dataclass(frozen=True, slots=True, init=False)
class AdapterTypeRegistry:
    """Immutable ordered registry of discovered adapter type specifications."""

    _specs: tuple[AdapterSpec, ...]
    _by_transport: Mapping[str, AdapterSpec]

    def __init__(self, specs: Iterable[AdapterSpec]) -> None:
        """Build and validate an immutable registry from *specs*.

        Raises :class:`ValueError` for invalid or duplicate transport names,
        invalid native-detection priorities, incomplete CLI hook pairs, or
        conflicting support-field declarations.
        """
        ordered = tuple(specs)
        by_transport: dict[str, AdapterSpec] = {}
        for spec in ordered:
            transport = spec.transport
            if not _TRANSPORT_NAME_RE.fullmatch(transport):
                raise ValueError(f"invalid adapter transport name: {transport!r}")
            if transport in by_transport:
                raise ValueError(f"duplicate adapter transport: {transport!r}")
            if spec.native_detection_priority < 0:
                raise ValueError(
                    f"native_detection_priority must be >= 0 for {transport!r}"
                )
            if (spec.cli_register is None) != (spec.cli_dispatch is None):
                raise ValueError(
                    f"CLI contribution for {transport!r} must declare both "
                    "cli_register and cli_dispatch"
                )
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
            by_transport[transport] = spec
        object.__setattr__(self, "_specs", ordered)
        object.__setattr__(self, "_by_transport", MappingProxyType(by_transport))

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

    def with_trait(self, trait: str) -> tuple[AdapterSpec, ...]:
        """Return specs declaring *trait* in registration order."""
        return tuple(spec for spec in self._specs if trait in spec.traits)

    def native_detection_order(self) -> tuple[AdapterSpec, ...]:
        """Return specs in stable native-metadata ambiguity precedence order."""
        indexed = enumerate(self._specs)
        return tuple(
            spec
            for _index, spec in sorted(
                indexed,
                key=lambda item: (item[1].native_detection_priority, item[0]),
            )
        )


def _discover_installed_specs() -> tuple[AdapterSpec, ...]:
    """Discover installed, explicitly packaged transport adapters.

    Entry points must be published under ``medre.adapters`` and resolve to
    zero-argument factories returning an AdapterSpec. A broken provider is a
    configuration error, not a silently skipped transport.
    """
    from importlib.metadata import entry_points

    discovered = []
    preferred = {"matrix": 0, "meshtastic": 1, "meshcore": 2, "lxmf": 3}
    providers = sorted(
        entry_points(group="medre.adapters"),
        key=lambda ep: (preferred.get(ep.name, 100), ep.name, ep.value),
    )
    for ep in providers:
        try:
            spec = ep.load()()
        except Exception as exc:
            raise RuntimeError(
                f"cannot load MEDRE adapter {ep.name!r} from {ep.value!r}"
            ) from exc
        if not isinstance(spec, AdapterSpec):
            raise TypeError(f"MEDRE adapter {ep.name!r} returned {type(spec)!r}")
        if spec.transport != ep.name:
            raise ValueError(
                f"MEDRE adapter entry point {ep.name!r} registered "
                f"transport {spec.transport!r}"
            )
        discovered.append(spec)
    return tuple(discovered)


# Historical name kept for internal call sites during the extraction tranche.
# This registry is now populated by installed distributions, not built-ins.
BUILTIN_ADAPTER_REGISTRY = AdapterTypeRegistry(_discover_installed_specs())


def iter_adapter_specs() -> tuple[AdapterSpec, ...]:
    """Return installed adapter specs in deterministic registration order."""
    return tuple(BUILTIN_ADAPTER_REGISTRY)


def native_detection_specs() -> tuple[AdapterSpec, ...]:
    """Return installed adapters in native-metadata precedence order."""
    return BUILTIN_ADAPTER_REGISTRY.native_detection_order()


def get_adapter_spec(transport: str) -> AdapterSpec | None:
    """Return the installed spec for *transport*, or ``None``."""
    return BUILTIN_ADAPTER_REGISTRY.get(transport)


def registered_transports() -> tuple[str, ...]:
    """Return the installed transport vocabulary."""
    return BUILTIN_ADAPTER_REGISTRY.transports()


def adapter_sdk_packages() -> tuple[str, ...]:
    """Return deduplicated SDK import roots declared by installed adapters."""
    seen: set[str] = set()
    ordered: list[str] = []
    for spec in BUILTIN_ADAPTER_REGISTRY:
        for package in spec.sdk_import_roots:
            if package not in seen:
                seen.add(package)
                ordered.append(package)
    return tuple(ordered)
