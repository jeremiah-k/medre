"""Read-only storage access helpers for CLI inspection commands."""
from __future__ import annotations

import sys
from typing import Any

from medre.config.loader import load_config

from .exit_codes import EXIT_CONFIG, EXIT_BUILD


async def _open_readonly_storage(config_path: str | None) -> Any:
    """Load config, resolve DB path, and open storage for read-only inspection.

    Opens the database in strict read-only mode — no file creation, no DDL,
    no schema writes.  Raises ``SystemExit`` on config or storage errors.
    """
    from medre.config.paths import MedrePathsError
    from medre.core.storage.sqlite import SQLiteStorage

    try:
        config, _source, paths = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    if config.storage.backend == "memory":
        print(
            "Error: storage backend is 'memory' — no persistent data to inspect.",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    if config.storage.path:
        try:
            db_path = str(paths.expand_placeholder(config.storage.path))
        except MedrePathsError as exc:
            print(f"Invalid storage path: {exc}", file=sys.stderr)
            sys.exit(EXIT_CONFIG)
    else:
        db_path = str(paths.database_path)

    try:
        storage = await SQLiteStorage.open_readonly(db_path)
    except Exception as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)
    return storage
