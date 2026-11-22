"""Retention paging advances past a blocked ceiling-owned candidate.

A ceiling-receipted event whose content was recreated after enforcement is a
blocked, non-mutating collection outcome, and the candidate cursor moves past
it so an eligible relay-only event on the same sweep is still reached and
collected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

_NOW = "2026-11-20T00:00:00+00:00"


async def test_pager_reaches_eligible_event_behind_blocked_residual(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "medre.db"))
    await storage.initialize()
    try:
        epoch = await storage.open_retention_epoch(
            activated_by="operator", now=datetime.now(UTC)
        )
        blocked_event = make_storage_event(
            event_id="evt-blocked",
            payload={"body": "blocked-body"},
            source_adapter="radio",
        )
        await storage.admit_ingress(
            blocked_event,
            None,
            "live",
            content_deadline=datetime.now(UTC) + timedelta(seconds=3600),
        )
        eligible_event = make_storage_event(
            event_id="evt-eligible",
            payload={"body": "eligible-body"},
            source_adapter="radio",
        )
        await storage.admit_ingress(eligible_event, None, "live")
        # Settle the eligible event's admission the way a completed ingress
        # leaves it: work finished and the elapsed mandatory dependency
        # observed as released, so nothing wholesale blocks its collection.
        await storage._write(
            "UPDATE durable_ingress_work SET status = 'completed'"
            " WHERE event_id = 'evt-eligible'",
        )
        await storage._write(
            "UPDATE event_content_dependencies SET released_at = ?"
            " WHERE event_id = 'evt-eligible' AND released_at IS NULL",
            (_NOW,),
        )

        ceiling_row = await storage._read_one(
            "SELECT hard_ceiling FROM event_content_access_policies"
            " WHERE event_id = 'evt-blocked'"
        )
        assert ceiling_row is not None
        ceiling = datetime.fromisoformat(str(ceiling_row["hard_ceiling"]))
        enforced = await storage.enforce_content_ceilings(
            now=ceiling + timedelta(seconds=1)
        )
        assert [summary.event_id for summary in enforced] == ["evt-blocked"]

        # Recreate one content-potential row after the enforcement receipt:
        # the collector must report blocked instead of collecting again.
        await storage._write(
            "INSERT INTO native_message_refs (id, event_id, adapter,"
            " native_message_id, direction, metadata, created_at)"
            " VALUES ('ref-residual', 'evt-blocked', 'radio', 'nm-residual',"
            " 'inbound', '{\"residual\": \"note\"}', ?)",
            (_NOW,),
        )
        blocked = await storage.collect_event_content(
            "evt-blocked", dry_run=False, retention_epoch=epoch.epoch
        )
        assert blocked["status"] == "blocked"
        assert blocked["rows_removed"] == 0
        residual = await storage._read_one(
            "SELECT id FROM native_message_refs WHERE id = 'ref-residual'"
        )
        assert residual is not None

        page_one = await storage.page_collection_candidates(
            after=None, limit=1, watermark=None
        )
        assert page_one.event_ids == ["evt-blocked"]
        assert page_one.has_more is True
        assert page_one.next_after is not None

        page_two = await storage.page_collection_candidates(
            after=page_one.next_after, limit=1, watermark=None
        )
        assert page_two.event_ids == ["evt-eligible"]

        collected = await storage.collect_event_content(
            "evt-eligible", dry_run=False, retention_epoch=epoch.epoch
        )
        assert collected["status"] == "collected"
        assert collected["rows_removed"] > 0

        page_three = await storage.page_collection_candidates(
            after=page_two.next_after, limit=1, watermark=None
        )
        assert page_three.event_ids == []
        assert page_three.has_more is False
    finally:
        await storage.close()
