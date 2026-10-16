"""Unavailable persisted ownership must never authorize adapter dispatch."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item


@pytest.mark.parametrize("existing", [False, True])
async def test_create_fails_closed_when_persisted_row_is_unavailable(
    outbox_temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    item = make_outbox_item()
    if existing:
        await outbox_temp_storage.create_outbox_item(item)
    monkeypatch.setattr(
        outbox_temp_storage, "get_outbox_item", AsyncMock(return_value=None)
    )

    with pytest.raises(StorageError, match="outbox row could not be re-read"):
        await outbox_temp_storage.create_outbox_item(item)


async def test_conflict_cannot_return_unpersisted_candidate_as_owner(
    outbox_temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner = make_outbox_item()
    await outbox_temp_storage.create_outbox_item(winner)
    candidate = make_outbox_item()
    assert candidate.outbox_id != winner.outbox_id

    def conflict(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(outbox_temp_storage, "_sync_atomic_create_outbox", conflict)
    monkeypatch.setattr(
        outbox_temp_storage, "get_outbox_item", AsyncMock(return_value=None)
    )
    with pytest.raises(StorageError, match="outbox row could not be re-read"):
        await outbox_temp_storage.create_outbox_item(candidate)
