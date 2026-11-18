# medre-test-tier: compatibility
"""Native deadline enforcement through the pinned mtjk SDK and meshtasticd.

C-0 positive control: a pre-bound delivery really invokes the pinned SDK's
``sendText`` entry against a genuine daemon session and retains its real
local feedback. At equality with the effective bound, the final synchronous
dispatch-start fence refuses with zero new SDK invocations, typed deadline
denial evidence, and no invented native acceptance. An earlier frozen plan
deadline wins over a later event content ceiling.

C-1 per-packet boundary: a genuine pre-expiry local packet handoff is
checkpointed with its real SDK feedback, the comparison clock crosses the
bound, and the next packet never enters the SDK; the accepted prefix's
handoff facts survive sweep and ledger reopen, and expiry does not
authorize a new attempt.

Labeled limits of this daemon fixture: there is no independent RF
reception, no independently observed daemon acceptance, and an earlier
native send cannot be canceled — the SDK packet ID and queue counters are
local handoff evidence only. Uncertainty (submitting/unknown) states are
not honestly producible by a deadline refusal at this seam, because the
one-use grant refuses before writing its submitting checkpoint; that
counterpart remains covered by the real-SQLite sweep and fence suites
(``tests/test_deadline_cleanup_fence.py``,
``tests/test_dispatch_start_deadline_fence.py``) and is modeled, not
native, here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

import medre.core.engine.pipeline.radio_delivery as radio_delivery_module
import medre.core.engine.pipeline.target_delivery as target_delivery_module
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.compat import HAS_MESHTASTIC
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.contracts.delivery import DeferredHandoffCompleted
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.target_delivery import (
    _DISPATCH_START_DEADLINE_ERROR,
    TargetDeliveryService,
    _AdapterDeliveryError,
)
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.rendering import RenderingPipeline, TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem, OutboxDispatchSnapshotWrite
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.async_utils import wait_until
from tests.integration.conftest import MeshtasticdEnvironment
from tests.integration.conftest import meshtasticd_env as meshtasticd_env

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not HAS_MESHTASTIC, reason="requires pinned mtjk SDK"),
]

_NATIVE = "meshtastic-deadline"
_SOURCE = "meshtastic-deadline-source"
_TRANSPORT = "deadline-proof"
_ROUTE = "meshtastic-deadline-fence"
_PLAN = "plan-deadline-fence"


def _future(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class _Clock(datetime):
    """Trusted local comparison clock moved only across the narrow seam."""

    current = datetime.now(timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current


def _cross_after_reservation(store: SQLiteStorage, cross_to: datetime | None) -> Any:
    """Move the pipeline clock right after the awaited second authorization.

    Storage samples its own real clock, so the reservation still passes and
    only the carried effective bound can fence the start — the gap the final
    synchronous fence closes. The clock moves after the post-render
    reservation and before the adjacent fence observation.
    """
    original = store.authorize_native_submission
    authorizations = 0

    async def cross_after_authorization(*args: Any, **kwargs: Any):
        nonlocal authorizations
        allowed = await original(*args, **kwargs)
        authorizations += 1
        if authorizations == 2 and cross_to is not None:
            _Clock.current = cross_to
        return allowed

    return cross_after_authorization


def _record_evidence(scenario: str, payload: dict[str, Any]) -> None:
    """Preserve the tier's SDK-invocation outcome as a CI artifact.

    ``MEDRE_DEADLINE_EVIDENCE_DIR`` is unset locally; CI points it at the
    artifact directory the execution guard requires. A write failure there
    fails the test loudly instead of quietly weakening the artifact.
    """
    root = os.environ.get("MEDRE_DEADLINE_EVIDENCE_DIR")
    if not root:
        return
    document = {
        "scenario": scenario,
        "mtjk_version": version("mtjk"),
        "adapter_module": MeshtasticAdapter.__module__,
        **payload,
    }
    path = Path(root) / "deadline-fence-sdk-evidence.json"
    recorded = json.loads(path.read_text()) if path.is_file() else {}
    recorded[scenario] = document
    path.write_text(json.dumps(recorded, indent=2, sort_keys=True))


@dataclass
class FenceCase:
    """One real daemon session wired to the pipeline's delivery service."""

    storage: SQLiteStorage
    service: TargetDeliveryService
    radio: MeshtasticAdapter
    event: CanonicalEvent
    deadline: datetime
    ceiling: datetime
    sdk_calls: list[tuple[str, int]] = field(default_factory=list)
    feedback: list[Any] = field(default_factory=list)

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def outbox_id(self) -> str:
        return f"obox-{self.event.event_id}"


def _canonical_event(event_id: str, body: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter=_SOURCE,
        source_transport_id=_TRANSPORT,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": body},
        metadata=EventMetadata(),
    )


def _route() -> Route:
    return Route(
        id=_ROUTE,
        source=RouteSource(
            adapter=_SOURCE, channel=None, event_kinds=("message.created",)
        ),
        targets=[RouteTarget(adapter=_NATIVE, channel="0")],
    )


def _adapter_context(case: FenceCase) -> AdapterContext:
    async def publish_inbound(event: CanonicalEvent) -> None:
        raise AssertionError("the fence scenarios emit no inbound events")

    async def report_delivery_feedback(item: Any) -> None:
        case.feedback.append(item)

    return AdapterContext(
        adapter_id=_NATIVE,
        publish_inbound=publish_inbound,
        report_delivery_feedback=report_delivery_feedback,
        logger=logging.getLogger("test.meshtastic.deadline_fence"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )


@asynccontextmanager
async def _case(
    env: MeshtasticdEnvironment,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    event_id: str,
    body: str,
    deadline: datetime,
    ceiling: datetime,
    multipart: bool = False,
    cross_clock_after_first_send: bool = False,
) -> Any:
    """One genuine daemon session with the pinned SDK boundary instrumented.

    ``cross_clock_after_first_send`` moves the trusted local comparison clock
    to the case deadline after the first real SDK invocation returns — the
    deterministic barrier for the per-packet boundary scenario. The SDK, the
    daemon and storage's own clock keep their real semantics throughout; only
    core's delivery modules compare the moved clock against the carried bound.
    """
    config = MeshtasticConfig(
        startup_packet_drain_seconds=0,
        adapter_id=_NATIVE,
        connection_type="tcp",
        host=env.host,
        port=env.port,
        message_delay_seconds=0,
        max_text_bytes=56 if multipart else 230,
        max_text_parts=8 if multipart else 1,
    ).validate()
    radio = MeshtasticAdapter(config)
    storage = SQLiteStorage(str(path))
    await storage.initialize()
    rendering = RenderingPipeline()
    rendering.register(MeshtasticRenderer(configs={_NATIVE: config}), priority=50)
    rendering.register(TextRenderer(), priority=100)
    service = TargetDeliveryService(
        adapters={_NATIVE: radio},
        rendering_pipeline=rendering,
        storage=storage,
        diagnostician=Diagnostician(),
        lifecycle=DeliveryLifecycleService(
            logger=logging.getLogger("test.meshtastic.deadline_fence.lifecycle")
        ),
        logger=logging.getLogger("test.meshtastic.deadline_fence"),
    )
    case = FenceCase(
        storage=storage,
        service=service,
        radio=radio,
        event=_canonical_event(event_id, body),
        deadline=deadline,
        ceiling=ceiling,
    )
    # The narrow clock seam only.
    monkeypatch.setattr(target_delivery_module, "datetime", _Clock)
    monkeypatch.setattr(radio_delivery_module, "datetime", _Clock)
    _Clock.current = deadline - timedelta(seconds=30)
    try:
        await radio.start(_adapter_context(case))
        # Instrument the actual pinned SDK entry without replacing its result
        # or the TCP daemon: every record precedes a real invocation, and the
        # returned packet object stays the SDK's own local handoff evidence.
        client = radio._session._client
        assert client is not None
        send_text = client.sendText

        def record_sdk_send(text: str, *, channelIndex: int = 0, **kwargs: Any):
            case.sdk_calls.append((text, channelIndex))
            handoff = send_text(text, channelIndex=channelIndex, **kwargs)
            if cross_clock_after_first_send and len(case.sdk_calls) == 1:
                _Clock.current = case.deadline
            return handoff

        monkeypatch.setattr(client, "sendText", record_sdk_send)
        yield case
    finally:
        try:
            await radio.stop()
        finally:
            await storage.close()


async def _stage_direct_owned(case: FenceCase) -> str:
    """Admit the event and stage one owned direct generation with its bound.

    Staging freezes the plan deadline into the dispatch snapshot exactly as
    production does, so the storage-authoritative bound is the earlier of the
    event ceiling, the frozen plan deadline and the adopted dependency.
    """
    await case.storage.admit_ingress(
        case.event, None, "live", content_deadline=case.ceiling
    )
    now = datetime.now(timezone.utc)
    created = await case.storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=case.outbox_id,
            event_id=case.event_id,
            delivery_plan_id=_PLAN,
            route_id=_ROUTE,
            target_adapter=_NATIVE,
            target_channel="0",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:clock",
            lease_until=(now + timedelta(minutes=5)).isoformat(),
        ),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="direct",
            context={
                "delivery_strategy": "direct",
                "capability_level": "native",
                "capability_field": None,
                "capability_reason": None,
                "deadline": case.deadline.isoformat(),
            },
        ),
    )
    return created.outbox_id


async def _stage_radio_claimed(case: FenceCase) -> str:
    """Admit the event, then claim and reserve one radio generation."""
    await case.storage.admit_ingress(
        case.event, None, "live", content_deadline=case.ceiling
    )
    created = await case.storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=case.outbox_id,
            event_id=case.event_id,
            delivery_plan_id=_PLAN,
            route_id=_ROUTE,
            target_adapter=_NATIVE,
            target_channel="0",
            status="pending",
            attempt_number=0,
        ),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="direct",
            context={
                "delivery_strategy": "direct",
                "capability_level": "native",
                "capability_field": None,
                "capability_reason": None,
                "deadline": case.deadline.isoformat(),
            },
        ),
    )
    claimed = await case.storage.claim_due_outbox_items(
        datetime.now(timezone.utc).isoformat(), "radio-owner"
    )
    assert [row.outbox_id for row in claimed] == [created.outbox_id]
    assert (
        await case.storage.reserve_outbox_attempt(created.outbox_id, "radio-owner", 0)
        == 1
    )
    return created.outbox_id


def _plan(case: FenceCase) -> DeliveryPlan:
    return DeliveryPlan(
        plan_id=_PLAN,
        event_id=case.event_id,
        target=RouteTarget(adapter=_NATIVE, channel="0"),
        primary_strategy=DeliveryStrategy(method="direct"),
        route_id=_ROUTE,
        deadline=case.deadline,
    )


# ---------------------------------------------------------------------------
# C-0: pre-bound positive control and the equality refusal
# ---------------------------------------------------------------------------


async def test_positive_control_invokes_pinned_sdk_before_the_bound(
    meshtasticd_env: MeshtasticdEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness really hands off through the pinned SDK before the bound.

    Without this witness a zero-invocation refusal would be indistinguishable
    from a scenario that can never reach the SDK at all.
    """
    deadline = _future(300)
    body = "deadline fence positive control"
    async with _case(
        meshtasticd_env,
        tmp_path / "medre.sqlite",
        monkeypatch,
        event_id="fence-positive-" + str(_future(0).timestamp()),
        body=body,
        deadline=deadline,
        ceiling=_future(600),
    ) as case:
        outbox_id = await _stage_direct_owned(case)
        result = await case.service.deliver_execution(
            case.event,
            _route(),
            _plan(case),
            outbox_id=outbox_id,
            reserved_attempt_number=1,
            expected_outbox_worker_id="pipeline:clock",
        )
        # Deferred admission is the adapter's real local contract: the queued
        # receipt records it, the row keeps its owner until feedback
        # finalization, and the queue drain reports the completed hand-off
        # through the adapter's real feedback boundary.
        assert result.primary_receipt.status == "queued"
        assert await wait_until(lambda: case.radio.queue.total_sent == 1, timeout=25)
        assert await wait_until(
            lambda: any(
                isinstance(item, DeferredHandoffCompleted) for item in case.feedback
            ),
            timeout=25,
        )
        assert case.radio.queue.total_failed == 0
        assert case.sdk_calls == [(_TRANSPORT + ": " + body, 0)]
        receipts = await case.storage.list_receipts_for_event(case.event_id)
        assert [receipt.status for receipt in receipts] == ["queued"]
        item = await case.storage.get_outbox_item(outbox_id)
        assert item is not None
        assert item.status == "in_progress"
        assert item.worker_id == "pipeline:clock"
        _record_evidence(
            "positive_control",
            {
                "sdk_invocations": len(case.sdk_calls),
                "queue_total_sent": case.radio.queue.total_sent,
                "queue_total_failed": case.radio.queue.total_failed,
                "daemon": f"{meshtasticd_env.host}:{meshtasticd_env.port}",
            },
        )


async def test_dispatch_start_refuses_at_bound_equality_with_zero_sdk_invocations(
    meshtasticd_env: MeshtasticdEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At equality with the effective bound the fence refuses before the SDK."""
    deadline = _future(300)
    async with _case(
        meshtasticd_env,
        tmp_path / "medre.sqlite",
        monkeypatch,
        event_id="fence-equality-" + str(_future(0).timestamp()),
        body="deadline fence equality refusal",
        deadline=deadline,
        ceiling=_future(600),
    ) as case:
        outbox_id = await _stage_direct_owned(case)
        # The real event ceiling and the adopted target dependency are live
        # storage authority; the plan deadline is the earlier effective bound.
        dependencies = await case.storage.list_mandatory_content_dependencies(
            case.event_id
        )
        assert any(kind == "outbox" for kind, _owner, _d, _r in dependencies)
        observed = await case.storage.observe_effective_dispatch_bound(
            case.event_id, outbox_id
        )
        assert observed is not None
        assert observed.source == "frozen_plan_deadline"
        assert observed.deadline == case.deadline
        assert observed.deadline < case.ceiling
        monkeypatch.setattr(
            case.storage,
            "authorize_native_submission",
            _cross_after_reservation(case.storage, case.deadline),
        )
        with pytest.raises(_AdapterDeliveryError) as caught:
            await case.service.deliver_execution(
                case.event,
                _route(),
                _plan(case),
                outbox_id=outbox_id,
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:clock",
            )
        evidence = caught.value.evidence
        assert caught.value.error == _DISPATCH_START_DEADLINE_ERROR
        assert evidence.failure_kind is not None
        assert evidence.failure_kind.value == "deadline_exceeded"
        assert evidence.attempt_receipt is None
        assert evidence.authority_receipt is None
        # Zero new SDK invocations and no invented native acceptance.
        assert case.sdk_calls == []
        assert case.radio.queue.total_sent == 0
        assert case.radio.queue.pending_count == 0
        assert await case.storage.count_receipts() == 0
        assert (
            await case.storage._read_one(
                "SELECT 1 FROM native_message_refs WHERE event_id=? LIMIT 1",
                (case.event_id,),
            )
            is None
        )
        item = await case.storage.get_outbox_item(outbox_id)
        assert item is not None
        assert item.status == "in_progress"
        assert item.worker_id == "pipeline:clock"
        assert _Clock.current == case.deadline
        _record_evidence(
            "equality_refusal",
            {
                "sdk_invocations": len(case.sdk_calls),
                "queue_total_sent": case.radio.queue.total_sent,
                "daemon": f"{meshtasticd_env.host}:{meshtasticd_env.port}",
            },
        )


async def test_earlier_plan_deadline_wins_over_later_event_ceiling(
    meshtasticd_env: MeshtasticdEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal fires at the earlier plan deadline, not the later ceiling."""
    deadline = _future(120)
    ceiling = _future(900)
    async with _case(
        meshtasticd_env,
        tmp_path / "medre.sqlite",
        monkeypatch,
        event_id="fence-plan-early-" + str(_future(0).timestamp()),
        body="deadline fence earlier plan wins",
        deadline=deadline,
        ceiling=ceiling,
    ) as case:
        outbox_id = await _stage_direct_owned(case)
        observed = await case.storage.observe_effective_dispatch_bound(
            case.event_id, outbox_id
        )
        assert observed is not None
        assert observed.deadline == deadline
        assert observed.deadline < case.ceiling
        monkeypatch.setattr(
            case.storage,
            "authorize_native_submission",
            _cross_after_reservation(case.storage, deadline),
        )
        with pytest.raises(_AdapterDeliveryError) as caught:
            await case.service.deliver_execution(
                case.event,
                _route(),
                _plan(case),
                outbox_id=outbox_id,
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:clock",
            )
        assert caught.value.error == _DISPATCH_START_DEADLINE_ERROR
        # The clock crossed only the plan deadline; the ceiling was still
        # future, so a fence bound to the ceiling would have invoked.
        assert _Clock.current == deadline
        assert _Clock.current < case.ceiling
        assert case.sdk_calls == []
        assert case.radio.queue.total_sent == 0
        _record_evidence(
            "earlier_plan_deadline_wins",
            {
                "sdk_invocations": len(case.sdk_calls),
                "plan_deadline": deadline.isoformat(),
                "event_ceiling": case.ceiling.isoformat(),
                "daemon": f"{meshtasticd_env.host}:{meshtasticd_env.port}",
            },
        )


# ---------------------------------------------------------------------------
# C-1: per-packet boundary, accepted prefix, sweep and reopen
# ---------------------------------------------------------------------------


async def test_radio_packet_boundary_preserves_accepted_prefix_across_reopen(
    meshtasticd_env: MeshtasticdEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accepted prefix survives; the post-bound packet never enters the SDK.

    The event ceiling and the plan deadline share one instant so the
    storage-authoritative reopen refusal observes the same effective bound
    the packet fence carried.
    """
    deadline = _future(120)
    path = tmp_path / "medre.sqlite"
    outbox_id: str | None = None
    event_id: str | None = None
    accepted_parts: list[Any] = []
    async with _case(
        meshtasticd_env,
        path,
        monkeypatch,
        event_id="fence-radio-" + str(_future(0).timestamp()),
        body="planned radio packet " * 7,
        deadline=deadline,
        ceiling=deadline,
        multipart=True,
        cross_clock_after_first_send=True,
    ) as case:
        outbox_id = await _stage_radio_claimed(case)
        event_id = case.event_id
        with pytest.raises(_AdapterDeliveryError) as caught:
            await case.service.deliver_execution(
                case.event,
                _route(),
                _plan(case),
                outbox_id=outbox_id,
                reserved_attempt_number=1,
                expected_outbox_worker_id="radio-owner",
            )
        assert caught.value.evidence.failure_kind is not None
        # Exactly the pre-bound prefix entered the SDK; the refused packet
        # never did.
        assert len(case.sdk_calls) == 1
        assert case.radio.queue.total_sent == 1
        checkpoint = await case.storage.get_radio_attempt(outbox_id, 1)
        assert checkpoint is not None
        parts = checkpoint.progress.parts
        assert len(parts) >= 2
        assert parts[0].state == "accepted"
        assert parts[0].handoff_id is not None
        assert all(part.state == "unsent" for part in parts[1:])
        assert case.sdk_calls == [(checkpoint.progress.plan.parts[0].wire_text, 0)]
        receipts = await case.storage.list_receipts_for_event(case.event_id)
        assert len(receipts) == 1
        assert receipts[0].status == "failed"
        accepted_parts = parts

        # The sweep may only settle lifecycle state; it must never erase the
        # accepted prefix's checkpointed handoff facts or re-enable a claim.
        row_before = await case.storage.get_outbox_item(outbox_id)
        assert row_before is not None
        await case.storage.retire_expired_unstarted_outbox(
            now=deadline + timedelta(seconds=1)
        )
        row_after = await case.storage.get_outbox_item(outbox_id)
        assert row_after is not None
        swept = await case.storage.get_radio_attempt(outbox_id, 1)
        assert swept is not None and swept.progress.parts == accepted_parts
        claimed_after = await case.storage.claim_due_outbox_items(
            datetime.now(timezone.utc).isoformat(), "post-sweep-worker"
        )
        assert all(row.outbox_id != outbox_id for row in claimed_after)

        _record_evidence(
            "radio_prefix_boundary",
            {
                "sdk_invocations": len(case.sdk_calls),
                "accepted_parts": sum(
                    part.state == "accepted" for part in accepted_parts
                ),
                "unsent_parts": sum(part.state == "unsent" for part in accepted_parts),
                "daemon": f"{meshtasticd_env.host}:{meshtasticd_env.port}",
            },
        )

    # Reopen the ledger: the accepted prefix and its handoff identity survive,
    # and expiry does not authorize a new attempt.
    assert outbox_id is not None and event_id is not None
    reopened = SQLiteStorage(str(path))
    await reopened.initialize()
    try:
        checkpoint = await reopened.get_radio_attempt(outbox_id, 1)
        assert checkpoint is not None
        assert checkpoint.progress.parts[0].state == "accepted"
        assert checkpoint.progress.parts[0].handoff_id is not None
        assert all(part.state == "unsent" for part in checkpoint.progress.parts[1:])
        assert not await reopened.authorize_native_submission(
            event_id,
            outbox_id,
            worker_id="radio-owner",
            attempt_number=1,
            now=deadline + timedelta(seconds=1),
        )
        # The retained evidence is not a resubmission grant either: the
        # continuation requires an owned claim, which a refused generation
        # no longer holds.
        assert (
            await reopened.resume_radio_attempt(
                checkpoint.provenance,
                "radio-owner",
                checkpoint.progress.plan,
                checkpoint,
            )
            is None
        )
    finally:
        await reopened.close()
