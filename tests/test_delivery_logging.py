"""Delivery logs describe the adapter boundary without claiming remote receipt."""

from __future__ import annotations

import logging

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterHandoffResult
from medre.core.engine.pipeline import PipelineRunner
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Router
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


@pytest.mark.parametrize(
    ("handoff", "label", "status"),
    [
        (AdapterHandoffResult(disposition="deferred"), "Delivery queued", "queued"),
        (
            AdapterHandoffResult(confirmation_level="local_transport"),
            "Handoff accepted",
            "sent",
        ),
    ],
)
async def test_handoff_log_matches_receipt_and_keeps_correlation(
    temp_storage: SQLiteStorage,
    router_with_routes: Router,
    caplog: pytest.LogCaptureFixture,
    handoff: AdapterHandoffResult,
    label: str,
    status: str,
) -> None:
    """Correlate human-readable handoffs with their durable delivery receipts."""

    class HandoffAdapter(FakePresentationAdapter):
        async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
            """Record a rendered payload and return the selected acceptance fact."""
            await super().deliver(result)
            return handoff

    adapter = HandoffAdapter(adapter_id="fake_presentation")
    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={adapter.adapter_id: adapter},
        )
    )
    await runner.start()
    try:
        with caplog.at_level(logging.INFO, logger="medre"):
            await runner.handle_ingress(make_event(event_id="log-handoff"))
        records = [
            record for record in caplog.records if record.getMessage().startswith(label)
        ]
        assert len(records) == 1
        record = records[0]
        message = record.getMessage()
        assert "adapter=fake_presentation" in message
        assert "event=log-handoff" in message
        assert f"confirmation={handoff.confirmation_level}" in message
        assert "Delivered:" not in message
        assert record.plan_id not in message
        assert record.event_id == "log-handoff"
        assert record.adapter_id == "fake_presentation"
        assert record.handoff == handoff.disposition
        assert record.attempt_number == 1
        receipts = await temp_storage.list_receipts_for_event("log-handoff")
        assert len(receipts) == 1
        assert record.plan_id == receipts[0].delivery_plan_id
        assert receipts[0].status == status
    finally:
        await runner.stop()
