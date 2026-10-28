"""Read-only storage access helpers for CLI inspection commands."""

from __future__ import annotations

import sys
from typing import Any

from .exit_codes import EXIT_BUILD


async def _open_readonly_storage(
    storage_path: str, *, refusal: tuple[str, int] | None = None
) -> Any:
    """Open a SQLite database directly by path in strict read-only mode.

    Opens the database with no file creation, no DDL, no schema writes.
    Raises ``SystemExit`` on storage errors. ``refusal`` replaces the default
    path-bearing error with a fixed message and exit code for commands whose
    output boundary forbids file paths.
    """
    from medre.core.storage.sqlite.storage import SQLiteStorage

    try:
        storage = await SQLiteStorage.open_readonly(storage_path)
    except Exception as exc:
        if refusal is not None:
            message, code = refusal
            print(message, file=sys.stderr)
            sys.exit(code)
        print(f"Storage error: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)
    return storage
