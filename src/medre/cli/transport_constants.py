"""Shared transport constants used across CLI command modules."""

from __future__ import annotations

from medre.adapter_registry import BUILTIN_ADAPTER_REGISTRY

# Radio transports use fire-and-forget delivery semantics in CLI smoke flows.
RADIO_TRANSPORTS = frozenset(
    spec.transport for spec in BUILTIN_ADAPTER_REGISTRY.with_trait("radio")
)
