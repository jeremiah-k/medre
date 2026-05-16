"""Report building for run-session evidence reports.

Provides cross-linked command generation and limitations text used by
:func:`~medre.runtime.run_session.orchestration.run_bridge_session`.
"""

from __future__ import annotations

import shlex
from typing import Any

# ---------------------------------------------------------------------------
# Limitations text
# ---------------------------------------------------------------------------

_LIMITATIONS: list[str] = [
    "Fake adapters only — no real transport connectivity proven",
    "Persistent storage (SQLite) but no crash-recovery proof",
    "Configurable message count (default single-event) — no sustained throughput or load evidence",
    "No reconnection resilience or retry-against-live proof",
    "Fire-and-forget delivery model for radio transports",
    (
        "Native refs are derived from actual stored receipts; adapters "
        "that return native_message_id=None (e.g. local enqueue) produce "
        "no native refs."
    ),
]

# ---------------------------------------------------------------------------
# Cross-linked commands
# ---------------------------------------------------------------------------


def _build_cross_linked_commands(
    event_id: str,
    config_path: str | None,
    snapshot_path: str | None,
) -> dict[str, Any]:
    """Build cross-linked CLI command strings for the report.

    Returns a dict with ``commands_text`` and ``commands_argv``, each nested
    under ``primary`` (inspect-first) and ``specialized`` (lower-level tools).

    Shape::

        {
            "commands_text": {
                "primary": { "inspect_event": "...", ... },
                "specialized": { "trace_event": "...", ... },
            },
            "commands_argv": {
                "primary": { "inspect_event": [...], ... },
                "specialized": { "trace_event": [...], ... },
            },
        }

    Inspect-first: the primary recommended commands use ``medre inspect``.
    Specialised keys (``trace_event``, ``evidence_bundle``, ``recover_event``)
    are lower-level tools retained for advanced use.
    """
    def _safe(val: str | None) -> str:
        return shlex.quote(val) if val else ""

    cfg_flag_text = f"--config {_safe(config_path)}" if config_path else ""
    cfg_flag_argv: list[str] = []
    if config_path:
        cfg_flag_argv = ["--config", config_path]

    # --- Primary: inspect-first commands ---
    primary_text: dict[str, str] = {
        "inspect_event": (
            f"medre inspect event {shlex.quote(event_id)} {cfg_flag_text}".strip()
        ),
        "inspect_timeline": (
            f"medre inspect event {shlex.quote(event_id)} --timeline {cfg_flag_text}".strip()
        ),
        "inspect_receipts": (
            f"medre inspect receipts --event {shlex.quote(event_id)} {cfg_flag_text}".strip()
        ),
        "inspect_evidence": (
            f"medre inspect event {shlex.quote(event_id)} --evidence {cfg_flag_text}".strip()
        ),
        "inspect_recovery": (
            f"medre inspect event {shlex.quote(event_id)} --recovery {cfg_flag_text}".strip()
        ),
    }
    primary_argv: dict[str, list[str]] = {
        "inspect_event": (
            ["medre", "inspect", "event", event_id] + cfg_flag_argv
        ),
        "inspect_timeline": (
            ["medre", "inspect", "event", event_id, "--timeline"] + cfg_flag_argv
        ),
        "inspect_receipts": (
            ["medre", "inspect", "receipts", "--event", event_id] + cfg_flag_argv
        ),
        "inspect_evidence": (
            ["medre", "inspect", "event", event_id, "--evidence"] + cfg_flag_argv
        ),
        "inspect_recovery": (
            ["medre", "inspect", "event", event_id, "--recovery"] + cfg_flag_argv
        ),
    }

    # --- Specialised: lower-level tools ---
    specialized_text: dict[str, str] = {
        "trace_event": (
            f"medre trace event {shlex.quote(event_id)} {cfg_flag_text}".strip()
        ),
        "evidence_bundle": (
            f"medre evidence --event {shlex.quote(event_id)} {cfg_flag_text} --json".strip()
        ),
        "recover_event": (
            f"medre recover --event {shlex.quote(event_id)} {cfg_flag_text}".strip()
        ),
    }
    specialized_argv: dict[str, list[str]] = {
        "trace_event": (
            ["medre", "trace", "event", event_id] + cfg_flag_argv
        ),
        "evidence_bundle": (
            ["medre", "evidence", "--event", event_id] + cfg_flag_argv + ["--json"]
        ),
        "recover_event": (
            ["medre", "recover", "--event", event_id] + cfg_flag_argv
        ),
    }

    return {
        "commands_text": {
            "primary": primary_text,
            "specialized": specialized_text,
        },
        "commands_argv": {
            "primary": primary_argv,
            "specialized": specialized_argv,
        },
    }
