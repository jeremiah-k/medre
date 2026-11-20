# medre-test-tier: compatibility
"""Crash-mid-deferral attempt accounting tests.

Pins the accounting contract around the fenced deferral release
(``defer_unsubmitted_outbox_attempt``) so crash windows stay explicit:

* the deferral commit lands as one storage transition: status to
  ``retry_wait``, ``next_attempt_at`` pushed, the reservation cleared, and
  ``attempt_number`` unchanged, with prior evidence untouched;
* a storage fault during the commit retains the live reservation for
  reconciliation and never reports the deferral as committed;
* a crash before the commit leaves a reservation without evidence, which
  the existing ambiguous-dispatch reconciliation consumes as a failed
  attempt (the transport may still have accepted the send);
* a crash after the commit leaves no reservation and no evidence: the
  restarted process re-reserves the same attempt identity and re-defers it
  without ever consuming it;
* across a restart with the adapter still unhealthy, the surviving
  obligation stays unconsumed under the lane's fresh fallback horizon and
  deferral diagnostics still count exactly the episodes that happened.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.adapter_health import (
    MAX_DEFERRAL_HORIZON_SECONDS,
    AdapterHealthGate,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline
from tests.helpers.storage_outbox import make_outbox_item

# ---------------------------------------------------------------------------
# Fixtures: one radio lane behind a health gate
# ---------------------------------------------------------------------------


def _radio_router() -> Router:
    return Router(
        routes=[
            Route(
                id="route-defer",
                source=RouteSource(
                    adapter="fake_transport",
                    event_kinds=("message.created",),
                    channel="ch-0",
                ),
                targets=[RouteTarget(adapter="radio")],
            )
        ]
    )


async def _make_runner(
    temp_storage: SQLiteStorage,
    adapters: dict[str, FakePresentationAdapter],
) -> PipelineRunner:
    config = make_pipeline_config_for_pipeline(temp_storage, _radio_router(), adapters)
    config.route_retry_policies["route-defer"] = RetryPolicy(
        max_attempts=4, jitter=False
    )
    runner = PipelineRunner(config)
    await runner.start()
    return runner


async def _radio_row(storage: SQLiteStorage, event_id: str) -> dict:
    rows = await storage._read_all(
        "SELECT * FROM delivery_outbox WHERE event_id = ? AND target_adapter = 'radio'",
        (event_id,),
    )
    assert len(rows) == 1
    return rows[0]


async def _receipt_rows(storage: SQLiteStorage, outbox_id: str) -> list[dict]:
    return await storage._read_all(
        "SELECT * FROM delivery_receipts WHERE outbox_id = ?", (outbox_id,)
    )


# ---------------------------------------------------------------------------
# The deferral commit itself
# ---------------------------------------------------------------------------


async def test_deferral_commit_is_one_atomic_transition(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """One release call lands every deferral effect as a single transition."""
    stored = await outbox_temp_storage.create_outbox_item(
        make_outbox_item(target_adapter="radio", attempt_number=0)
    )
    lifecycle = DeliveryLifecycleService()
    worker = "atomic-owner"
    due = datetime.now(timezone.utc).isoformat()

    claimed = {
        row.outbox_id: row
        for row in await outbox_temp_storage.claim_due_outbox_items(
            now=due, worker_id=worker
        )
    }[stored.outbox_id]
    assert await lifecycle.reserve_retry_attempt(outbox_temp_storage, claimed) == 1

    assert await lifecycle.defer_unsubmitted_attempt(
        outbox_temp_storage,
        stored.outbox_id,
        worker_id=worker,
        attempt_number=1,
        next_attempt_at=due,
    )

    row = await _radio_row(outbox_temp_storage, stored.event_id)
    assert row["status"] == "retry_wait"
    assert row["next_attempt_at"] == due
    assert row["attempt_number"] == 0
    assert row["active_attempt"] is None
    assert row["worker_id"] is None
    assert row["locked_at"] is None
    assert row["lease_until"] is None
    assert await _receipt_rows(outbox_temp_storage, stored.outbox_id) == []


async def test_crash_after_commit_re_defer_never_consumes_attempts(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """A committed deferral restarts on the same attempt identity, unconsumed."""
    stored = await outbox_temp_storage.create_outbox_item(
        make_outbox_item(target_adapter="radio", attempt_number=0)
    )
    lifecycle = DeliveryLifecycleService()
    due = datetime.now(timezone.utc).isoformat()

    claimed = {
        row.outbox_id: row
        for row in await outbox_temp_storage.claim_due_outbox_items(
            now=due, worker_id="crashed-owner"
        )
    }[stored.outbox_id]
    assert await lifecycle.reserve_retry_attempt(outbox_temp_storage, claimed) == 1
    assert await lifecycle.defer_unsubmitted_attempt(
        outbox_temp_storage,
        stored.outbox_id,
        worker_id="crashed-owner",
        attempt_number=1,
        next_attempt_at=due,
    )

    # The process dies; a restarted process re-claims the due row. The
    # deferral consumed nothing, so the interrupted episode's attempt
    # identity is re-reserved exactly, for as many restarts as happen.
    for restart in ("restart-a", "restart-b"):
        claimed = {
            row.outbox_id: row
            for row in await outbox_temp_storage.claim_due_outbox_items(
                now=due, worker_id=restart
            )
        }[stored.outbox_id]
        assert await lifecycle.reserve_retry_attempt(outbox_temp_storage, claimed) == 1
        assert await lifecycle.defer_unsubmitted_attempt(
            outbox_temp_storage,
            stored.outbox_id,
            worker_id=restart,
            attempt_number=1,
            next_attempt_at=due,
        )
        row = await _radio_row(outbox_temp_storage, stored.event_id)
        assert row["status"] == "retry_wait"
        assert row["attempt_number"] == 0
        assert row["active_attempt"] is None
        assert await _receipt_rows(outbox_temp_storage, stored.outbox_id) == []


async def test_storage_fault_mid_deferral_retains_reservation_for_reconciliation(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed release keeps the live reservation and is not a committed deferral."""
    adapters = {"radio": FakePresentationAdapter(adapter_id="radio")}
    runner = await _make_runner(temp_storage, adapters)
    health = {"radio": "degraded"}
    runner.set_adapter_health_gate(AdapterHealthGate(health.get))

    async def failing_release(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("storage unavailable mid-deferral")

    monkeypatch.setattr(
        temp_storage, "defer_unsubmitted_outbox_attempt", failing_release
    )
    try:
        with caplog.at_level(logging.INFO):
            await runner.handle_ingress(make_event(event_id="fault-defer"))

        assert adapters["radio"].delivered_payloads == []
        row = await _radio_row(temp_storage, "fault-defer")
        assert row["status"] == "in_progress"
        assert row["active_attempt"] == 1
        assert row["attempt_number"] == 0
        assert row["worker_id"] is not None
        assert await _receipt_rows(temp_storage, row["outbox_id"]) == []

        warn_lines = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "Health deferral did not commit" in record.getMessage()
        ]
        assert len(warn_lines) == 1
        assert runner._diagnostician.snapshot()["deferrals"] == {"radio": 1}

        # Reconciliation after lease expiry follows the existing ambiguous
        # dispatch rule: the reserved identity is consumed as a failed
        # attempt because the transport may still have accepted the send.
        monkeypatch.undo()
        lifecycle = DeliveryLifecycleService()
        lease_end = datetime.fromisoformat(row["lease_until"]) + timedelta(seconds=1)
        claimed = {
            r.outbox_id: r
            for r in await temp_storage.claim_due_outbox_items(
                now=lease_end.isoformat(), worker_id="reconciler"
            )
        }[row["outbox_id"]]
        finalization = await lifecycle.reconcile_retry_claim(
            temp_storage,
            claimed,
            RetryPolicy(max_attempts=4, jitter=False),
        )
        assert finalization is not None
        assert finalization.attempt_number == 1
        reconciled = await _radio_row(temp_storage, "fault-defer")
        assert reconciled["status"] == "retry_wait"
        assert reconciled["attempt_number"] == 1
    finally:
        await runner.stop()


# ---------------------------------------------------------------------------
# Restart with the adapter still unhealthy
# ---------------------------------------------------------------------------


async def test_restart_under_continuous_degradation_delivers_on_fresh_lane_horizon(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A surviving obligation restarts under a fresh fallback horizon, unburned."""
    clock = [0.0]
    monkeypatch.setattr(
        "medre.core.engine.pipeline.adapter_health.monotonic", lambda: clock[0]
    )
    monkeypatch.setattr(
        "medre.core.engine.pipeline.target_delivery."
        "DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS",
        0.0,
    )
    adapters = {"radio": FakePresentationAdapter(adapter_id="radio")}
    runner = await _make_runner(temp_storage, adapters)
    health = {"radio": "degraded"}
    runner.set_adapter_health_gate(AdapterHealthGate(health.get))
    try:
        with caplog.at_level(logging.INFO):
            await runner.handle_ingress(make_event(event_id="restart-lane"))

            row = await _radio_row(temp_storage, "restart-lane")
            assert row["status"] == "retry_wait"
            assert row["attempt_number"] == 0
            assert runner._diagnostician.snapshot()["deferrals"] == {"radio": 1}
            deferred_lines = [
                record
                for record in caplog.records
                if "Deferred: adapter=" in record.getMessage()
            ]
            assert len(deferred_lines) == 1

            # Restart: the fresh process holds no horizons. The worker's
            # held-work probe anchors a new lane-level fallback horizon, so
            # the lane stays closed for a fresh full horizon and the
            # surviving obligation is neither claimed nor re-counted.
            restarted = RetryWorker(
                cast(StorageBackend, temp_storage),
                runner,
                None,
                enabled=True,
                interval_seconds=60,
                available_target_adapters=["radio"],
                adapter_health_gate=AdapterHealthGate(health.get),
            )
            now = datetime.now(timezone.utc)
            assert await restarted._health_excluded_adapters(now) == ("radio",)
            await restarted._process_due(now)
            assert adapters["radio"].delivered_payloads == []
            assert runner._diagnostician.snapshot()["deferrals"] == {"radio": 1}

            # When the fallback horizon fires, the lane opens and the
            # surviving obligation dispatches under normal rules with the
            # deferred attempts still unconsumed.
            clock[0] = MAX_DEFERRAL_HORIZON_SECONDS
            assert await restarted._health_excluded_adapters(now) == ()
            await restarted._process_due(datetime.now(timezone.utc))
            assert len(adapters["radio"].delivered_payloads) == 1
            row = await _radio_row(temp_storage, "restart-lane")
            assert row["status"] == "sent"
            assert row["attempt_number"] == 1
            assert runner._diagnostician.snapshot()["deferrals"] == {"radio": 1}
            deferred_lines = [
                record
                for record in caplog.records
                if "Deferred: adapter=" in record.getMessage()
            ]
            assert len(deferred_lines) == 1
    finally:
        await runner.stop()
