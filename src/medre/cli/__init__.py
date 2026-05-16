"""MEDRE command-line interface.

Usage::

    medre run [--config PATH]       Start the MEDRE runtime
    medre smoke [--config] [--message TEXT] [--json]
                                    Local validation: fake-adapter pipeline test
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

The package also supports ``python -m medre`` and ``python -m medre.cli``
via their respective ``__main__`` modules.
"""
from __future__ import annotations

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
