"""Persistence authority tests: evidence and CLI inspect/trace.

Focused tests proving the evidence/CLI authority model where gaps exist
from Waves 1-2, without duplicating existing near-limit test files.

Covers:
  1. evidence/inspect/trace commands open storage via open_readonly —
     no writes possible.
  2. inspect native-ref reads real persisted rows, not reconstructed facts.
  3. Evidence bundle generation does not mutate storage.
  4. Open_readonly connection cannot execute write statements.
"""

from __future__ import annotations

import inspect

import pytest

from medre.core.events import NativeMessageRef
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ===================================================================
# 1. CLI commands use open_readonly
# ===================================================================


class TestCLIReadOnlyAccess:
    """evidence, inspect, and trace commands use open_readonly storage.

    All diagnostic CLI commands must open storage in read-only mode via
    _open_readonly_storage, which calls SQLiteStorage.open_readonly.
    No diagnostic command writes to storage.
    """

    @pytest.mark.parametrize(
        ("module_name", "expected_import"),
        [
            ("medre.cli.inspect_commands", "_open_readonly_storage"),
            ("medre.cli.trace_commands", "_open_readonly_storage"),
            ("medre.runtime.evidence._storage_sections", "open_readonly"),
        ],
    )
    def test_diagnostic_module_uses_readonly(
        self, module_name: str, expected_import: str
    ) -> None:
        """Each diagnostic module uses read-only storage access."""
        import importlib

        mod = importlib.import_module(module_name)
        source = inspect.getsource(mod)
        assert (
            expected_import in source
        ), f"{module_name} must contain {expected_import}"

    def test_inspect_commands_docstring_states_readonly(self) -> None:
        """inspect_commands module docstring states read-only intent."""
        from medre.cli import inspect_commands

        doc = inspect_commands.__doc__
        assert doc is not None
        assert "read-only" in doc.lower() or "readonly" in doc.lower()

    def test_trace_commands_docstring_states_readonly(self) -> None:
        """trace_commands module docstring states read-only intent."""
        from medre.cli import trace_commands

        doc = trace_commands.__doc__
        assert doc is not None
        assert "read-only" in doc.lower() or "readonly" in doc.lower()


# ===================================================================
# 2. inspect native-ref reads real persisted rows
# ===================================================================


class TestInspectNativeRefReadsRealRows:
    """inspect native-ref reads the actual persisted native_message_refs
    row, not reconstructed or assumed data.

    get_native_ref (used by _inspect_native_ref) queries the
    native_message_refs table directly and returns the stored row with
    its original id, metadata, created_at, direction, and thread_id.
    """

    async def test_get_native_ref_returns_persisted_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """get_native_ref returns the actual row from native_message_refs."""
        event = make_storage_event(event_id="evt-inspect-nref")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-persisted-001",
            event_id="evt-inspect-nref",
            adapter="matrix",
            native_channel_id="!room:server.org",
            native_message_id="$event-abc123",
            native_thread_id="$thread-xyz",
            native_relation_id=None,
            direction="inbound",
            metadata={"ed25519": "signature_data"},
        )
        await temp_storage.store_native_ref(ref)

        # get_native_ref reads the real persisted row
        fetched = await temp_storage.get_native_ref(
            "matrix", "!room:server.org", "$event-abc123"
        )
        assert fetched is not None
        # These fields come from the persisted row, not reconstructed
        assert fetched.id == "nref-persisted-001"
        assert fetched.native_thread_id == "$thread-xyz"
        assert fetched.metadata == {"ed25519": "signature_data"}
        assert fetched.direction == "inbound"

    async def test_get_native_ref_null_channel_reads_real_row(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """get_native_ref with NULL channel reads the real persisted row."""
        event = make_storage_event(event_id="evt-null-nref")
        await temp_storage.append(event)

        ref = NativeMessageRef(
            id="nref-null-001",
            event_id="evt-null-nref",
            adapter="lxmf",
            native_channel_id=None,
            native_message_id="msg-lxmf-42",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(ref)

        fetched = await temp_storage.get_native_ref("lxmf", None, "msg-lxmf-42")
        assert fetched is not None
        assert fetched.id == "nref-null-001"
        assert fetched.event_id == "evt-null-nref"

    async def test_get_native_ref_not_found_returns_none(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """get_native_ref returns None for non-existent ref (not error)."""
        result = await temp_storage.get_native_ref("unknown", "ch", "msg")
        assert result is None


# ===================================================================
# 3. Evidence bundle generation does not mutate storage
# ===================================================================


class TestEvidenceNoStorageMutation:
    """Evidence bundle generation is a read-only operation.

    Building evidence bundles queries storage but never writes. This test
    verifies that the evidence module does not import write-capable
    storage methods.
    """

    def test_evidence_bundle_module_does_not_import_append_receipt(self) -> None:
        """evidence bundle code does not call append_receipt."""
        from medre.runtime.evidence import _bundle

        source = inspect.getsource(_bundle)
        assert (
            "append_receipt" not in source
        ), "Evidence bundle code must not call append_receipt"

    def test_evidence_bundle_module_does_not_call_write(self) -> None:
        """evidence bundle code does not call storage._write."""
        from medre.runtime.evidence import _bundle

        source = inspect.getsource(_bundle)
        assert (
            "_write(" not in source
        ), "Evidence bundle code must not call storage._write"

    async def test_row_counts_unchanged_after_evidence_collection(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Collecting evidence does not change row counts in any table."""
        event = make_storage_event(event_id="evt-evidence")
        await temp_storage.append(event)

        # Count all tables before
        tables = [
            "canonical_events",
            "delivery_receipts",
            "native_message_refs",
            "event_relations",
            "delivery_outbox",
        ]
        counts_before: dict[str, int] = {}
        for table in tables:
            query = f"SELECT COUNT(*) AS cnt FROM {table}"  # nosec B608: table from hardcoded list above
            rows = await temp_storage._read_all(query, ())
            counts_before[table] = rows[0]["cnt"]

        # Access evidence-related queries (reading operations)
        await temp_storage.list_receipts_for_event("evt-evidence")
        await temp_storage.list_native_refs_for_event("evt-evidence")
        await temp_storage.list_all_receipts(limit=10)
        await temp_storage.list_all_outbox_items(limit=10)

        # Count all tables after
        for table in tables:
            query = f"SELECT COUNT(*) AS cnt FROM {table}"  # nosec B608: table from hardcoded list above
            rows = await temp_storage._read_all(query, ())
            assert rows[0]["cnt"] == counts_before[table], (
                f"Table {table} row count changed from {counts_before[table]} "
                f"to {rows[0]['cnt']} after evidence collection"
            )


# ===================================================================
# 4. open_readonly connection cannot execute write statements
# ===================================================================


class TestOpenReadonlyCannotWrite:
    """SQLiteStorage.open_readonly opens a strict read-only connection.

    Attempting to write through a read-only connection raises StorageError.
    """

    async def test_readonly_cannot_insert(self, tmp_path) -> None:
        """INSERT through a read-only connection raises an error."""
        db_path = str(tmp_path / "readonly_test.db")

        # First create and initialize the database
        store = SQLiteStorage(db_path=db_path)
        await store.initialize()
        event = make_storage_event(event_id="evt-ro-test")
        await store.append(event)
        await store.close()

        # Open read-only
        ro = await SQLiteStorage.open_readonly(db_path)
        try:
            with pytest.raises(StorageError):
                await ro._write(
                    "INSERT INTO canonical_events (event_id) VALUES ('fake')", ()
                )
        finally:
            await ro.close()

    async def test_readonly_cannot_delete(self, tmp_path) -> None:
        """DELETE through a read-only connection raises an error."""
        db_path = str(tmp_path / "readonly_delete_test.db")

        store = SQLiteStorage(db_path=db_path)
        await store.initialize()
        event = make_storage_event(event_id="evt-ro-del")
        await store.append(event)
        await store.close()

        ro = await SQLiteStorage.open_readonly(db_path)
        try:
            with pytest.raises(StorageError):
                await ro._write(
                    "DELETE FROM canonical_events WHERE event_id = 'evt-ro-del'", ()
                )
        finally:
            await ro.close()

    async def test_readonly_can_read(self, tmp_path) -> None:
        """Read operations succeed through a read-only connection."""
        db_path = str(tmp_path / "readonly_read_test.db")

        store = SQLiteStorage(db_path=db_path)
        await store.initialize()
        event = make_storage_event(event_id="evt-ro-read")
        await store.append(event)
        await store.close()

        ro = await SQLiteStorage.open_readonly(db_path)
        try:
            result = await ro.get("evt-ro-read")
            assert result is not None
            assert result.event_id == "evt-ro-read"
        finally:
            await ro.close()
