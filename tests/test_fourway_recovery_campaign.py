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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.config.model import RuntimeLimits
from medre.core.contracts.adapter import AdapterHandoffResult, AdapterSendError
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, NativeRef
from medre.core.ingress import DurableIngressWorker
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
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
            await wait_until(self._release.is_set, timeout=5.0)
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
    await runner.admit_ingress(event, "live")
    ingress = DurableIngressWorker(storage=store, pipeline=runner, batch_size=1)
    assert await ingress.run_once() == 1
    return event, native


async def _wait_row_statuses(
    store: SQLiteStorage,
    event_id: str,
    expected: dict[str, str],
    *,
    timeout: float = 8.0,
) -> dict[str, object]:
    """Poll until each target adapter's row reaches its expected status."""
    rows: dict[str, object] = {}

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
    await dispatcher.start()

    try:
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
        await dispatcher.stop()
        assert not held.delivered_payloads
    finally:
        await runner.stop()
        await store.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
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
    try:
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
    finally:
        await resumed.stop()
        await restored_store.close()
