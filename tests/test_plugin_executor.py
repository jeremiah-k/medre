"""Durable execution across stage decisions, crashes, grants, and async cleanup."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.engine.plugin_executor import (
    PluginExecutionDeferredError,
    PluginExecutionFailedError,
    PluginExecutionLimits,
    PluginExecutor,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.plugins import (
    Allow,
    Deny,
    Derive,
    DerivedEvent,
    Pass,
    PluginCapabilityError,
    PluginStage,
    Supersede,
)
from medre.plugins.records import PluginExecutionKey, PluginFailure
from tests.helpers.plugin_host import binding
from tests.helpers.storage import make_storage_event


async def test_empty_plan_is_frozen_before_any_invocation(temp_storage) -> None:
    await temp_storage.append(make_storage_event())
    empty = PluginExecutor(temp_storage, ())
    await empty.start()
    assert len(await empty.process("evt-1")) == 1
    await empty.stop()
    added = binding("later")
    changed = PluginExecutor(temp_storage, (added,))
    await changed.start()
    try:
        with pytest.raises(
            PluginExecutionFailedError, match="recovery plan or execution evidence"
        ):
            await changed.process("evt-1")
        assert added.implementation.calls == []
    finally:
        assert await changed.stop()


async def test_forward_cursor_replacements_and_policy_preserve_downstream_gates(
    temp_storage,
) -> None:
    await temp_storage.append(make_storage_event())

    async def derive(context, event):
        return Derive((DerivedEvent("message.created", {"text": "child"}),))

    async def transform(context, event):
        return Supersede(
            DerivedEvent("message.created", {"text": event.payload["text"] + "!"})
        )

    async def policy(context, event):
        return Deny("child policy") if event.payload["text"] == "child!" else Allow()

    first, second = binding("first", action=derive), binding("second")
    replacement, gate = binding(
        "replacement", PluginStage.TRANSFORM, transform
    ), binding("gate", PluginStage.EVENT_POLICY, policy)
    host = PluginExecutor(temp_storage, (first, second, replacement, gate))
    await host.start()
    try:
        (event,) = await host.process("evt-1")
        assert event.payload["text"] == "hello!"
        assert event.parent_event_id == "evt-1"
        assert len(first.implementation.calls) == 1
        assert (
            len(second.implementation.calls)
            == len(replacement.implementation.calls)
            == len(gate.implementation.calls)
            == 2
        )
        assert (await temp_storage.get("evt-1")).payload == {"text": "hello"}
        rows = await temp_storage._read_all(
            "SELECT record_json FROM plugin_execution_records WHERE plugin_id='gate'"
        )
        assert any('"kind":"deny"' in row["record_json"] for row in rows)
    finally:
        assert await host.stop()


@pytest.mark.parametrize("decision", [None, object(), Deny("wrong stage")])
async def test_required_invalid_results_have_evidence_and_no_routable_output(
    temp_storage, decision
) -> None:
    await temp_storage.append(make_storage_event())

    async def invalid(context, event):
        return decision

    item = binding("invalid", action=invalid)
    host = PluginExecutor(temp_storage, (item,))
    await host.start()
    try:
        with pytest.raises(
            PluginExecutionFailedError, match="no successful disposition"
        ):
            await host.process("evt-1")
        record = await temp_storage.get_plugin_execution(
            PluginExecutionKey("evt-1", "invalid", "main", PluginStage.ENRICH)
        )
        assert record.failure is PluginFailure.CONTRACT
        assert record.decision is None
        assert record.produced_event_ids == ()
    finally:
        assert await host.stop()


async def test_buffered_state_is_committed_only_with_success(temp_storage) -> None:
    await temp_storage.append(make_storage_event("evt-1"))
    await temp_storage.append(make_storage_event("evt-2"))

    async def count(context, event):
        state = dict(context.state.read())
        state["count"] = state.get("count", 0) + 1
        context.state.write(state)
        if event.event_id == "evt-2":
            raise RuntimeError("unavailable")
        return Pass()

    item = binding("counter", action=count, required=False)
    host = PluginExecutor(temp_storage, (item,))
    await host.start()
    try:
        await host.process("evt-1")
        await host.process("evt-2")
        snapshot = await temp_storage.get_plugin_state("counter", "main")
        assert snapshot.revision == 1 and snapshot.values == {"count": 1}
        with pytest.raises(PluginCapabilityError, match="active invocation"):
            item.implementation.context.state.write({"outside": True})
    finally:
        assert await host.stop()


async def test_committed_decisions_are_reused_after_lost_commit_acknowledgement(
    tmp_path, monkeypatch
) -> None:
    path = str(tmp_path / "crash.sqlite")
    storage = SQLiteStorage(path)
    await storage.initialize()
    await storage.append(make_storage_event())

    async def derive(context, event):
        context.state.write({"seen": True})
        return Derive((DerivedEvent("message.created", {"text": "child"}),))

    first = binding("derive", action=derive)
    host = PluginExecutor(storage, (first,))
    await host.start()
    original = storage.commit_plugin_execution

    async def crash_after_commit(record, **kwargs):
        await original(record, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(storage, "commit_plugin_execution", crash_after_commit)
    with pytest.raises(asyncio.CancelledError):
        await host.process("evt-1")
    await host.stop()
    await storage.close()
    recovered_storage = SQLiteStorage(path)
    await recovered_storage.initialize()
    replacement = binding("derive", action=derive)
    recovered = PluginExecutor(recovered_storage, (replacement,))
    await recovered.start()
    try:
        events = await recovered.process("evt-1")
        assert [event.payload["text"] for event in events] == ["hello", "child"]
        assert replacement.implementation.calls == []
        assert (
            await recovered_storage.get_plugin_state("derive", "main")
        ).revision == 1
    finally:
        await recovered.stop()
        await recovered_storage.close()


async def test_total_expansion_budget_is_checked_before_admission(temp_storage) -> None:
    await temp_storage.append(make_storage_event())

    async def derive(context, event):
        return Derive(
            (
                DerivedEvent("message.created", {"text": "one"}),
                DerivedEvent("message.created", {"text": "two"}),
            )
        )

    host = PluginExecutor(
        temp_storage,
        (binding("large", action=derive),),
        limits=PluginExecutionLimits(max_events=2),
    )
    await host.start()
    try:
        with pytest.raises(PluginExecutionFailedError):
            await host.process("evt-1")
        record = await temp_storage.get_plugin_execution(
            PluginExecutionKey("evt-1", "large", "main", PluginStage.ENRICH)
        )
        assert record.failure is PluginFailure.CONTRACT
        assert record.produced_event_ids == ()
        assert (
            await temp_storage._read_one("SELECT COUNT(*) AS n FROM canonical_events")
        )["n"] == 1
    finally:
        await host.stop()


async def test_transient_retries_are_bounded_and_do_not_quarantine(
    temp_storage,
) -> None:
    await temp_storage.append(make_storage_event())

    async def unavailable(context, event):
        raise RuntimeError("unavailable")

    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    item = binding("unstable", action=unavailable)
    host = PluginExecutor(
        temp_storage,
        (item,),
        limits=PluginExecutionLimits(max_attempts=2),
        clock=lambda: now[0],
    )
    await host.start()
    try:
        for error in (
            PluginExecutionDeferredError,
            PluginExecutionFailedError,
            PluginExecutionFailedError,
        ):
            with pytest.raises(error):
                await host.process("evt-1")
            now[0] += timedelta(seconds=2)
        assert len(item.implementation.calls) == 2
        assert not host._instances[0].quarantined
        assert (await temp_storage.get_plugin_state("unstable", "main")).revision == 0
    finally:
        await host.stop()


async def test_timed_out_late_tasks_cannot_write_state_or_invent_success(
    temp_storage,
) -> None:
    await temp_storage.append(make_storage_event())
    entered, release, late = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def ignore_cancel(context, event):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        with pytest.raises(PluginCapabilityError, match="active invocation"):
            context.state.write({"late": True})
        late.set()
        return Pass()

    item = binding("slow", action=ignore_cancel)
    host = PluginExecutor(
        temp_storage, (item,), limits=PluginExecutionLimits(timeout_seconds=0.05)
    )
    await host.start()
    try:
        with pytest.raises(PluginExecutionDeferredError):
            await host.process("evt-1")
        assert entered.is_set()
        assert not await host.stop()
        release.set()
        await asyncio.wait_for(late.wait(), 1)
        assert await host.stop()
        snapshot = await temp_storage.get_plugin_state("slow", "main")
        assert snapshot.revision == 0 and snapshot.values == {}
    finally:
        release.set()
        await host.stop()


def test_plan_order_limits_and_versions_are_part_of_immutable_identity(
    temp_storage,
) -> None:
    one, two = binding("one"), binding("two")
    baseline = PluginExecutor(temp_storage, (one, two))
    assert baseline.plan_digest != PluginExecutor(temp_storage, (two, one)).plan_digest
    assert (
        baseline.plan_digest
        != PluginExecutor(
            temp_storage, (one, two), limits=PluginExecutionLimits(max_events=2)
        ).plan_digest
    )
    with pytest.raises(AttributeError):
        baseline.plan_digest = "forged"
    with pytest.raises(ValueError, match="duplicate"):
        PluginExecutor(temp_storage, (one, one))


async def test_self_cancelled_plugin_is_a_recorded_failure_not_host_cancellation(
    temp_storage,
) -> None:
    await temp_storage.append(make_storage_event())

    async def cancel_self(context, event):
        raise asyncio.CancelledError

    host = PluginExecutor(temp_storage, (binding("cancelled", action=cancel_self),))
    await host.start()
    try:
        with pytest.raises(PluginExecutionDeferredError):
            await host.process("evt-1")
        record = await temp_storage.get_plugin_execution(
            PluginExecutionKey("evt-1", "cancelled", "main", PluginStage.ENRICH)
        )
        assert record.failure is PluginFailure.TRANSIENT
    finally:
        await host.stop()


async def test_concurrent_sources_serialize_one_instances_state(temp_storage) -> None:
    for name in ["evt-1", "evt-2"]:
        await temp_storage.append(make_storage_event(name))
    entered, release = asyncio.Event(), asyncio.Event()

    async def count(context, event):
        current = context.state.read().get("n", 0)
        if event.event_id == "evt-1":
            entered.set()
            await release.wait()
        context.state.write({"n": current + 1})
        return Pass()

    item = binding("count", action=count)
    host = PluginExecutor(temp_storage, (item,))
    await host.start()
    first = asyncio.create_task(host.process("evt-1"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(host.process("evt-2"))
        release.set()
        await asyncio.gather(first, second)
        snapshot = await temp_storage.get_plugin_state("count", "main")
        assert snapshot.values == {"n": 2}
        assert snapshot.revision == 2
    finally:
        release.set()
        await host.stop()


async def test_optional_quarantine_passes_with_host_skip_evidence(temp_storage) -> None:
    for name in ["evt-1", "evt-2"]:
        await temp_storage.append(make_storage_event(name))

    async def invalid(context, event):
        return Deny("wrong stage")

    item = binding("optional", action=invalid, required=False)
    host = PluginExecutor(temp_storage, (item,))
    await host.start()
    try:
        assert len(await host.process("evt-1")) == 1
        assert len(await host.process("evt-2")) == 1
        assert len(item.implementation.calls) == 1
        record = await temp_storage.get_plugin_execution(
            PluginExecutionKey("evt-2", "optional", "main", PluginStage.ENRICH)
        )
        assert record.failure is PluginFailure.QUARANTINED
        assert isinstance(record.decision, Pass)
        assert "not invoked" in record.failure_reason
    finally:
        await host.stop()


async def test_active_execution_identity_is_stable_across_retries(temp_storage) -> None:
    await temp_storage.append(make_storage_event())
    keys = []

    async def external(context, event):
        keys.append(context.current_execution())
        if len(keys) == 1:
            raise RuntimeError("external acknowledgement lost")
        return Pass()

    item = binding("external", action=external)
    host = PluginExecutor(
        temp_storage, (item,), limits=PluginExecutionLimits(retry_delay_seconds=0)
    )
    await host.start()
    try:
        with pytest.raises(PluginExecutionDeferredError):
            await host.process("evt-1")
        await host.process("evt-1")
        assert len(keys) == 2 and keys[0].idempotency_key == keys[1].idempotency_key
        with pytest.raises(PluginCapabilityError):
            item.implementation.context.current_execution()
    finally:
        await host.stop()
