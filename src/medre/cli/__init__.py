"""MEDRE command-line interface.

Usage::

    medre run [--config PATH]       Start the MEDRE runtime
    medre smoke [--config] [--message TEXT] [--json]
                                    Run fake bridge smoke test
    medre config check [--config]   Validate config file
    medre config sample             Print a sample TOML config
    medre paths                     Print resolved MEDRE paths
    medre version                   Print MEDRE version
    medre adapters                  List available and configured adapters
    medre diagnostics [--config]    Print runtime snapshot JSON (no server)
    medre diagnostics --refresh-health [--config]  Start runtime, refresh adapter health once, print live snapshot
    medre routes validate [--config]  Validate route configuration
    medre routes topology [--config]  Print route topology preview
    medre routes list [--config]      List configured routes
    medre evidence [--config] [--json]  Collect evidence bundle for support

The package also supports ``python -m medre.cli`` via the ``__main__``
module.
"""
from __future__ import annotations

import importlib

# Transport adapter types that medre supports.
# Each entry: (transport_key, dist_name, import_module_names).
_TRANSPORTS: list[tuple[str, str, tuple[str, ...]]] = [
    ("matrix", "mindroom-nio", ("mindroom_nio", "nio")),
    ("meshtastic", "mtjk", ("mtjk", "meshtastic")),
    ("meshcore", "meshcore", ("meshcore",)),
    ("lxmf", "lxmf", ("lxmf", "RNS")),
]


def is_transport_installed(transport: str) -> bool:
    """Check whether a transport SDK is available via dynamic import."""
    for t_key, _dist, import_names in _TRANSPORTS:
        if t_key == transport:
            for mod_name in import_names:
                try:
                    importlib.import_module(mod_name)
                    return True
                except ImportError:
                    pass
            return False
    return False


# Exit codes — used by tests via ``from medre.cli import EXIT_*``
from .exit_codes import (
    EXIT_OK,
    EXIT_CONFIG,
    EXIT_BUILD,
    EXIT_STARTUP,
    EXIT_NOT_FOUND,
)

# Main entry point and parser
from .main import main

__all__ = [
    "main",
    "EXIT_OK",
    "EXIT_CONFIG",
    "EXIT_BUILD",
    "EXIT_STARTUP",
    "EXIT_NOT_FOUND",
]
