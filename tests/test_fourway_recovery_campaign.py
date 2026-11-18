"""SDK-free four-target recovery campaign using real SQLite and pipeline state.

This does not claim BLE, SDK or transport availability qualification. It
proves that a failed target's durable work is independent of healthy targets
across a process-generation boundary.

The held-destination scenario extends this campaign to an actually stalled
native handoff: one destination operation parks behind an explicit barrier at
the adapter boundary while the production background dispatcher runs with its
real lifetime task group. Healthy destinations must complete fresh work during
the hold, restart must preserve exact obligations, and refusal, accepted work
and uncertain handoff must keep their distinct recovery behaviors.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.config.model import RuntimeLimits
from medre.core.contracts.adapter import AdapterHandoffResult, AdapterSendError
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, NativeRef
from medre.core.ingress import DurableIngressWorker
from medre.core.observability.work_status import (
    WORK_STATUS_CATEGORIES,
    assemble_work_status_report,
)
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.content_ceiling import ContentCeilingPolicy
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.capacity import CapacityController
from medre.runtime.retry import RetryWorker
from tests.helpers.async_utils import wait_until
from tests.helpers.pipeline import (
    dispatch_initial_batch,
    make_event,
    make_pipeline_config_for_pipeline,
)


class _OfflineUntilRestart(FakePresentationAdapter):
    """Reject one target before native handoff acceptance."""

    calls = 0

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self.calls += 1
        raise AdapterSendError(
            "radio unavailable before native handoff", retry_after_seconds=3600
        )


def _pipeline(
    store: SQLiteStorage, adapters: dict[str, FakePresentationAdapter]
) -> PipelineRunner:
    route = Route(
        id="fourway",
        source=RouteSource(
            adapter="ingress", channel="room", event_kinds=("message.created",)
        ),
        targets=[RouteTarget(adapter=name, channel="out") for name in adapters],
    )
    config = make_pipeline_config_for_pipeline(store, Router(routes=[route]), adapters)
    config.route_retry_policies[route.id] = RetryPolicy(jitter=False)
    return PipelineRunner(config)


async def test_fourway_fanout_broken_link_does_not_replay_healthy_targets(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "bridge.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    failed = _OfflineUntilRestart(adapter_id="FXLF")
    healthy = {
        name: FakePresentationAdapter(adapter_id=name)
        for name in ("FXLT", "meshcore", "matrix")
    }
    worker = _pipeline(store, {"FXLF": failed, **healthy})
    await worker.start()
    native = NativeRef(
        adapter="ingress", native_channel_id="room", native_message_id="native-101"
    )
    event = make_event(
        event_id="fourway-message",
        source_adapter="ingress",
        source_channel_id="room",
        source_native_ref=native,
    )
    try:
        await worker.admit_ingress(event, "live")
        ingress = DurableIngressWorker(storage=store, pipeline=worker, batch_size=1)
        assert await ingress.run_once() == 1
        await dispatch_initial_batch(store, worker)
        rows = await store.list_outbox_items_for_event(event.event_id)
        assert len(rows) == 4
        waiting = next(x for x in rows if x.target_adapter == "FXLF")
        assert waiting.status == "retry_wait" and waiting.attempt_number == 1
        assert waiting.next_attempt_at is not None
        assert failed.calls == 1 and not failed.delivered_payloads
        assert all(len(a.delivered_payloads) == 1 for a in healthy.values())
        assert all(x.status == "sent" for x in rows if x.target_adapter != "FXLF")
        completed_rows = {
            row.target_adapter: row for row in rows if row.target_adapter != "FXLF"
        }
        completed_receipts = {
            name: await store.list_receipts_for_outbox(row.outbox_id)
            for name, row in completed_rows.items()
        }
        assert all(completed_receipts.values())
    finally:
        await worker.stop()
        await store.close()

    # A new store, pipeline and instances model process restart. Re-decoding
    # the same packet must not create a second set of outbox obligations.
    restored_store = SQLiteStorage(path)
    await restored_store.initialize()
    restored = {
        name: FakePresentationAdapter(adapter_id=name)
        for name in ("FXLF", "FXLT", "meshcore", "matrix")
    }
    resumed = _pipeline(restored_store, restored)
    await resumed.start()
    try:
        duplicate = await resumed.admit_ingress(
            make_event(
                event_id="fourway-redelivery",
                source_adapter="ingress",
                source_channel_id="room",
                source_native_ref=native,
            ),
            "recovered",
        )
        assert duplicate.created is False
        assert duplicate.event_id == event.event_id
        assert await restored_store.get("fourway-redelivery") is None
        assert await restored_store.list_outbox_items_for_event(event.event_id) == rows
        assert all(not adapter.delivered_payloads for adapter in restored.values())

        # Advance scheduling deterministically, without wall-clock sleeps.
        due_at = datetime.fromisoformat(waiting.next_attempt_at) + timedelta(seconds=1)
        retry = RetryWorker(restored_store, resumed, None)
        await retry._process_due(due_at)
        assert retry.state.succeeded == 1
        assert len(restored["FXLF"].delivered_payloads) == 1
        # Repeated recovery polling must not send even the formerly failed
        # target again after its outbox obligation has become terminal.
        await retry._process_due(due_at + timedelta(seconds=5))
        assert retry.state.succeeded == 1
        assert len(restored["FXLF"].delivered_payloads) == 1
        assert all(
            not restored[name].delivered_payloads
            for name in ("FXLT", "meshcore", "matrix")
        )
        after = await restored_store.list_outbox_items_for_event(event.event_id)
        assert len(after) == 4
        assert all(row.status == "sent" for row in after)
        for row in after:
            if row.target_adapter in completed_rows:
                assert row == completed_rows[row.target_adapter]
                assert (
                    await restored_store.list_receipts_for_outbox(row.outbox_id)
                    == completed_receipts[row.target_adapter]
                )
        assert (
            next(row for row in after if row.target_adapter == "FXLF").attempt_number
            == 2
        )
    finally:
        await resumed.stop()
        await restored_store.close()


# ---------------------------------------------------------------------------
# Held-destination isolation under the production background dispatcher
# ---------------------------------------------------------------------------
#
# The barrier sits at the adapter handoff boundary (``AdapterContract.deliver``),
# not inside dispatcher internals: the production pipeline renders, reserves,
# renews its lease and invokes the fake native boundary exactly as it would a
# real SDK. Fake handoff observations prove local acceptance only — never that
# a recipient received anything.

_FANOUT_ADAPTERS = ("held", "lane-a", "lane-b", "lane-c")
_REFUSER_ADAPTER = "refuser"

#: Global delivery capacity configured for the campaign. One slot is occupied
#: by the held operation; the remaining three are the recorded spare capacity
#: that makes concurrent healthy-lane progress a contract at all. A runtime
#: whose only configured slot is occupied cannot promise concurrent progress.
_HELD_DELIVERY_LIMIT = 4
#: One held lane plus the three healthy fanout lanes must be in native
#: handoff simultaneously under that spare capacity.
_EXPECTED_CONCURRENT_LANES = 4
_FAST_POLL_SECONDS = 0.2


class _LaneTracker:
    """Process-local sampler for fake native handoff concurrency."""

    def __init__(self) -> None:
        self._current = 0
        self.max_concurrent = 0
        self._current_by_adapter: dict[str, int] = {}
        self.max_by_adapter: dict[str, int] = {}

    @property
    def current(self) -> int:
        """Number of fake native handoffs currently in flight."""
        return self._current

    def enter(self, adapter_id: str) -> None:
        self._current += 1
        self.max_concurrent = max(self.max_concurrent, self._current)
        current = self._current_by_adapter.get(adapter_id, 0) + 1
        self._current_by_adapter[adapter_id] = current
        self.max_by_adapter[adapter_id] = max(
            self.max_by_adapter.get(adapter_id, 0), current
        )

    def exit(self, adapter_id: str) -> None:
        self._current -= 1
        self._current_by_adapter[adapter_id] -= 1


class _HeldDestinationAdapter(FakePresentationAdapter):
    """Fake destination whose native handoff parks on an explicit barrier.

    ``deliver`` is the real adapter boundary the production pipeline invokes;
    parking here stalls a genuine dispatch, including its lease renewal and
    capacity slot, rather than a monkeypatched scheduler helper.
    """

    def __init__(
        self,
        adapter_id: str,
        tracker: _LaneTracker,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(adapter_id=adapter_id)
        self._tracker = tracker
        self.entered = entered
        self._release = release

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self.entered.set()
        self._tracker.enter(self.adapter_id)
        try:
            await self._release.wait()
            return await super().deliver(result)
        finally:
            self._tracker.exit(self.adapter_id)


class _RendezvousDestinationAdapter(FakePresentationAdapter):
    """Healthy destination that joins a lane-count rendezvous.

    The rendezvous is bounded: a scheduler that serialized lanes would return
    ``False`` here and the campaign's max-concurrency assertion would fail
    with evidence instead of hanging the test.
    """

    def __init__(
        self,
        adapter_id: str,
        tracker: _LaneTracker,
        release: asyncio.Event,
    ) -> None:
        super().__init__(adapter_id=adapter_id)
        self._tracker = tracker
        self._release = release

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self._tracker.enter(self.adapter_id)
        try:
            if not await wait_until(self._release.is_set, timeout=5.0):
                raise TimeoutError("healthy lane rendezvous was not released")
            return await super().deliver(result)
        finally:
            self._tracker.exit(self.adapter_id)


class _BeforeStartRefusalAdapter(FakePresentationAdapter):
    """Refuses the first handoff before native acceptance, then accepts.

    The refusal raises at the adapter boundary before any
    ``AdapterHandoffResult``, so it models a before-start destination refusal:
    no acceptance evidence exists and a later retry must carry a new attempt.
    """

    def __init__(
        self,
        adapter_id: str,
        tracker: _LaneTracker,
        retry_after_seconds: float,
    ) -> None:
        super().__init__(adapter_id=adapter_id)
        self._tracker = tracker
        self._retry_after_seconds = retry_after_seconds
        self.deliver_calls = 0
        self.call_times: list[datetime] = []

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self._tracker.enter(self.adapter_id)
        try:
            self.deliver_calls += 1
            self.call_times.append(datetime.now(timezone.utc))
            if self.deliver_calls == 1:
                raise AdapterSendError(
                    "destination refused before native handoff",
                    retry_after_seconds=self._retry_after_seconds,
                )
            return await super().deliver(result)
        finally:
            self._tracker.exit(self.adapter_id)


def _held_lane_pipeline(
    store: SQLiteStorage, adapters: dict[str, FakePresentationAdapter]
) -> PipelineRunner:
    """Production pipeline assembly: fanout route plus a solo refusal route.

    The content privacy ceiling is configured, so durable ``admit_ingress`` is
    the only authorized new-start path; direct ingress/store fences must stay
    intact for the whole campaign.
    """
    fanout = Route(
        id="held-fanout",
        source=RouteSource(
            adapter="ingress", channel="room", event_kinds=("message.created",)
        ),
        targets=[RouteTarget(adapter=name, channel="out") for name in _FANOUT_ADAPTERS],
    )
    solo = Route(
        id="held-solo",
        source=RouteSource(
            adapter="ingress", channel="solo", event_kinds=("message.created",)
        ),
        targets=[RouteTarget(adapter=_REFUSER_ADAPTER, channel="out")],
    )
    config = make_pipeline_config_for_pipeline(
        store, Router(routes=[fanout, solo]), adapters
    )
    config.route_retry_policies[fanout.id] = RetryPolicy(jitter=False)
    config.route_retry_policies[solo.id] = RetryPolicy(jitter=False)
    config.content_ceiling = ContentCeilingPolicy(max_age_seconds=3600)
    return PipelineRunner(config)


async def _stage_event(
    store: SQLiteStorage,
    runner: PipelineRunner,
    event_id: str,
    channel: str,
) -> tuple[CanonicalEvent, NativeRef]:
    """Admit one event durably and stage its outbox obligations."""
    native = NativeRef(
        adapter="ingress", native_channel_id=channel, native_message_id=event_id
    )
    event = make_event(
        event_id=event_id,
        source_adapter="ingress",
        source_channel_id=channel,
        source_native_ref=native,
    )
    admitted = await runner.admit_ingress(event, "live")
    assert admitted.created and admitted.event_id == event_id
    ingress = DurableIngressWorker(storage=store, pipeline=runner, batch_size=1)
    processed = await ingress.run_once()
    assert processed == 1
    return event, native


async def _wait_row_statuses(
    store: SQLiteStorage,
    event_id: str,
    expected: dict[str, str],
    *,
    timeout: float = 8.0,
) -> dict[str, DeliveryOutboxItem]:
    """Poll until each target adapter's row reaches its expected status."""
    rows: dict[str, DeliveryOutboxItem] = {}

    async def settled() -> bool:
        found = {
            row.target_adapter: row
            for row in await store.list_outbox_items_for_event(event_id)
        }
        rows.clear()
        rows.update(found)
        return all(
            adapter in found and found[adapter].status == status
            for adapter, status in expected.items()
        )

    assert await wait_until(settled, timeout=timeout), (
        f"rows for {event_id} did not settle to {expected}; observed "
        f"{ {a: getattr(r, 'status', None) for a, r in rows.items()} }"
    )
    return rows


async def test_held_destination_isolation_and_restart_recovery(
    tmp_path: Path,
) -> None:
    """One held native handoff must not block healthy destinations.

    Generation A runs the production background dispatcher with its lifetime
    task group. One held operation parks at the adapter boundary; fresh work
    admitted during the hold completes on healthy destinations, per-adapter
    lanes stay exclusive, global capacity bounds concurrent handoffs, and a
    before-start refusal recovers through its durable cooldown. Generation A
    stops with the held operation still in flight: an uncertain native
    handoff plus never-started work. Generation B reopens the same SQLite
    state and must recover exact obligations without replaying completed
    siblings or reusing the consumed attempt identity.
    """
    path = str(tmp_path / "held-isolation.sqlite")
    store = SQLiteStorage(path)
    async with AsyncExitStack() as resources:
        resources.push_async_callback(store.close)
        await store.initialize()
        tracker = _LaneTracker()
        held_entered, held_release = asyncio.Event(), asyncio.Event()
        lanes_up = asyncio.Event()
        held = _HeldDestinationAdapter("held", tracker, held_entered, held_release)
        healthy = {
            name: _RendezvousDestinationAdapter(name, tracker, lanes_up)
            for name in _FANOUT_ADAPTERS[1:]
        }
        refuser = _BeforeStartRefusalAdapter(
            _REFUSER_ADAPTER, tracker, retry_after_seconds=1.0
        )
        adapters = {"held": held, **healthy, _REFUSER_ADAPTER: refuser}

        runner = _held_lane_pipeline(store, adapters)
        capacity = CapacityController(
            RuntimeLimits(
                max_inflight_deliveries=_HELD_DELIVERY_LIMIT,
                delivery_acquire_timeout_seconds=0.05,
            )
        )
        runner.set_capacity_controller(capacity)
        resources.push_async_callback(runner.stop)
        await runner.start()

        # Content-ceiling new-start fence: with a ceiling configured, direct
        # ingress handling is refused; fresh work must cross durable admission.
        with pytest.raises(ValueError):
            await runner.handle_ingress(
                make_event(event_id="direct-start", source_adapter="ingress")
            )

        dispatcher = RetryWorker(
            store,
            runner,
            capacity,
            initial_dispatch=True,
            enabled=True,
            interval_seconds=_FAST_POLL_SECONDS,
            batch_size=8,
            max_attempts=4,
            stop_timeout_seconds=0.5,
        )
        dispatcher.set_available_target_adapters(adapters.keys())
        resources.push_async_callback(dispatcher.stop)
        await dispatcher.start()

        held_event, held_native = await _stage_event(
            store, runner, "held-message", "room"
        )
        assert await wait_until(held_entered.is_set, timeout=5.0)
        # The dispatcher claimed one row per adapter under the configured spare
        # capacity: all four lanes must be inside their native handoff together.
        assert await wait_until(
            lambda: tracker.current >= _EXPECTED_CONCURRENT_LANES, timeout=5.0
        )
        lanes_up.set()

        held_rows = await _wait_row_statuses(
            store,
            held_event.event_id,
            {"held": "in_progress", **{name: "sent" for name in healthy}},
        )
        held_row = held_rows["held"]
        assert held_row.status == "in_progress"
        assert held_row.active_attempt == 1
        assert held_row.worker_id and held_row.worker_id.startswith("retry-worker")
        assert held_row.lease_until is not None

        # Spare global capacity is a recorded configuration, and it was used:
        # four lanes were concurrently inside the real handoff boundary.
        assert capacity.delivery_limit == _HELD_DELIVERY_LIMIT
        assert tracker.max_concurrent == _EXPECTED_CONCURRENT_LANES
        # Per-adapter lanes are exclusive: no adapter ever ran two handoffs.
        assert set(tracker.max_by_adapter) == set(_FANOUT_ADAPTERS)
        assert all(value == 1 for value in tracker.max_by_adapter.values())
        # Local handoff acceptance only: fake payloads prove the pipeline
        # crossed the boundary, never that a recipient received anything.
        assert all(len(a.delivered_payloads) == 1 for a in healthy.values())
        assert not held.delivered_payloads
        for row in held_rows.values():
            if row.target_adapter == "held":
                continue
            assert await store.list_receipts_for_outbox(row.outbox_id)

        # Fresh work admitted while the held operation remains in flight must
        # complete on healthy destinations and queue behind the held lane on
        # the held adapter without a second concurrent handoff.
        fresh_event, fresh_native = await _stage_event(
            store, runner, "fresh-during-hold", "room"
        )
        await _wait_row_statuses(
            store, fresh_event.event_id, {name: "sent" for name in healthy}
        )
        fresh_rows = await store.list_outbox_items_for_event(fresh_event.event_id)
        fresh_held_row = next(r for r in fresh_rows if r.target_adapter == "held")
        assert fresh_held_row.status == "pending"
        assert fresh_held_row.attempt_number == 0
        assert all(
            a.delivered_payloads
            and a.delivered_payloads[-1].event_id == fresh_event.event_id
            for a in healthy.values()
        )
        assert not held.delivered_payloads
        # Only the held lane is still active; the held operation holds exactly
        # one capacity slot, and the rest of the configured capacity stayed
        # usable by healthy lanes during the whole hold. Lane bookkeeping is
        # reaped one loop turn after the rows commit, so poll briefly.
        assert await wait_until(
            lambda: set(dispatcher._active) == {"held"}, timeout=2.0
        )
        assert await wait_until(lambda: capacity.delivery_current == 1, timeout=2.0)

        # A before-start refusal recovers through its durable cooldown while
        # the held operation stays parked: one consumed attempt, one
        # scheduled re-dispatch, exactly one accepted handoff.
        cooldown_event, _ = await _stage_event(store, runner, "cooldown-solo", "solo")
        cooldown_rows = await _wait_row_statuses(
            store, cooldown_event.event_id, {_REFUSER_ADAPTER: "retry_wait"}
        )
        cooldown_row = cooldown_rows[_REFUSER_ADAPTER]
        assert cooldown_row.attempt_number == 1
        assert cooldown_row.next_attempt_at is not None
        scheduled = datetime.fromisoformat(cooldown_row.next_attempt_at)
        assert refuser.deliver_calls == 1 and not refuser.delivered_payloads
        cooldown_rows = await _wait_row_statuses(
            store, cooldown_event.event_id, {_REFUSER_ADAPTER: "sent"}
        )
        cooldown_row = cooldown_rows[_REFUSER_ADAPTER]
        assert cooldown_row.attempt_number == 2
        assert refuser.deliver_calls == 2
        # The re-dispatch honored the durable schedule (one-sided bound: a
        # slow machine only moves this later, never earlier).
        assert refuser.call_times[1] >= scheduled
        assert not held.delivered_payloads
        # The refusal lane stayed exclusive too: exactly one handoff in
        # flight for the refuser adapter at any time.
        assert tracker.max_by_adapter[_REFUSER_ADAPTER] == 1

        # Bounded admission: the dispatcher's count snapshot must settle to
        # exactly the work above — seven accepted handoffs, one refusal, one
        # held dispatch. The snapshot is last-known and refreshes on the
        # completion wake, so poll it instead of asserting a mid-flight sample.
        def settled_counts() -> dict[str, int] | None:
            snapshot = dispatcher.outbox_counts
            if (
                snapshot is not None
                and snapshot.get("sent") == 7
                and snapshot.get("in_progress") == 1
                and snapshot.get("pending") == 1
            ):
                return snapshot
            return None

        assert await wait_until(settled_counts, timeout=2.0)
        counts = dispatcher.outbox_counts
        assert counts is not None
        assert counts.get("sent") == 7
        assert counts.get("in_progress") == 1
        assert counts.get("pending") == 1
        # Retry counters record attempt>=1 dispatches only: the initial
        # (attempt-zero) handoffs finalize through the delivery coordinator
        # and are visible in the outbox counts above, while the single counted
        # retry here is the cooldown re-dispatch.
        assert dispatcher.state.processed == 1
        assert dispatcher.state.succeeded == 1
        assert dispatcher.state.failed == 0

        # Capture generation-A identity before the restart boundary.
        sibling_receipts = {}
        for event in (held_event, fresh_event, cooldown_event):
            for row in await store.list_outbox_items_for_event(event.event_id):
                if row.target_adapter == "held":
                    continue
                sibling_receipts[row.outbox_id] = await store.list_receipts_for_outbox(
                    row.outbox_id
                )
        sibling_rows = {
            event.event_id: await store.list_outbox_items_for_event(event.event_id)
            for event in (held_event, fresh_event, cooldown_event)
        }
        native_ref_counts = {
            event.event_id: len(await store.list_native_refs_for_event(event.event_id))
            for event in (held_event, fresh_event, cooldown_event)
        }

        # Stop generation A with the held handoff still in flight. The
        # dispatch task is cancelled inside the native boundary, so the
        # reserved attempt is left without receipt evidence: an uncertain
        # native handoff.
    assert not held.delivered_payloads
    assert not dispatcher.state.abandoned
    assert tracker.current == 0
    assert capacity.delivery_current == 0

    reopened = SQLiteStorage(path)
    try:
        await reopened.initialize()
        after_stop = {
            event_id: await reopened.list_outbox_items_for_event(event_id)
            for event_id in sibling_rows
        }
        held_after = next(
            r for r in after_stop["held-message"] if r.target_adapter == "held"
        )
        fresh_after = next(
            r for r in after_stop["fresh-during-hold"] if r.target_adapter == "held"
        )
        assert held_after.status == "in_progress"
        assert held_after.active_attempt == 1
        assert held_after.lease_until is not None
        # Uncertainty: the reserved dispatch has no receipt evidence at all.
        assert not await reopened.list_receipts_for_outbox(held_after.outbox_id)
        assert fresh_after.status == "pending"
        assert fresh_after.attempt_number == 0
        # Completed siblings are byte-identical across the restart boundary.
        for event_id, before_rows in sibling_rows.items():
            before_by_adapter = {r.target_adapter: r for r in before_rows}
            for adapter, before_row in before_by_adapter.items():
                if adapter == "held":
                    continue
                after_row = next(
                    r for r in after_stop[event_id] if r.target_adapter == adapter
                )
                assert after_row == before_row
                assert (
                    await reopened.list_receipts_for_outbox(after_row.outbox_id)
                    == sibling_receipts[before_row.outbox_id]
                )
    finally:
        await reopened.close()

    # Generation B: fresh adapter instances over the same durable state.
    restored_store = SQLiteStorage(path)
    async with AsyncExitStack() as resources:
        resources.push_async_callback(restored_store.close)
        await restored_store.initialize()
        restored_healthy = {
            name: FakePresentationAdapter(adapter_id=name) for name in healthy
        }
        restored_held = FakePresentationAdapter(adapter_id="held")
        restored_refuser = FakePresentationAdapter(adapter_id=_REFUSER_ADAPTER)
        restored_adapters = {
            "held": restored_held,
            **restored_healthy,
            _REFUSER_ADAPTER: restored_refuser,
        }
        resumed = _held_lane_pipeline(restored_store, restored_adapters)
        resources.push_async_callback(resumed.stop)
        await resumed.start()
        recovery = RetryWorker(
            restored_store,
            resumed,
            None,
            initial_dispatch=True,
            enabled=True,
            interval_seconds=_FAST_POLL_SECONDS,
            batch_size=8,
            max_attempts=4,
        )
        # Canonical event/target identity: re-decoding the same native
        # references must not create a second set of obligations.
        for original, native in (
            (held_event, held_native),
            (fresh_event, fresh_native),
        ):
            duplicate = await resumed.admit_ingress(
                make_event(
                    event_id=f"{original.event_id}-redelivery",
                    source_adapter="ingress",
                    source_channel_id=original.source_channel_id,
                    source_native_ref=native,
                ),
                "recovered",
            )
            assert duplicate.created is False
            assert duplicate.event_id == original.event_id
            assert await restored_store.get(f"{original.event_id}-redelivery") is None
            assert (
                len(await restored_store.list_outbox_items_for_event(original.event_id))
                == 4
            )

        # Deterministic recovery clock: the generation-A lease must expire
        # before the uncertain handoff is reclaimable.
        lease_ready = datetime.fromisoformat(held_after.lease_until) + timedelta(
            seconds=1
        )
        await recovery._process_due(lease_ready)
        rows_after_first = {
            event_id: await restored_store.list_outbox_items_for_event(event_id)
            for event_id in ("held-message", "fresh-during-hold")
        }
        fresh_recovered = next(
            r
            for r in rows_after_first["fresh-during-hold"]
            if r.target_adapter == "held"
        )
        held_recovered = next(
            r for r in rows_after_first["held-message"] if r.target_adapter == "held"
        )
        # Never-started work dispatches as a fresh initial attempt.
        assert fresh_recovered.status == "sent"
        assert fresh_recovered.attempt_number == 1
        assert len(restored_held.delivered_payloads) == 1
        assert restored_held.delivered_payloads[0].event_id == "fresh-during-hold"
        # The uncertain handoff is not re-sent under the reserved identity:
        # the ambiguous attempt is consumed and a strictly newer attempt is
        # scheduled through the durable cooldown.
        assert held_recovered.status == "retry_wait"
        assert held_recovered.attempt_number == 1
        assert held_recovered.active_attempt is None
        assert held_recovered.next_attempt_at is not None
        # Completed siblings were not replayed into the healthy destinations.
        assert all(not a.delivered_payloads for a in restored_healthy.values())
        assert not restored_refuser.delivered_payloads

        due = datetime.fromisoformat(held_recovered.next_attempt_at) + timedelta(
            seconds=1
        )
        await recovery._process_due(due)
        final_rows = await restored_store.list_outbox_items_for_event("held-message")
        held_final = next(r for r in final_rows if r.target_adapter == "held")
        assert held_final.status == "sent"
        assert held_final.attempt_number == 2
        assert len(restored_held.delivered_payloads) == 2
        assert restored_held.delivered_payloads[1].event_id == "held-message"
        assert all(not a.delivered_payloads for a in restored_healthy.values())

        # Outbox identity sets are unchanged; every obligation is terminal.
        for event_id in ("held-message", "fresh-during-hold", "cooldown-solo"):
            rows = await restored_store.list_outbox_items_for_event(event_id)
            assert sorted(r.outbox_id for r in rows) == sorted(
                r.outbox_id for r in sibling_rows[event_id]
            )
            assert all(r.status == "sent" for r in rows)
        # Receipts of completed siblings are preserved exactly; only the
        # recovered held obligation gained new attempt evidence.
        for outbox_id, before in sibling_receipts.items():
            assert await restored_store.list_receipts_for_outbox(outbox_id) == before
        assert await restored_store.count_events() == 3
        # Exactly one new native reference per recovered held obligation; the
        # count includes the inbound admission reference from generation A.
        # The cooldown obligation completed in generation A and gains none.
        for event_id, gained in (
            ("held-message", 1),
            ("fresh-during-hold", 1),
            ("cooldown-solo", 0),
        ):
            refs_now = await restored_store.list_native_refs_for_event(event_id)
            assert len(refs_now) == native_ref_counts[event_id] + gained
        assert recovery.state.succeeded == 1
        assert recovery.state.failed == 1
        assert recovery.state.processed == 2


# ---------------------------------------------------------------------------
# Read-only work-status view across the held-lane campaign
# ---------------------------------------------------------------------------

_STATUS_WORK_CATEGORIES: tuple[str, ...] = tuple(WORK_STATUS_CATEGORIES)


def _application_state(db: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Row-level application state for mutation-freedom comparison."""
    names = [
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        )
    ]
    return {
        name: sorted(
            tuple(row)
            for row in db.execute(
                f'SELECT * FROM "{name}"'  # nosec B608 - table names come from sqlite_master
            ).fetchall()
        )
        for name in names
    }


def _assert_view_consistent(summary: dict) -> None:
    """The category partition must sum exactly, on every single sample."""
    total = sum(int(row["current_generations"]) for row in summary["adapter_rows"])
    assert total == summary["current_generations_total"]
    for row in summary["adapter_rows"]:
        assert (
            sum(int(row[category]) for category in _STATUS_WORK_CATEGORIES)
            == row["current_generations"]
        )


def _by_adapter(summary: dict) -> dict[str, dict]:
    """Key rows by adapter for either shape: raw storage aggregates
    (``adapter_rows``, categories flat) or the assembled document
    (``adapters``, categories nested under ``categories``/``uncertainty``)."""
    rows = summary.get("adapter_rows") or summary.get("adapters") or []
    return {row["target_adapter"]: row for row in rows}


async def _sample_work_status(
    path: str, *, sampled_at: str | None = None
) -> tuple[dict, dict]:
    """One read-only work-status sample over a fully separate connection.

    Returns the raw storage aggregates and the assembled operator document;
    assembly itself fails closed on any broken accounting invariant.
    """
    readonly = await SQLiteStorage.open_readonly(path)
    try:
        summary = await readonly.query_work_status_summary(sampled_at=sampled_at)
        report = assemble_work_status_report(
            sampled_at=summary["sampled_at"],
            adapter_filter=None,
            adapter_rows=summary["adapter_rows"],
            oldest_rows=summary["oldest_rows"],
            raw_status_rows=summary["raw_status_rows"],
            current_generations_total=summary["current_generations_total"],
            receipts_by_status=summary["receipts_by_status"],
        )
        return summary, report.to_dict()
    finally:
        await readonly.close()


async def test_work_status_view_across_held_lane_campaign(tmp_path: Path) -> None:
    """The read-only work-status view stays honest across the held-lane campaign.

    While the production dispatcher runs with one operation parked at the
    adapter boundary, every sample classifies the held generation as
    reserved/active without claiming a live native send, tracks healthy
    sibling progress and fresh admitted work, reports the long-cooldown
    refusal as a scheduled retry with its due-time origin, and stays
    internally consistent across repeated delayed reads. After generation
    A stops with the uncertain handoff in flight, reopening storage shows
    the reserved generation without attempt evidence, the recorded lease
    expiry at a sampled time past the lease, completed siblings as
    terminal history that never masquerades as outstanding work, and no
    application-data mutation from the reads themselves.
    """
    path = str(tmp_path / "held-status.sqlite")
    store = SQLiteStorage(path)
    async with AsyncExitStack() as resources:
        resources.push_async_callback(store.close)
        await store.initialize()
        tracker = _LaneTracker()
        held_entered, held_release = asyncio.Event(), asyncio.Event()
        lanes_up = asyncio.Event()
        held = _HeldDestinationAdapter("held", tracker, held_entered, held_release)
        healthy = {
            name: _RendezvousDestinationAdapter(name, tracker, lanes_up)
            for name in _FANOUT_ADAPTERS[1:]
        }
        # A multi-hour cooldown keeps the refused obligation scheduled
        # (never due) for the whole generation-A run, so the scheduled-retry
        # sample is deterministic under the running dispatcher.
        refuser = _BeforeStartRefusalAdapter(
            _REFUSER_ADAPTER, tracker, retry_after_seconds=3600.0
        )
        adapters = {"held": held, **healthy, _REFUSER_ADAPTER: refuser}

        runner = _held_lane_pipeline(store, adapters)
        capacity = CapacityController(
            RuntimeLimits(
                max_inflight_deliveries=_HELD_DELIVERY_LIMIT,
                delivery_acquire_timeout_seconds=0.05,
            )
        )
        runner.set_capacity_controller(capacity)
        resources.push_async_callback(runner.stop)
        await runner.start()

        dispatcher = RetryWorker(
            store,
            runner,
            capacity,
            initial_dispatch=True,
            enabled=True,
            interval_seconds=_FAST_POLL_SECONDS,
            batch_size=8,
            max_attempts=4,
            stop_timeout_seconds=0.5,
        )
        dispatcher.set_available_target_adapters(adapters.keys())
        resources.push_async_callback(dispatcher.stop)
        await dispatcher.start()

        held_event, _ = await _stage_event(store, runner, "status-held", "room")
        assert await wait_until(held_entered.is_set, timeout=5.0)
        assert await wait_until(
            lambda: tracker.current >= _EXPECTED_CONCURRENT_LANES, timeout=5.0
        )
        lanes_up.set()

        # Held send in flight: the view reports a reserved generation with
        # no attempt evidence and never claims a live native send or a
        # healthy completion for it.
        held_summary, held_document = await _sample_work_status(path)
        _assert_view_consistent(held_summary)
        held_counts = _by_adapter(held_document)["held"]
        assert held_counts["categories"]["reserved_active"] == 1
        assert held_counts["uncertainty"]["reserved_without_generation_receipt"] == 1
        assert held_counts["uncertainty"]["lease_missing_or_expired"] == 0
        assert held_counts["categories"]["terminal_outcome"] == 0
        # Unrelated adapters are visible with their own honest tallies.
        assert all(
            _by_adapter(held_summary)[name]["current_generations"] == 1
            for name in healthy
        )

        # Healthy siblings complete during the hold; only the held lane
        # stays reserved.
        await _wait_row_statuses(
            store, held_event.event_id, {name: "sent" for name in healthy}
        )
        siblings_summary, siblings_document = await _sample_work_status(path)
        _assert_view_consistent(siblings_summary)
        for name in healthy:
            counts = _by_adapter(siblings_document)[name]
            assert counts["categories"]["terminal_outcome"] == 1
            assert counts["outstanding"] == 0
        assert _by_adapter(siblings_document)["held"]["outstanding"] == 1

        # Fresh work admitted during the hold is eligible unstarted work on
        # the held adapter and completes on healthy destinations.
        fresh_event, _ = await _stage_event(store, runner, "status-fresh", "room")
        await _wait_row_statuses(
            store, fresh_event.event_id, {name: "sent" for name in healthy}
        )
        fresh_summary, fresh_document = await _sample_work_status(path)
        _assert_view_consistent(fresh_summary)
        held_fresh = _by_adapter(fresh_document)["held"]
        assert held_fresh["outstanding"] == 2
        assert held_fresh["categories"]["unstarted_eligible"] == 1
        assert held_fresh["categories"]["reserved_active"] == 1
        oldest_held = next(
            row
            for row in fresh_summary["oldest_rows"]
            if row["target_adapter"] == "held"
        )
        assert (oldest_held["category"], oldest_held["age_origin"]) in {
            ("unstarted_eligible", "admission"),
            ("reserved_active", "claim"),
        }
        for name in healthy:
            assert _by_adapter(fresh_summary)[name]["terminal_outcome"] == 2

        # A before-start refusal recovers into a scheduled retry whose age
        # anchor is the due time, not the admission time.
        cooldown_event, _ = await _stage_event(store, runner, "status-cooldown", "solo")
        retry_rows = await _wait_row_statuses(
            store, cooldown_event.event_id, {_REFUSER_ADAPTER: "retry_wait"}
        )
        retry_due = retry_rows[_REFUSER_ADAPTER].next_attempt_at
        assert retry_due is not None
        cooldown_summary, cooldown_document = await _sample_work_status(path)
        _assert_view_consistent(cooldown_summary)
        refuser_counts = _by_adapter(cooldown_document)[_REFUSER_ADAPTER]
        assert refuser_counts["categories"]["retry_scheduled"] == 1
        refuser_oldest = next(
            row
            for row in cooldown_summary["oldest_rows"]
            if row["target_adapter"] == _REFUSER_ADAPTER
        )
        assert refuser_oldest["age_origin"] == "retry_due"
        assert refuser_oldest["category"] == "retry_scheduled"
        assert refuser_oldest["age_anchor"] == retry_due

        # Repeated reads during a settled hold. The deterministic between-
        # statement commit is tested directly in test_work_status_storage.
        for _ in range(4):
            loop_summary, _loop_document = await _sample_work_status(path)
            _assert_view_consistent(loop_summary)

    # Generation A stopped with the uncertain handoff in flight. The
    # writable reopen only stages deterministic state for the assertions.
    reopened = SQLiteStorage(path)
    try:
        await reopened.initialize()
        rows_after_stop = await reopened.list_outbox_items_for_event(
            held_event.event_id
        )
        held_after = next(r for r in rows_after_stop if r.target_adapter == "held")
        assert held_after.status == "in_progress"
        assert held_after.active_attempt == 1
        assert held_after.lease_until is not None
        lease_ready = datetime.fromisoformat(held_after.lease_until) + timedelta(
            seconds=1
        )
    finally:
        await reopened.close()

    # Stale-observation honesty at a deterministic sampled time: the
    # recorded lease has expired and the reserved generation still carries
    # no attempt evidence, so owner liveness and the native handoff outcome
    # are both unknown.
    with contextlib.closing(sqlite3.connect(path)) as snapshot_db:
        before_stale = _application_state(snapshot_db)
        stale_summary, stale_report = await _sample_work_status(
            path, sampled_at=lease_ready.isoformat()
        )
        assert _application_state(snapshot_db) == before_stale
    _assert_view_consistent(stale_summary)
    stale_counts = _by_adapter(stale_report)["held"]
    assert stale_counts["categories"]["reserved_active"] == 1
    assert stale_counts["uncertainty"]["reserved_without_generation_receipt"] == 1
    assert stale_counts["uncertainty"]["lease_missing_or_expired"] == 1
    # Completed siblings stay terminal history and never masquerade as
    # outstanding work; fresh admitted work stays unstarted.
    for name in healthy:
        counts = _by_adapter(stale_report)[name]
        assert counts["categories"]["terminal_outcome"] == 2
        assert counts["outstanding"] == 0
    assert _by_adapter(stale_report)["held"]["categories"]["unstarted_eligible"] == 1
    retry_category = (
        "retry_scheduled"
        if datetime.fromisoformat(retry_due) > lease_ready
        else "unstarted_eligible"
    )
    assert (
        _by_adapter(stale_report)[_REFUSER_ADAPTER]["categories"][retry_category] == 1
    )

    # The reads mutate no application data while nothing else runs.
    writable = SQLiteStorage(path)
    try:
        await writable.initialize()
        before = _application_state(writable._require_db())
        for _ in range(3):
            _summary, document = await _sample_work_status(path)
            _assert_view_consistent(_summary)
            assert document["totals"]["outstanding"] == 3
        assert _application_state(writable._require_db()) == before
    finally:
        await writable.close()
