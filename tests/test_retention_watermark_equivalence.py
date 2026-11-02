"""Equivalent UTC spellings never expand an activation's admission scope."""

from datetime import datetime, timezone

import msgspec
import pytest

from medre.core.storage.backend import ContentCollectionEpochChanged
from tests.helpers.storage import make_storage_event


async def _admit(store, event_id, created_at):
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": "private"}, metadata={}
    )
    await store.append(event)
    await store._write(
        "UPDATE canonical_events SET created_at=? WHERE event_id=?",
        (created_at, event_id),
    )
    await store._write(
        "UPDATE event_content_dependencies SET released_at=? WHERE event_id=?",
        (created_at, event_id),
    )


@pytest.mark.parametrize(
    "boundary,equivalent",
    [
        ("2026-11-01T12:00:00+00:00", "2026-11-01T12:00:00.000000+00:00"),
        ("2026-11-01T12:00:00.000000+00:00", "2026-11-01T12:00:00+00:00"),
    ],
)
async def test_equivalent_watermark_is_excluded_by_preview_selector_and_write(
    temp_storage, boundary, equivalent
):
    await _admit(temp_storage, "boundary", boundary)
    epoch = await temp_storage.open_retention_epoch(
        activated_by="operator", now=datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    )
    assert epoch.admission_watermark == boundary  # Stored facts remain unchanged.
    await _admit(temp_storage, "same-instant", equivalent)
    await _admit(temp_storage, "later", "2026-11-01T12:00:00.000001+00:00")
    page = await temp_storage.page_collection_candidates(
        after=None, limit=10, watermark=epoch.admission_watermark
    )
    assert page.event_ids == ["later"]
    preview = await temp_storage.preview_content_retention(limit=10)
    assert preview["events_examined"] == 1
    assert preview["eligible"] == 1
    with pytest.raises(ContentCollectionEpochChanged):
        await temp_storage.collect_event_content(
            "same-instant", dry_run=False, retention_epoch=epoch.epoch
        )
    assert await temp_storage._read_one(
        "SELECT COUNT(*) AS count FROM event_content_collection_log"
    ) == {"count": 0}
    result = await temp_storage.collect_event_content(
        "later", dry_run=False, retention_epoch=epoch.epoch
    )
    assert result["status"] == "collected"
