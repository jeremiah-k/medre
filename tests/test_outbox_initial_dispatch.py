"""Initial obligations retain exact planning and reservation authority."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.retry_plan import reconstruct_retry_delivery_plan
from medre.core.ingress import DurableIngressWorker
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


def _pipeline(store: SQLiteStorage):
    adapters = {
        name: FakePresentationAdapter(adapter_id=name) for name in ("radio", "matrix")
    }
    route = Route(
        id="mapping",
        source=RouteSource("source", ("message.created",), None, "Original mesh"),
        targets=[RouteTarget(name, "out") for name in adapters],
    )
    config = make_pipeline_config_for_pipeline(store, Router([route]), adapters)
    config.route_retry_policies[route.id] = RetryPolicy(max_attempts=4, jitter=False)
    return PipelineRunner(config), route, adapters


async def _stage(store: SQLiteStorage, runner: PipelineRunner, event_id: str = "event"):
    await runner.admit_ingress(
        make_event(event_id=event_id, source_adapter="source"), "live"
    )
    ingress = DurableIngressWorker(storage=store, pipeline=runner, batch_size=1)
    assert await ingress.run_once() == 1
    return await store.list_outbox_items_for_event(event_id)


async def test_staged_work_keeps_exact_plan_and_first_live_attempt(
    temp_storage: SQLiteStorage,
) -> None:
    runner, route, adapters = _pipeline(temp_storage)
    rows = await _stage(temp_storage, runner)
    assert all(
        row.attempt_number == 0
        and row.active_attempt is None
        and row.status == "pending"
        for row in rows
    )
    assert all(not adapter.delivered_payloads for adapter in adapters.values())
    original = reconstruct_retry_delivery_plan(
        item=rows[0], previous_receipt=None, default_max_attempts=99
    )
    assert original.route == route
    assert original.plan.retry_policy == RetryPolicy(max_attempts=4, jitter=False)
    # Mutable route configuration cannot rewrite an accepted obligation.
    route.source = RouteSource("other", (), None, "Changed origin")
    frozen = reconstruct_retry_delivery_plan(
        item=rows[0], previous_receipt=None, default_max_attempts=1
    )
    assert frozen == original
    dispatcher = RetryWorker(
        temp_storage, runner, None, initial_dispatch=True, enabled=False
    )
    await dispatcher._process_due(datetime.now(timezone.utc))
    after = await temp_storage.list_outbox_items_for_event("event")
    assert all(
        row.status == "sent" and row.attempt_number == 1 and row.active_attempt is None
        for row in after
    )
    assert all(len(adapter.delivered_payloads) == 1 for adapter in adapters.values())
    for row in after:
        receipts = await temp_storage.list_receipts_for_outbox(row.outbox_id)
        assert len(receipts) == 1
        assert receipts[0].attempt_number == 1 and receipts[0].source == "live"
    assert dispatcher.state.processed == 0, "Initial dispatch is not a retry"


async def test_adapter_lane_is_not_claimed_behind_an_unexpired_native_owner(
    temp_storage: SQLiteStorage,
) -> None:
    runner, _, _ = _pipeline(temp_storage)
    await _stage(temp_storage, runner, "first")
    await _stage(temp_storage, runner, "second")
    now = datetime.now(timezone.utc)
    first = await temp_storage.claim_due_outbox_items(
        now.isoformat(), "owner", limit=1, one_per_adapter=True
    )
    assert len(first) == 1
    other = await temp_storage.claim_due_outbox_items(
        now.isoformat(), "competitor", limit=20, one_per_adapter=True
    )
    assert len(other) == 1
    assert other[0].target_adapter != first[0].target_adapter
    assert not await temp_storage.claim_due_outbox_items(
        now.isoformat(), "third", limit=20, one_per_adapter=True
    )
    # After expiry each adapter's oldest row is recoverable, rather than a
    # later event overtaking its still-unresolved predecessor.
    expired = await temp_storage.claim_due_outbox_items(
        (now + timedelta(seconds=31)).isoformat(), "resumed", one_per_adapter=True
    )
    assert {row.outbox_id for row in expired} == {
        first[0].outbox_id,
        other[0].outbox_id,
    }


@pytest.mark.parametrize(
    "field,value",
    [("event_id", "foreign"), ("plan_id", "foreign"), ("target_identity", "foreign")],
)
async def test_rebound_stored_plan_is_held_without_native_submission(
    temp_storage: SQLiteStorage, field: str, value: str
) -> None:
    runner, _, adapters = _pipeline(temp_storage)
    rows = await _stage(temp_storage, runner)
    row = rows[0]
    metadata = dict(row.metadata)
    metadata["dispatch_context"]["plan"][field] = value
    await temp_storage._write(
        "UPDATE delivery_outbox SET metadata=? WHERE outbox_id=?",
        (msgspec.json.encode(metadata).decode(), row.outbox_id),
    )
    dispatcher = RetryWorker(
        temp_storage, runner, None, initial_dispatch=True, enabled=False
    )
    await dispatcher._process_due(datetime.now(timezone.utc))
    held = await temp_storage.get_outbox_item(row.outbox_id)
    assert held.status == "abandoned"
    assert held.active_attempt is None
    assert not adapters[row.target_adapter].delivered_payloads
    assert not await temp_storage.list_receipts_for_outbox(row.outbox_id)


async def test_startup_unavailable_defers_without_consuming_first_attempt(
    temp_storage: SQLiteStorage,
) -> None:
    runner, _, adapters = _pipeline(temp_storage)
    await _stage(temp_storage, runner)
    dispatcher = RetryWorker(
        temp_storage,
        runner,
        None,
        initial_dispatch=True,
        enabled=False,
        available_target_adapters=[],
    )
    now = datetime.now(timezone.utc)
    await dispatcher._process_due(now)
    rows = await temp_storage.list_outbox_items_for_event("event")
    assert all(
        row.attempt_number == 0
        and row.active_attempt is None
        and row.status == "retry_wait"
        for row in rows
    )
    dispatcher.set_available_target_adapters(adapters)
    await dispatcher._process_due(now + timedelta(seconds=61))
    after = await temp_storage.list_outbox_items_for_event("event")
    assert all(row.status == "sent" and row.attempt_number == 1 for row in after)


async def test_missing_initial_snapshot_cannot_fall_back_to_guessed_planning(
    temp_storage: SQLiteStorage,
) -> None:
    runner, _, adapters = _pipeline(temp_storage)
    rows = await _stage(temp_storage, runner)
    metadata = dict(rows[0].metadata)
    metadata.pop("dispatch_context")
    await temp_storage._write(
        "UPDATE delivery_outbox SET metadata=? WHERE outbox_id=?",
        (msgspec.json.encode(metadata).decode(), rows[0].outbox_id),
    )
    dispatcher = RetryWorker(
        temp_storage, runner, None, initial_dispatch=True, enabled=False
    )
    await dispatcher._process_due(datetime.now(timezone.utc))
    row = await temp_storage.get_outbox_item(rows[0].outbox_id)
    assert row.status == "abandoned" and row.active_attempt is None
    assert not adapters[row.target_adapter].delivered_payloads


async def test_initial_obligations_survive_reopen_without_native_or_route_replanning(
    tmp_path,
) -> None:
    path = str(tmp_path / "bridge.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    runner, _, adapters = _pipeline(store)
    before = await _stage(store, runner)
    assert all(not adapter.delivered_payloads for adapter in adapters.values())
    await store.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    runner, route, adapters = _pipeline(reopened)
    route.source = RouteSource("replacement-source", (), None, "replacement label")
    try:
        assert await reopened.list_outbox_items_for_event("event") == before
        dispatcher = RetryWorker(
            reopened, runner, None, initial_dispatch=True, enabled=False
        )
        await dispatcher._process_due(datetime.now(timezone.utc))
        after = await reopened.list_outbox_items_for_event("event")
        assert {row.outbox_id for row in after} == {row.outbox_id for row in before}
        assert all(row.status == "sent" and row.attempt_number == 1 for row in after)
        assert all(
            len(adapter.delivered_payloads) == 1 for adapter in adapters.values()
        )
        for row in after:
            receipts = await reopened.list_receipts_for_outbox(row.outbox_id)
            assert len(receipts) == 1 and receipts[0].source == "live"
            plan = reconstruct_retry_delivery_plan(
                item=row, previous_receipt=receipts[0], default_max_attempts=1
            )
            assert plan.route.source.origin_label == "Original mesh"
    finally:
        await reopened.close()


async def test_pending_shape_is_refused_without_rewriting_existing_state(
    tmp_path, monkeypatch
) -> None:
    """A database requiring positive finalized attempts cannot admit staged work."""
    import sqlite3

    import medre.core.storage.sqlite.connection as connection
    from medre.core.storage.backend import PreReleaseSchemaConstraintMismatchError

    path = tmp_path / "positive-attempts.sqlite"
    old_ddl = connection._SCHEMA.replace(
        "CHECK (attempt_number >= 0)", "CHECK (attempt_number >= 1)"
    )
    with monkeypatch.context() as patch:
        patch.setattr(connection, "_SCHEMA", old_ddl)
        old = connection.sync_open(str(path))
        old.close()
    reopened = SQLiteStorage(str(path))
    with pytest.raises(
        PreReleaseSchemaConstraintMismatchError, match="delivery_outbox"
    ):
        await reopened.initialize()
    with sqlite3.connect(path) as check:
        shape = check.execute(
            "SELECT sql FROM sqlite_master WHERE name='delivery_outbox'"
        ).fetchone()[0]
        assert "CHECK (attempt_number >= 1)" in shape
    check.close()


async def test_empty_channel_keeps_normalized_identity_during_dispatch(
    temp_storage: SQLiteStorage,
) -> None:
    runner, route, adapters = _pipeline(temp_storage)
    route.targets = [RouteTarget(name, "") for name in adapters]
    rows = await _stage(temp_storage, runner)
    assert all(row.target_channel is None for row in rows)
    dispatcher = RetryWorker(
        temp_storage, runner, None, initial_dispatch=True, enabled=False
    )
    await dispatcher._process_due(datetime.now(timezone.utc))
    after = await temp_storage.list_outbox_items_for_event("event")
    assert all(row.status == "sent" and row.attempt_number == 1 for row in after)
    assert all(len(adapter.delivered_payloads) == 1 for adapter in adapters.values())


async def test_expired_unreserved_initial_claim_dispatches_without_automatic_retries(
    temp_storage: SQLiteStorage,
) -> None:
    runner, _, adapters = _pipeline(temp_storage)
    await _stage(temp_storage, runner)
    now = datetime.now(timezone.utc)
    claimed = await temp_storage.claim_due_outbox_items(
        now.isoformat(), "interrupted", lease_seconds=30
    )
    assert len(claimed) == 2
    assert all(
        row.attempt_number == 0 and row.active_attempt is None for row in claimed
    )
    dispatcher = RetryWorker(
        temp_storage, runner, None, initial_dispatch=True, enabled=False
    )
    await dispatcher._process_due(now + timedelta(seconds=1))
    assert all(not adapter.delivered_payloads for adapter in adapters.values())
    await dispatcher._process_due(now + timedelta(seconds=31))
    after = await temp_storage.list_outbox_items_for_event("event")
    assert all(row.status == "sent" and row.attempt_number == 1 for row in after)
    assert all(len(adapter.delivered_payloads) == 1 for adapter in adapters.values())


@pytest.mark.parametrize("one_per_adapter", [False, True])
async def test_due_retry_is_not_hidden_by_fresh_pending_work(
    temp_storage: SQLiteStorage, one_per_adapter: bool
) -> None:
    runner, route, _ = _pipeline(temp_storage)
    route.targets = [RouteTarget("radio", "out")]
    original = (await _stage(temp_storage, runner, "retry"))[0]
    now = datetime.now(timezone.utc)
    claimed = await temp_storage.claim_due_outbox_items(
        now.isoformat(), "first", limit=1
    )
    assert claimed[0].outbox_id == original.outbox_id
    assert (
        await temp_storage.reserve_outbox_attempt(original.outbox_id, "first", 0) == 1
    )
    due = (now + timedelta(seconds=2)).isoformat()
    assert await temp_storage.mark_outbox_retry_wait(
        original.outbox_id,
        next_attempt_at=due,
        attempt_number=1,
        expected_worker_id="first",
    )
    for index in range(3):
        await _stage(temp_storage, runner, f"fresh-{index}")
    # Make every fresh obligation newer than the retry's due time using
    # durable fixture timestamps, independently of wall-clock speed.
    await temp_storage._write(
        "UPDATE delivery_outbox SET created_at=? WHERE event_id != ?",
        ((now + timedelta(seconds=3)).isoformat(), "retry"),
    )
    candidate = await temp_storage.claim_due_outbox_items(
        (now + timedelta(seconds=4)).isoformat(),
        "fair",
        limit=1,
        one_per_adapter=one_per_adapter,
    )
    assert len(candidate) == 1
    assert candidate[0].outbox_id == original.outbox_id


async def test_staged_routing_metadata_reaches_rendering_after_reload_and_retry(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from medre.core.contracts.adapter import AdapterSendError
    from medre.core.events.metadata import RoutingMetadata
    from medre.core.rendering.attribution import build_relay_attribution
    from medre.core.rendering.text import TextRenderer

    runner, route, adapters = _pipeline(temp_storage)
    route.targets = [RouteTarget("radio", "out")]
    prior = RoutingMetadata(
        matched_routes=("prior",),
        fanout_group="retained",
        route_trace=tuple(f"prior-{index}" for index in range(16)),
    )
    event = make_event(event_id="routed", source_adapter="source")
    event = msgspec.structs.replace(
        event, metadata=msgspec.structs.replace(event.metadata, routing=prior)
    )
    await runner.admit_ingress(event, "live")
    assert (
        await DurableIngressWorker(storage=temp_storage, pipeline=runner).run_once()
        == 1
    )
    # Routing changes after staging cannot be consulted to rebuild attribution.
    route.source = RouteSource("replacement", (), None, "replacement")
    observed = []

    class RecordingRenderer(TextRenderer):
        async def render(self, event, ctx):
            observed.append(
                (event.metadata.routing, build_relay_attribution(event).route_id)
            )
            return await super().render(event, ctx)

    runner._rendering_pipeline.register(RecordingRenderer(), priority=-100)
    deliver = adapters["radio"].deliver
    calls = 0

    async def fail_once(result):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AdapterSendError("controlled outage")
        return await deliver(result)

    monkeypatch.setattr(adapters["radio"], "deliver", fail_once)
    dispatcher = RetryWorker(
        temp_storage, runner, None, initial_dispatch=True, enabled=True
    )
    now = datetime.now(timezone.utc)
    await dispatcher._process_due(now)
    await dispatcher._process_due(now + timedelta(seconds=61))
    expected = RoutingMetadata(
        matched_routes=("mapping",),
        fanout_group="retained",
        route_trace=(*prior.route_trace[1:], "mapping"),
    )
    assert observed == [(expected, "prior-1"), (expected, "prior-1")]
    assert (await temp_storage.get("routed")).metadata.routing == prior
    rows = await temp_storage.list_outbox_items_for_event("routed")
    assert rows[0].status == "sent" and rows[0].attempt_number == 2


async def test_initial_event_preparation_keeps_the_reserved_claim_alive(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    import medre.core.engine.pipeline.outbox_manager as outbox_manager
    from tests.helpers.async_utils import wait_until

    runner, route, adapters = _pipeline(temp_storage)
    route.targets = [RouteTarget("radio", "out")]
    await _stage(temp_storage, runner)
    entered, release, renewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    resolve = runner._resolve_relations
    renew = temp_storage.renew_outbox_lease

    async def paused_projection(event):
        entered.set()
        await release.wait()
        return await resolve(event)

    async def observed_renewal(*args, **kwargs):
        result = await renew(*args, **kwargs)
        if entered.is_set() and result:
            renewed.set()
        return result

    monkeypatch.setattr(runner, "_resolve_relations", paused_projection)
    monkeypatch.setattr(temp_storage, "renew_outbox_lease", observed_renewal)
    monkeypatch.setattr(outbox_manager, "_OUTBOX_RENEWAL_INTERVAL_SECONDS", 0.01)
    dispatcher = RetryWorker(
        temp_storage, runner, None, initial_dispatch=True, enabled=False
    )
    task = asyncio.create_task(dispatcher._process_due(datetime.now(timezone.utc)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await wait_until(renewed.is_set, timeout=0.5)
        assert not adapters["radio"].delivered_payloads
    finally:
        release.set()
        await task


async def test_unavailable_adapter_cooldown_does_not_walk_its_pending_backlog(
    temp_storage: SQLiteStorage,
) -> None:
    runner, _, adapters = _pipeline(temp_storage)
    await _stage(temp_storage, runner, "first")
    await _stage(temp_storage, runner, "second")
    dispatcher = RetryWorker(
        temp_storage,
        runner,
        None,
        initial_dispatch=True,
        enabled=False,
        available_target_adapters=["matrix"],
        interval_seconds=10,
    )
    now = datetime.now(timezone.utc)
    async with asyncio.TaskGroup() as group:
        await dispatcher._process_due(now, group=group)
    first = await temp_storage.list_outbox_items_for_event("first")
    deferred = next(row for row in first if row.target_adapter == "radio")
    assert datetime.fromisoformat(deferred.next_attempt_at) >= now + timedelta(
        seconds=10
    )
    for offset in (1, 2):
        async with asyncio.TaskGroup() as group:
            await dispatcher._process_due(now + timedelta(seconds=offset), group=group)
    assert dispatcher.state.failed == 1
    second = await temp_storage.list_outbox_items_for_event("second")
    waiting = next(row for row in second if row.target_adapter == "radio")
    assert waiting.status == "pending" and waiting.worker_id is None
    assert not adapters["radio"].delivered_payloads
    assert len(adapters["matrix"].delivered_payloads) == 2


@pytest.mark.parametrize("capacity_error", [False, True])
async def test_initial_capacity_deferral_keeps_poll_interval_floor(
    temp_storage: SQLiteStorage, capacity_error: bool
) -> None:
    from unittest.mock import AsyncMock

    runner, _, adapters = _pipeline(temp_storage)
    await _stage(temp_storage, runner)
    capacity = type("Capacity", (), {"delivery_limit": 4})()
    capacity.acquire_delivery = (
        AsyncMock(side_effect=RuntimeError("capacity unavailable"))
        if capacity_error
        else AsyncMock(return_value=False)
    )
    dispatcher = RetryWorker(
        temp_storage,
        runner,
        capacity,
        initial_dispatch=True,
        enabled=False,
        interval_seconds=10,
    )
    now = datetime.now(timezone.utc)
    async with asyncio.TaskGroup() as group:
        await dispatcher._process_due(now, group=group)
    after = await temp_storage.list_outbox_items_for_event("event")
    assert all(row.attempt_number == 0 and row.active_attempt is None for row in after)
    assert all(
        datetime.fromisoformat(row.next_attempt_at) >= now + timedelta(seconds=10)
        for row in after
    )
    assert all(not adapter.delivered_payloads for adapter in adapters.values())


@pytest.mark.parametrize("rebind", [False, True])
async def test_snapshot_destination_is_bound_to_retained_outbox_address(
    temp_storage: SQLiteStorage,
    rebind: bool,
) -> None:
    from medre.core.planning.delivery_plan import delivery_target_identity
    from medre.core.routing.models import RouteDestination

    runner, route, adapters = _pipeline(temp_storage)
    target = RouteTarget(
        "radio", destination=RouteDestination("contact", "original", "Recipient")
    )
    route.targets = [target]
    rows = await _stage(temp_storage, runner)
    row = rows[0]
    if rebind:
        metadata = dict(row.metadata)
        rebound = RouteTarget(
            "radio", destination=RouteDestination("contact", "foreign", "Recipient")
        )
        snapshot = metadata["dispatch_context"]
        snapshot["plan"]["target"]["destination"]["destination_hash"] = "foreign"
        snapshot["route"]["targets"][0]["destination"]["destination_hash"] = "foreign"
        snapshot["plan"]["target_identity"] = delivery_target_identity(rebound)
        await temp_storage._write(
            "UPDATE delivery_outbox SET metadata=? WHERE outbox_id=?",
            (msgspec.json.encode(metadata).decode(), row.outbox_id),
        )
    dispatcher = RetryWorker(
        temp_storage, runner, None, initial_dispatch=True, enabled=False
    )
    await dispatcher._process_due(datetime.now(timezone.utc))
    held = await temp_storage.get_outbox_item(row.outbox_id)
    assert held.status == ("abandoned" if rebind else "sent")
    assert held.active_attempt is None
    assert len(adapters["radio"].delivered_payloads) == (0 if rebind else 1)
