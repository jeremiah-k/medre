"""CLI exit codes.

Numeric constants returned by ``main()`` and sub-commands on error.
"""

from __future__ import annotations

# Successful exit.
EXIT_OK: int = 0
# Generic operational failure: smoke/run-session drill failed or unexpected error.
EXIT_FAILED: int = 1
# Config parse or validation error.
EXIT_CONFIG: int = 2
# Runtime build error: missing dependency, bad path, or construction failure.
EXIT_BUILD: int = 3
# Total startup failure: zero adapters started or core subsystem failure.
EXIT_STARTUP: int = 4
# Requested entity (event, receipt, native ref) not found in storage.
EXIT_NOT_FOUND: int = 5
