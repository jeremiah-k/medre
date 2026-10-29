"""Read-only operator decision audit; actor labels are provenance, not auth."""

from __future__ import annotations

import sys

from .exit_codes import EXIT_NOT_FOUND
from .json import to_json
from .storage_helpers import _open_readonly_storage


async def _inspect_decisions(event_id: str, *, storage_path: str) -> None:
    """Display only the persisted cancellation record; never authorize writes."""
    storage = await _open_readonly_storage(storage_path)
    missing = False
    try:
        if not await storage.event_exists(event_id):
            missing = True
        else:
            rows = await storage.list_operator_decisions(event_id)
            print(
                to_json(
                    {
                        "version": 1,
                        "event_id": event_id,
                        "actor_labels_authenticated": False,
                        "decisions": [
                            {
                                "decision_id": row["decision_id"],
                                "action": row["action"],
                                "outbox_id": row["outbox_id"],
                                "delivery_plan_id": row["delivery_plan_id"],
                                "target_adapter": row["target_adapter"],
                                "target_channel": row["target_channel"],
                                "observed_generation": row["observed_generation"],
                                "observed_status": row["observed_status"],
                                "observed_updated_at": row["observed_updated_at"],
                                "recorded_at": row["recorded_at"],
                                "actor_label": row["actor_id"],
                            }
                            for row in rows
                        ],
                    }
                )
            )
    finally:
        await storage.close()
    if missing:
        print(f"Error: event not found: {event_id}", file=sys.stderr)
        raise SystemExit(EXIT_NOT_FOUND)
