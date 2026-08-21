"""Shared dispatch for built-in native metadata namespaces.

Concrete adapter packages remain isolated from sibling adapter imports. When a
cross-transport consumer needs to inspect another built-in transport's
standardized canonical native metadata, it goes through this shared adapter
infrastructure module instead of importing the sibling package directly.

This module performs dispatch only. Each adapter-local ``event_shape`` module
remains authoritative for its namespace name, schema version, and validation
rules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from medre.adapters.lxmf.event_shape import lxmf_namespace, lxmf_versioned_namespace
from medre.adapters.matrix.event_shape import (
    matrix_namespace,
    matrix_versioned_namespace,
)
from medre.adapters.meshcore.event_shape import (
    meshcore_namespace,
    meshcore_versioned_namespace,
)
from medre.adapters.meshtastic.event_shape import (
    meshtastic_namespace,
    meshtastic_versioned_namespace,
)

_NamespaceReader = Callable[[Mapping[str, Any]], Mapping[str, Any]]

_CURRENT_NAMESPACE_READERS: dict[str, _NamespaceReader] = {
    "matrix": matrix_namespace,
    "meshtastic": meshtastic_namespace,
    "meshcore": meshcore_namespace,
    "lxmf": lxmf_namespace,
}

_VERSIONED_NAMESPACE_READERS: dict[str, _NamespaceReader] = {
    "matrix": matrix_versioned_namespace,
    "meshtastic": meshtastic_versioned_namespace,
    "meshcore": meshcore_versioned_namespace,
    "lxmf": lxmf_versioned_namespace,
}


def current_native_namespace(
    native_data: Mapping[str, Any],
    transport: str,
) -> Mapping[str, Any]:
    """Return the current versioned namespace for *transport*."""
    reader = _CURRENT_NAMESPACE_READERS.get(transport)
    return reader(native_data) if reader is not None else {}


def versioned_native_namespace(
    native_data: Mapping[str, Any],
    transport: str,
) -> Mapping[str, Any]:
    """Return any positively versioned namespace for *transport*.

    This lookup is for platform detection only. Consumers that interpret
    transport-specific fields MUST use :func:`current_native_namespace`.
    """
    reader = _VERSIONED_NAMESPACE_READERS.get(transport)
    return reader(native_data) if reader is not None else {}
