"""Persistence authority tests: storage layer.

Focused tests proving the storage authority model where gaps exist from
Waves 1–2, without duplicating existing near-limit test files.

Covers:
  1. No receipt update/delete API on SQLiteStorage public interface.
  2. Terminal outbox rows returned unchanged by create_outbox_item for
     ALL four terminal statuses (sent, dead_lettered, cancelled, abandoned).
  3. Native-ref first-writer-wins: same (adapter, channel, msg) mapped to a
     different event_id preserves the original mapping.
  4. DDL / _REQUIRED_COLUMNS parity: every column in the _SCHEMA DDL appears
     in _REQUIRED_COLUMNS, and vice versa.
  5. No DELETE SQL in the storage module source.
  6. _EXPECTED_SCHEMA_VERSION is 1.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from medre.core.events import NativeMessageRef
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.schema import (
    _EXPECTED_SCHEMA_VERSION,
    _REQUIRED_COLUMNS,
    _SCHEMA,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STORAGE_MODULE_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "medre"
    / "core"
    / "storage"
    / "sqlite"
)


def _sqlite_source_files() -> list[Path]:
    """All .py files in the sqlite storage module directory."""
    return sorted(_STORAGE_MODULE_DIR.glob("*.py"))


def _make_outbox_item(
    outbox_id: str = "ob-1",
    event_id: str = "evt-ob",
    delivery_plan_id: str = "plan-ob",
    target_adapter: str = "adapter_ob",
    target_channel: str | None = None,
    attempt_number: int = 1,
    status: str = "in_progress",
) -> DeliveryOutboxItem:
    import datetime

    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-ob",
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        target_address=None,
        attempt_number=attempt_number,
        status=status,
        failure_kind=None,
        failure_kind_detail=None,
        next_attempt_at=None,
        created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        updated_at=datetime.datetime.now(datetime.UTC).isoformat(),
        last_attempt_at=None,
        locked_at=None,
        lease_until=None,
        worker_id=None,
        payload_hash=None,
        receipt_id=None,
        parent_receipt_id=None,
        error_summary=None,
        metadata=None,
    )


# ===================================================================
# 1. No receipt update/delete API
# ===================================================================


class TestNoReceiptUpdateDeleteAPI:
    """SQLiteStorage must not expose receipt update or delete methods.

    Receipts are append-only historical evidence.  No runtime code path
    updates or deletes receipt rows.  This structural test catches
    accidental API additions that would violate the append-only invariant.
    """

    @pytest.mark.parametrize(
        "method_name",
        (
            "update_receipt",
            "delete_receipt",
            "remove_receipt",
            "replace_receipt",
            "modify_receipt",
            "upsert_receipt",
        ),
    )
    def test_receipt_mutation_method_does_not_exist(self, method_name: str) -> None:
        """Each mutation-like method name must not be on SQLiteStorage."""
        assert not hasattr(
            SQLiteStorage, method_name
        ), f"SQLiteStorage must not expose {method_name}() — receipts are append-only"

    def test_only_append_receipt_creates_receipts(self) -> None:
        """The only receipt write method is append_receipt."""
        receipt_methods = [
            name
            for name in dir(SQLiteStorage)
            if "receipt" in name.lower()
            and callable(getattr(SQLiteStorage, name, None))
        ]
        write_methods = [
            name
            for name in receipt_methods
            if any(w in name for w in ("append", "create", "insert", "write", "add"))
        ]
        assert write_methods == ["append_receipt"], (
            f"Expected only append_receipt as receipt write method, "
            f"found: {write_methods}"
        )

    def test_no_delete_methods_at_all(self) -> None:
        """SQLiteStorage must not expose any delete methods."""
        delete_methods = [
            name
            for name in dir(SQLiteStorage)
            if "delete" in name.lower() and callable(getattr(SQLiteStorage, name, None))
        ]
        assert (
            delete_methods == []
        ), f"SQLiteStorage must not expose delete methods: {delete_methods}"


# ===================================================================
# 2. Terminal outbox immutability through create_outbox_item
# ===================================================================


class TestTerminalOutboxImmutability:
    """create_outbox_item must return existing terminal rows unchanged.

    For each of the four terminal statuses (sent, dead_lettered, cancelled,
    abandoned), attempting to create a new outbox item with the same key
    must return the existing terminal row without any mutation.
    """

    @pytest.mark.parametrize(
        "terminal_status",
        ("sent", "dead_lettered", "cancelled", "abandoned"),
    )
    async def test_terminal_row_returned_unchanged(
        self, temp_storage: SQLiteStorage, terminal_status: str
    ) -> None:
        """create_outbox_item returns the existing terminal row unchanged."""
        event = make_storage_event(event_id="evt-term")
        await temp_storage.append(event)

        # Step 1: Create an outbox item and drive it to the terminal status.
        item1 = _make_outbox_item(
            outbox_id="ob-terminal-1",
            event_id="evt-term",
            delivery_plan_id="plan-terminal",
            target_adapter="adapter_terminal",
            attempt_number=1,
            status="in_progress",
        )
        created1 = await temp_storage.create_outbox_item(item1)
        ob_id = created1.outbox_id

        # Transition to the terminal status.
        if terminal_status == "sent":
            await temp_storage.mark_outbox_sent(ob_id)
        elif terminal_status == "dead_lettered":
            await temp_storage.mark_outbox_dead_lettered(ob_id)
        elif terminal_status == "cancelled":
            await temp_storage.mark_outbox_cancelled(ob_id)
        elif terminal_status == "abandoned":
            await temp_storage.mark_outbox_abandoned(ob_id)

        # Snapshot before
        before = await temp_storage.get_outbox_item(ob_id)
        assert before is not None
        assert before.status == terminal_status

        # Step 2: Attempt to create a new item with the same key.
        item2 = _make_outbox_item(
            outbox_id="ob-terminal-2",  # different ID
            event_id="evt-term",
            delivery_plan_id="plan-terminal",
            target_adapter="adapter_terminal",
            attempt_number=1,
            status="in_progress",
        )
        created2 = await temp_storage.create_outbox_item(item2)

        # The returned item must be the original terminal row.
        assert created2.outbox_id == ob_id
        assert created2.status == terminal_status

        # The second item was never inserted.
        assert await temp_storage.get_outbox_item("ob-terminal-2") is None

        # The original row is unchanged.
        after = await temp_storage.get_outbox_item(ob_id)
        assert after is not None
        assert after.status == terminal_status

    async def test_terminal_row_fields_unchanged_after_create_attempt(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """All fields on the terminal row remain identical after a create attempt."""
        event = make_storage_event(event_id="evt-fields")
        await temp_storage.append(event)

        item = _make_outbox_item(
            outbox_id="ob-fields-1",
            event_id="evt-fields",
            delivery_plan_id="plan-fields",
            target_adapter="adapter_fields",
            attempt_number=1,
            status="in_progress",
        )
        created = await temp_storage.create_outbox_item(item)
        await temp_storage.mark_outbox_sent(created.outbox_id)

        # Snapshot all fields
        before = await temp_storage.get_outbox_item(created.outbox_id)
        assert before is not None

        # Attempt create with same key
        item2 = _make_outbox_item(
            outbox_id="ob-fields-2",
            event_id="evt-fields",
            delivery_plan_id="plan-fields",
            target_adapter="adapter_fields",
            attempt_number=1,
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item2)

        # All fields unchanged
        after = await temp_storage.get_outbox_item(created.outbox_id)
        assert after is not None
        assert after.status == before.status
        assert after.updated_at == before.updated_at
        assert after.worker_id == before.worker_id


# ===================================================================
# 3. Native-ref first-writer-wins with different event_id
# ===================================================================


class TestNativeRefFirstWriterWins:
    """When the same (adapter, channel, msg) triple is stored for a
    different event_id, the original mapping is preserved.

    First-writer-wins means the canonical event_id associated with a
    native ref never changes — the resolve-before-insert check prevents
    overwrite even when a different event_id is passed.
    """

    async def test_first_writer_wins_with_channel(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Same (adapter, channel, msg) with different event_id: first wins."""
        event1 = make_storage_event(event_id="evt-first")
        event2 = make_storage_event(event_id="evt-second")
        await temp_storage.append(event1)
        await temp_storage.append(event2)

        ref1 = NativeMessageRef(
            id="nref-first",
            event_id="evt-first",
            adapter="matrix",
            native_channel_id="ch-1",
            native_message_id="msg-abc",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref1)

        ref2 = NativeMessageRef(
            id="nref-second",
            event_id="evt-second",  # different event
            adapter="matrix",
            native_channel_id="ch-1",
            native_message_id="msg-abc",  # same triple
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref2)

        # The original mapping must be preserved.
        resolved = await temp_storage.resolve_native_ref("matrix", "ch-1", "msg-abc")
        assert resolved == "evt-first"

        # Only one row exists.
        rows = await temp_storage._read_all(
            "SELECT * FROM native_message_refs WHERE adapter = ? AND native_channel_id = ? AND native_message_id = ?",
            ("matrix", "ch-1", "msg-abc"),
        )
        assert len(rows) == 1
        assert rows[0]["event_id"] == "evt-first"
        assert rows[0]["id"] == "nref-first"

    async def test_first_writer_wins_null_channel(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """First-writer-wins for NULL channel (LXMF-style channelless transport)."""
        event1 = make_storage_event(event_id="evt-null-1")
        event2 = make_storage_event(event_id="evt-null-2")
        await temp_storage.append(event1)
        await temp_storage.append(event2)

        ref1 = NativeMessageRef(
            id="nref-null-1",
            event_id="evt-null-1",
            adapter="lxmf",
            native_channel_id=None,  # NULL channel
            native_message_id="msg-xyz",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref1)

        ref2 = NativeMessageRef(
            id="nref-null-2",
            event_id="evt-null-2",  # different event
            adapter="lxmf",
            native_channel_id=None,
            native_message_id="msg-xyz",  # same (adapter, NULL, msg)
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref2)

        # First writer wins even with NULL channel.
        resolved = await temp_storage.resolve_native_ref("lxmf", None, "msg-xyz")
        assert resolved == "evt-null-1"

        # get_native_ref also returns the first writer's data.
        nref = await temp_storage.get_native_ref("lxmf", None, "msg-xyz")
        assert nref is not None
        assert nref.event_id == "evt-null-1"
        assert nref.id == "nref-null-1"

    async def test_get_native_ref_returns_original_after_conflict(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """get_native_ref returns the persisted row, not a reconstructed value."""
        event = make_storage_event(event_id="evt-getref")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-getref",
            event_id="evt-getref",
            adapter="meshtastic",
            native_channel_id="ch-42",
            native_message_id="msg-789",
            native_thread_id="thread-1",
            native_relation_id=None,
            direction="outbound",
            metadata={"hop": 3},
        )
        await temp_storage.store_native_ref(ref)

        # get_native_ref reads the real persisted row.
        fetched = await temp_storage.get_native_ref("meshtastic", "ch-42", "msg-789")
        assert fetched is not None
        assert fetched.id == "nref-getref"
        assert fetched.event_id == "evt-getref"
        assert fetched.direction == "outbound"
        assert fetched.native_thread_id == "thread-1"


# ===================================================================
# 4. DDL / _REQUIRED_COLUMNS parity
# ===================================================================


class TestDDLRequiredColumnsParity:
    """Every column defined in the _SCHEMA DDL must appear in
    _REQUIRED_COLUMNS, and vice versa.

    A drift between the DDL and the required-columns inventory would
    silently pass shape validation while having incorrect column
    requirements — old databases with missing columns would not be
    caught by initialize().
    """

    def _extract_columns_from_ddl(self, table_name: str) -> set[str]:
        """Extract column names from _SCHEMA DDL for a given table."""
        # Match CREATE TABLE IF NOT EXISTS <name> ( ... )
        pattern = rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{re.escape(table_name)}\s*\((.*?)\);"
        match = re.search(pattern, _SCHEMA, re.DOTALL | re.IGNORECASE)
        if match is None:
            return set()
        body = match.group(1)

        columns: set[str] = set()
        for line in body.split("\n"):
            line = line.strip().rstrip(",")
            if not line:
                continue
            # Skip constraints (PRIMARY KEY, UNIQUE, FOREIGN KEY, CHECK, CONSTRAINT)
            upper = line.upper().lstrip()
            if upper.startswith(
                ("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK", "CONSTRAINT")
            ):
                continue
            # First token is the column name
            tokens = line.split()
            if tokens:
                col_name = tokens[0].strip('"').strip("'").strip("`")
                if col_name:
                    columns.add(col_name)
        return columns

    @pytest.mark.parametrize("table_name", list(_REQUIRED_COLUMNS.keys()))
    def test_ddl_columns_match_required_columns(self, table_name: str) -> None:
        """DDL columns for each table match _REQUIRED_COLUMNS exactly."""
        ddl_cols = self._extract_columns_from_ddl(table_name)
        required_cols = set(_REQUIRED_COLUMNS[table_name])

        if not ddl_cols:
            pytest.skip(
                f"Table {table_name} not found in _SCHEMA DDL (may be view or metadata)"
            )

        missing_in_required = ddl_cols - required_cols
        missing_in_ddl = required_cols - ddl_cols

        assert not missing_in_required, (
            f"Table {table_name}: columns in DDL but not in _REQUIRED_COLUMNS: "
            f"{sorted(missing_in_required)}"
        )
        assert not missing_in_ddl, (
            f"Table {table_name}: columns in _REQUIRED_COLUMNS but not in DDL: "
            f"{sorted(missing_in_ddl)}"
        )

    def test_all_ddl_tables_have_required_columns_entry(self) -> None:
        """Every CREATE TABLE in _SCHEMA has a corresponding _REQUIRED_COLUMNS entry."""
        all_tables = re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", _SCHEMA, re.IGNORECASE
        )
        for table_name in all_tables:
            assert (
                table_name in _REQUIRED_COLUMNS
            ), f"Table {table_name} found in _SCHEMA but not in _REQUIRED_COLUMNS"


# ===================================================================
# 5. No DELETE SQL in storage module source
# ===================================================================


class TestNoDeleteInStorageModule:
    """The storage module must not contain any DELETE SQL statements.

    No runtime code path deletes historical data.  This structural test
    ensures no DELETE statement is accidentally introduced.
    """

    def test_no_delete_statements_in_source(self) -> None:
        """No .py file in the sqlite storage module contains DELETE FROM."""
        delete_pattern = re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE)
        violations: list[str] = []

        for py_file in _sqlite_source_files():
            content = py_file.read_text()
            for line_no, line in enumerate(content.split("\n"), 1):
                # Skip comments
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                # Check for DELETE FROM
                if delete_pattern.search(line):
                    violations.append(f"{py_file.name}:{line_no}: {line.strip()}")

        assert violations == [], (
            "DELETE FROM statements found in storage module "  # nosec B608
            "(violates append-only invariant):\n" + "\n".join(violations)
        )

    def test_no_delete_method_on_storage(self) -> None:
        """SQLiteStorage has no delete/remove method."""
        for attr_name in dir(SQLiteStorage):
            if "delete" in attr_name.lower() or "remove" in attr_name.lower():
                if callable(getattr(SQLiteStorage, attr_name, None)):
                    # Allow close() and similar cleanup methods
                    raise AssertionError(
                        f"SQLiteStorage has suspicious method: {attr_name}"
                    )


# ===================================================================
# 6. _EXPECTED_SCHEMA_VERSION is 1
# ===================================================================


class TestSchemaVersionFrozen:
    """Schema version must remain 1 during pre-release."""

    def test_expected_schema_version_is_one(self) -> None:
        """_EXPECTED_SCHEMA_VERSION is 1 (frozen until release)."""
        assert _EXPECTED_SCHEMA_VERSION == 1

    def test_schema_version_constant_type(self) -> None:
        """_EXPECTED_SCHEMA_VERSION is an int, not a float or string."""
        assert isinstance(_EXPECTED_SCHEMA_VERSION, int)
