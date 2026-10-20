"""Plugin persistence against SQLite: atomicity, recovery, and isolation."""

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import msgspec
import pytest

from medre.core.events import (
    EventMetadata,
    NativeMetadata,
    NativeRef,
    RoutingMetadata,
)
from medre.core.storage.backend import (
    EventNotFoundError,
    PluginExecutionConflictError,
    PluginStateConflictError,
    StorageError,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.plugins import Derive, DerivedEvent, Pass
from medre.plugins.records import MAX_PLUGIN_DEPTH, PluginFailure, state_digest
from tests.helpers.plugin_execution import make_record
from tests.helpers.storage import make_storage_event


async def _admit(storage):
    source = msgspec.structs.replace(
        make_storage_event(),
        source_native_ref=NativeRef(
            adapter="fake_transport",
            native_channel_id="ch-0",
            native_message_id="native-one",
        ),
    )
    await storage.append(source)
    return source


async def test_decision_children_and_state_survive_reopen(tmp_path) -> None:
    path = str(tmp_path / "plugin.sqlite")
    storage = SQLiteStorage(path)
    await storage.initialize()
    source = await _admit(storage)
    values = {"seen": 1}
    record = make_record(
        decision=Derive((DerivedEvent("message.created", {"text": "derived"}),)),
        state_digest=state_digest(values),
    )
    await storage.commit_plugin_execution(record, state=values)
    await storage.close()
    recovered = SQLiteStorage(path)
    await recovered.initialize()
    try:
        assert await recovered.get_plugin_execution(record.key) == record
        state = await recovered.get_plugin_state("text_stats", "one")
        assert state.revision == 1
        assert state.values == values
        child = await recovered.get(record.produced_event_ids[0])
        assert child is not None
        assert child.parent_event_id == source.event_id
        assert child.lineage == (source.event_id,)
        assert child.depth == 1
        assert child.timestamp == record.recorded_at
        assert child.source_adapter == source.source_adapter
        assert child.source_channel_id == source.source_channel_id
        assert child.source_native_ref is None
        assert child.root_event_id == source.event_id
        assert child.payload == {"text": "derived"}
        assert await recovered.get(source.event_id) == source
        assert (
            await recovered._read_one(
                "SELECT 1 FROM durable_ingress_work WHERE event_id=?", (child.event_id,)
            )
            is None
        )
    finally:
        await recovered.close()


async def test_derived_event_uses_its_payload_schema_version(temp_storage) -> None:
    source = await _admit(temp_storage)
    record = make_record(
        decision=Derive(
            (DerivedEvent("plugin.custom", {"subtype": "example"}, schema_version=3),)
        )
    )
    await temp_storage.commit_plugin_execution(record)
    child = await temp_storage.get(record.produced_event_ids[0])
    assert child is not None
    assert source.schema_version == 1
    assert child.schema_version == 3
    assert child.event_kind == "plugin.custom"
    assert child.parent_event_id == source.event_id
    assert await temp_storage.get_plugin_execution(record.key) == record


async def test_derived_children_drop_source_native_and_routing_metadata(
    temp_storage,
) -> None:
    source = msgspec.structs.replace(
        make_storage_event(),
        metadata=EventMetadata(
            native=NativeMetadata(data={"opaque": "adapter"}),
            routing=RoutingMetadata(matched_routes=("route-a",)),
            custom={"sentinel": "keep-me"},
        ),
    )
    await temp_storage.append(source)
    record = make_record(
        decision=Derive((DerivedEvent("plugin.custom", {"subtype": "s"}),))
    )
    await temp_storage.commit_plugin_execution(record)
    child = await temp_storage.get(record.produced_event_ids[0])
    assert child is not None
    assert child.metadata.native is None
    assert child.metadata.routing is None
    assert child.metadata.custom == {"sentinel": "keep-me"}


async def test_exact_commit_retry_does_not_increment_state(temp_storage) -> None:
    await _admit(temp_storage)
    values = {"seen": 1}
    record = make_record(state_digest=state_digest(values))
    assert await temp_storage.commit_plugin_execution(record, state=values) == record
    assert await temp_storage.commit_plugin_execution(record, state=values) == record
    assert (await temp_storage.get_plugin_state("text_stats", "one")).revision == 1
    with pytest.raises(ValueError, match="state digest"):
        await temp_storage.commit_plugin_execution(record, state={"seen": 2})
    with pytest.raises(PluginExecutionConflictError, match="different evidence"):
        await temp_storage.commit_plugin_execution(
            replace(record, decision=Pass("different")), state=values
        )


@pytest.mark.parametrize("table", ["plugin_execution_records", "plugin_state"])
async def test_transaction_failure_rolls_back_events_decisions_and_state(
    temp_storage, table
) -> None:
    await _admit(temp_storage)
    # Fault injection after derived insertion exercises the real transaction;
    # no mock of commit/rollback or the event storage implementation.
    await temp_storage._write(
        f"CREATE TEMP TRIGGER reject_plugin_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected storage fault'); END"
    )
    values = {"seen": 1}
    record = make_record(
        decision=Derive((DerivedEvent("plugin.custom", {"subtype": "s"}),)),
        state_digest=state_digest(values),
    )
    with pytest.raises(StorageError, match="transaction failed"):
        await temp_storage.commit_plugin_execution(record, state=values)
    assert await temp_storage.get_plugin_execution(record.key) is None
    assert await temp_storage.get(record.produced_event_ids[0]) is None
    assert (await temp_storage.get_plugin_state("text_stats", "one")).revision == 0
    await temp_storage._write("DROP TRIGGER reject_plugin_write")
    await temp_storage.commit_plugin_execution(record, state=values)
    assert await temp_storage.get(record.produced_event_ids[0]) is not None


async def test_instance_type_and_replay_state_are_isolated(temp_storage) -> None:
    await _admit(temp_storage)
    first = make_record()
    keys = [
        first.key,
        replace(first.key, instance_id="two"),
        replace(first.key, plugin_id="other"),
        replace(first.key, scope="replay:one"),
    ]
    for index, key in enumerate(keys):
        values = {"index": index}
        await temp_storage.commit_plugin_execution(
            make_record(key=key, state_digest=state_digest(values)), state=values
        )
    for index, key in enumerate(keys):
        state = await temp_storage.get_plugin_state(
            key.plugin_id, key.instance_id, scope=key.scope
        )
        assert state.revision == 1
        assert state.values == {"index": index}
    assert (await temp_storage.get_plugin_state("other", "absent")).revision == 0


async def test_concurrent_connections_cannot_commit_stale_state(tmp_path) -> None:
    path = str(tmp_path / "plugin.sqlite")
    one, two = SQLiteStorage(path), SQLiteStorage(path)
    await one.initialize()
    await two.initialize()
    try:
        await one.append(make_storage_event("evt-1"))
        await one.append(make_storage_event("evt-2"))
        values = {"written": True}
        first = make_record(state_digest=state_digest(values))
        second = make_record(
            key=replace(first.key, source_event_id="evt-2"),
            state_digest=state_digest(values),
        )
        results = await asyncio.gather(
            one.commit_plugin_execution(first, state=values),
            two.commit_plugin_execution(second, state=values),
            return_exceptions=True,
        )
        assert (
            sum(isinstance(result, PluginStateConflictError) for result in results) == 1
        )
        assert sum(result in (first, second) for result in results) == 1
        assert (await one.get_plugin_state("text_stats", "one")).revision == 1
        loser = second if isinstance(results[1], PluginStateConflictError) else first
        assert await one.get_plugin_execution(loser.key) is None
        await one.commit_plugin_execution(
            replace(loser, state_revision=1), state=values
        )
        assert (await one.get_plugin_state("text_stats", "one")).revision == 2
    finally:
        await one.close()
        await two.close()


async def test_transient_attempt_can_retry_but_success_cannot_be_recomputed(
    temp_storage,
) -> None:
    await _admit(temp_storage)
    failed = make_record(
        decision=None, failure=PluginFailure.TIMEOUT, failure_reason="deadline"
    )
    await temp_storage.commit_plugin_execution(failed)
    assert await temp_storage.get_plugin_execution(failed.key) == failed
    with pytest.raises(PluginExecutionConflictError, match="configuration changed"):
        await temp_storage.commit_plugin_execution(
            make_record(attempt=2, state_revision=0, plugin_version="2")
        )
    succeeded = make_record(attempt=2, state_revision=0)
    await temp_storage.commit_plugin_execution(succeeded)
    assert await temp_storage.get_plugin_execution(succeeded.key) == succeeded
    with pytest.raises(PluginExecutionConflictError, match="replay scope"):
        await temp_storage.commit_plugin_execution(
            make_record(attempt=3, state_revision=0)
        )
    replay_key = replace(succeeded.key, scope="replay:operator")
    await temp_storage.commit_plugin_execution(
        make_record(key=replay_key, plugin_version="2")
    )
    assert (
        await temp_storage.get_plugin_execution(failed.key)
    ).plugin_version == "1.0.0"


async def test_failed_invocation_cannot_commit_state(temp_storage) -> None:
    await _admit(temp_storage)
    failed = make_record(
        decision=None, failure=PluginFailure.TRANSIENT, failure_reason="unavailable"
    )
    with pytest.raises(ValueError, match="failed plugin execution"):
        await temp_storage.commit_plugin_execution(failed, state={"uncommitted": True})
    assert await temp_storage.get_plugin_execution(failed.key) is None


async def test_attempt_sequence_and_missing_source_fail_closed(temp_storage) -> None:
    with pytest.raises(EventNotFoundError):
        await temp_storage.commit_plugin_execution(make_record())
    await _admit(temp_storage)
    with pytest.raises(PluginExecutionConflictError, match="attempt sequence"):
        await temp_storage.commit_plugin_execution(make_record(attempt=2))


async def test_lineage_overflow_has_no_partial_commit(temp_storage) -> None:
    source = msgspec.structs.replace(make_storage_event(), depth=MAX_PLUGIN_DEPTH)
    await temp_storage.append(source)
    record = make_record(decision=Derive((DerivedEvent("plugin.custom"),)))
    with pytest.raises(ValueError, match="lineage depth"):
        await temp_storage.commit_plugin_execution(record)
    assert await temp_storage.get_plugin_execution(record.key) is None
    assert await temp_storage.get(record.produced_event_ids[0]) is None


async def test_corrupt_evidence_is_not_treated_as_absent_work(temp_storage) -> None:
    await _admit(temp_storage)
    record = make_record()
    await temp_storage.commit_plugin_execution(record)
    data = record.to_data()
    data["produced_event_ids"] = ["forged"]
    # Test-only fault injection: production exposes no record update method.
    await temp_storage._write(
        "UPDATE plugin_execution_records SET record_json=? WHERE record_id=?",
        (json.dumps(data), record.record_id),
    )
    with pytest.raises(StorageError, match="does not match its contract"):
        await temp_storage.get_plugin_execution(record.key)


async def test_unreadable_plan_evidence_is_a_storage_failure(temp_storage) -> None:
    await _admit(temp_storage)
    record = make_record()
    await temp_storage.commit_plugin_execution(record)
    data = record.to_data()
    data["produced_event_ids"] = ["forged"]
    # Test-only fault injection: production exposes no record update method.
    await temp_storage._write(
        "UPDATE plugin_execution_records SET record_json=? WHERE record_id=?",
        (json.dumps(data), record.record_id),
    )
    await temp_storage._write("DELETE FROM plugin_execution_plans")
    with pytest.raises(StorageError, match="plan evidence is unreadable"):
        await temp_storage.bind_plugin_plan("evt-1", "live", "b" * 64)


async def test_additive_record_table_preserves_existing_database(tmp_path) -> None:
    path = str(tmp_path / "legacy.sqlite")
    storage = SQLiteStorage(path)
    await storage.initialize()
    await storage.append(make_storage_event())
    await storage.close()
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE plugin_execution_records")
    # Read-only inspection must not force an upgrade or create the table.
    readonly = await SQLiteStorage.open_readonly(path)
    assert await readonly.get("evt-1") is not None
    await readonly.close()
    with closing(sqlite3.connect(path)) as db:
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='plugin_execution_records'"
            ).fetchone()
            is None
        )
    await storage.initialize()
    try:
        assert await storage.get("evt-1") is not None
        await storage.commit_plugin_execution(make_record())
    finally:
        await storage.close()


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE plugin_execution_records (record_id TEXT PRIMARY KEY)",
        "CREATE TABLE plugin_execution_records (record_id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL, plugin_id TEXT NOT NULL, instance_id TEXT NOT NULL, stage TEXT NOT NULL, scope TEXT NOT NULL, attempt INTEGER NOT NULL, record_json TEXT NOT NULL, recorded_at TEXT NOT NULL)",
    ],
)
async def test_existing_partial_plugin_schema_is_rejected(tmp_path, ddl) -> None:
    path = str(tmp_path / "bad-shape.sqlite")
    with closing(sqlite3.connect(path)) as db:
        db.execute(ddl)
    storage = SQLiteStorage(path)
    with pytest.raises(StorageError, match="plugin_execution_records"):
        await storage.initialize()


async def test_source_execution_plan_cannot_change_during_recovery(
    temp_storage,
) -> None:
    await _admit(temp_storage)
    first = make_record()
    await temp_storage.commit_plugin_execution(first)
    other_instance = replace(first.key, instance_id="two")
    changed = make_record(key=other_instance, plan_digest="c" * 64)
    with pytest.raises(PluginExecutionConflictError, match="plan changed"):
        await temp_storage.commit_plugin_execution(changed)
    assert await temp_storage.get_plugin_execution(other_instance) is None
    await temp_storage.commit_plugin_execution(make_record(key=other_instance))


async def test_cancelled_commit_is_recovered_from_durable_evidence(
    temp_storage,
) -> None:
    import threading

    await _admit(temp_storage)
    entered = threading.Event()
    release = threading.Event()

    def hold_before_record():
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test release was not signalled")
        return 0

    await temp_storage._run_in_thread(
        temp_storage._require_db().create_function,
        "hold_plugin_record",
        0,
        hold_before_record,
    )
    await temp_storage._write(
        "CREATE TEMP TRIGGER hold_record BEFORE INSERT ON plugin_execution_records BEGIN SELECT hold_plugin_record(); END"
    )
    values = {"seen": 1}
    record = make_record(
        decision=Derive((DerivedEvent("plugin.custom", {"subtype": "s"}),)),
        state_digest=state_digest(values),
    )
    task = asyncio.create_task(
        temp_storage.commit_plugin_execution(record, state=values)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    # The synchronous writer may finish after its awaiter is cancelled. A
    # queued read observes its committed outcome before a retry can mutate it.
    assert await temp_storage.get_plugin_execution(record.key) == record
    assert await temp_storage.get(record.produced_event_ids[0]) is not None
    assert await temp_storage.commit_plugin_execution(record, state=values) == record
    assert (await temp_storage.get_plugin_state("text_stats", "one")).revision == 1


async def test_decisions_without_state_changes_do_not_write_plugin_state(
    temp_storage,
) -> None:
    await _admit(temp_storage)
    record = make_record()
    await temp_storage.commit_plugin_execution(record)
    assert (await temp_storage.get_plugin_state("text_stats", "one")).revision == 0
    assert (
        await temp_storage._read_one(
            "SELECT value FROM plugin_state WHERE plugin_id=?",
            ("text_stats",),
        )
        is None
    )


async def test_read_only_decision_preserves_existing_state(temp_storage) -> None:
    await _admit(temp_storage)
    state = {"cursor": "A"}
    initial = make_record(state_digest=state_digest(state))
    await temp_storage.commit_plugin_execution(initial, state=state)
    await temp_storage.append(make_storage_event("evt-2"))
    second = make_record(
        key=replace(initial.key, source_event_id="evt-2"),
        state_revision=1,
    )
    await temp_storage.commit_plugin_execution(second)
    after = await temp_storage.get_plugin_state("text_stats", "one")
    assert after.revision == 1
    assert after.values == state


async def test_failed_attempt_keeps_state_revision_and_allows_unchanged_retry(
    temp_storage,
) -> None:
    await _admit(temp_storage)
    failed = make_record(
        decision=None, failure=PluginFailure.TRANSIENT, failure_reason="temporary"
    )
    await temp_storage.commit_plugin_execution(failed)
    assert (await temp_storage.get_plugin_state("text_stats", "one")).revision == 0
    retry = make_record(attempt=2, state_revision=0)
    await temp_storage.commit_plugin_execution(retry)
    assert await temp_storage.get_plugin_execution(retry.key) == retry
    assert (await temp_storage.get_plugin_state("text_stats", "one")).revision == 0


async def test_near_cap_failure_reason_does_not_poison_other_instances(
    temp_storage,
) -> None:
    from medre.core.observability.sanitization import (
        _MAX_ERROR_DETAIL_LEN,
        REDACTED_TOKEN,
    )

    await _admit(temp_storage)
    reason = (
        "!" * (_MAX_ERROR_DETAIL_LEN - len(REDACTED_TOKEN)) + "token=" + "secret" * 50
    )
    record = make_record(
        decision=None, failure=PluginFailure.TRANSIENT, failure_reason=reason
    )
    await temp_storage.commit_plugin_execution(record)
    assert await temp_storage.get_plugin_execution(record.key) == record
    await temp_storage.commit_plugin_execution(
        make_record(key=replace(record.key, instance_id="two"))
    )


async def test_truncated_empty_secret_assignment_survives_sqlite_recovery(
    temp_storage,
) -> None:
    await _admit(temp_storage)
    record = make_record(
        decision=None,
        failure=PluginFailure.TRANSIENT,
        failure_reason="!" * 500 + "token=" + " " * 100,
    )
    await temp_storage.commit_plugin_execution(record)
    assert await temp_storage.get_plugin_execution(record.key) == record
