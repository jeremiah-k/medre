"""Replay planning stage: delivery plan construction."""

from __future__ import annotations

import time
from typing import Any

from medre.core.engine.replay.helpers import _elapsed_ms
from medre.core.engine.replay.types import ReplayResult
from medre.core.events import CanonicalEvent


class _ReplayPlanningMixin:
    async def _stage_plan(
        self,
        event: CanonicalEvent,
        route_result: list[tuple[Any, Any]] | None,
    ) -> tuple[ReplayResult, list[Any] | None]:
        """Build delivery plans for *event* based on routing results.

        Returns the :class:`ReplayResult` and the delivery plans for use
        by downstream stages.

        When *route_result* already contains ``DeliveryPlan`` objects
        (i.e. from the real PipelineRunner), the route--plan pairs are
        preserved as ``list[tuple[Route, DeliveryPlan]]`` so that
        :meth:`_stage_deliver` can call ``deliver_to_targets``.
        For stub pipelines where the second element is not a
        ``DeliveryPlan``, the ``plan_delivery`` fallback path is used
        and bare plans are returned so that :meth:`_stage_deliver`
        can process them.
        """
        t0 = time.monotonic()
        if route_result is None:
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="skipped",
                    error="No route result available; routing may have errored",
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )

        # Empty route_result means routes were filtered out (e.g. loop
        # prevention) --- nothing to plan.
        if not route_result:
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="skipped",
                    error="No routes matched after filtering",
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )

        # If route_result items already contain DeliveryPlan objects
        # (real pipeline returns list[tuple[Route, DeliveryPlan]]),
        # preserve the route--plan pairs.  For stub pipelines where the
        # second element is not a DeliveryPlan, we fall through to the
        # plan_delivery path below.
        plans: list[Any] = []
        all_delivery_plans = True
        for route, plan_or_target in route_result:
            if hasattr(plan_or_target, "target") and hasattr(plan_or_target, "plan_id"):
                # Preserve route--plan pairs so that _stage_deliver can
                # call deliver_to_targets with the correct shape.
                plans.append((route, plan_or_target))
            else:
                all_delivery_plans = False
                break

        if all_delivery_plans and plans:
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="passed",
                    output=plans,
                    duration_ms=_elapsed_ms(t0),
                ),
                plans,
            )

        # Fall back to pipeline's plan_delivery for stubs.
        if self._pipeline is None:
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="error",
                    error="No pipeline configured; planning requires a pipeline",
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )
        if not hasattr(self._pipeline, "plan_delivery"):
            raise RuntimeError(
                "Pipeline has no deliver_to_targets and no plan_delivery; "
                "cannot build delivery plans for event_id=" + event.event_id
            )
        try:
            plans = await self._pipeline.plan_delivery(event, route_result)
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="passed",
                    output=plans,
                    duration_ms=_elapsed_ms(t0),
                ),
                plans,
            )
        except Exception as exc:
            if self._diagnostician is not None:
                self._diagnostician.record_planner_failure(
                    event.event_id,
                    str(exc),
                )
            return (
                ReplayResult(
                    event_id=event.event_id,
                    stage="plan",
                    status="error",
                    error=str(exc),
                    duration_ms=_elapsed_ms(t0),
                ),
                None,
            )
