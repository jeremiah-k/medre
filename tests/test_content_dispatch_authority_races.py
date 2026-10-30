"""Native boundaries refuse deadlines that pass during authorization waits."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.contracts.adapter import AdapterHandoffResult
from medre.core.engine.pipeline.target_delivery import _AdapterDeliveryError
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.rendering.renderer import RenderingResult
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.target_delivery import (
    _FakeAdapter,
    _FakeRenderingPipeline,
    _make_event,
    _make_route_and_plan,
    _make_service,
)


async def test_deadline_crossed_during_final_authorization_never_invokes_adapter(
    tmp_path, monkeypatch
):
    import medre.core.engine.pipeline.target_delivery as target_module

    store = SQLiteStorage(str(tmp_path / "boundary.db"))
    await store.initialize()
    event = _make_event()
    try:
        await store.admit_ingress(event, None, "live")
        await store.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id="boundary",
                event_id=event.event_id,
                delivery_plan_id="plan-001",
                route_id="route-001",
                target_adapter="test_adapter",
                status="in_progress",
                attempt_number=1,
                worker_id="pipeline:clock",
                lease_until=(
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            )
        )

        class Clock(datetime):
            current = datetime.now(timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current

        monkeypatch.setattr(target_module, "datetime", Clock)
        route, plan = _make_route_and_plan()
        plan = replace(plan, deadline=Clock.current + timedelta(seconds=10))
        original = store.authorize_native_submission
        authorizations = 0

        async def wait_at_boundary(*args, **kwargs):
            nonlocal authorizations
            allowed = await original(*args, **kwargs)
            authorizations += 1
            if authorizations == 2:
                Clock.current = plan.deadline + timedelta(seconds=1)
            return allowed

        monkeypatch.setattr(store, "authorize_native_submission", wait_at_boundary)

        class Adapter(_FakeAdapter):
            calls = 0

            async def deliver(self, result):
                self.calls += 1
                return AdapterHandoffResult()

        adapter = Adapter()
        service, _ = _make_service(
            adapters={"test_adapter": adapter},
            storage=store,
            rendering_pipeline=_FakeRenderingPipeline(
                result=RenderingResult(
                    event_id=event.event_id,
                    target_adapter="test_adapter",
                    target_channel=None,
                    payload={"text": "hello"},
                )
            ),
        )
        with pytest.raises(_AdapterDeliveryError) as caught:
            await service.deliver_to_target(
                event,
                route,
                plan,
                outbox_id="boundary",
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:clock",
            )
        assert authorizations == 2
        assert adapter.calls == 0
        assert (
            caught.value.receipt.failure_kind
            == DeliveryFailureKind.DEADLINE_EXCEEDED.value
        )
    finally:
        await store.close()


@pytest.mark.parametrize("operation", ["replay", "reply", "native"])
async def test_authority_samples_default_clock_after_transaction_acquisition(
    tmp_path, monkeypatch, operation
):
    import sqlite3

    import medre.core.storage.sqlite._content_grants as grants_module

    store = SQLiteStorage(str(tmp_path / "queued-clock.db"))
    await store.initialize()
    event = _make_event()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    try:
        await store.admit_ingress(
            event,
            None,
            "live",
            content_deadline=None if operation == "native" else deadline,
        )
        if operation == "native":
            await store.create_outbox_item(
                DeliveryOutboxItem(
                    outbox_id="clock",
                    event_id=event.event_id,
                    delivery_plan_id="plan",
                    route_id="route",
                    target_adapter="fake",
                    status="in_progress",
                    worker_id="pipeline:clock",
                    attempt_number=1,
                    lease_until=deadline.isoformat(),
                )
            )

        class Clock(datetime):
            current = datetime.now(timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current

        monkeypatch.setattr(grants_module, "datetime", Clock)

        def transaction_acquired(action, first, second, database, source):
            if action == sqlite3.SQLITE_TRANSACTION and first == "BEGIN":
                Clock.current = deadline + timedelta(seconds=1)
            return sqlite3.SQLITE_OK

        await store._run_in_thread(
            store._require_db().set_authorizer, transaction_acquired
        )
        try:
            if operation == "native":
                assert not await store.authorize_native_submission(
                    event.event_id,
                    "clock",
                    worker_id="pipeline:clock",
                    attempt_number=1,
                )
            elif operation == "reply":
                assert (
                    await store.read_reply_context(event.event_id)
                ).status == "denied"
            else:
                assert (
                    await store.read_event_for_replay(event.event_id)
                ).status == "denied"
        finally:
            await store._run_in_thread(store._require_db().set_authorizer, None)
        assert await store.get_metadata(event.event_id) is not None
    finally:
        await store.close()
