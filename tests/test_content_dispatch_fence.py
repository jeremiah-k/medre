"""Native handoff is authorized by exact lease and attempt, not only an outbox ID."""

from datetime import datetime, timedelta, timezone

from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


async def test_native_submission_fenced_by_worker_attempt_lease_and_takeover(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "claims.db"))
    await storage.initialize()
    now = datetime.now(timezone.utc)
    try:
        await storage.admit_ingress(make_storage_event(event_id="evt"), None, "live")
        item = DeliveryOutboxItem(
            outbox_id="outbox-a",
            attempt_number=0,
            event_id="evt",
            delivery_plan_id="plan-a",
            route_id="route-a",
            target_adapter="fake",
            target_channel="one",
        )
        await storage.create_outbox_item(item)
        now = datetime.now(timezone.utc)
        await storage._write(
            "UPDATE delivery_outbox SET status='in_progress', worker_id=?, "
            "active_attempt=?, lease_until=? WHERE outbox_id=?",
            ("worker-a", 1, _iso(now + timedelta(minutes=1)), item.outbox_id),
        )

        async def allowed(worker: str, attempt: int, at: datetime = now) -> bool:
            return await storage.authorize_native_submission(
                "evt",
                item.outbox_id,
                worker_id=worker,
                attempt_number=attempt,
                now=at,
            )

        assert await allowed("worker-a", 1)
        assert not await allowed("worker-b", 1)
        assert not await allowed("worker-a", 2)
        assert not await allowed("worker-a", 1, now + timedelta(minutes=2))
        await storage._write(
            "UPDATE delivery_outbox SET worker_id=?, attempt_number=1, active_attempt=?, lease_until=? "
            "WHERE outbox_id=?",
            ("worker-b", 2, _iso(now + timedelta(minutes=3)), item.outbox_id),
        )
        assert not await allowed("worker-a", 1)
        assert not await allowed("worker-b", 1)
        assert await allowed("worker-b", 2)
        await storage._write(
            "UPDATE delivery_outbox SET status='sent', active_attempt=NULL, attempt_number=2 WHERE outbox_id=?",
            (item.outbox_id,),
        )
        assert not await allowed("worker-b", 2)
    finally:
        await storage.close()


async def test_newer_sibling_blocks_stale_native_generation(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "sibling.db"))
    await storage.initialize()
    now = datetime.now(timezone.utc)
    try:
        await storage.admit_ingress(make_storage_event(event_id="evt"), None, "live")
        for suffix in ("old", "new"):
            await storage.create_outbox_item(
                DeliveryOutboxItem(
                    outbox_id=suffix,
                    attempt_number=0 if suffix == "old" else 1,
                    event_id="evt",
                    delivery_plan_id="plan-a",
                    route_id="route-a",
                    target_adapter="fake",
                    target_channel="one",
                )
            )
        now = datetime.now(timezone.utc)
        await storage._write(
            "UPDATE delivery_outbox SET status='in_progress', worker_id=?, "
            "active_attempt=?, lease_until=? WHERE outbox_id=?",
            ("worker-old", 1, _iso(now + timedelta(minutes=1)), "old"),
        )
        await storage._write(
            "UPDATE delivery_outbox SET status='in_progress', worker_id=?, "
            "active_attempt=?, lease_until=? WHERE outbox_id=?",
            ("worker-new", 2, _iso(now + timedelta(minutes=1)), "new"),
        )
        assert not await storage.authorize_native_submission(
            "evt",
            "old",
            worker_id="worker-old",
            attempt_number=1,
            now=now,
        )
        assert await storage.authorize_native_submission(
            "evt",
            "new",
            worker_id="worker-new",
            attempt_number=2,
            now=now,
        )
    finally:
        await storage.close()


async def test_initial_pipeline_generation_has_precise_lease_authority(tmp_path):
    store = SQLiteStorage(str(tmp_path / "live.db"))
    await store.initialize()
    now = datetime.now(timezone.utc)
    try:
        await store.admit_ingress(make_storage_event(event_id="live"), None, "live")
        await store.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id="initial",
                event_id="live",
                delivery_plan_id="plan",
                route_id="route",
                target_adapter="adapter",
                target_channel=None,
                attempt_number=1,
                status="in_progress",
                worker_id="pipeline:abc",
                lease_until=_iso(now + timedelta(minutes=1)),
            )
        )
        now = datetime.now(timezone.utc)

        async def allowed(worker: str, attempt: int, at: datetime = now) -> bool:
            return await store.authorize_native_submission(
                "live",
                "initial",
                worker_id=worker,
                attempt_number=attempt,
                now=at,
            )

        assert await allowed("pipeline:abc", 1)
        assert not await allowed("pipeline:other", 1)
        assert not await allowed("pipeline:abc", 2)
        assert not await allowed("pipeline:abc", 1, now + timedelta(minutes=2))
        await store._write(
            "UPDATE delivery_outbox SET worker_id='retry:new' WHERE outbox_id='initial'"
        )
        assert not await allowed("pipeline:abc", 1)
        assert not await allowed("retry:new", 1)
    finally:
        await store.close()


async def test_rendering_cannot_cross_delivery_deadline_into_native_send(monkeypatch):
    from dataclasses import replace

    import pytest

    import medre.core.engine.pipeline.target_delivery as target_module
    from medre.core.contracts.adapter import AdapterHandoffResult
    from medre.core.engine.pipeline.target_delivery import _AdapterDeliveryError
    from medre.core.rendering.renderer import RenderingResult
    from tests.helpers.target_delivery import (
        _FakeAdapter,
        _FakeRenderingPipeline,
        _make_event,
        _make_route_and_plan,
        _make_service,
    )

    class Clock(datetime):
        current = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(target_module, "datetime", Clock)

    class SlowRenderer(_FakeRenderingPipeline):
        async def render(self, *args, **kwargs):
            Clock.current += timedelta(minutes=1)
            return await super().render(*args, **kwargs)

    class RecordingAdapter(_FakeAdapter):
        calls = 0

        async def deliver(self, rendering_result):
            self.calls += 1
            return AdapterHandoffResult(native_message_id="unsafe")

    adapter = RecordingAdapter()
    service, _ = _make_service(
        adapters={"test_adapter": adapter},
        rendering_pipeline=SlowRenderer(
            result=RenderingResult(
                event_id="evt-001",
                target_adapter="test_adapter",
                target_channel=None,
                payload={"text": "secret"},
            ),
        ),
    )
    route, plan = _make_route_and_plan()
    plan = replace(plan, deadline=Clock.current + timedelta(seconds=15))
    with pytest.raises(_AdapterDeliveryError):
        await service.deliver_to_target(_make_event(), route, plan)
    assert adapter.calls == 0


async def test_worker_lease_takeover_during_render_blocks_native_handoff(tmp_path):
    import pytest

    from medre.core.contracts.adapter import AdapterHandoffResult
    from medre.core.engine.pipeline.target_delivery import _AdapterDeliveryError
    from medre.core.rendering.renderer import RenderingResult
    from tests.helpers.target_delivery import (
        _FakeAdapter,
        _FakeRenderingPipeline,
        _make_event,
        _make_route_and_plan,
        _make_service,
    )

    store = SQLiteStorage(str(tmp_path / "takeover.db"))
    await store.initialize()
    event = _make_event()
    await store.admit_ingress(event, None, "live")
    now = datetime.now(timezone.utc)
    await store.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="takeover",
            event_id=event.event_id,
            delivery_plan_id="plan-001",
            route_id="route-001",
            target_adapter="test_adapter",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:old",
            lease_until=_iso(now + timedelta(minutes=2)),
        )
    )

    class StolenLeaseRenderer(_FakeRenderingPipeline):
        async def render(self, *args, **kwargs):
            await store._write(
                "UPDATE delivery_outbox SET worker_id='pipeline:new' "
                "WHERE outbox_id='takeover'"
            )
            return await super().render(*args, **kwargs)

    class RecordingAdapter(_FakeAdapter):
        calls = 0

        async def deliver(self, rendering_result):
            self.calls += 1
            return AdapterHandoffResult(native_message_id="unsafe")

    adapter = RecordingAdapter()
    service, _ = _make_service(
        adapters={"test_adapter": adapter},
        storage=store,
        rendering_pipeline=StolenLeaseRenderer(
            result=RenderingResult(
                event_id="evt-001",
                target_adapter="test_adapter",
                target_channel=None,
                payload={"text": "secret"},
            )
        ),
    )
    try:
        route, plan = _make_route_and_plan()
        with pytest.raises(_AdapterDeliveryError):
            await service.deliver_to_target(
                event,
                route,
                plan,
                outbox_id="takeover",
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:old",
            )
        assert adapter.calls == 0
    finally:
        await store.close()


async def test_native_lease_comparison_uses_instants_not_offset_text(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "offset-lease.db"))
    await storage.initialize()
    try:
        await storage.admit_ingress(make_storage_event(event_id="offset"), None, "live")
        now = datetime.now(timezone.utc)
        await storage.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id="offset",
                event_id="offset",
                delivery_plan_id="plan",
                route_id="route",
                target_adapter="fake",
                status="in_progress",
                attempt_number=1,
                worker_id="pipeline:offset",
                lease_until=_iso(now + timedelta(minutes=5)),
            )
        )
        now = datetime.now(timezone.utc)
        for until, expected in [
            (
                (now + timedelta(seconds=1))
                .astimezone(timezone(timedelta(hours=-5)))
                .isoformat(),
                True,
            ),
            (
                (now - timedelta(seconds=1))
                .astimezone(timezone(timedelta(hours=5)))
                .isoformat(),
                False,
            ),
            (now.astimezone(timezone(timedelta(hours=5))).isoformat(), False),
            ("malformed lease", False),
            (now.replace(tzinfo=None).isoformat(), False),
        ]:
            await storage._write(
                "UPDATE delivery_outbox SET lease_until=? WHERE outbox_id='offset'",
                (until,),
            )
            assert (
                await storage.authorize_native_submission(
                    "offset",
                    "offset",
                    worker_id="pipeline:offset",
                    attempt_number=1,
                    now=now,
                )
                is expected
            )
    finally:
        await storage.close()


async def test_native_submission_denies_an_orphaned_event(tmp_path):
    """A corrupt outbox row cannot treat a missing canonical event as contentless."""
    storage = SQLiteStorage(str(tmp_path / "orphan.db"))
    await storage.initialize()
    try:
        await storage.append(make_storage_event(event_id="orphan"))
        await storage.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id="orphan-owner",
                event_id="orphan",
                delivery_plan_id="plan",
                route_id="route",
                target_adapter="fake",
                status="in_progress",
                attempt_number=1,
                worker_id="pipeline:orphan",
                lease_until=_iso(datetime.now(timezone.utc) + timedelta(minutes=5)),
            )
        )
        assert await storage.authorize_native_submission(
            "orphan", "orphan-owner", worker_id="pipeline:orphan", attempt_number=1
        )

        def corrupt():
            db = storage._require_db()
            with storage._lock:
                db.execute("PRAGMA foreign_keys=OFF")
                try:
                    with db:
                        db.execute(
                            "DELETE FROM canonical_events WHERE event_id='orphan'"
                        )
                finally:
                    db.execute("PRAGMA foreign_keys=ON")

        await storage._run_in_thread(corrupt)
        assert not await storage.authorize_native_submission(
            "orphan", "orphan-owner", worker_id="pipeline:orphan", attempt_number=1
        )
    finally:
        await storage.close()


async def test_outbox_dispatch_without_exact_claim_is_denied_before_rendering():
    """An outbox generation without its worker and attempt is not a claim."""
    import pytest

    from medre.core.engine.pipeline.target_delivery import _AdapterDeliveryError
    from tests.helpers.target_delivery import (
        _FakeAdapter,
        _make_event,
        _make_route_and_plan,
        _make_service,
    )

    calls = []

    class RecordingAdapter(_FakeAdapter):
        async def deliver(self, rendering_result):
            calls.append(rendering_result)
            return await super().deliver(rendering_result)

    service, _ = _make_service(adapters={"test_adapter": RecordingAdapter()})
    route, plan = _make_route_and_plan()
    with pytest.raises(_AdapterDeliveryError, match="content_authority_denied"):
        await service.deliver_to_target(
            _make_event(), route, plan, outbox_id="obox-unclaimed"
        )
    assert calls == []
