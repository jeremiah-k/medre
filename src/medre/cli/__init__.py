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

# Exit codes — used by tests via ``from medre.cli import EXIT_*``
from .exit_codes import (
    EXIT_OK,
    EXIT_CONFIG,
    EXIT_BUILD,
    EXIT_STARTUP,
    EXIT_NOT_FOUND,
)

# Main entry point and parser
from .main import main, _build_parser, _get_version

# Re-export symbols accessed by tests via ``from medre.cli import ...``
from .replay_commands import _BEST_EFFORT_WARNING
from .recover_commands import _infer_failure_kind, _failure_category, _recover
from .storage_helpers import _open_readonly_storage

# Re-export config loader and env overrides into the cli namespace so that
# ``patch("medre.cli.load_config")`` and ``patch("medre.cli.apply_env_overrides")``
# continue to work during the test migration phase.
from medre.config.loader import load_config
from medre.config.env import apply_env_overrides

__all__ = [
    "main",
    "EXIT_OK",
    "EXIT_CONFIG",
    "EXIT_BUILD",
    "EXIT_STARTUP",
    "EXIT_NOT_FOUND",
]
