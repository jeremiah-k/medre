"""Built-in transport SDK availability probing."""

from __future__ import annotations

import importlib

from medre.adapter_registry import iter_adapter_specs

# Public compatibility shape consumed by config/status commands.
TRANSPORTS: list[tuple[str, str | None, tuple[str, ...]]] = [
    (spec.transport, spec.distribution, spec.import_names)
    for spec in iter_adapter_specs()
]


def is_transport_installed(transport: str) -> bool:
    """Check whether a registered transport SDK is importable."""
    for t_key, _dist, import_names in TRANSPORTS:
        if t_key != transport:
            continue
        if not import_names:
            return True
        for mod_name in import_names:
            try:
                importlib.import_module(mod_name)
                return True
            except ImportError:
                continue
        return False
    return False
