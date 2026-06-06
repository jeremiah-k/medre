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
    storage_path: str | None = None,
) -> dict[str, Any]:
    """Build cross-linked CLI command strings for the report.

    Returns a dict with ``commands_text`` and ``commands_argv``, each nested
    under ``primary`` (inspect-first) and ``specialized`` (lower-level tools).

    Shape (when *storage_path* is provided)::

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

    Shape (when *storage_path* is ``None``)::

        {
            "commands_text": {
                "primary": { "inspect_event": None, ... },
                "specialized": { "trace_event": None, ... },
            },
            "commands_argv": {
                "primary": { "inspect_event": None, ... },
                "specialized": { "trace_event": None, ... },
            },
            "read_only_commands_unavailable": "<explanation>",
        }

    Inspect-first: the primary recommended commands use ``medre inspect``.
    Specialised keys (``trace_event``, ``evidence_bundle``, ``recover_event``)
    are lower-level tools retained for advanced use.

    All read-only inspection commands (inspect, trace, evidence, recover)
    **require** ``--storage-path`` (it is a required argparse argument).  When
    *storage_path* is ``None``, no valid executable commands can be produced,
    so all command values are set to ``None`` and a
    ``read_only_commands_unavailable`` explanation is included.
    """
    _PRIMARY_KEYS = (
        "inspect_event",
        "inspect_timeline",
        "inspect_receipts",
        "inspect_evidence",
        "inspect_recovery",
    )
    _SPECIALIZED_KEYS = (
        "trace_event",
        "evidence_bundle",
        "recover_event",
    )

    # --- No storage_path: cannot emit valid read-only commands -----------
    # All read-only commands (inspect, trace, evidence, recover) have
    # required=True on --storage-path in main.py argparse.  Without it,
    # emitted commands would be invalid (argparse would reject them).
    if not storage_path:
        return {
            "commands_text": {
                "primary": {k: None for k in _PRIMARY_KEYS},
                "specialized": {k: None for k in _SPECIALIZED_KEYS},
            },
            "commands_argv": {
                "primary": {k: None for k in _PRIMARY_KEYS},
                "specialized": {k: None for k in _SPECIALIZED_KEYS},
            },
            "read_only_commands_unavailable": (
                "Read-only inspection commands (inspect, trace, evidence, "
                "recover) require --storage-path.  No persistent storage path "
                "was available at report time."
            ),
        }

    # --- Storage flags (for read-only commands) --------------------------
    ro_flag_argv: list[str] = ["--storage-path", storage_path]

    # Helper: build both argv list and shell-safe text from an argv list.
    def _cmd(argv: list[str]) -> tuple[list[str], str]:
        return argv, shlex.join(argv)

    # --- Primary: inspect-first commands (read-only → storage-path) ---
    primary_argv: dict[str, list[str]] = {}
    primary_text: dict[str, str] = {}

    for key, argv in [
        ("inspect_event", ["medre", "inspect", "event", event_id] + ro_flag_argv),
        (
            "inspect_timeline",
            ["medre", "inspect", "event", event_id, "--timeline"] + ro_flag_argv,
        ),
        (
            "inspect_receipts",
            ["medre", "inspect", "receipts", "--event", event_id] + ro_flag_argv,
        ),
        (
            "inspect_evidence",
            ["medre", "inspect", "event", event_id, "--evidence"] + ro_flag_argv,
        ),
        (
            "inspect_recovery",
            ["medre", "inspect", "event", event_id, "--recovery"] + ro_flag_argv,
        ),
    ]:
        primary_argv[key], primary_text[key] = _cmd(argv)

    # --- Specialised: lower-level tools ---
    specialized_argv: dict[str, list[str]] = {}
    specialized_text: dict[str, str] = {}

    # trace_event and evidence_bundle are read-only → storage-path
    for key, argv in [
        ("trace_event", ["medre", "trace", "event", event_id] + ro_flag_argv),
        (
            "evidence_bundle",
            ["medre", "evidence", "--event", event_id] + ro_flag_argv + ["--json"],
        ),
    ]:
        specialized_argv[key], specialized_text[key] = _cmd(argv)

    # recover_event is read-only → storage-path
    for key, argv in [
        ("recover_event", ["medre", "recover", "--event", event_id] + ro_flag_argv),
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
