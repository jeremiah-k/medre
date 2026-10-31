"""Storage CLI commands: inspection, explicit content collection, and reset.

``medre storage status``  — read-only schema health check.
``medre storage reset``   — destructive reset with optional backup.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .exit_codes import EXIT_BUILD, EXIT_CONFIG

_SQLITE_MAGIC = b"SQLite format 3\x00"


async def _storage_status(storage_path: str) -> None:
    """Open the database read-only and report schema health.

    Reports the stored schema version, expected schema version, and the
    result of required-column validation for every table.

    Uses a raw ``sqlite3`` connection with ``?mode=ro`` instead of
    :class:`SQLiteStorage` so that unhealthy / mismatched databases can
    still be inspected without triggering schema validation errors.
    """
    import sqlite3

    from medre.core.storage.sqlite.schema import (
        _EXPECTED_SCHEMA_VERSION,
        _REQUIRED_COLUMNS,
    )

    resolved = Path(storage_path).resolve()
    if not resolved.exists():
        print(f"Error: file not found: {resolved}", file=sys.stderr)
        sys.exit(EXIT_BUILD)

    try:
        conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)

    try:
        # Read schema version.
        try:
            row = conn.execute(
                "SELECT value FROM _medre_schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            stored_version = int(row["value"]) if row else None
        except Exception:
            stored_version = None

        expected_version = _EXPECTED_SCHEMA_VERSION
        version_match = stored_version == expected_version

        # Validate column shape for each required table.
        table_results: dict[str, dict[str, object]] = {}
        all_ok = version_match
        for table, required in _REQUIRED_COLUMNS.items():
            try:
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                existing = {r["name"] for r in rows}
            except Exception:
                existing = set()
            missing = sorted(required - existing)
            table_results[table] = {
                "ok": len(missing) == 0,
                "missing_columns": missing,
            }
            if missing:
                all_ok = False

        # Print report.
        print(f"Storage: {resolved}")
        print(
            f"Schema version: {stored_version} (expected: {expected_version}, "
            f"match: {version_match})"
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
        conn.close()


async def _storage_reset(storage_path: str, *, backup: bool, yes: bool) -> None:
    """Delete the storage database file, optionally creating a backup first.

    Refuses to proceed without the ``--yes`` confirmation flag.
    Validates the SQLite magic header before deletion to guard against
    accidentally removing non-database files.
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

    # Validate SQLite magic bytes before touching the file.
    try:
        with open(resolved, "rb") as f:
            header = f.read(16)
    except OSError as exc:
        print(f"Error: cannot read {resolved}: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)

    if header[:16] != _SQLITE_MAGIC:
        print(
            f"Error: {resolved} does not appear to contain SQLite magic bytes. "
            f"Refusing to delete a non-database file.",
            file=sys.stderr,
        )
        sys.exit(EXIT_BUILD)

    # Backup (if requested), then delete main DB and WAL/SHM sidecars.
    try:
        if backup:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = resolved.with_suffix(f".bak-{ts}.db")
            shutil.copy2(resolved, backup_path)
            print(f"Backup: {backup_path}")
            for suffix in ("-wal", "-shm"):
                sidecar = resolved.with_name(resolved.name + suffix)
                if sidecar.exists():
                    shutil.copy2(
                        sidecar, backup_path.with_name(backup_path.name + suffix)
                    )
                    print(f"Backup: {backup_path.name + suffix}")

        os.remove(resolved)
        print(f"Deleted: {resolved}")
        for suffix in ("-wal", "-shm"):
            sidecar = resolved.with_name(resolved.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
                print(f"Deleted: {sidecar}")
        print("Storage reset complete.")
    except PermissionError:
        print(
            f"Error: permission denied accessing {resolved}. "
            f"Check filesystem permissions.",
            file=sys.stderr,
        )
        sys.exit(EXIT_BUILD)
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUILD)


async def _storage_collect(
    storage_path: str,
    *,
    mode: str = "retain",
    limit: int = 25,
    after: str | None = None,
    apply: bool = False,
    yes: bool = False,
) -> None:
    """Run an explicit, bounded content policy batch without a scheduler.

    Dry runs use a strict read-only SQLite handle. Apply requires two
    independent declarations and rechecks each event inside its mutation
    transaction, never authorizing from preview output.
    """
    from medre.core.storage.backend import StorageError
    from medre.core.storage.sqlite.storage import SQLiteStorage

    from .json import to_json
    from .storage_helpers import _open_readonly_storage

    if mode not in {"retain", "relay-only"} or not 1 <= limit <= 100:
        print("Error: choose a valid mode and limit 1–100", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
    if after is not None and (not after or len(after) > 512):
        print("Error: --after needs a bounded event ID", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
    if apply and (mode != "relay-only" or not yes):
        print(
            "Error: --apply requires --mode relay-only and --yes; "
            "stop running bridges before applying collection",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG)
    if yes and not apply:
        print("Error: --yes requires --apply", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
    if not Path(storage_path).is_file():
        print("Error: existing SQLite database required", file=sys.stderr)
        raise SystemExit(EXIT_BUILD)
    if apply:
        storage = SQLiteStorage(storage_path)
        try:
            await storage.initialize()
        except Exception as exc:
            print(f"Storage error: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_BUILD) from None
    else:
        storage = await _open_readonly_storage(storage_path)
    try:
        event_ids = await storage.list_event_ids_page(after_event_id=after, limit=limit)
        reports: list[dict[str, Any]] = []
        for event_id in event_ids:
            if mode == "retain":
                reports.append({"event_id": event_id, "status": "retained_by_policy"})
            else:
                reports.append(
                    await storage.collect_event_content(event_id, dry_run=not apply)
                )
        result = {
            "format": "medre-content-collection-batch",
            "version": 1,
            "mode": mode,
            "dry_run": not apply,
            "events_examined": len(event_ids),
            "limit": limit,
            "next_after": event_ids[-1] if event_ids else None,
            "rows_removed": sum(r.get("rows_removed", 0) for r in reports),
            "logical_bytes_removed": sum(
                r.get("logical_bytes_removed", 0) for r in reports
            ),
            "physical_erasure_claimed": False,
            "results": reports,
        }
        print(to_json(result))
    except (StorageError, ValueError):
        print(
            "Collection refused: storage state is not safe to assess", file=sys.stderr
        )
        raise SystemExit(EXIT_BUILD) from None
    finally:
        await storage.close()
