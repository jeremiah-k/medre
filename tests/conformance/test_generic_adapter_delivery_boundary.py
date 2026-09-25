"""Executable authoring contract for a future generic adapter.

The synthetic adapter deliberately has no platform-specific pipeline support.  It
proves that a fifth transport can express immediate hand-off, deferred hand-off,
terminal failure, and post-hand-off observations using only the public adapter
boundary.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from medre.core.contracts import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterHandoffResult,
    AdapterInfo,
    AdapterRole,
    DeferredHandoffCompleted,
    DeferredHandoffFailed,
    DeliveryFeedback,
    PostHandoffObservation,
)
from medre.core.events import DeliveryAttemptProvenance
from medre.core.rendering.renderer import RenderingResult


class _GenericProviderAdapter(AdapterContract):
    adapter_id = "generic-provider"
    platform = "generic-provider"
    role = AdapterRole.PRESENTATION

    def __init__(self, *, deferred: bool) -> None:
        super().__init__()
        self.deferred = deferred
        self.ctx: AdapterContext | None = None

    async def start(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self._mark_started(ctx)

    async def stop(self, timeout: float) -> None:
        del timeout
        self.ctx = None

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="test",
            capabilities=AdapterCapabilities(),
            health="healthy",
        )

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        if self.deferred:
            return AdapterHandoffResult(
                disposition="deferred",
                confirmation_level="local_queue",
            )
        return AdapterHandoffResult(
            disposition="transport_handoff",
            native_message_id="provider-123",
            native_channel_id=result.target_channel,
            confirmation_level="remote_service",
        )

    async def report(self, feedback: DeliveryFeedback) -> None:
        assert self.ctx is not None
        assert self.ctx.report_delivery_feedback is not None
        await self.ctx.report_delivery_feedback(feedback)


def _provenance() -> DeliveryAttemptProvenance:
    return DeliveryAttemptProvenance(
        event_id="evt-generic",
        delivery_plan_id="plan-generic",
        target_adapter="generic-provider",
        target_channel="room-1",
        outbox_id="outbox-generic",
        attempt_number=1,
        source="live",
    )


def _rendering_result() -> RenderingResult:
    provenance = _provenance()
    return RenderingResult(
        event_id=provenance.event_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        payload={"text": "hello"},
        delivery_plan_id=provenance.delivery_plan_id,
        outbox_id=provenance.outbox_id,
        attempt_number=provenance.attempt_number,
        attempt_provenance=provenance,
    )


async def _context(feedback: list[DeliveryFeedback]) -> AdapterContext:
    async def _publish(_event) -> None:
        return None

    async def _report(item: DeliveryFeedback) -> None:
        feedback.append(item)

    return AdapterContext(
        adapter_id="generic-provider",
        publish_inbound=_publish,
        logger=logging.getLogger("test.generic-provider"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        report_delivery_feedback=_report,
    )


async def test_fifth_adapter_needs_no_pipeline_specific_handoff_type() -> None:
    adapter = _GenericProviderAdapter(deferred=False)
    feedback: list[DeliveryFeedback] = []
    await adapter.start(await _context(feedback))

    handoff = await adapter.deliver(_rendering_result())

    assert handoff.disposition == "transport_handoff"
    assert handoff.native_message_id == "provider-123"
    assert handoff.confirmation_level == "remote_service"
    assert feedback == []


async def test_fifth_adapter_uses_one_feedback_sink_for_all_async_facts() -> None:
    adapter = _GenericProviderAdapter(deferred=True)
    feedback: list[DeliveryFeedback] = []
    await adapter.start(await _context(feedback))
    rendering = _rendering_result()

    handoff = await adapter.deliver(rendering)
    assert handoff.disposition == "deferred"

    provenance = rendering.attempt_provenance
    assert provenance is not None
    await adapter.report(
        DeferredHandoffCompleted(
            attempt_provenance=provenance,
            handoff=AdapterHandoffResult(
                native_message_id="provider-later-1",
                native_channel_id="room-1",
                confirmation_level="remote_service",
            ),
        )
    )
    await adapter.report(
        PostHandoffObservation(
            attempt_provenance=provenance,
            state="delivered",
            native_message_id="provider-later-1",
            native_channel_id="room-1",
            confirmation_level="end_to_end",
        )
    )
    await adapter.report(
        DeferredHandoffFailed(
            attempt_provenance=provenance,
            outcome="abandoned",
            error="synthetic shutdown race",
        )
    )

    assert [type(item) for item in feedback] == [
        DeferredHandoffCompleted,
        PostHandoffObservation,
        DeferredHandoffFailed,
    ]
    assert all(item.attempt_provenance is provenance for item in feedback)
