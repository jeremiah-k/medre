"""Installed transport SDK availability probing."""

from __future__ import annotations

import importlib

from medre.adapter_registry import get_adapter_spec, iter_adapter_specs

# Public compatibility shape consumed by config/status commands.
TRANSPORTS: list[tuple[str, str | None, tuple[str, ...]]] = [
    (spec.transport, spec.distribution, spec.import_names)
    for spec in iter_adapter_specs()
]


def is_transport_installed(transport: str) -> bool:
    """Return whether a registered transport's required SDK is usable.

    The adapter's own dependency probe is authoritative. A single importable
    name is not proof of availability when an SDK needs multiple modules (for
    example LXMF requires *both* LXMF and RNS). For a provider with no probe,
    import names retain their historical alternative-name semantics.
    """
    for t_key, _dist, import_names in TRANSPORTS:
        if t_key != transport:
            continue
        spec = get_adapter_spec(transport)
        if spec is not None and spec.dependency_probe is not None:
            try:
                return bool(spec.dependency_probe.load())
            except Exception:
                return False
        if not import_names:
            return True
        for mod_name in import_names:
            try:
                importlib.import_module(mod_name)
                return True
            except Exception:
                continue
        return False
    return False
