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

    Returns a dict with ``commands_text`` (shell-safe string) and
    ``commands_argv`` (list form) for each command.

    Inspect-first: the primary recommended commands use ``medre inspect``.
    The ``trace`` entry is retained as a specialised / lower-level tool.
    """
    def _safe(val: str | None) -> str:
        return shlex.quote(val) if val else ""

    cfg_flag_text = f"--config {_safe(config_path)}" if config_path else ""
    cfg_flag_argv: list[str] = []
    if config_path:
        cfg_flag_argv = ["--config", config_path]

    # Inspect-first primary commands.
    inspect_event_argv = ["medre", "inspect", "event", event_id] + cfg_flag_argv
    inspect_timeline_argv = ["medre", "inspect", "event", event_id, "--timeline"] + cfg_flag_argv
    inspect_receipts_argv = ["medre", "inspect", "receipts", "--event", event_id] + cfg_flag_argv
    inspect_evidence_argv = ["medre", "inspect", "event", event_id, "--evidence"] + cfg_flag_argv
    inspect_recovery_argv = ["medre", "inspect", "event", event_id, "--recovery"] + cfg_flag_argv
    evidence_argv = ["medre", "evidence", "--event", event_id] + cfg_flag_argv + ["--json"]

    # Specialised / lower-level.
    trace_argv = ["medre", "trace", "event", event_id] + cfg_flag_argv

    commands_text: dict[str, str] = {
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
        "evidence": (
            f"medre evidence --event {shlex.quote(event_id)} {cfg_flag_text} --json".strip()
        ),
        "trace": f"medre trace event {shlex.quote(event_id)} {cfg_flag_text}".strip(),
        "final_snapshot": f"cat {_safe(snapshot_path)}" if snapshot_path else "(not saved)",
    }
    commands_argv: dict[str, list[str]] = {
        "inspect_event": inspect_event_argv,
        "inspect_timeline": inspect_timeline_argv,
        "inspect_receipts": inspect_receipts_argv,
        "inspect_evidence": inspect_evidence_argv,
        "inspect_recovery": inspect_recovery_argv,
        "evidence": evidence_argv,
        "trace": trace_argv,
        "final_snapshot": [],  # No medre CLI command — just a file path
    }
    return {
        "commands_text": commands_text,
        "commands_argv": commands_argv,
    }
