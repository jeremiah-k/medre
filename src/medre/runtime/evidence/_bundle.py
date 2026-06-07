"""Concrete entry point: :func:`collect_evidence_bundle`."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.core.evidence.tiers import infer_evidence_tier
from medre.core.observability.sanitization import sanitize_error

from ._config_sections import (
    _collect_config_summary,
    _collect_route_validation,
)
from ._diagnostics_sections import (
    _collect_diagnostics_snapshot,
    _collect_live_health,
)
from ._helpers import (
    _LIMITATIONS,
    SCHEMA_VERSION,
    _compute_overall_status,
    _get_version,
    _now_utc,
    _section_skipped,
)
from ._recovery_sections import _collect_recovery_section
from ._storage_sections import (
    _collect_storage_path_bundle,
    _collect_storage_section,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def collect_evidence_bundle(
    config_path: str | None = None,
    *,
    event_id: str | None = None,
    replay_run_id: str | None = None,
    include_refresh_health: bool = False,
    storage_path: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Collect a comprehensive evidence bundle.

    **Persistence boundary:** This function reads from storage (events,
    receipts, native refs, outbox items) via read-only queries.  It never
    writes to storage or mutates lifecycle state.  All derived sections
    (delivery outcome ledger, retry summary, convergence, orphan report,
    recovery) are computed on demand from loaded rows.  The diagnostics
    snapshot builds and immediately stops a runtime — any state changes
    during that ephemeral lifecycle are not persisted to the operator's
    database.

    Parameters
    ----------
    config_path:
        Path to TOML config file (``None`` uses standard discovery).
    event_id:
        When provided, include the event and its delivery receipts from
        storage (read-only).
    replay_run_id:
        When provided, include delivery receipts for this replay run from
        storage (read-only).
    include_refresh_health:
        When ``True``, start the runtime once, refresh adapter health,
        capture a live snapshot, and stop the runtime cleanly.  The report
        will have ``runtime_started: true``.  Incompatible with
        *storage_path*.
    storage_path:
        When provided, open the SQLite database at this path directly in
        read-only mode.  Config-related sections (config_summary,
        route_validation, diagnostics_snapshot, live_health) are skipped.
        Only the storage section is collected.  Mutually exclusive with
        *config_path*.
    now_fn:
        Injectable clock for deterministic testing.

    Returns
    -------
    dict[str, Any]
        JSON-safe evidence bundle with ``schema_version``, ``status``,
        ``sections``, ``errors``, and ``limitations``.
    """
    _now = now_fn or _now_utc

    # -- storage_path direct mode: skip config entirely ---------------------
    if storage_path is not None:
        return await _collect_storage_path_bundle(
            storage_path,
            event_id=event_id,
            replay_run_id=replay_run_id,
            now_fn=_now,
        )

    # -- Step 1: Load config ------------------------------------------------
    try:
        config, source, paths = load_config(config_path)
    except Exception as exc:
        return {
            "adapter_status": None,
            "collected_at": _now().isoformat(),
            "command": "evidence",
            "config_source": None,
            "convergence_summary": None,
            "errors": [sanitize_error(str(exc))],
            "evidence_tier": "synthetic",
            "generated_at": _now().isoformat(),
            "limitations": _LIMITATIONS,
            "lifecycle_convergence_report": None,
            "medre_version": _get_version(),
            "orphan_report": None,
            "recovery_ledger": None,
            "recovery_summary": None,
            "runtime_started": False,
            "schema_version": SCHEMA_VERSION,
            "sections": {},
            "shutdown_evidence": None,
            "status": "error",
        }

    config = apply_env_overrides(config, paths)

    sections: dict[str, Any] = {}
    errors: list[str] = []
    runtime_started = False

    # -- Config summary (always ok if we got here) --------------------------
    sections["config_summary"] = _collect_config_summary(config, source, paths)

    # -- Route validation ---------------------------------------------------
    sections["route_validation"] = _collect_route_validation(config)
    if sections["route_validation"]["error"]:
        errors.append(sections["route_validation"]["error"])

    # -- Diagnostics snapshot (no start) ------------------------------------
    sections["diagnostics_snapshot"] = await _collect_diagnostics_snapshot(
        config,
        paths,
    )
    if sections["diagnostics_snapshot"]["error"]:
        errors.append(sections["diagnostics_snapshot"]["error"])

    # -- Live health (only if requested) ------------------------------------
    if include_refresh_health:
        sections["live_health"] = await _collect_live_health(config, paths)
        # Mark runtime as started if the section isn't an outright error
        # from the build phase (build errors mean it never started).
        lh_status = sections["live_health"].get("status")
        if lh_status in ("passed", "partial"):
            runtime_started = True
        if sections["live_health"]["error"]:
            errors.append(sections["live_health"]["error"])
    else:
        sections["live_health"] = _section_skipped(
            "Use --include-refresh-health to populate this section"
        )

    # -- Storage section ----------------------------------------------------
    sections["storage"] = await _collect_storage_section(
        config,
        paths,
        event_id,
        replay_run_id,
    )
    if sections["storage"]["error"]:
        errors.append(sections["storage"]["error"])

    # -- Recovery section ---------------------------------------------------
    sections["recovery"] = await _collect_recovery_section(config, paths)
    if sections["recovery"].get("error"):
        errors.append(sections["recovery"]["error"])

    # -- Compute overall status ---------------------------------------------
    overall = _compute_overall_status(sections)

    # -- Tier inference (conservative) --------------------------------------
    # Walk adapter configs to find adapter_kind; never infer live/hardware.
    _adapter_kind: str | None = None
    try:
        adapters_cfg = getattr(config, "adapters", None)
        if adapters_cfg is not None:
            for _transport, _aid, _rtc in adapters_cfg.all_configs():
                kind = getattr(_rtc, "adapter_kind", None)
                if kind == "fake":
                    _adapter_kind = "fake"
                    break
    except Exception:
        _logger.debug(
            "Failed to scan adapter configs for tier inference", exc_info=True
        )

    evidence_tier = infer_evidence_tier(adapter_kind=_adapter_kind)

    # -- Extract derived evidence from snapshot for top-level access --------
    _shutdown_evidence: dict[str, Any] | None = None
    _adapter_status: list[dict[str, Any]] | None = None
    diag_snapshot_data = sections.get("diagnostics_snapshot", {}).get("data")
    if diag_snapshot_data is not None:
        _shutdown_evidence = diag_snapshot_data.get("shutdown_evidence")
        _adapter_status = diag_snapshot_data.get("adapter_status")

    # -- Extract recovery summary / ledger into top-level fields ------------
    _recovery_summary: dict[str, Any] | None = None
    _recovery_ledger: dict[str, Any] | None = None
    recovery_data = sections.get("recovery", {}).get("data")
    if recovery_data is not None:
        _recovery_summary = recovery_data.get("recovery_summary")
        _recovery_ledger = recovery_data.get("recovery_ledger")

    # -- Extract convergence surfaces from storage data --------------------
    # These are event-scoped when event_id is provided, otherwise the
    # global storage-wide view.  See docs/spec/diagnostics-evidence.md §21-23.
    _convergence_summary: dict[str, Any] | None = None
    _orphan_report: dict[str, Any] | None = None
    _lifecycle_convergence_report: dict[str, Any] | None = None
    storage_data = sections.get("storage", {}).get("data")
    if storage_data is not None:
        _convergence_summary = storage_data.get("convergence_summary")
        _orphan_report = storage_data.get("orphan_report")
        _lifecycle_convergence_report = storage_data.get("lifecycle_convergence_report")

    return {
        "adapter_status": _adapter_status,
        "collected_at": _now().isoformat(),
        "command": "evidence",
        "config_source": source.value,
        "convergence_summary": _convergence_summary,
        "errors": errors,
        "evidence_tier": evidence_tier,
        "generated_at": _now().isoformat(),
        "limitations": _LIMITATIONS,
        "lifecycle_convergence_report": _lifecycle_convergence_report,
        "medre_version": _get_version(),
        "orphan_report": _orphan_report,
        "recovery_ledger": _recovery_ledger,
        "recovery_summary": _recovery_summary,
        "runtime_started": runtime_started,
        "schema_version": SCHEMA_VERSION,
        "sections": sections,
        "shutdown_evidence": _shutdown_evidence,
        "status": overall,
    }
