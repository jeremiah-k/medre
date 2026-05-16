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
    storage_path: str | None = None,
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

    When *storage_path* is provided, read-only commands (inspect, trace,
    evidence) use ``--storage-path`` pointing at the actual runtime DB.
    Config-required commands (recover, replay) always use ``--config`` because
    those subcommands need the full config, not just the storage backend.
    """
    # --- Config flags (for config-required commands) ---
    cfg_flag_argv: list[str] = []
    if config_path:
        cfg_flag_argv = ["--config", config_path]

    # --- Storage flags (for read-only commands when storage_path is known) ---
    # Prefer --storage-path over --config for read-only inspection because
    # the runtime overrides config storage to a temporary/custom DB path.
    if storage_path:
        ro_flag_argv: list[str] = ["--storage-path", storage_path]
    else:
        ro_flag_argv = list(cfg_flag_argv)

    # Helper: build both argv list and shell-safe text from an argv list.
    def _cmd(argv: list[str]) -> tuple[list[str], str]:
        return argv, shlex.join(argv)

    # --- Primary: inspect-first commands (read-only → storage-path) ---
    primary_argv: dict[str, list[str]] = {}
    primary_text: dict[str, str] = {}

    for key, argv in [
        ("inspect_event",
         ["medre", "inspect", "event", event_id] + ro_flag_argv),
        ("inspect_timeline",
         ["medre", "inspect", "event", event_id, "--timeline"] + ro_flag_argv),
        ("inspect_receipts",
         ["medre", "inspect", "receipts", "--event", event_id] + ro_flag_argv),
        ("inspect_evidence",
         ["medre", "inspect", "event", event_id, "--evidence"] + ro_flag_argv),
        ("inspect_recovery",
         ["medre", "inspect", "event", event_id, "--recovery"] + ro_flag_argv),
    ]:
        primary_argv[key], primary_text[key] = _cmd(argv)

    # --- Specialised: lower-level tools ---
    specialized_argv: dict[str, list[str]] = {}
    specialized_text: dict[str, str] = {}

    # trace_event and evidence_bundle are read-only → storage-path
    for key, argv in [
        ("trace_event",
         ["medre", "trace", "event", event_id] + ro_flag_argv),
        ("evidence_bundle",
         ["medre", "evidence", "--event", event_id] + ro_flag_argv + ["--json"]),
    ]:
        specialized_argv[key], specialized_text[key] = _cmd(argv)

    # recover_event is config-required → --config
    for key, argv in [
        ("recover_event",
         ["medre", "recover", "--event", event_id] + cfg_flag_argv),
    ]:
        specialized_argv[key], specialized_text[key] = _cmd(argv)

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
