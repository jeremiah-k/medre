"""Recovery evidence section for the runtime evidence bundle.

Builds a snapshot-diagnostics recovery section from offline storage —
no live runtime required.  Uses :func:`build_startup_recovery_ledger`
and :func:`build_recovery_summary` as pure functions over the current
outbox snapshot, labelled as snapshot diagnostics rather than actual
startup recovery.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from medre.config.paths import MedrePaths, MedrePathsError

from ._helpers import (
    _section_error,
    _section_ok,
    _section_skipped,
)

_logger = logging.getLogger(__name__)


async def _collect_recovery_section(
    config: Any,
    paths: MedrePaths,
) -> dict[str, Any]:
    """Build the recovery section from offline storage snapshots.

    Queries storage for all outbox items and builds recovery evidence
    as snapshot diagnostics.  Does **not** start a runtime or claim
    actual startup recovery.

    Parameters
    ----------
    config:
        Loaded MEDRE configuration.
    paths:
        Resolved MEDRE paths.

    Returns
    -------
    dict[str, Any]
        Status-envelope section dict with recovery summary and ledger
        in ``data``.
    """
    from medre.core.recovery.builder import (
        build_recovery_summary,
        build_startup_recovery_ledger,
    )
    from medre.core.recovery.recovery_source import RecoverySource
    from medre.core.storage.sqlite.storage import SQLiteStorage

    enabled_adapters = config.adapters.all_enabled()
    if not enabled_adapters:
        return _section_skipped(
            "No adapters enabled — recovery evidence requires an active configuration"
        )

    storage_config = config.storage

    # Memory backend — nothing persistent to inspect.
    if storage_config.backend == "memory":
        return _section_skipped(
            "Storage backend is 'memory' — no persistent outbox data for recovery evidence"
        )

    # Resolve DB path.
    if storage_config.path:
        try:
            db_path = str(paths.expand_placeholder(storage_config.path))
        except MedrePathsError as exc:
            return _section_error(f"Invalid storage path: {exc}")
    else:
        db_path = str(paths.database_path)

    if not os.path.exists(db_path):
        return _section_skipped(
            "Database file does not exist — no recovery evidence available"
        )

    # Open read-only.
    storage: Any | None = None
    try:
        storage = await SQLiteStorage.open_readonly(db_path)
    except Exception as exc:
        return _section_error(f"Cannot open database read-only: {exc}")

    try:
        # Query all outbox items for recovery analysis.
        # list_all_outbox_items uses a 10k default limit (matching
        # list_all_receipts) which is appropriate for recovery diagnostics.
        all_items = await storage.list_all_outbox_items()

        # Generate collection-scoped recovery_run_id.
        collection_ts = datetime.now(timezone.utc)
        recovery_run_id = (
            f"snapshot-{collection_ts.strftime('%Y%m%dT%H%M%SZ')}"
            f"-{uuid.uuid4().hex[:8]}"
        )

        # Build recovery ledger with snapshot context (no startup_timestamp).
        # Snapshot diagnostics source — no runtime startup or retry worker
        # performed actual recovery.
        recovery_ledger = build_startup_recovery_ledger(
            outbox_items=all_items,
            startup_timestamp=None,
            recovery_run_id=recovery_run_id,
            recovery_source=str(RecoverySource.SNAPSHOT_DIAGNOSTICS),
        )
        recovery_summary = build_recovery_summary(recovery_ledger)

        return _section_ok(
            {
                "recovery_summary": recovery_summary.to_dict(),
                "recovery_ledger": recovery_ledger.to_dict(),
                "snapshot_context": {
                    "source": "storage_snapshot",
                    "collection_timestamp": collection_ts.isoformat(),
                },
            }
        )
    except Exception as exc:
        return _section_error(f"Recovery evidence collection error: {exc}")
    finally:
        if storage is not None:
            await storage.close()
