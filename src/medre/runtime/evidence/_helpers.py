"""Shared constants, timestamps, section builders, and status helpers."""

from __future__ import annotations

import importlib.metadata
import logging
from datetime import datetime, timezone
from typing import Any

from medre.observability.sanitization import sanitize_error

__all__ = [
    "SCHEMA_VERSION",
    "_compute_overall_status",
    "_fixed_mono",
    "_fixed_now",
    "_get_version",
    "_now_utc",
    "_section_error",
    "_section_ok",
    "_section_partial",
    "_section_skipped",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1
"""Evidence bundle schema version.  Frozen at 1 during pre-release."""

_MAX_ERROR_LEN: int = 512
"""Truncation limit for error strings in the report."""

_LIMITATIONS: list[str] = [
    "Evidence is a point-in-time snapshot, not continuous monitoring",
    "Diagnostics snapshot reflects build-time state unless --include-refresh-health is used",
    "Storage section requires an existing initialised database",
    "Fake adapters report synthetic health, not real transport connectivity",
    "No sustained throughput, reconnection resilience, or load evidence",
]

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _get_version() -> str:
    """Return the MEDRE version string."""
    try:
        return importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fixed_now() -> datetime:
    """Deterministic timestamp for non-live sections."""
    return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _fixed_mono() -> float:
    return 0.0


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _section_ok(data: Any) -> dict[str, Any]:
    return {"status": "passed", "error": None, "data": data}


def _section_partial(data: Any, error: str) -> dict[str, Any]:
    return {"status": "partial", "error": sanitize_error(error), "data": data}


def _section_error(error: str) -> dict[str, Any]:
    return {"status": "error", "error": sanitize_error(error), "data": None}


def _section_skipped(note: str) -> dict[str, Any]:
    return {"status": "skipped", "error": None, "data": None, "note": note}


# ---------------------------------------------------------------------------
# Status computation
# ---------------------------------------------------------------------------


def _compute_overall_status(sections: dict[str, dict[str, Any]]) -> str:
    """Compute overall status from per-section statuses."""
    statuses = {s.get("status") for s in sections.values()}
    if not statuses or statuses == {"skipped"}:
        return "passed"
    if statuses <= {"passed", "skipped"}:
        return "passed"
    if "error" in statuses and all(s in ("error", "skipped") for s in statuses):
        # All attempted sections errored.
        return "partial"
    return "partial"
