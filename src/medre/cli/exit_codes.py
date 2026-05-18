"""CLI exit codes.

Numeric constants returned by ``main()`` and sub-commands on error.
"""

from __future__ import annotations

EXIT_OK = 0
"""Successful exit."""
EXIT_CONFIG = 2
"""Config parse or validation error."""
EXIT_BUILD = 3
"""Runtime build error (missing dependency, bad path, adapter construction failure)."""
EXIT_STARTUP = 4
"""Total startup failure (zero adapters started, core subsystem failure)."""
EXIT_NOT_FOUND = 5
"""Requested entity (event, receipt, native ref) not found in storage."""
