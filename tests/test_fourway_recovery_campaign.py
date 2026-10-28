"""SDK-free four-target recovery campaign using real SQLite and pipeline state.

This does not claim BLE, SDK or transport availability qualification. It
proves that a failed target's durable work is independent of healthy targets
across a process-generation boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterHandoffResult, AdapterSendError
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import NativeRef
from medre.core.ingress import DurableIngressWorker
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


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
