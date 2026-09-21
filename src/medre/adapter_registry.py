"""Authoritative registry for MEDRE's built-in adapter types.

The registry contains *declarative registration metadata only*. It deliberately
stores implementation references as strings so importing configuration code
never imports adapter implementations or optional transport SDKs.

Adding a built-in adapter should normally require one :class:`AdapterSpec`
entry here plus adapter-owned implementation/configuration modules. Runtime,
configuration, CLI, support tooling, path, metadata-dispatch, and architecture
reporting consume this registry instead of maintaining their own transport
lists.

This is not a third-party plugin loader. The set is fixed at process import
time and ships with MEDRE.
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
        module = importlib.import_module(self.module)
        return getattr(module, self.name)


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """Declarative assembly metadata for one built-in adapter type.

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
    """Immutable ordered registry of built-in adapter type specifications."""

    _specs: tuple[AdapterSpec, ...]
    _by_transport: Mapping[str, AdapterSpec]

    def __init__(self, specs: Iterable[AdapterSpec]) -> None:
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
        return self._by_transport.get(transport)

    def require(self, transport: str) -> AdapterSpec:
        try:
            return self._by_transport[transport]
        except KeyError as exc:
            raise KeyError(
                f"unknown adapter transport {transport!r}; "
                f"known transports: {', '.join(self.transports())}"
            ) from exc

    def transports(self) -> tuple[str, ...]:
        return tuple(spec.transport for spec in self._specs)

    def with_trait(self, trait: str) -> tuple[AdapterSpec, ...]:
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


BUILTIN_ADAPTER_REGISTRY = AdapterTypeRegistry(
    (
        AdapterSpec(
            transport="matrix",
            config=SymbolRef("medre.config.adapters.matrix", "MatrixConfig"),
            runtime_config=SymbolRef("medre.config.model", "MatrixRuntimeConfig"),
            adapter=SymbolRef("medre.adapters.matrix.adapter", "MatrixAdapter"),
            fake_adapter=SymbolRef("medre.adapters.fakes.matrix", "FakeMatrixAdapter"),
            renderer_factory=SymbolRef(
                "medre.adapters.matrix.renderer", "build_matrix_renderer"
            ),
            dependency_probe=SymbolRef("medre.adapters.matrix.compat", "HAS_NIO"),
            distribution="mindroom-nio",
            import_names=("mindroom_nio", "nio"),
            sdk_import_roots=("nio", "aiohttp"),
            native_namespace_reader=SymbolRef(
                "medre.adapters.matrix.event_shape", "matrix_namespace"
            ),
            versioned_namespace_reader=SymbolRef(
                "medre.adapters.matrix.event_shape", "matrix_versioned_namespace"
            ),
            attribution_projector=SymbolRef(
                "medre.adapters.matrix.attribution", "project_matrix_attribution"
            ),
            runtime_config_preparer=SymbolRef(
                "medre.adapters.matrix.runtime", "prepare_matrix_runtime_config"
            ),
            runtime_directories=SymbolRef(
                "medre.adapters.matrix.runtime", "matrix_runtime_directories"
            ),
            cli_register=SymbolRef(
                "medre.adapters.matrix.cli_contrib", "register_matrix_cli"
            ),
            cli_dispatch=SymbolRef(
                "medre.adapters.matrix.cli_contrib", "dispatch_matrix_cli"
            ),
            support_endpoint_fields=("homeserver", "user_id", "room_allowlist"),
            support_secret_fields=("access_token",),
            native_detection_priority=20,
        ),
        AdapterSpec(
            transport="meshtastic",
            config=SymbolRef("medre.config.adapters.meshtastic", "MeshtasticConfig"),
            runtime_config=SymbolRef("medre.config.model", "MeshtasticRuntimeConfig"),
            adapter=SymbolRef("medre.adapters.meshtastic.adapter", "MeshtasticAdapter"),
            fake_adapter=SymbolRef(
                "medre.adapters.fakes.meshtastic", "FakeMeshtasticAdapter"
            ),
            renderer_factory=SymbolRef(
                "medre.adapters.meshtastic.renderer", "build_meshtastic_renderer"
            ),
            dependency_probe=SymbolRef(
                "medre.adapters.meshtastic.compat", "HAS_MESHTASTIC"
            ),
            distribution="mtjk",
            import_names=("mtjk", "meshtastic"),
            sdk_import_roots=("meshtastic", "serial", "serial_asyncio"),
            native_namespace_reader=SymbolRef(
                "medre.adapters.meshtastic.event_shape", "meshtastic_namespace"
            ),
            versioned_namespace_reader=SymbolRef(
                "medre.adapters.meshtastic.event_shape",
                "meshtastic_versioned_namespace",
            ),
            attribution_projector=SymbolRef(
                "medre.adapters.meshtastic.attribution",
                "project_meshtastic_attribution",
            ),
            support_endpoint_fields=(
                "host",
                "port",
                "serial_port",
                "ble_address",
                "channel_mapping",
            ),
            traits=frozenset({"radio"}),
            native_detection_priority=30,
        ),
        AdapterSpec(
            transport="meshcore",
            config=SymbolRef("medre.config.adapters.meshcore", "MeshCoreConfig"),
            runtime_config=SymbolRef("medre.config.model", "MeshCoreRuntimeConfig"),
            adapter=SymbolRef("medre.adapters.meshcore.adapter", "MeshCoreAdapter"),
            fake_adapter=SymbolRef(
                "medre.adapters.fakes.meshcore", "FakeMeshCoreAdapter"
            ),
            renderer_factory=SymbolRef(
                "medre.adapters.meshcore.renderer", "build_meshcore_renderer"
            ),
            dependency_probe=SymbolRef(
                "medre.adapters.meshcore.compat", "HAS_MESHCORE"
            ),
            distribution="meshcore",
            import_names=("meshcore",),
            sdk_import_roots=("meshcore", "bleak", "serial", "serial_asyncio"),
            native_namespace_reader=SymbolRef(
                "medre.adapters.meshcore.event_shape", "meshcore_namespace"
            ),
            versioned_namespace_reader=SymbolRef(
                "medre.adapters.meshcore.event_shape", "meshcore_versioned_namespace"
            ),
            attribution_projector=SymbolRef(
                "medre.adapters.meshcore.attribution", "project_meshcore_attribution"
            ),
            support_endpoint_fields=(
                "host",
                "port",
                "serial_port",
                "ble_address",
                "serial_baudrate",
            ),
            support_secret_fields=("ble_pin",),
            traits=frozenset({"radio"}),
            native_detection_priority=10,
        ),
        AdapterSpec(
            transport="lxmf",
            config=SymbolRef("medre.config.adapters.lxmf", "LxmfConfig"),
            runtime_config=SymbolRef("medre.config.model", "LxmfRuntimeConfig"),
            adapter=SymbolRef("medre.adapters.lxmf.adapter", "LxmfAdapter"),
            fake_adapter=SymbolRef("medre.adapters.fakes.lxmf", "FakeLxmfAdapter"),
            renderer_factory=SymbolRef(
                "medre.adapters.lxmf.renderer", "build_lxmf_renderer"
            ),
            dependency_probe=SymbolRef("medre.adapters.lxmf.compat", "HAS_LXMF"),
            distribution="lxmf",
            import_names=("lxmf", "RNS"),
            sdk_import_roots=("RNS", "LXMF", "lxmf"),
            native_namespace_reader=SymbolRef(
                "medre.adapters.lxmf.event_shape", "lxmf_namespace"
            ),
            versioned_namespace_reader=SymbolRef(
                "medre.adapters.lxmf.event_shape", "lxmf_versioned_namespace"
            ),
            attribution_projector=SymbolRef(
                "medre.adapters.lxmf.attribution", "project_lxmf_attribution"
            ),
            support_endpoint_fields=("storage_path", "display_name"),
            support_secret_fields=("identity_path",),
            traits=frozenset({"radio"}),
            native_detection_priority=40,
        ),
    )
)


def iter_adapter_specs() -> tuple[AdapterSpec, ...]:
    """Return built-in adapter specs in deterministic registration order."""
    return tuple(BUILTIN_ADAPTER_REGISTRY)


def native_detection_specs() -> tuple[AdapterSpec, ...]:
    """Return built-ins in native-metadata disambiguation precedence order."""
    return BUILTIN_ADAPTER_REGISTRY.native_detection_order()


def get_adapter_spec(transport: str) -> AdapterSpec | None:
    """Return the built-in spec for *transport*, or ``None``."""
    return BUILTIN_ADAPTER_REGISTRY.get(transport)


def registered_transports() -> tuple[str, ...]:
    """Return the authoritative built-in transport vocabulary."""
    return BUILTIN_ADAPTER_REGISTRY.transports()


def adapter_sdk_packages() -> tuple[str, ...]:
    """Return the deduplicated SDK import roots declared by built-ins."""
    seen: set[str] = set()
    ordered: list[str] = []
    for spec in BUILTIN_ADAPTER_REGISTRY:
        for package in spec.sdk_import_roots:
            if package not in seen:
                seen.add(package)
                ordered.append(package)
    return tuple(ordered)
