"""Shared validation for built-in versioned native-metadata namespaces.

Adapter-local ``event_shape`` modules remain authoritative for transport
namespace names and current schema-version constants. This module owns the
common structural rule used by every built-in transport: a versioned namespace
must be a mapping with a positive integer ``schema_version`` that is not a
boolean.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def versioned_namespace(
    native_data: Mapping[str, Any],
    namespace: str,
) -> Mapping[str, Any]:
    """Return a positively versioned namespace or an empty mapping."""
    data = native_data.get(namespace)
    if not isinstance(data, Mapping):
        return {}
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return {}
    return data


def current_namespace(
    native_data: Mapping[str, Any],
    namespace: str,
    current_version: int,
) -> Mapping[str, Any]:
    """Return *namespace* only when it matches *current_version*."""
    data = versioned_namespace(native_data, namespace)
    if data.get("schema_version") != current_version:
        return {}
    return data
