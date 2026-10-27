"""SDK-free registration value types, independent of provider discovery.

Entry-point modules import these declarations without loading the registry.
This prevents a factory imported directly by its own tests from re-entering
entry-point discovery before that factory has finished being defined.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AdapterSpec",
    "AdapterSample",
    "DestinationSpec",
    "SymbolRef",
    "NativeSetupField",
]


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
class DestinationSpec:
    """An adapter-owned addressing kind and its pure config validator.

    The validator accepts a RouteDestinationConfig, returns None on success,
    and raises ValueError for an invalid address. It must not perform I/O or
    import native transport SDKs. The generic channel kind belongs to core.
    """

    kind: str
    validator: SymbolRef


@dataclass(frozen=True, slots=True)
class AdapterSample:
    """SDK-free fake values and an opaque conversation address.

    Config excludes generic lifecycle fields, which core supplies. Adapter
    authors choose native fields and addresses without core inference.
    """

    config: Mapping[str, Any]
    channel: str | None = None


@dataclass(frozen=True, slots=True)
class NativeSetupField:
    """A provider-owned prompt; MEDRE does not know native field semantics.

    A field may be conditional on a prior choice. Native values are still
    validated using the provider's actual config model and preflight.
    """

    name: str
    label: str
    required: bool = True
    secret: bool = False
    choices: tuple[str, ...] = ()
    active_when: tuple[str, str] | None = None
    numeric: bool = False


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
    renderer_factory: SymbolRef | None
    dependency_probe: SymbolRef | None
    distribution: str | None
    import_names: tuple[str, ...]
    sdk_import_roots: tuple[str, ...]
    native_namespace_reader: SymbolRef
    versioned_namespace_reader: SymbolRef
    attribution_projector: SymbolRef
    fake_adapter: SymbolRef | None = None
    runtime_config_preparer: SymbolRef | None = None
    runtime_directories: SymbolRef | None = None
    cli_register: SymbolRef | None = None
    cli_dispatch: SymbolRef | None = None
    support_endpoint_fields: tuple[str, ...] = ()
    support_secret_fields: tuple[str, ...] = ()
    native_detection_priority: int = 100
    destinations: tuple[DestinationSpec, ...] = ()
    sample_config: SymbolRef | None = None
    native_setup_fields: tuple[NativeSetupField, ...] = ()
    logger_levels: tuple[tuple[str, str], ...] = ()
    stopped_state_declaration: SymbolRef | None = None

    @property
    def has_python_sdk(self) -> bool:
        """Whether this adapter declares a separately importable Python SDK."""
        return bool(self.distribution or self.import_names or self.sdk_import_roots)
