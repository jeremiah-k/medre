# medre-test-tier: compatibility
"""RetryWorker-level uncertain-native-handoff remediation tests.

Gap G02 remediation at the recovery boundary: a reclaimed dispatch whose
native handoff outcome is not settled by receipts is resolved from evidence
the backend already holds before the transport may be invoked again.

* a crashed radio dispatch whose retained checkpoint shows the whole plan
  handed off settles the obligation ``sent`` without invoking the adapter
  and without fabricating a receipt;
* a crashed radio dispatch with an interrupted packet stays explicitly
  uncertain on the durable row and never reaches the transport;
* a receipt-less non-radio dispatch with an adapter-reported provider
  acceptance settles ``sent`` instead of being consumed as ambiguous.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.adapter_health import AdapterHealthGate
from medre.core.events import DeliveryObservation
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.radio_handoff import RadioLocalHandoff
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

_PAST = datetime.now(timezone.utc) - timedelta(seconds=120)


def _router(target: str = "radio") -> Router:
    return Router(
        routes=[
            Route(
                id="route-handoff-remediation",
                source=RouteSource(
                    adapter="fake_transport",
                    event_kinds=("message.created",),
                    channel="ch-0",
                ),
                targets=[RouteTarget(adapter=target)],
            )
        ]
    )


async def _make_runner(
    temp_storage: SQLiteStorage,
    adapters: dict[str, FakePresentationAdapter],
) -> PipelineRunner:
    config = make_pipeline_config_for_pipeline(
        temp_storage, _router(next(iter(adapters))), adapters
    )
    config.route_retry_policies["route-handoff-remediation"] = RetryPolicy(
        max_attempts=4, jitter=False
    )
    runner = PipelineRunner(config)
    await runner.start()
    return runner


async def _deferred_claim(
    temp_storage: SQLiteStorage,
    adapters: dict[str, FakePresentationAdapter],
    event_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> PipelineRunner:
    """Admit an obligation and crash its deferral commit, leaving the claim.

    A storage fault during the guarded deferral release retains the live
    dispatch reservation on the ``in_progress`` row — exactly the crash
    window whose uncertain handoff recovery must resolve.
    """

    async def failing_release(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("storage unavailable mid-deferral")

    runner = await _make_runner(temp_storage, adapters)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                temp_storage, "defer_unsubmitted_outbox_attempt", failing_release
            )
            runner.set_adapter_health_gate(
                AdapterHealthGate({next(iter(adapters)): "degraded"}.get)
            )
            await runner.handle_ingress(make_event(event_id=event_id))
    except BaseException:
        await runner.stop()
        raise
    return runner


async def _row(temp_storage: SQLiteStorage, event_id: str) -> dict:
    rows = await temp_storage._read_all(
        "SELECT * FROM delivery_outbox WHERE event_id = ?", (event_id,)
    )
    assert len(rows) == 1
    return rows[0]


async def _expire_lease(temp_storage: SQLiteStorage, outbox_id: str) -> None:
    await temp_storage._write_rowcount(
        "UPDATE delivery_outbox SET lease_until = ? WHERE outbox_id = ?",
        (_PAST.isoformat(), outbox_id),
    )


async def _bind_radio_checkpoint(
    temp_storage: SQLiteStorage, row: dict, states: list[str]
) -> None:
    """Drive the real grant fence through the requested packet states."""
    from medre.core.rendering.radio_text import plan_radio_text

    plan = plan_radio_text("café α " * 6, max_text_bytes=40, prefix="[North] ")
    assert len(states) == len(plan.parts)
    provenance = DeliveryAttemptProvenance(
        event_id=row["event_id"],
        delivery_plan_id=row["delivery_plan_id"],
        target_adapter=row["target_adapter"],
        target_channel=row["target_channel"],
        outbox_id=row["outbox_id"],
        attempt_number=row["active_attempt"],
        source=row["dispatch_source"],
        replay_run_id=row["replay_run_id"],
    )
    worker = row["worker_id"]
    checkpoint = await temp_storage.bind_radio_attempt(provenance, worker, plan)
    assert checkpoint is not None
    for ordinal, state in enumerate(states, 1):
        if state == "unsent":
            continue
        submitting = await temp_storage.begin_radio_part(checkpoint, worker, ordinal)
        assert submitting is not None
        if state == "accepted":
            observed = await temp_storage.observe_radio_part(
                submitting,
                worker,
                ordinal,
                handoff_id=RadioLocalHandoff(
                    ordinal, f"native-{ordinal}", "ch-0", "local_transport"
                ).encode(),
            )
        else:
            observed = await temp_storage.observe_radio_part(
                submitting, worker, ordinal
            )
        assert observed is not None
        checkpoint = observed


async def _run_worker_once(
    temp_storage: SQLiteStorage,
    runner: PipelineRunner,
    now: datetime | None = None,
    *,
    worker: RetryWorker | None = None,
) -> RetryWorker:
    if worker is None:
        worker = RetryWorker(
            temp_storage,
            runner,
            None,
            enabled=True,
            interval_seconds=5.0,
            max_attempts=4,
        )
    await worker._process_due(now or datetime.now(timezone.utc))
    return worker


async def test_worker_settles_complete_radio_handoff_without_transport(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully handed-off crashed dispatch settles sent; the adapter is idle."""
    adapters = {"radio": FakePresentationAdapter(adapter_id="radio")}
    runner = await _deferred_claim(
        temp_storage, adapters, "evt-worker-complete", monkeypatch
    )
    try:
        row = await _row(temp_storage, "evt-worker-complete")
        assert row["status"] == "in_progress"
        assert row["active_attempt"] == 1
        await _bind_radio_checkpoint(
            temp_storage, row, ["accepted", "accepted", "accepted"]
        )
        receipts_before = await temp_storage.list_receipts_for_outbox(row["outbox_id"])
        assert receipts_before == []

        await _expire_lease(temp_storage, row["outbox_id"])
        await _run_worker_once(temp_storage, runner)

        settled = await _row(temp_storage, "evt-worker-complete")
        assert settled["status"] == "sent"
        assert settled["attempt_number"] == 1
        assert settled["active_attempt"] is None
        assert await temp_storage.list_receipts_for_outbox(row["outbox_id"]) == []
        assert adapters["radio"].delivered_payloads == []
    finally:
        await runner.stop()


async def test_worker_keeps_uncertain_radio_dispatch_explicitly_unresolved(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted packet blocks the transport and stays operator-visible."""
    adapters = {"radio": FakePresentationAdapter(adapter_id="radio")}
    runner = await _deferred_claim(
        temp_storage, adapters, "evt-worker-uncertain", monkeypatch
    )
    try:
        row = await _row(temp_storage, "evt-worker-uncertain")
        await _bind_radio_checkpoint(
            temp_storage, row, ["accepted", "unknown", "unsent"]
        )

        await _expire_lease(temp_storage, row["outbox_id"])
        await _run_worker_once(temp_storage, runner)

        settled = await _row(temp_storage, "evt-worker-uncertain")
        assert settled["status"] == "retry_wait"
        assert settled["attempt_number"] == 1
        projected = await temp_storage.get_outbox_item(row["outbox_id"])
        assert projected is not None
        assert projected.failure_kind_detail == "uncertain_native_handoff"
        assert adapters["radio"].delivered_payloads == []
    finally:
        await runner.stop()


async def test_worker_settles_recorded_provider_acceptance_as_sent(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt-less dispatch with recorded acceptance settles sent."""
    adapters = {
        "fake_presentation": FakePresentationAdapter(adapter_id="fake_presentation")
    }
    runner = await _deferred_claim(
        temp_storage, adapters, "evt-worker-observation", monkeypatch
    )
    try:
        row = await _row(temp_storage, "evt-worker-observation")
        assert row["active_attempt"] == 1
        observation = DeliveryObservation(
            observation_id="obs-worker-acceptance",
            event_id=row["event_id"],
            delivery_plan_id=row["delivery_plan_id"],
            target_adapter=row["target_adapter"],
            target_channel=row["target_channel"],
            outbox_id=row["outbox_id"],
            attempt_number=row["active_attempt"],
            adapter_message_id="native-worker-1",
            state="delivered",
            confirmation_level="end_to_end",
            observed_at=datetime.now(timezone.utc),
        )
        assert await temp_storage.append_delivery_observation(observation) is True

        await _expire_lease(temp_storage, row["outbox_id"])
        await _run_worker_once(temp_storage, runner)

        settled = await _row(temp_storage, "evt-worker-observation")
        assert settled["status"] == "sent"
        assert settled["attempt_number"] == 1
        assert adapters["fake_presentation"].delivered_payloads == []
    finally:
        await runner.stop()


def _late_acceptance(row: dict) -> DeliveryObservation:
    return DeliveryObservation(
        observation_id="obs-late-worker-acceptance",
        event_id=row["event_id"],
        delivery_plan_id=row["delivery_plan_id"],
        target_adapter=row["target_adapter"],
        target_channel=row["target_channel"],
        outbox_id=row["outbox_id"],
        attempt_number=1,
        state="delivered",
        confirmation_level="end_to_end",
        observed_at=datetime.now(timezone.utc),
    )


async def test_worker_fences_acceptance_during_owned_content_read(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new observation between recheck and reservation prevents a resend."""
    adapters = {
        "fake_presentation": FakePresentationAdapter(adapter_id="fake_presentation")
    }
    runner = await _deferred_claim(
        temp_storage, adapters, "evt-worker-late-acceptance", monkeypatch
    )
    try:
        row = await _row(temp_storage, "evt-worker-late-acceptance")
        await _expire_lease(temp_storage, row["outbox_id"])
        await _run_worker_once(temp_storage, runner)
        row = await _row(temp_storage, row["event_id"])
        assert row["status"] == "retry_wait" and row["attempt_number"] == 1
        runner.set_adapter_health_gate(AdapterHealthGate(lambda _: "healthy"))
        read_owned = temp_storage.read_event_for_outbox
        reserve = temp_storage.reserve_outbox_attempt
        reservation_results: list[int | None] = []

        async def observe_reservation(*args, **kwargs):
            result = await reserve(*args, **kwargs)
            reservation_results.append(result)
            return result

        reserved = AsyncMock(wraps=observe_reservation)

        async def append_then_read(*args, **kwargs):
            assert await temp_storage.append_delivery_observation(_late_acceptance(row))
            return await read_owned(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(temp_storage, "read_event_for_outbox", append_then_read)
            patch.setattr(temp_storage, "reserve_outbox_attempt", reserved)
            await _run_worker_once(
                temp_storage,
                runner,
                datetime.fromisoformat(row["next_attempt_at"]) + timedelta(seconds=1),
            )
        reserved.assert_awaited_once()
        assert reservation_results == [None]
        assert reserved.await_args.kwargs["expected_observation_count"] == 0
        assert reserved.await_args.kwargs["expected_receipt_count"] == 0
        assert reserved.await_args.kwargs["expected_checkpoint_revision"] == -1
        row = await _row(temp_storage, row["event_id"])
        assert row["status"] == "in_progress"
        assert row["active_attempt"] is None and row["attempt_number"] == 1
        assert adapters["fake_presentation"].delivered_payloads == []
        await _run_worker_once(
            temp_storage,
            runner,
            datetime.fromisoformat(row["lease_until"]) + timedelta(seconds=1),
        )
        settled = await _row(temp_storage, row["event_id"])
        assert settled["status"] == "sent" and settled["attempt_number"] == 1
        assert adapters["fake_presentation"].delivered_payloads == []
    finally:
        await runner.stop()


@pytest.mark.parametrize("commit_before_raise", [False, True])
async def test_worker_recovers_lost_recheck_write_on_ordinary_poll(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
    commit_before_raise: bool,
) -> None:
    adapters = {
        "fake_presentation": FakePresentationAdapter(adapter_id="fake_presentation")
    }
    runner = await _deferred_claim(
        temp_storage, adapters, "evt-worker-lost-recheck", monkeypatch
    )
    try:
        row = await _row(temp_storage, "evt-worker-lost-recheck")
        await _expire_lease(temp_storage, row["outbox_id"])
        worker = await _run_worker_once(temp_storage, runner)
        row = await _row(temp_storage, row["event_id"])
        assert row["status"] == "retry_wait"
        runner.set_adapter_health_gate(AdapterHealthGate(lambda _: "healthy"))
        original = temp_storage.mark_outbox_sent
        original_claim = temp_storage.claim_due_outbox_items

        async def claim_then_append(*args, **kwargs):
            claims = await original_claim(*args, **kwargs)
            assert len(claims) == 1
            assert await temp_storage.append_delivery_observation(_late_acceptance(row))
            return claims

        async def lost_write(*args, **kwargs):
            if commit_before_raise:
                assert await original(*args, **kwargs)
            raise RuntimeError("test lost settlement response")

        now = datetime.fromisoformat(row["next_attempt_at"]) + timedelta(seconds=1)
        with monkeypatch.context() as patch:
            patch.setattr(temp_storage, "claim_due_outbox_items", claim_then_append)
            patch.setattr(temp_storage, "mark_outbox_sent", lost_write)
            await _run_worker_once(temp_storage, runner, now, worker=worker)
        row = await _row(temp_storage, row["event_id"])
        assert row["status"] == ("sent" if commit_before_raise else "in_progress")
        if not commit_before_raise:
            assert row["active_attempt"] is None
            projected = await temp_storage.get_outbox_item(row["outbox_id"])
            assert projected is not None
            assert projected.failure_kind_detail == "uncertain_native_handoff"
        # No status or schedule edits: use the next ordinary poll after expiry.
        await _run_worker_once(
            temp_storage, runner, now + timedelta(minutes=6), worker=worker
        )
        settled = await _row(temp_storage, row["event_id"])
        assert settled["status"] == "sent" and settled["attempt_number"] == 1
        assert adapters["fake_presentation"].delivered_payloads == []
    finally:
        await runner.stop()
