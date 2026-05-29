"""Tests for PipelineRunner._deliver_one exception handlers.

Exercises the ``_AdapterDeliveryError``, ``_RendererDeliveryError``,
``CancelledError``, and generic ``Exception`` paths in the inner
``_deliver_one`` closure, verifying that each produces a correct
:class:`DeliveryOutcome` with the expected failure kind and status.
"""

from __future__ import annotations

import asyncio

import pytest

from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.target_delivery import (
    _AdapterDeliveryError,
    _RendererDeliveryError,
)
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    DeliveryStrategy,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


def _make_route() -> Route:
    return Route(
        id="route-del-err",
        source=RouteSource(
            adapter="src", channel=None, event_kinds=("message.created",)
        ),
        targets=[RouteTarget(adapter="dest")],
    )


def _make_plan(plan_id: str = "plan-del-err") -> DeliveryPlan:
    return DeliveryPlan(
        plan_id=plan_id,
        event_id="evt-del-err",
        target=RouteTarget(adapter="dest"),
        primary_strategy=DeliveryStrategy(method="direct"),
    )


def _make_runner(
    temp_storage: StorageBackend,
) -> PipelineRunner:
    """Build a minimal PipelineRunner with no capacity controller or stats."""
    router = Router(routes=[_make_route()])
    config = make_pipeline_config_for_pipeline(
        storage=temp_storage,
        router=router,
        adapters={"dest": _BrokenAdapter()},
    )
    return PipelineRunner(config)


class _BrokenAdapter:
    """A minimal adapter stub that always raises on every call.

    Used to satisfy the adapter-registry check in ``_deliver_one``
    Phase 2.5 so the delivery attempt reaches Phase 4.
    """

    async def deliver(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("unused")

    @property
    def platform(self) -> str | None:
        return None


# ===================================================================
# _AdapterDeliveryError handler (lines 1417-1445)
# ===================================================================


class TestDeliverOneAdapterDeliveryError:
    """_deliver_one handles _AdapterDeliveryError with correct failure_kind."""

    async def test_failure_kind_from_exception(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """exc.failure_kind is set → used directly."""
        from unittest.mock import AsyncMock

        runner = _make_runner(temp_storage)
        event = make_event(event_id="evt-ad-1", source_adapter="src")
        route = _make_route()
        plan = _make_plan()
        receipt = _dummy_receipt(event.event_id, plan.plan_id)

        runner.deliver_to_target = AsyncMock(  # type: ignore[assignment]
            side_effect=_AdapterDeliveryError(
                "dest",
                "adapter missing",
                failure_kind=DeliveryFailureKind.ADAPTER_MISSING,
                receipt=receipt,
            ),
        )
        outcomes = await runner.deliver_to_targets(event, [(route, plan)])
        assert len(outcomes) == 1
        assert outcomes[0].failure_kind == DeliveryFailureKind.ADAPTER_MISSING
        assert outcomes[0].status == "permanent_failure"
        assert outcomes[0].error == "adapter missing"

    async def test_failure_kind_from_original(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """exc.failure_kind is None, exc.original set → classify_failure."""
        from unittest.mock import AsyncMock

        runner = _make_runner(temp_storage)
        event = make_event(event_id="evt-ad-2", source_adapter="src")
        route = _make_route()
        plan = _make_plan()
        receipt = _dummy_receipt(event.event_id, plan.plan_id)

        # Create an error that classify_failure maps to ADAPTER_TRANSIENT.
        import medre.core.contracts.adapter as ca

        original = ca.AdapterSendError("wire timeout")
        runner.deliver_to_target = AsyncMock(  # type: ignore[assignment]
            side_effect=_AdapterDeliveryError(
                "dest",
                "send failed",
                original=original,
                failure_kind=None,
                receipt=receipt,
            ),
        )
        outcomes = await runner.deliver_to_targets(event, [(route, plan)])
        assert len(outcomes) == 1
        assert outcomes[0].failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT
        assert outcomes[0].status == "transient_failure"
        assert "send failed" in (outcomes[0].error or "")

    async def test_failure_kind_fallback_transient(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Both failure_kind and original are None → ADAPTER_TRANSIENT."""
        from unittest.mock import AsyncMock

        runner = _make_runner(temp_storage)
        event = make_event(event_id="evt-ad-3", source_adapter="src")
        route = _make_route()
        plan = _make_plan()
        receipt = _dummy_receipt(event.event_id, plan.plan_id)

        runner.deliver_to_target = AsyncMock(  # type: ignore[assignment]
            side_effect=_AdapterDeliveryError(
                "dest",
                "unknown error",
                failure_kind=None,
                receipt=receipt,
            ),
        )
        outcomes = await runner.deliver_to_targets(event, [(route, plan)])
        assert len(outcomes) == 1
        assert outcomes[0].failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT
        assert outcomes[0].status == "transient_failure"


# ===================================================================
# _RendererDeliveryError handler (lines 1446-1471)
# ===================================================================


class TestDeliverOneRendererDeliveryError:
    """_deliver_one handles _RendererDeliveryError → permanent_failure."""

    async def test_renderer_delivery_error_handler(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """RendererDeliveryError → permanent_failure, RENDERER_FAILURE."""
        from unittest.mock import AsyncMock

        runner = _make_runner(temp_storage)
        event = make_event(event_id="evt-rd-1", source_adapter="src")
        route = _make_route()
        plan = _make_plan()
        receipt = _dummy_receipt(event.event_id, plan.plan_id)

        runner.deliver_to_target = AsyncMock(  # type: ignore[assignment]
            side_effect=_RendererDeliveryError(
                "dest",
                "render failed",
                receipt=receipt,
                failure_kind=DeliveryFailureKind.RENDERER_FAILURE,
            ),
        )
        outcomes = await runner.deliver_to_targets(event, [(route, plan)])
        assert len(outcomes) == 1
        assert outcomes[0].failure_kind == DeliveryFailureKind.RENDERER_FAILURE
        assert outcomes[0].status == "permanent_failure"
        assert outcomes[0].receipt is None

    async def test_renderer_delivery_error_default_kind(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """No failure_kind on error → falls back to RENDERER_FAILURE."""
        from unittest.mock import AsyncMock

        runner = _make_runner(temp_storage)
        event = make_event(event_id="evt-rd-2", source_adapter="src")
        route = _make_route()
        plan = _make_plan()
        receipt = _dummy_receipt(event.event_id, plan.plan_id)

        runner.deliver_to_target = AsyncMock(  # type: ignore[assignment]
            side_effect=_RendererDeliveryError(
                "dest",
                "render boom",
                receipt=receipt,
            ),
        )
        outcomes = await runner.deliver_to_targets(event, [(route, plan)])
        assert len(outcomes) == 1
        assert outcomes[0].failure_kind == DeliveryFailureKind.RENDERER_FAILURE
        assert outcomes[0].status == "permanent_failure"


# ===================================================================
# CancelledError propagation (lines 1472-1475)
# ===================================================================


class TestDeliverOneCancelledError:
    """CancelledError propagates through _deliver_one."""

    async def test_cancelled_error_propagates(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """CancelledError is re-raised, not caught as generic failure."""
        from unittest.mock import AsyncMock

        runner = _make_runner(temp_storage)
        event = make_event(event_id="evt-cancel", source_adapter="src")
        route = _make_route()
        plan = _make_plan()

        runner.deliver_to_target = AsyncMock(  # type: ignore[assignment]
            side_effect=asyncio.CancelledError(),
        )
        with pytest.raises(asyncio.CancelledError):
            await runner.deliver_to_targets(event, [(route, plan)])


# ===================================================================
# Generic Exception handler (lines 1476-1509)
# ===================================================================


class TestDeliverOneGenericException:
    """Generic Exception in deliver_to_target → classified outcome."""

    async def test_generic_exception_classified(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Plain Exception → classify_failure with adapter_registered flag."""
        from unittest.mock import AsyncMock

        runner = _make_runner(temp_storage)
        event = make_event(event_id="evt-gen-1", source_adapter="src")
        route = _make_route()
        plan = _make_plan()

        runner.deliver_to_target = AsyncMock(  # type: ignore[assignment]
            side_effect=ValueError("something broke"),
        )
        outcomes = await runner.deliver_to_targets(event, [(route, plan)])
        assert len(outcomes) == 1
        # ValueError is not an adapter error → classify_failure decides.
        assert outcomes[0].failure_kind is not None
        assert outcomes[0].error is not None
        assert "ValueError" in outcomes[0].error  # type: ignore[operator]


# ===================================================================
# Helpers
# ===================================================================


def _dummy_receipt(event_id: str, plan_id: str) -> object:
    """Build a minimal receipt stub for exception payloads."""
    from medre.core.events.canonical import DeliveryReceipt

    return DeliveryReceipt(
        sequence=0,
        receipt_id="rcpt-dummy",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="dest",
        target_channel=None,
        route_id="route-del-err",
        status="failed",
        error=None,
        failure_kind=None,
        created_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        attempt_number=1,
    )
