"""Settled canonical content fixtures shared by policy and operator tests."""

import msgspec

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def seed_eligible_content(
    store: SQLiteStorage,
    event_id: str,
    created_at: str,
    body: str | None = None,
) -> None:
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id),
        payload={"body": f"DO-NOT-DELETE-{event_id}" if body is None else body},
        metadata={},
    )
    await store.append(event)
    await store._write(
        "UPDATE canonical_events SET created_at=? WHERE event_id=?",
        (created_at, event_id),
    )
    await store._write(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind='append'",
        ("2026-10-31T01:00:00+00:00", event_id),
    )
