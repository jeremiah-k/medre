"""Foreign-key enforcement tests for the SQLite storage layer.

Covers:
- ``PRAGMA foreign_keys=ON`` is set on write connections (sync and aiosqlite).
- The ``delivery_outbox.event_id`` foreign key to ``canonical_events(event_id)``
  is enforced at the DB level and surfaces as ``StorageError``.
- Happy-path outbox creation still works when the parent event is admitted
  first.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    PreReleaseSchemaConstraintMismatchError,
    StorageError,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item


def _make_event(event_id: str = "evt-fk-1") -> CanonicalEvent:
    """Build a minimal canonical event for FK tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="fake_transport",
        source_transport_id="t1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "fk test"},
        metadata=EventMetadata(),
    )


# ===================================================================
# PRAGMA foreign_keys enforcement
# ===================================================================


class TestForeignKeysEnabled:
    """``PRAGMA foreign_keys=ON`` is set on write connections."""

    async def test_foreign_keys_on_write_connection(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A write connection reports ``foreign_keys=1``."""
        row = await temp_storage._read_one("PRAGMA foreign_keys")
        assert row is not None
        # PRAGMA returns integer 0/1 in SQLite.
        assert int(list(row.values())[0]) == 1

    async def test_foreign_keys_off_on_readonly_connection(
        self, tmp_path: Path
    ) -> None:
        """Read-only inspectors do not enable write-side FK enforcement."""
        db_path = str(tmp_path / "test.db")
        writer = SQLiteStorage(db_path=db_path)
        await writer.initialize()
        await writer.close()

        reader = await SQLiteStorage.open_readonly(db_path)
        try:
            row = await reader._read_one("PRAGMA foreign_keys")
            assert row is not None
            assert int(next(iter(row.values()))) == 0
        finally:
            await reader.close()


# ===================================================================
# delivery_outbox.event_id FK enforcement
# ===================================================================


class TestDeliveryOutboxForeignKey:
    """``delivery_outbox.event_id`` REFERENCES ``canonical_events(event_id)``."""

    async def test_unknown_event_id_raises_storage_error(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inserting an outbox row whose event_id has no canonical row
        fails with ``StorageError`` (the underlying ``IntegrityError``
        is surfaced as a ``StorageError``)."""
        # No prior event admission — outbox row points at a non-existent event.
        item = make_outbox_item(
            delivery_plan_id="plan-orphan-fk",
        ).__class__(
            outbox_id="obox-orphan-fk",
            event_id="evt-does-not-exist",
            route_id="route-orphan-fk",
            delivery_plan_id="plan-orphan-fk",
            target_adapter="fake_presentation",
            target_channel="ch-0",
            attempt_number=1,
            status="pending",
        )
        with pytest.raises(StorageError) as exc_info:
            await temp_storage.create_outbox_item(item)
        # Must NOT be a DuplicateEventError — this is an FK violation.
        from medre.core.storage.backend import DuplicateEventError

        assert not isinstance(exc_info.value, DuplicateEventError)

    async def test_happy_path_outbox_create_succeeds(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """An outbox row that references a real canonical event is accepted."""
        event = _make_event(event_id="evt-fk-happy")
        await temp_storage.append(event)

        item = make_outbox_item(
            delivery_plan_id="plan-fk-happy",
        ).__class__(
            outbox_id="obox-fk-happy",
            event_id="evt-fk-happy",
            route_id="route-fk-happy",
            delivery_plan_id="plan-fk-happy",
            target_adapter="fake_presentation",
            target_channel="ch-0",
            attempt_number=1,
            status="pending",
        )
        created = await temp_storage.create_outbox_item(item)
        assert created.outbox_id == "obox-fk-happy"
        assert created.event_id == "evt-fk-happy"

    async def test_schema_declares_fk_clause(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """The delivery_outbox.event_id column has a REFERENCES clause
        in the actual on-disk schema (defence-in-depth against accidental
        DDL drift)."""
        rows = await temp_storage._read_all(
            "SELECT * FROM pragma_foreign_key_list('delivery_outbox')"
        )
        assert rows, "delivery_outbox must declare at least one FK"
        assert any(
            row["table"] == "canonical_events"
            and row["from"] == "event_id"
            and row["to"] == "event_id"
            for row in rows
        )

    async def test_existing_outbox_without_fk_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """A stamped prerelease DB cannot masquerade as the current FK shape."""
        db_path = tmp_path / "missing-outbox-fk.db"
        writer = SQLiteStorage(str(db_path))
        await writer.initialize()
        await writer.close()

        with sqlite3.connect(db_path) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                "ALTER TABLE delivery_outbox RENAME TO delivery_outbox_current"
            )
            original_sql = db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='delivery_outbox_current'"
            ).fetchone()[0]
            without_fk = original_sql.replace(
                " REFERENCES canonical_events(event_id)", ""
            ).replace("delivery_outbox_current", "delivery_outbox", 1)
            db.execute(without_fk)
            db.execute("DROP TABLE delivery_outbox_current")
            db.commit()

        storage = SQLiteStorage(str(db_path))
        with pytest.raises(
            PreReleaseSchemaConstraintMismatchError, match="event_id"
        ):
            await storage.initialize()
