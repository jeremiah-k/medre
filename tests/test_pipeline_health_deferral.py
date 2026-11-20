"""Health-aware delivery admission tests.

Covers the owner bench-log defects (2026-11-19): a meshcore adapter's BLE
link dropped and the pipeline still called its ``deliver()``, logged a
15-line traceback for an expected transient failure, and burned the retry
budget against a link that recovered minutes later. These tests pin the
deferral behavior:

* a degraded target is never attempted; the obligation is held durably
  without consuming an attempt and a ``delivery_deferred`` diagnostic (not
  ``adapter_failure``) is recorded while sibling healthy targets deliver;
* a degraded-to-healthy transition redispatches the held obligation;
* a stale-healthy race (health healthy, send raises a typed transient
  failure) logs exactly one WARN line and no traceback while keeping retry
  semantics intact;
* non-transient failures keep traceback logging (regression guard);
* the maximum deferral horizon resumes normal retry rules so an adapter
  that never recovers cannot strand obligations;
* each deferred obligation carries its own horizon keyed by the outbox
  identity: later work in a continuously unhealthy lane starts a fresh
  horizon, one obligation's expiry re-attempts only that obligation, and
  re-arms inside an episode stay silent;
* restart during deferral preserves the obligation through storage.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, cast

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterSendError
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.adapter_health import (
    MAX_DEFERRAL_HORIZON_SECONDS,
    AdapterHealthGate,
)
from medre.core.engine.pipeline.target_delivery import (
    DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from medre.core.storage.sqlite.constants import STALE_QUEUED_GRACE_SECONDS
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline
from tests.helpers.storage_outbox import make_outbox_item

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _TransientFailAdapter(FakePresentationAdapter):
    """Fake adapter whose ``deliver()`` raises a typed adapter send error."""

    def __init__(self, *, transient: bool, message: str) -> None:
        super().__init__(adapter_id="fake_presentation")
        self._transient = transient
        self._message = message

    async def deliver(self, result: Any):  # type: ignore[override]
        self.delivered_payloads.append(result)
        raise AdapterSendError(
            self._message,
            transient=self._transient,
        )


def _two_target_router() -> Router:
    """Router fanning out to a degraded and a healthy presentation target."""
    return Router(
        routes=[
            Route(
                id="route-degraded",
                source=RouteSource(
                    adapter="fake_transport",
                    event_kinds=("message.created",),
                    channel="ch-0",
                ),
                targets=[RouteTarget(adapter="fake_presentation")],
            ),
            Route(
                id="route-healthy",
                source=RouteSource(
                    adapter="fake_transport",
                    event_kinds=("message.created",),
                    channel="ch-0",
                ),
                targets=[RouteTarget(adapter="fake_presentation_2")],
            ),
        ]
    )


async def _make_runner(
    temp_storage: SQLiteStorage,
    router: Router,
    adapters: dict[str, Any],
) -> PipelineRunner:
    config = make_pipeline_config_for_pipeline(
        storage=temp_storage,
        router=router,
        adapters=adapters,
    )
    runner = PipelineRunner(config)
    await runner.start()
    return runner


async def _outbox_rows(storage: SQLiteStorage, event_id: str) -> list[dict]:
    return await storage._read_all(
        "SELECT * FROM delivery_outbox WHERE event_id = ? ORDER BY target_adapter",
        (event_id,),
    )


# ---------------------------------------------------------------------------
# Admission: degraded targets are deferred, not attempted
# ---------------------------------------------------------------------------


async def test_degraded_target_deferred_before_send(
    temp_storage: SQLiteStorage,
    router_with_routes: Router,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Degraded target: deliver() never called, obligation held, no failure evidence."""
    adapter = FakePresentationAdapter(adapter_id="fake_presentation")
    runner = await _make_runner(
        temp_storage, router_with_routes, {"fake_presentation": adapter}
    )
    runner.set_adapter_health_gate(
        AdapterHealthGate(
            lambda adapter_id: (
                "degraded" if adapter_id == "fake_presentation" else None
            )
        )
    )
    try:
        with caplog.at_level(logging.INFO):
            await runner.handle_ingress(make_event(event_id="def-001"))

        # deliver() was never invoked.
        assert adapter.delivered_payloads == []

        # Exactly one INFO deferral line in the existing style.
        deferred_lines = [
            record
            for record in caplog.records
            if "Deferred: adapter=" in record.getMessage()
        ]
        assert len(deferred_lines) == 1
        assert deferred_lines[0].levelno == logging.INFO
        assert "health=degraded" in deferred_lines[0].getMessage()

        # The obligation is held durably: retry_wait with a pushed
        # next_attempt and an unchanged attempt number.
        rows = await _outbox_rows(temp_storage, "def-001")
        assert len(rows) == 1
        assert rows[0]["status"] == "retry_wait"
        assert rows[0]["attempt_number"] == 0
        assert rows[0]["next_attempt_at"] is not None

        # No failure occurred: no attempt receipt, no dead-letter, and
        # the diagnostic lands under the deferral category, not
        # adapter_failure.
        receipts = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE event_id = 'def-001'"
        )
        assert receipts == []
        diagnostician = runner._diagnostician
        assert diagnostician.snapshot()["deferrals"] == {"fake_presentation": 1}
        assert diagnostician.snapshot()["adapter_failures"] == {}
    finally:
        await runner.stop()


async def test_sibling_healthy_target_still_delivered(
    temp_storage: SQLiteStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A degraded target defers while its healthy sibling receives its copy."""
    degraded = FakePresentationAdapter(adapter_id="fake_presentation")
    healthy = FakePresentationAdapter(adapter_id="fake_presentation_2")
    runner = await _make_runner(
        temp_storage,
        _two_target_router(),
        {"fake_presentation": degraded, "fake_presentation_2": healthy},
    )
    runner.set_adapter_health_gate(
        AdapterHealthGate(
            lambda adapter_id: (
                "degraded" if adapter_id == "fake_presentation" else "healthy"
            )
        )
    )
    try:
        with caplog.at_level(logging.INFO):
            await runner.handle_ingress(make_event(event_id="def-002"))

        assert degraded.delivered_payloads == []
        assert len(healthy.delivered_payloads) == 1

        rows = await _outbox_rows(temp_storage, "def-002")
        by_adapter = {row["target_adapter"]: row for row in rows}
        assert by_adapter["fake_presentation"]["status"] == "retry_wait"
        assert by_adapter["fake_presentation_2"]["status"] == "sent"

        deferred_lines = [
            record
            for record in caplog.records
            if "Deferred: adapter=" in record.getMessage()
        ]
        assert len(deferred_lines) == 1
    finally:
        await runner.stop()


# ---------------------------------------------------------------------------
# Durability: recovery redispatch, horizon safety net, restart continuity
# ---------------------------------------------------------------------------


async def test_recovery_redispatches_deferred_obligation(
    temp_storage: SQLiteStorage,
    router_with_routes: Router,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Degraded-to-healthy transition redispatches and delivers the obligation."""
    # Deterministic deferral push: the held obligation is immediately due
    # on recovery instead of waiting the default horizon push.
    monkeypatch.setattr(
        "medre.core.engine.pipeline.target_delivery."
        "DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS",
        0.0,
    )
    assert DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS == 30.0

    adapter = FakePresentationAdapter(adapter_id="fake_presentation")
    runner = await _make_runner(
        temp_storage, router_with_routes, {"fake_presentation": adapter}
    )
    health: dict[str, str] = {"fake_presentation": "degraded"}
    runner.set_adapter_health_gate(AdapterHealthGate(health.get))
    try:
        await runner.handle_ingress(make_event(event_id="def-003"))
        assert adapter.delivered_payloads == []
        rows = await _outbox_rows(temp_storage, "def-003")
        assert rows[0]["status"] == "retry_wait"

        # The degraded-to-healthy transition notifies the worker, which
        # clears deferral state and wakes dispatch.
        health["fake_presentation"] = "healthy"
        worker = RetryWorker(
            cast(StorageBackend, temp_storage),
            runner,
            None,
            enabled=True,
            interval_seconds=0.05,
            adapter_health_gate=AdapterHealthGate(health.get),
        )
        worker.notify_adapter_health("fake_presentation", "healthy")
        assert worker._health_gate is not None
        assert worker._health_gate._obligation_started == {}
        assert worker._health_gate._lane_started == {}
        assert await worker._health_excluded_adapters(datetime.now(timezone.utc)) == ()

        with caplog.at_level(logging.INFO, logger="medre.runtime.retry"):
            await worker._process_due(datetime.now(timezone.utc))

        assert len(adapter.delivered_payloads) == 1
        rows = await _outbox_rows(temp_storage, "def-003")
        assert rows[0]["status"] == "sent"
    finally:
        await runner.stop()


async def test_worker_skips_unhealthy_adapter_obligations(
    temp_storage: SQLiteStorage,
    router_with_routes: Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Due obligations of a degraded adapter are not claimed or attempted."""
    monkeypatch.setattr(
        "medre.core.engine.pipeline.target_delivery."
        "DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS",
        0.0,
    )
    adapter = FakePresentationAdapter(adapter_id="fake_presentation")
    runner = await _make_runner(
        temp_storage, router_with_routes, {"fake_presentation": adapter}
    )
    health: dict[str, str] = {"fake_presentation": "degraded"}
    runner.set_adapter_health_gate(AdapterHealthGate(health.get))
    try:
        await runner.handle_ingress(make_event(event_id="def-004"))
        rows = await _outbox_rows(temp_storage, "def-004")
        assert rows[0]["status"] == "retry_wait"

        worker = RetryWorker(
            cast(StorageBackend, temp_storage),
            runner,
            None,
            enabled=True,
            interval_seconds=0.05,
            available_target_adapters=["fake_presentation"],
            adapter_health_gate=AdapterHealthGate(health.get),
        )
        # Even with the obligation due, the degraded adapter is excluded
        # from claiming, so no attempt is burned and nothing is sent.
        assert await worker._health_excluded_adapters(datetime.now(timezone.utc)) == (
            "fake_presentation",
        )
        await worker._process_due(datetime.now(timezone.utc))
        assert adapter.delivered_payloads == []
        rows = await _outbox_rows(temp_storage, "def-004")
        assert rows[0]["status"] == "retry_wait"
    finally:
        await runner.stop()


async def test_deferral_horizon_expires_to_normal_retry_rules(
    temp_storage: SQLiteStorage,
    router_with_routes: Router,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Horizon expiry stops skipping the adapter so obligations are not stranded."""
    monkeypatch.setattr(
        "medre.core.engine.pipeline.target_delivery."
        "DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS",
        0.0,
    )
    adapter = FakePresentationAdapter(adapter_id="fake_presentation")
    runner = await _make_runner(
        temp_storage, router_with_routes, {"fake_presentation": adapter}
    )
    health: dict[str, str] = {"fake_presentation": "degraded"}
    # One shared gate, as the runtime wires it: the pipeline's admission
    # and the worker's claim exclusion compose on the same horizon state.
    shared_gate = AdapterHealthGate(health.get)
    runner.set_adapter_health_gate(shared_gate)
    try:
        await runner.handle_ingress(make_event(event_id="def-005"))

        worker = RetryWorker(
            cast(StorageBackend, temp_storage),
            runner,
            None,
            enabled=True,
            interval_seconds=0.05,
            available_target_adapters=["fake_presentation"],
            adapter_health_gate=shared_gate,
        )
        now = datetime.now(timezone.utc)
        assert await worker._health_excluded_adapters(now) == ("fake_presentation",)

        # Simulate the obligation having been deferred for the full horizon:
        # age every horizon the gate recorded for this lane.
        lane_clocks = shared_gate._obligation_started["fake_presentation"]
        assert lane_clocks
        for obligation_id in list(lane_clocks):
            lane_clocks[obligation_id] = monotonic() - MAX_DEFERRAL_HORIZON_SECONDS
        with caplog.at_level(
            logging.INFO, logger="medre.core.engine.pipeline.adapter_health"
        ):
            assert await worker._health_excluded_adapters(now) == ()
        horizon_lines = [
            record
            for record in caplog.records
            if "Deferral horizon expired" in record.getMessage()
        ]
        assert len(horizon_lines) == 1

        # Normal rules resume: the due obligation is claimed and
        # delivered (the fake accepts the send even while "degraded").
        await worker._process_due(now)
        assert len(adapter.delivered_payloads) == 1
        rows = await _outbox_rows(temp_storage, "def-005")
        assert rows[0]["status"] == "sent"
    finally:
        await runner.stop()


@pytest.mark.parametrize("retries_enabled", [True, False])
async def test_stale_queued_work_ages_deferral_only_when_retries_are_enabled(
    outbox_temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
    retries_enabled: bool,
) -> None:
    clock = 0.0
    monkeypatch.setattr(
        "medre.core.engine.pipeline.adapter_health.monotonic", lambda: clock
    )
    target = "meshcore"
    gate = AdapterHealthGate(lambda _: "degraded", max_horizon_seconds=10)
    worker = RetryWorker(
        outbox_temp_storage,
        cast(PipelineRunner, object()),
        None,
        enabled=retries_enabled,
        initial_dispatch=True,
        available_target_adapters=[target],
        adapter_health_gate=gate,
    )
    stored = await outbox_temp_storage.create_outbox_item(
        make_outbox_item(target_adapter=target)
    )
    assert stored.updated_at is not None
    assert await outbox_temp_storage.claim_due_outbox_items(
        stored.updated_at, "test-worker"
    )
    assert await outbox_temp_storage.mark_outbox_queued(stored.outbox_id)
    queued = await outbox_temp_storage.get_outbox_item(stored.outbox_id)
    assert queued is not None and queued.updated_at is not None
    fresh = datetime.fromisoformat(queued.updated_at)
    stale = fresh + timedelta(seconds=STALE_QUEUED_GRACE_SECONDS)

    assert await worker._health_excluded_adapters(fresh) == (target,)
    clock = 10.0
    assert await worker._health_excluded_adapters(stale) == (target,)
    clock = 20.0
    assert await worker._health_excluded_adapters(stale) == (
        () if retries_enabled else (target,)
    )


async def test_restart_during_deferral_preserves_obligation(
    temp_storage: SQLiteStorage,
    router_with_routes: Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deferral survives a runner restart: durable row, delivered after recovery."""
    monkeypatch.setattr(
        "medre.core.engine.pipeline.target_delivery."
        "DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS",
        0.0,
    )
    adapter = FakePresentationAdapter(adapter_id="fake_presentation")
    runner = await _make_runner(
        temp_storage, router_with_routes, {"fake_presentation": adapter}
    )
    health: dict[str, str] = {"fake_presentation": "degraded"}
    runner.set_adapter_health_gate(AdapterHealthGate(health.get))
    await runner.handle_ingress(make_event(event_id="def-006"))
    await runner.stop()

    # Restart: fresh pipeline on the same storage; the obligation is
    # still there, still deferred, with attempts unchanged.
    restarted = await _make_runner(
        temp_storage, router_with_routes, {"fake_presentation": adapter}
    )
    try:
        rows = await _outbox_rows(temp_storage, "def-006")
        assert rows[0]["status"] == "retry_wait"
        assert rows[0]["attempt_number"] == 0

        health["fake_presentation"] = "healthy"
        restarted.set_adapter_health_gate(AdapterHealthGate(health.get))
        worker = RetryWorker(
            cast(StorageBackend, temp_storage),
            restarted,
            None,
            enabled=True,
            interval_seconds=0.05,
            adapter_health_gate=AdapterHealthGate(health.get),
        )
        await worker._process_due(datetime.now(timezone.utc))
        assert len(adapter.delivered_payloads) == 1
        rows = await _outbox_rows(temp_storage, "def-006")
        assert rows[0]["status"] == "sent"
    finally:
        await restarted.stop()


# ---------------------------------------------------------------------------
# Per-obligation horizons: later work in a continuously unhealthy lane
# starts its own horizon and one expiry never strands its siblings
# ---------------------------------------------------------------------------


async def test_later_obligation_gets_fresh_horizon_and_sibling_expiry_reopens_lane(
    temp_storage: SQLiteStorage,
    router_with_routes: Router,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Obligations deferred minutes apart each keep a full horizon of their own."""
    clock = [0.0]
    monkeypatch.setattr(
        "medre.core.engine.pipeline.adapter_health.monotonic", lambda: clock[0]
    )
    monkeypatch.setattr(
        "medre.core.engine.pipeline.target_delivery."
        "DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS",
        0.0,
    )
    adapter = FakePresentationAdapter(adapter_id="fake_presentation")
    runner = await _make_runner(
        temp_storage, router_with_routes, {"fake_presentation": adapter}
    )
    health: dict[str, str] = {"fake_presentation": "degraded"}
    shared_gate = AdapterHealthGate(health.get)
    runner.set_adapter_health_gate(shared_gate)
    try:
        with caplog.at_level(logging.INFO):
            await runner.handle_ingress(make_event(event_id="horizon-001"))
            clock[0] = MAX_DEFERRAL_HORIZON_SECONDS / 2
            await runner.handle_ingress(make_event(event_id="horizon-002"))

            first = (await _outbox_rows(temp_storage, "horizon-001"))[0]
            second = (await _outbox_rows(temp_storage, "horizon-002"))[0]
            assert first["status"] == "retry_wait" and second["status"] == "retry_wait"
            assert first["attempt_number"] == 0 and second["attempt_number"] == 0

            worker = RetryWorker(
                cast(StorageBackend, temp_storage),
                runner,
                None,
                enabled=True,
                interval_seconds=0.05,
                available_target_adapters=["fake_presentation"],
                adapter_health_gate=shared_gate,
            )
            now = datetime.now(timezone.utc)

            # Both horizons run: the lane stays excluded for the full window.
            assert await worker._health_excluded_adapters(now) == ("fake_presentation",)

            # The first obligation's horizon expires; the lane opens for it
            # while the later obligation keeps its own remaining horizon.
            clock[0] = MAX_DEFERRAL_HORIZON_SECONDS
            assert await worker._health_excluded_adapters(now) == ()
            await worker._process_due(now)
            assert len(adapter.delivered_payloads) == 1
            first = (await _outbox_rows(temp_storage, "horizon-001"))[0]
            assert first["status"] == "sent"

            # The opened lane no longer shields the sibling from claims, but
            # its own running horizon keeps admission re-arming it silently:
            # repeat dispatch cycles neither deliver it nor burn an attempt,
            # and add no new deferral lines or diagnostics. Each re-arm
            # pushes next_attempt_at forward, so re-capture the clock.
            await worker._process_due(now)
            await worker._process_due(datetime.now(timezone.utc))
            second = (await _outbox_rows(temp_storage, "horizon-002"))[0]
            assert second["status"] == "retry_wait"
            assert second["attempt_number"] == 0

            # Its own horizon expiring admits it under normal rules.
            clock[0] = MAX_DEFERRAL_HORIZON_SECONDS * 1.5
            assert await worker._health_excluded_adapters(now) == ()
            await worker._process_due(datetime.now(timezone.utc))
            assert len(adapter.delivered_payloads) == 2
            second = (await _outbox_rows(temp_storage, "horizon-002"))[0]
            assert second["status"] == "sent"

            # Exactly one deferral episode per obligation: two deferral
            # lines, two deferral diagnostics, two horizon expiry lines, no
            # re-arm chatter for the sibling between the two expiries.
            deferred_lines = [
                record
                for record in caplog.records
                if "Deferred: adapter=" in record.getMessage()
            ]
            assert len(deferred_lines) == 2
            expiry_lines = [
                record
                for record in caplog.records
                if record.name == "medre.core.engine.pipeline.adapter_health"
                and "Deferral horizon expired" in record.getMessage()
            ]
            assert len(expiry_lines) == 2
            diagnostician = runner._diagnostician
            assert diagnostician.snapshot()["deferrals"] == {"fake_presentation": 2}
    finally:
        await runner.stop()


def test_gate_horizon_is_per_obligation_and_expiry_logs_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each obligation's horizon runs from its own first blocked admission."""
    clock = [0.0]
    monkeypatch.setattr(
        "medre.core.engine.pipeline.adapter_health.monotonic", lambda: clock[0]
    )
    gate = AdapterHealthGate(lambda _: "degraded", max_horizon_seconds=10)
    with caplog.at_level(
        logging.INFO, logger="medre.core.engine.pipeline.adapter_health"
    ):
        assert gate.blocking_health("radio", obligation_id="ob-1") == "degraded"
        assert gate.holds_obligation("radio", "ob-1")
        clock[0] = 5
        assert gate.blocking_health("radio", obligation_id="ob-1") == "degraded"
        # A later arrival starts its own horizon instead of inheriting the
        # first obligation's elapsed time.
        assert gate.blocking_health("radio", obligation_id="ob-2") == "degraded"
        # The first horizon expiring admits that obligation only.
        clock[0] = 10
        assert gate.blocking_health("radio", obligation_id="ob-1") is None
        assert gate.blocking_health("radio", obligation_id="ob-2") == "degraded"
        # The consumed horizon stays admitted; the later one expires on its
        # own schedule.
        clock[0] = 15
        assert gate.blocking_health("radio", obligation_id="ob-1") is None
        clock[0] = 21
        assert gate.blocking_health("radio", obligation_id="ob-2") is None

    expiry_lines = [
        record
        for record in caplog.records
        if "Deferral horizon expired" in record.getMessage()
    ]
    assert len(expiry_lines) == 2
    assert "obligation=ob-1" in expiry_lines[0].getMessage()
    assert "obligation=ob-2" in expiry_lines[1].getMessage()


def test_drained_lane_and_healthy_observation_clear_all_horizons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An observed empty lane or healthy observation ends every episode."""
    clock = [0.0]
    monkeypatch.setattr(
        "medre.core.engine.pipeline.adapter_health.monotonic", lambda: clock[0]
    )
    health = {"radio": "degraded"}
    gate = AdapterHealthGate(health.get, max_horizon_seconds=10)
    assert gate.blocking_health("radio", obligation_id="ob-1") == "degraded"
    assert gate.blocking_health("radio", obligation_id="ob-2") == "degraded"

    # Drained-lane observation: all obligation horizons end with the
    # episode, and the unhealthy idle lane stays excluded from claims.
    clock[0] = 1
    assert gate.excluded_adapters(["radio"], held=[]) == ("radio",)
    assert gate._obligation_started == {} and gate._obligation_expired == {}

    # The next blocked admission starts a fresh episode, not a residual one.
    clock[0] = 2
    assert gate.blocking_health("radio", obligation_id="ob-1") == "degraded"
    clock[0] = 12
    assert gate.blocking_health("radio", obligation_id="ob-1") is None

    # A healthy observation clears the fallback episode too, so a later
    # degradation anchors a new lane episode from its own first block.
    health["radio"] = "healthy"
    gate.observe("radio", "healthy")
    health["radio"] = "degraded"
    clock[0] = 20
    assert gate.blocking_health("radio") == "degraded"
    clock[0] = 31
    assert gate.blocking_health("radio") is None


# ---------------------------------------------------------------------------
# Logging discipline: transient WARN without traceback, traceback regression guard
# ---------------------------------------------------------------------------


async def test_transient_send_failure_logs_single_warn_without_traceback(
    temp_storage: SQLiteStorage,
    router_with_routes: Router,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stale-healthy race: typed transient failure is one WARN line, no traceback."""
    adapter = _TransientFailAdapter(
        transient=True,
        message="Session is not connected; native handoff has not occurred",
    )
    runner = await _make_runner(
        temp_storage, router_with_routes, {"fake_presentation": adapter}
    )
    runner.set_adapter_health_gate(
        AdapterHealthGate(
            lambda adapter_id: (
                "healthy" if adapter_id == "fake_presentation" else None
            )
        )
    )
    try:
        with caplog.at_level(logging.DEBUG, logger="medre.core.engine.pipeline.runner"):
            await runner.handle_ingress(make_event(event_id="warn-001"))

        records = [
            record for record in caplog.records if "warn-001" in record.getMessage()
        ]
        warnings = [record for record in records if record.levelno == logging.WARNING]
        assert len(warnings) == 1
        warn = warnings[0]
        assert "Transient delivery failure" in warn.getMessage()
        assert "Session is not connected" in warn.getMessage()
        assert "fake_presentation" in warn.getMessage()
        assert warn.event_id == "warn-001" if hasattr(warn, "event_id") else True
        # No traceback anywhere on the delivery logger for this event.
        assert all(not record.exc_info for record in records)
        assert all(not record.exc_text for record in records)

        # Existing retry semantics intact: without a route retry policy a
        # failed dispatch is terminal evidence at the same attempt.
        rows = await _outbox_rows(temp_storage, "warn-001")
        assert rows[0]["status"] == "dead_lettered"
        assert rows[0]["attempt_number"] == 1
    finally:
        await runner.stop()


async def test_non_transient_failure_still_logs_traceback(
    temp_storage: SQLiteStorage,
    router_with_routes: Router,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard: non-transient failures keep traceback logging."""
    adapter = _TransientFailAdapter(
        transient=False,
        message="adapter refused the payload permanently",
    )
    runner = await _make_runner(
        temp_storage, router_with_routes, {"fake_presentation": adapter}
    )
    runner.set_adapter_health_gate(
        AdapterHealthGate(
            lambda adapter_id: (
                "healthy" if adapter_id == "fake_presentation" else None
            )
        )
    )
    try:
        with caplog.at_level(logging.DEBUG, logger="medre.core.engine.pipeline.runner"):
            await runner.handle_ingress(make_event(event_id="warn-002"))

        records = [
            record
            for record in caplog.records
            if record.name == "medre.core.engine.pipeline.runner"
        ]
        errors = [
            record
            for record in records
            if record.levelno == logging.ERROR
            and "Delivery failed" in record.getMessage()
        ]
        assert len(errors) == 1
        assert errors[0].exc_info is not None
        # Exactly one traceback-bearing error for the unexpected failure.
        tracebacks = [record for record in records if record.exc_info]
        assert len(tracebacks) == 1
    finally:
        await runner.stop()


def test_idle_horizon_and_healthy_lookup_start_new_episode(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "medre.core.engine.pipeline.adapter_health.monotonic", lambda: clock[0]
    )
    health = {"radio": "degraded"}
    gate = AdapterHealthGate(health.get, max_horizon_seconds=10)
    assert gate.excluded_adapters(["radio"], held=[]) == ("radio",)
    clock[0] = 100
    assert gate.excluded_adapters(["radio"], held=["radio"]) == ("radio",)
    clock[0] = 110
    assert gate.excluded_adapters(["radio"], held=["radio"]) == ()
    health["radio"] = "healthy"
    assert gate.blocking_health("radio") is None
    health["radio"] = "degraded"
    assert gate.blocking_health("radio") == "degraded"
    clock[0] = 120
    assert gate.blocking_health("radio") is None
    assert gate.excluded_adapters(["radio"], held=[]) == ("radio",)
    assert gate.excluded_adapters(["radio"], held=["radio"]) == ("radio",)


async def test_late_health_change_releases_initial_reservation_and_notification_wakes_worker(
    temp_storage, monkeypatch, caplog
):
    from tests.helpers.async_utils import wait_until
    from tests.helpers.initial_outbox import _pipeline, _stage

    runner, route, adapters = _pipeline(temp_storage)
    route.targets = [RouteTarget("radio", "out")]
    health = {"radio": "healthy"}
    gate = AdapterHealthGate(health.get)
    runner.set_adapter_health_gate(gate)
    monkeypatch.setattr(
        "medre.core.engine.pipeline.target_delivery.DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS",
        0.0,
    )
    await _stage(temp_storage, runner, "late-initial")
    original = temp_storage.authorize_native_submission
    calls = 0

    async def authorize(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            health["radio"] = "degraded"
        return result

    monkeypatch.setattr(temp_storage, "authorize_native_submission", authorize)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        enabled=False,
        initial_dispatch=True,
        interval_seconds=60,
        adapter_health_gate=gate,
    )
    try:
        with caplog.at_level(logging.INFO):
            await worker._process_due(datetime.now(timezone.utc))
        (row,) = await _outbox_rows(temp_storage, "late-initial")
        assert calls == 2
        assert row["status"] == "retry_wait"
        assert row["attempt_number"] == 0 and row["active_attempt"] is None
        assert row["worker_id"] is None
        assert adapters["radio"].delivered_payloads == []
        assert (
            await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id='late-initial'"
            )
            == []
        )
        assert (
            len([r for r in caplog.records if "Deferred: adapter=" in r.getMessage()])
            == 1
        )
        assert await worker._health_excluded_adapters(datetime.now(timezone.utc)) == (
            "radio",
        )
        scanned = asyncio.Event()
        process_due = worker._process_due

        async def scan(*args, **kwargs):
            await process_due(*args, **kwargs)
            scanned.set()

        monkeypatch.setattr(worker, "_process_due", scan)
        await worker.start()
        await asyncio.wait_for(scanned.wait(), 3)
        assert adapters["radio"].delivered_payloads == []
        health["radio"] = "healthy"
        worker.notify_adapter_health("radio", "healthy")

        async def sent():
            (row,) = await _outbox_rows(temp_storage, "late-initial")
            return row["status"] == "sent" and row["attempt_number"] == 1

        assert await wait_until(sent)
        assert len(adapters["radio"].delivered_payloads) == 1
    finally:
        await worker.stop()
        await runner.stop()


@pytest.mark.parametrize("has_current_evidence", [False, True])
async def test_health_release_preserves_prior_attempt_and_refuses_current_evidence(
    temp_storage, has_current_evidence
):
    from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
    from medre.core.events.canonical import DeliveryReceipt
    from tests.helpers.initial_outbox import _pipeline, _stage

    runner, route, adapters = _pipeline(temp_storage)
    route.targets = [RouteTarget("radio", "out")]
    (item,) = await _stage(temp_storage, runner, "health-release")
    lifecycle = DeliveryLifecycleService()
    worker = "health-owner"

    async def claim():
        (row,) = await temp_storage.claim_due_outbox_items(
            now=datetime.now(timezone.utc).isoformat(), worker_id=worker
        )
        return row

    async def receipt(attempt):
        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id=f"health-receipt-{attempt}",
                event_id=item.event_id,
                delivery_plan_id=item.delivery_plan_id,
                target_adapter=item.target_adapter,
                target_channel=item.target_channel,
                route_id=item.route_id,
                status="failed",
                failure_kind="adapter_transient",
                error="seeded failure",
                attempt_number=attempt,
                source="retry",
                outbox_id=item.outbox_id,
                created_at=datetime.now(timezone.utc),
            )
        )

    try:
        claimed = await claim()
        assert await lifecycle.reserve_retry_attempt(temp_storage, claimed) == 1
        await receipt(1)
        assert await temp_storage.mark_outbox_retry_wait(
            item.outbox_id,
            next_attempt_at=datetime.now(timezone.utc).isoformat(),
            failure_kind="adapter_transient",
            expected_worker_id=worker,
            attempt_number=1,
        )
        claimed = await claim()
        assert await lifecycle.reserve_retry_attempt(temp_storage, claimed) == 2
        if has_current_evidence:
            await receipt(2)
        before = await temp_storage.get_outbox_item(item.outbox_id)
        receipts = await temp_storage._read_all(
            "SELECT * FROM delivery_receipts WHERE outbox_id=? ORDER BY attempt_number",
            (item.outbox_id,),
        )
        due = datetime.now(timezone.utc).isoformat()
        assert not await temp_storage.defer_unsubmitted_outbox_attempt(
            item.outbox_id,
            worker_id="other-worker",
            attempt_number=2,
            next_attempt_at=due,
        )
        assert not await temp_storage.defer_unsubmitted_outbox_attempt(
            item.outbox_id, worker_id=worker, attempt_number=3, next_attempt_at=due
        )
        assert await temp_storage.get_outbox_item(item.outbox_id) == before
        released = await temp_storage.defer_unsubmitted_outbox_attempt(
            item.outbox_id, worker_id=worker, attempt_number=2, next_attempt_at=due
        )
        assert released is not has_current_evidence
        after = await temp_storage.get_outbox_item(item.outbox_id)
        assert after is not None
        if has_current_evidence:
            assert after == before
        else:
            assert after.status == "retry_wait" and after.attempt_number == 1
            assert after.active_attempt is None and after.worker_id is None
            assert after.next_attempt_at == due
            assert (
                await lifecycle.reserve_retry_attempt(temp_storage, await claim()) == 2
            )
        assert (
            await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE outbox_id=? ORDER BY attempt_number",
                (item.outbox_id,),
            )
            == receipts
        )
    finally:
        await runner.stop()
