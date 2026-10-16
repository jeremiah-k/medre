"""Cross-worker recovery must retain each target's existing live delivery."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterHandoffResult, AdapterSendError
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import NativeRef
from medre.core.ingress import DurableIngressWorker
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem, StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


class _FailOnceAdapter(FakePresentationAdapter):
    def __init__(self) -> None:
        super().__init__(adapter_id="first")
        self.calls = 0

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self.calls += 1
        if self.calls == 1:
            raise AdapterSendError("temporary disconnect", retry_after_seconds=3600)
        return await super().deliver(result)


def _runner(
    storage: SQLiteStorage, adapters: dict[str, FakePresentationAdapter]
) -> PipelineRunner:
    route = Route(
        id="recover-fanout",
        source=RouteSource(
            adapter="fake_transport", event_kinds=("message.created",), channel=None
        ),
        targets=[RouteTarget(adapter=aid, channel="out") for aid in adapters],
    )
    config = make_pipeline_config_for_pipeline(
        storage, Router(routes=[route]), adapters
    )
    config.route_retry_policies[route.id] = RetryPolicy(jitter=False)
    return PipelineRunner(config)


@pytest.mark.parametrize("finish_retry", [False, True])
async def test_recovered_ingress_preserves_independent_target_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish_retry: bool
) -> None:
    """Replanning a partial fanout cannot bypass or repeat the first target's retry."""
    path = str(tmp_path / "recovery.sqlite3")
    storage = SQLiteStorage(path)
    await storage.initialize()
    first = _FailOnceAdapter()
    second = FakePresentationAdapter(adapter_id="second")
    runner = _runner(storage, {"first": first, "second": second})
    await runner.start()
    native_ref = NativeRef(
        adapter="fake_transport", native_channel_id="ch-0", native_message_id="original"
    )
    event = make_event(event_id="partial-fanout", source_native_ref=native_ref)
    await runner.admit_ingress(event, "live")
    create = storage.create_outbox_item

    async def fail_second(
        item: DeliveryOutboxItem, **kwargs: bool
    ) -> DeliveryOutboxItem:
        if item.target_adapter == "second":
            raise StorageError("injected failure before second target admission")
        return await create(item, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage, "create_outbox_item", fail_second)
            ingress = DurableIngressWorker(
                storage=storage, pipeline=runner, batch_size=1
            )
            assert await ingress.run_once() == 0
            assert ingress.deferrals == 1
            assert await storage.count_ingress_work_by_status() == {"pending": 1}
        pending = await storage.list_outbox_items_for_event(event.event_id)
        assert len(pending) == 1
        before = pending[0]
        assert before.status == "retry_wait"
        assert before.next_attempt_at is not None
        assert datetime.fromisoformat(before.next_attempt_at) > datetime.now(
            timezone.utc
        )
        assert first.calls == 1
        assert second.delivered_payloads == []

        if finish_retry:
            # Advance only durable scheduling, without sleeping an hour.
            await storage._write(
                "UPDATE delivery_outbox SET next_attempt_at=? WHERE outbox_id=?",
                ("2000-01-01T00:00:00+00:00", before.outbox_id),
            )
            retry = RetryWorker(storage, runner, None)
            await retry._process_due(datetime.now(timezone.utc))
            before = await storage.get_outbox_item(before.outbox_id)
            assert before is not None and before.status == "sent"
            assert before.attempt_number == 2
            assert first.calls == 2
    finally:
        await runner.stop()
        await storage.close()

    # New storage, pipeline and adapter instances model a fresh runtime generation.
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    resumed_first = FakePresentationAdapter(adapter_id="first")
    resumed_second = FakePresentationAdapter(adapter_id="second")
    resumed = _runner(reopened, {"first": resumed_first, "second": resumed_second})
    await resumed.start()
    try:
        duplicate = await resumed.admit_ingress(
            make_event(event_id="redecoded", source_native_ref=native_ref), "recovered"
        )
        assert duplicate.created is False and duplicate.event_id == event.event_id
        assert await reopened.get("redecoded") is None
        recovered = DurableIngressWorker(
            storage=reopened, pipeline=resumed, batch_size=1
        )
        assert await recovered.run_once() == 1
        assert await reopened.count_ingress_work_by_status() == {"completed": 1}
        assert (
            resumed_first.delivered_payloads == []
        ), "Existing retry belongs to the outbox worker"
        assert len(resumed_second.delivered_payloads) == 1
        items = await reopened.list_outbox_items_for_event(event.event_id)
        assert len(items) == 2
        first_items = [item for item in items if item.target_adapter == "first"]
        assert len(first_items) == 1
        after = first_items[0]
        assert after.outbox_id == before.outbox_id
        assert after.status == before.status
        assert after.attempt_number == before.attempt_number
        assert after.next_attempt_at == before.next_attempt_at
        receipts = await reopened.list_receipts_for_event(event.event_id)
        first_receipts = [
            receipt for receipt in receipts if receipt.target_adapter == "first"
        ]
        assert [(r.attempt_number, r.status, r.source) for r in first_receipts] == (
            [(1, "failed", "live"), (2, "sent", "retry")]
            if finish_retry
            else [(1, "failed", "live")]
        )
    finally:
        await resumed.stop()
        await reopened.close()
