"""Shared registry-driven dispatch for registered native metadata namespaces.

Concrete adapter packages remain isolated from sibling adapter imports.  Cross-
transport consumers resolve adapter-owned namespace readers through the registered
adapter registry, keeping native schema interpretation inside each adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast

from medre.adapter_registry import get_adapter_spec


class _NamespaceReader(Protocol):
    """Adapter-provided native namespace reader resolved from the registry."""

    def __call__(self, native_data: Mapping[str, Any]) -> Mapping[str, Any]: ...


def current_native_namespace(
    native_data: Mapping[str, Any],
    transport: str,
) -> Mapping[str, Any]:
    """Return the current native namespace for a registered *transport*."""
    spec = get_adapter_spec(transport)
    if spec is None:
        return {}
    # Registry-loaded adapter callable; the AdapterSpec contract fixes its
    # signature.  The load boundary is untyped by design (lazy import).
    reader = cast(_NamespaceReader, spec.native_namespace_reader.load())
    return reader(native_data)


def versioned_native_namespace(
    native_data: Mapping[str, Any],
    transport: str,
) -> Mapping[str, Any]:
    """Return any positively versioned namespace for a registered transport.

    This lookup is for platform detection only.  Consumers interpreting native
    fields must use :func:`current_native_namespace`.
    """
    spec = get_adapter_spec(transport)
    if spec is None:
        return {}
    # Registry-loaded adapter callable; the AdapterSpec contract fixes its
    # signature.  The load boundary is untyped by design (lazy import).
    reader = cast(_NamespaceReader, spec.versioned_namespace_reader.load())
    return reader(native_data)
