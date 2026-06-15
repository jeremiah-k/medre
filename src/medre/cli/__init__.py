"""MEDRE command-line interface.

Product path (daily bridge operation)::

    medre run [--config PATH]           Start the MEDRE runtime
    medre config check [--config]       Validate config file
    medre routes validate [--config]    Validate route configuration
    medre diagnostics [--config]        Print runtime snapshot JSON (no server)
    medre diagnostics --refresh-health  Start runtime, refresh health, print snapshot
    medre inspect event <ID> --storage-path PATH   Primary read-only event investigation
    medre inspect event <ID> --storage-path PATH --timeline   With chronological timeline
    medre inspect event <ID> --storage-path PATH --evidence   With per-event evidence bundle
    medre inspect event <ID> --storage-path PATH --recovery   With recovery runbook
    medre inspect receipts --storage-path PATH      Delivery receipt queries
    medre inspect native-ref --storage-path PATH    Native message reference lookup
    medre inspect replay --storage-path PATH        Replay run inspection
    medre replay [--config]             Re-deliver historical events

Validation (developers/CI)::

    medre smoke [--config] [--json]     Local validation: fake-adapter pipeline test

Specialized (available, not primary daily path)::

    medre trace event <ID> --storage-path PATH     Standalone timeline (inspect event --timeline preferred)
    medre trace replay <ID> --storage-path PATH    Standalone replay timeline
    medre evidence --storage-path PATH [--json]  Full support-bundle collection
    medre recover --storage-path PATH            Standalone recovery classification

Utility::

    medre config sample                 Print a sample YAML config
    medre paths                         Print resolved MEDRE paths
    medre version                       Print MEDRE version
    medre adapters                      List available and configured adapters
    medre routes topology [--config]    Print route topology preview
    medre routes list [--config]        List configured routes

The package also supports ``python -m medre`` and ``python -m medre.cli``
via their respective ``__main__`` modules.
"""

from __future__ import annotations

# Exit codes — used by tests via ``from medre.cli import EXIT_*``
from .exit_codes import (
    EXIT_BUILD,
    EXIT_CONFIG,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_STARTUP,
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
