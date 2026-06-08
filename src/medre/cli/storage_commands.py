"""Storage CLI commands: status inspection and prerelease reset.

``medre storage status``  — read-only schema health check.
``medre storage reset``   — destructive reset with optional backup.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .exit_codes import EXIT_BUILD, EXIT_CONFIG
from .storage_helpers import _open_readonly_storage


async def _storage_status(storage_path: str) -> None:
    """Open the database read-only and report schema health.

    Reports the stored schema version, expected schema version, and the
    result of required-column validation for every table.
    """
    from medre.core.storage.sqlite.schema import (
        _EXPECTED_SCHEMA_VERSION,
        _REQUIRED_COLUMNS,
    )

    storage = await _open_readonly_storage(storage_path)
    try:
        # Read schema version.
        row = await storage._read_one(
            "SELECT value FROM _medre_schema_meta WHERE key = 'schema_version'"
        )
        stored_version = int(row["value"]) if row else None
        expected_version = _EXPECTED_SCHEMA_VERSION

        version_match = stored_version == expected_version

        # Validate column shape for each required table.
        table_results: dict[str, dict[str, object]] = {}
        all_ok = version_match
        for table, required in _REQUIRED_COLUMNS.items():
            rows = await storage._read_all(f"PRAGMA table_info({table})")
            existing = {r["name"] for r in rows}
            missing = sorted(required - existing)
            table_results[table] = {
                "ok": len(missing) == 0,
                "missing_columns": missing,
            }
            if missing:
                all_ok = False

        # Print report.
        print(f"Storage: {storage_path}")
        print(
            f"Schema version: {stored_version} (expected: {expected_version}, match: {version_match})"
        )
        print()
        for table, info in table_results.items():
            status = "OK" if info["ok"] else f"MISSING {info['missing_columns']}"
            print(f"  {table}: {status}")
        print()
        if all_ok:
            print("Status: HEALTHY")
        else:
            print("Status: MISMATCH — storage requires operator intervention")
    finally:
        await storage.close()


async def _storage_reset(storage_path: str, *, backup: bool, yes: bool) -> None:
    """Delete the storage database file, optionally creating a backup first.

    Refuses to proceed without the ``--yes`` confirmation flag.
    """
    if not yes:
        print(
            "Error: destructive operation requires --yes to confirm.",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    resolved = Path(storage_path).resolve()
    if not resolved.exists():
        print(f"Error: file not found: {resolved}", file=sys.stderr)
        sys.exit(EXIT_BUILD)

    if backup:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = resolved.with_suffix(f".bak-{ts}.db")
        shutil.copy2(resolved, backup_path)
        print(f"Backup: {backup_path}")

    os.remove(resolved)
    print(f"Deleted: {resolved}")
    print("Storage reset complete.")
