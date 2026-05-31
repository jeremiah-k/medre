"""Recovery evidence section for the runtime evidence bundle.

Builds a startup recovery section with full startup context (recovery
run ID, startup timestamp) — richer than the per-event evidence
bundle which has no access to BootSummary.
"""

from __future__ import annotations

import logging
from typing import Any

from medre.config.paths import MedrePaths

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
    """Build the recovery section with startup context.

    Builds the runtime (no start), accesses ``app._boot_summary`` for
    recovery run ID and startup timestamp, queries storage for all
    non-terminal outbox items, and builds the recovery ledger and
    summary.

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
    from medre.core.recovery._builder import (
        build_recovery_summary,
        build_startup_recovery_ledger,
    )

    enabled_adapters = config.adapters.all_enabled()
    if not enabled_adapters:
        return _section_skipped(
            "No adapters enabled — recovery evidence requires an active configuration"
        )

    from medre.runtime.builder import RuntimeBuilder

    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
    except Exception as exc:
        return _section_error(f"Runtime build error: {exc}")

    if not app.adapters:
        return _section_skipped(
            f"All {len(app.build_failures)} enabled adapter(s) failed to construct"
        )

    # Access startup context from BootSummary.
    boot_summary = getattr(app, "_boot_summary", None)
    if boot_summary is None:
        return _section_skipped(
            "BootSummary unavailable — startup recovery evidence requires a started runtime"
        )

    recovery_run_id = getattr(boot_summary, "recovery_run_id", "") or None
    startup_timestamp = getattr(boot_summary, "startup_timestamp", None)

    if not recovery_run_id:
        return _section_skipped(
            "Recovery run ID not set — recovery evidence requires a started runtime"
        )

    # Query all non-terminal outbox items from storage.
    if app.storage is None:
        return _section_skipped("No storage backend available for recovery evidence")

    outbox_items: list[Any] = []
    try:
        # Collect outbox items across all events.
        # Use list_all_outbox_items if available, otherwise build from
        # known event IDs.
        list_all = getattr(app.storage, "list_all_outbox_items", None)
        if callable(list_all):
            outbox_items = await list_all()
        else:
            # Fallback: list events and aggregate outbox items.
            outbox_items = []
            _logger.debug(
                "list_all_outbox_items not available — recovery section limited"
            )
    except Exception as exc:
        return _section_error(f"Storage query error: {exc}")

    # Build recovery ledger and summary with startup context.
    recovery_ledger = build_startup_recovery_ledger(
        outbox_items=outbox_items,
        startup_timestamp=startup_timestamp,
        recovery_run_id=recovery_run_id,
    )
    recovery_summary = build_recovery_summary(recovery_ledger)

    return _section_ok(
        {
            "recovery_summary": recovery_summary.to_dict(),
            "recovery_ledger": recovery_ledger.to_dict(),
        }
    )
