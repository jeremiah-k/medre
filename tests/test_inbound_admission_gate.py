"""Bounded inbound admission gate tests.

The radio transports schedule one ingress coroutine per SDK callback with
no transport-level bound. The runtime's inbound admission gate bounds how
many of those coroutines may cross durable admission concurrently, rejects
arrivals that wait past the admission timeout, and exposes wait-depth and
oldest-wait diagnostics. These tests pin the controller contract, the
runtime seam behavior, and the environment override surface.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from medre.config.env import apply_env_overrides
from medre.config.errors import ConfigValidationError
from medre.config.model import (
    LoggingConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.routes import RouteConfigSet
from medre.core.ingress.types import DurableIngressDeferredError
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import (
    CapacityController,
    InboundAdmissionRejected,
)
from tests.helpers.async_utils import wait_until
from tests.helpers.inbound_gate import FakeAdmitRunner as _FakeRunner
from tests.helpers.inbound_gate import GateAppDouble as _GateApp
from tests.helpers.inbound_gate import LimitsDouble as _Limits


class TestInboundAdmissionController:
    """CapacityController bounds concurrent inbound admissions."""

    async def test_admissions_within_limit_run_concurrently(self) -> None:
        controller = CapacityController(_Limits())
        results = [await controller.acquire_inbound() for _ in range(2)]
        assert [bool(r) for r in results] == [True, True]
        assert controller.inbound_current == 2
        await controller.release_inbound()
        await controller.release_inbound()
        assert controller.inbound_current == 0

    async def test_beyond_limit_waits_until_release(self) -> None:
        controller = CapacityController(_Limits(inbound_admission_timeout_seconds=5.0))
        assert await controller.acquire_inbound()
        assert await controller.acquire_inbound()

        waiter = asyncio.create_task(controller.acquire_inbound())
        assert await wait_until(
            lambda: controller.snapshot()["inbound_admission_waiting"] == 1
        )
        assert not waiter.done()

        await controller.release_inbound()
        assert await asyncio.wait_for(waiter, timeout=1)
        assert controller.inbound_current == 2

    async def test_wait_past_timeout_rejects_and_counts(self) -> None:
        controller = CapacityController(_Limits())
        assert await controller.acquire_inbound()
        assert await controller.acquire_inbound()

        assert not await asyncio.wait_for(controller.acquire_inbound(), timeout=1)
        snapshot = controller.snapshot()
        assert snapshot["inbound_timeouts"] == 1
        assert snapshot["inbound_rejections"] == 0
        assert snapshot["inbound_admission_waiting"] == 0

    async def test_stop_accepting_keeps_inbound_admissible(self) -> None:
        controller = CapacityController(_Limits())
        controller.stop_accepting()
        # Delivery/replay acceptance closes; inbound stays open so late
        # adapter callbacks can still cross durable admission.
        assert controller.accepting_work is False
        assert controller.inbound_accepting is True
        assert await controller.acquire_inbound()
        assert controller.snapshot()["inbound_rejections"] == 0
        await controller.release_inbound()

    async def test_stop_accepting_inbound_rejects_immediately(self) -> None:
        controller = CapacityController(_Limits())
        controller.stop_accepting_inbound()
        assert controller.inbound_accepting is False
        assert not await controller.acquire_inbound()
        assert controller.snapshot()["inbound_rejections"] == 1

    async def test_stop_accepting_rejects_waiter_on_wake(self) -> None:
        controller = CapacityController(_Limits(inbound_admission_timeout_seconds=5.0))
        assert await controller.acquire_inbound()
        assert await controller.acquire_inbound()

        waiter = asyncio.create_task(controller.acquire_inbound())
        assert await wait_until(
            lambda: controller.snapshot()["inbound_admission_waiting"] == 1
        )
        controller.stop_accepting_inbound()

        # A queued waiter only wakes when a slot frees (the delivery and
        # replay gates behave the same way); on wake it observes the
        # stopped controller, releases the slot back, and is rejected.
        await controller.release_inbound()
        assert not await asyncio.wait_for(waiter, timeout=1)

        snapshot = controller.snapshot()
        assert snapshot["inbound_rejections"] == 1
        assert snapshot["inbound_current"] == 1

    async def test_snapshot_reports_oldest_wait_age(self) -> None:
        controller = CapacityController(_Limits(inbound_admission_timeout_seconds=5.0))
        assert await controller.acquire_inbound()
        assert await controller.acquire_inbound()

        waiter = asyncio.create_task(controller.acquire_inbound())
        assert await wait_until(
            lambda: controller.snapshot()["inbound_admission_waiting"] == 1
        )
        snapshot = controller.snapshot()
        assert snapshot["inbound_admission_oldest_wait_seconds"] is not None
        assert snapshot["inbound_admission_oldest_wait_seconds"] >= 0.0

        await controller.release_inbound()
        assert await asyncio.wait_for(waiter, timeout=1)
        await controller.release_inbound()
        await controller.release_inbound()
        final = controller.snapshot()
        assert final["inbound_admission_waiting"] == 0
        assert final["inbound_admission_oldest_wait_seconds"] is None

    async def test_wait_queue_overflow_rejects_immediately(self) -> None:
        controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=1,
                inbound_admission_timeout_seconds=5.0,
            )
        )
        assert await controller.acquire_inbound()
        # One arrival may wait; the next overflows the bounded wait queue.
        queued = asyncio.create_task(controller.acquire_inbound())
        assert await wait_until(
            lambda: controller.snapshot()["inbound_admission_waiting"] == 1
        )
        assert not await asyncio.wait_for(controller.acquire_inbound(), timeout=1)
        snapshot = controller.snapshot()
        assert snapshot["inbound_rejections"] == 1
        assert snapshot["inbound_timeouts"] == 0

        await controller.release_inbound()
        assert await asyncio.wait_for(queued, timeout=1)
        await controller.release_inbound()

    async def test_inbound_source_configuration_is_generation_stable(self) -> None:
        controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=2,
                inbound_admission_timeout_seconds=0.05,
            )
        )
        controller.configure_inbound_sources(["matrix", "radio"])

        # Repeating the same builder declaration is harmless, but a later
        # source-set mutation would change fair-share semantics mid-generation.
        controller.configure_inbound_sources(["radio", "matrix"])
        with pytest.raises(RuntimeError, match="fixed for the runtime generation"):
            controller.configure_inbound_sources(["matrix", "meshcore"])

    async def test_registered_sources_partition_wait_queue_fairly(self) -> None:
        controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=4,
                inbound_admission_timeout_seconds=5.0,
            )
        )
        controller.configure_inbound_sources(["noisy", "quiet"])

        # Active slots stay work-conserving: one source may use the whole
        # execution budget when nobody else is contending.
        for _ in range(4):
            assert await controller.acquire_inbound("noisy")

        # Pending overload is partitioned: each of two sources gets two of
        # the four bounded waiting slots, so noisy cannot consume quiet's
        # entire overload cushion.
        noisy_waiters = [
            asyncio.create_task(controller.acquire_inbound("noisy")) for _ in range(2)
        ]
        assert await wait_until(
            lambda: controller.snapshot()["inbound_sources"]["noisy"]["waiting"] == 2
        )
        assert not await controller.acquire_inbound("noisy")

        quiet_waiter = asyncio.create_task(controller.acquire_inbound("quiet"))
        assert await wait_until(
            lambda: controller.snapshot()["inbound_sources"]["quiet"]["waiting"] == 1
        )
        snapshot = controller.snapshot()
        assert snapshot["inbound_sources"]["noisy"]["wait_limit"] == 2
        assert snapshot["inbound_sources"]["quiet"]["wait_limit"] == 2
        assert snapshot["inbound_sources"]["noisy"]["rejections"] == 1

        # Grants rotate by source once both have pending work. The first
        # release serves noisy (queued first); the second serves quiet even
        # though noisy still has another waiter.
        await controller.release_inbound("noisy")
        assert await asyncio.wait_for(noisy_waiters[0], timeout=1)
        await controller.release_inbound("noisy")
        assert await asyncio.wait_for(quiet_waiter, timeout=1)
        assert not noisy_waiters[1].done()

        # Drain remaining ownership cleanly.
        await controller.release_inbound("quiet")
        assert await asyncio.wait_for(noisy_waiters[1], timeout=1)
        for _ in range(4):
            await controller.release_inbound("noisy")

    async def test_source_snapshot_attributes_timeout(self) -> None:
        controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=1,
                inbound_admission_timeout_seconds=0.02,
            )
        )
        controller.configure_inbound_sources(["a", "b"])
        assert await controller.acquire_inbound("a")
        assert not await controller.acquire_inbound("b")

        snapshot = controller.snapshot()
        assert snapshot["inbound_timeouts"] == 1
        assert snapshot["inbound_sources"]["a"]["current"] == 1
        assert snapshot["inbound_sources"]["b"]["timeouts"] == 1
        assert snapshot["inbound_sources"]["b"]["waiting"] == 0
        await controller.release_inbound("a")

    async def test_cancellation_after_grant_does_not_leak_slot(self) -> None:
        controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=1,
                inbound_admission_timeout_seconds=5.0,
            )
        )
        controller.configure_inbound_sources(["a", "b"])
        assert await controller.acquire_inbound("a")

        waiter = asyncio.create_task(controller.acquire_inbound("b"))
        assert await wait_until(
            lambda: controller.snapshot()["inbound_sources"]["b"]["waiting"] == 1
        )
        await controller.release_inbound("a")
        waiter.cancel()
        # A cancellation racing a completed grant resolves one of two lawful
        # ways: the cancel propagates and the controller returns the slot, or
        # (py3.11 wait_for semantics) the completed future's result wins, the
        # acquire returns True, and the caller owns the slot it must release.
        acquired = False
        try:
            acquired = await waiter
        except asyncio.CancelledError:
            acquired = False
        if acquired:
            await controller.release_inbound("b")

        assert controller.snapshot()["inbound_current"] == 0
        assert await controller.acquire_inbound("a")
        await controller.release_inbound("a")

    async def test_oldest_wait_age_follows_remaining_waiters(self) -> None:
        controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=2,
                inbound_admission_timeout_seconds=5.0,
            )
        )
        assert await controller.acquire_inbound()
        assert await controller.acquire_inbound()

        first = asyncio.create_task(controller.acquire_inbound())
        assert await wait_until(
            lambda: controller.snapshot()["inbound_admission_waiting"] == 1
        )
        second = asyncio.create_task(controller.acquire_inbound())
        assert await wait_until(
            lambda: controller.snapshot()["inbound_admission_waiting"] == 2
        )

        # The oldest waiter departs first (cancelled here; timing out or
        # acquiring would exercise the same bookkeeping). The reported
        # oldest wait must then reference the remaining, strictly younger
        # arrival instead of keeping the departed arrival's timestamp.
        departing_age = controller.snapshot()["inbound_admission_oldest_wait_seconds"]
        assert departing_age is not None
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first

        snapshot = controller.snapshot()
        assert snapshot["inbound_admission_waiting"] == 1
        remaining_age = snapshot["inbound_admission_oldest_wait_seconds"]
        assert remaining_age is not None
        assert remaining_age < departing_age

        await controller.release_inbound()
        assert await asyncio.wait_for(second, timeout=1)
        await controller.release_inbound()
        await controller.release_inbound()

    async def test_snapshot_keys_are_json_safe_and_sorted(self) -> None:
        controller = CapacityController(_Limits())
        snapshot = controller.snapshot()
        assert list(snapshot) == sorted(snapshot)
        for key in (
            "inbound_accepting",
            "inbound_admission_oldest_wait_seconds",
            "inbound_admission_waiting",
            "inbound_current",
            "inbound_limit",
            "inbound_rejections",
            "inbound_sources",
            "inbound_timeouts",
        ):
            assert key in snapshot


def _make_seam(runner: _FakeRunner, capacity: CapacityController | None) -> Any:
    app = _GateApp()
    app.pipeline_runner = runner
    app._capacity_controller = capacity
    return app._make_publish_inbound()


class TestInboundAdmissionSeam:
    """The runtime publish seam crosses the admission gate."""

    async def test_publish_runs_under_gate_and_admits(self) -> None:
        runner = _FakeRunner()
        controller = CapacityController(_Limits())
        publish = _make_seam(runner, controller)
        await publish("event-1")
        assert runner.admitted == ["event-1"]
        assert controller.snapshot()["inbound_timeouts"] == 0

    async def test_saturation_rejects_with_typed_error(self) -> None:
        runner = _FakeRunner(delay=0.2)
        controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=1,
                inbound_admission_timeout_seconds=0.05,
            )
        )
        publish = _make_seam(runner, controller)

        first = asyncio.create_task(publish("event-1"))
        assert await wait_until(lambda: controller.inbound_current == 1)
        with pytest.raises(InboundAdmissionRejected):
            await asyncio.wait_for(publish("event-2"), timeout=1)
        await asyncio.wait_for(first, timeout=1)

        assert runner.admitted == ["event-1"]
        snapshot = controller.snapshot()
        assert snapshot["inbound_timeouts"] == 1
        assert snapshot["inbound_current"] == 0

    async def test_publish_rejection_is_attributed_and_accounted(self) -> None:
        runner = _FakeRunner(delay=0.2)
        controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=1,
                inbound_admission_timeout_seconds=0.02,
            )
        )
        controller.configure_inbound_sources(["radio"])
        app = _GateApp()
        app.pipeline_runner = runner
        app._capacity_controller = controller
        app._runtime_accounting = RuntimeAccounting()
        publish = app._make_publish_inbound("radio")

        first = asyncio.create_task(publish("event-1"))
        assert await wait_until(lambda: controller.inbound_current == 1)
        with pytest.raises(InboundAdmissionRejected, match="radio"):
            await publish("event-2")
        await first

        assert app._runtime_accounting.snapshot()["capacity_rejections"] == 1
        assert controller.snapshot()["inbound_sources"]["radio"]["timeouts"] == 1

    async def test_cursor_aware_rejection_becomes_durable_deferral(self) -> None:
        runner = _FakeRunner(delay=0.2)
        controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=1,
                inbound_admission_timeout_seconds=0.02,
            )
        )
        controller.configure_inbound_sources(["matrix"])
        app = _GateApp()
        app.pipeline_runner = runner
        app._capacity_controller = controller
        app._runtime_accounting = RuntimeAccounting()
        admit = app._make_admit_inbound("matrix")

        first = asyncio.create_task(admit({"event_id": "$first"}, "live"))
        assert await wait_until(lambda: controller.inbound_current == 1)
        with pytest.raises(DurableIngressDeferredError) as exc_info:
            await admit({"event_id": "$second"}, "live")
        await first

        assert exc_info.value.event_id == "$second"
        assert exc_info.value.reasons == ("inbound_admission_capacity",)
        assert app._runtime_accounting.snapshot()["capacity_rejections"] == 1

    async def test_admission_failure_releases_the_slot(self) -> None:
        class _FailingRunner(_FakeRunner):
            async def admit_ingress(self, event, provenance, **_: Any) -> Any:
                self.admitted.append(event)
                raise RuntimeError("storage commit failed")

        runner = _FailingRunner()
        controller = CapacityController(_Limits())
        publish = _make_seam(runner, controller)

        with pytest.raises(RuntimeError, match="storage commit failed"):
            await publish("event-1")

        assert controller.inbound_current == 0
        # The slot was released: an immediate acquire succeeds without
        # waiting on the semaphore.
        assert await asyncio.wait_for(controller.acquire_inbound(), timeout=0.2)
        await controller.release_inbound()

    async def test_no_controller_runs_ungated(self) -> None:
        runner = _FakeRunner()
        publish = _make_seam(runner, None)
        await publish("event-1")
        assert runner.admitted == ["event-1"]


class TestInboundAdmissionConfig:
    """Limits validation and environment overrides reach the gate."""

    def test_defaults_are_positive(self) -> None:
        limits = RuntimeLimits().validate()
        assert limits.max_inflight_inbound_admissions == 100
        assert limits.inbound_admission_timeout_seconds == 5.0

    def test_non_positive_inbound_limits_rejected(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="max_inflight_inbound_admissions"
        ):
            RuntimeLimits(max_inflight_inbound_admissions=0).validate()
        with pytest.raises(
            ConfigValidationError, match="inbound_admission_timeout_seconds"
        ):
            RuntimeLimits(inbound_admission_timeout_seconds=0).validate()
        with pytest.raises(ConfigValidationError, match="finite"):
            RuntimeLimits(inbound_admission_timeout_seconds=float("inf")).validate()
        with pytest.raises(ConfigValidationError, match="finite"):
            RuntimeLimits(inbound_admission_timeout_seconds=float("nan")).validate()

    def test_env_overrides_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test"),
            logging=LoggingConfig(level="INFO"),
            storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
            routes=RouteConfigSet(),
        )
        monkeypatch.setenv("MEDRE_RUNTIME_MAX_INFLIGHT_INBOUND_ADMISSIONS", "7")
        monkeypatch.setenv("MEDRE_RUNTIME_INBOUND_ADMISSION_TIMEOUT_SECONDS", "2.5")
        overridden = apply_env_overrides(config)
        assert overridden.limits.max_inflight_inbound_admissions == 7
        assert overridden.limits.inbound_admission_timeout_seconds == 2.5

    def test_env_override_malformed_int_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test"),
            logging=LoggingConfig(level="INFO"),
            storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
            routes=RouteConfigSet(),
        )
        monkeypatch.setenv("MEDRE_RUNTIME_MAX_INFLIGHT_INBOUND_ADMISSIONS", "seven")
        with pytest.raises(ConfigValidationError):
            apply_env_overrides(config)
