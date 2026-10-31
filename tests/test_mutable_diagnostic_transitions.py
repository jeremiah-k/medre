"""Mutable work diagnostics follow transitions without rewriting history."""

from datetime import datetime, timezone

import msgspec
import pytest

from medre.core.events import DeliveryReceipt
from medre.core.storage.backend import DeliveryOutboxItem, TerminalOutboxFinalization
from tests.helpers.storage import make_storage_event


async def _admit(store):
    event = msgspec.structs.replace(
        make_storage_event(event_id="diagnostic-transition"), payload={}, metadata={}
    )
    await store.admit_ingress(event, None, "live")


@pytest.mark.parametrize("transition", ["release_ingress_work", "defer_ingress_work"])
@pytest.mark.parametrize("error", ["latest failure", ""])
async def test_ingress_claim_uses_latest_transition_diagnostic(
    temp_storage, transition, error
):
    await _admit(temp_storage)
    await temp_storage._write(
        "UPDATE durable_ingress_work SET last_error='historical failure'"
    )
    first = await temp_storage.claim_ingress_work(worker_id="first")
    assert first[0].last_error == "historical failure"
    assert await getattr(temp_storage, transition)(
        "diagnostic-transition", worker_id="first", error=error
    )
    second = await temp_storage.claim_ingress_work(worker_id="second")
    assert second[0].last_error == (error or None)
    row = await temp_storage._read_one("SELECT last_error FROM durable_ingress_work")
    assert row["last_error"] == "historical failure"


async def _outbox(store):
    await _admit(store)
    return await store.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="diagnostic-outbox",
            route_id="diagnostic-route",
            event_id="diagnostic-transition",
            delivery_plan_id="diagnostic-plan",
            target_adapter="fake_transport",
            status="in_progress",
            error_summary="prior summary",
            failure_kind_detail="prior detail",
        )
    )


async def test_explicit_empty_outbox_diagnostic_clears_the_current_value(temp_storage):
    await _outbox(temp_storage)
    assert await temp_storage.mark_outbox_retry_wait(
        "diagnostic-outbox", "2099-01-01T00:00:00+00:00", error_summary=""
    )
    item = await temp_storage.get_outbox_item("diagnostic-outbox")
    assert item.error_summary is None
    rows = await temp_storage._read_all(
        "SELECT diagnostic FROM delivery_evidence_diagnostics "
        "WHERE family='outbox' AND field='error_summary' ORDER BY diag_id"
    )
    assert [row["diagnostic"] for row in rows] == ["prior summary", ""]


@pytest.mark.parametrize("error", [None, "terminal summary"])
async def test_atomic_terminal_outcome_clears_superseded_diagnostic_fields(
    temp_storage, error
):
    item = await _outbox(temp_storage)
    receipt = DeliveryReceipt(
        receipt_id="terminal-receipt",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        outbox_id=item.outbox_id,
        attempt_number=item.attempt_number,
        status="dead_lettered",
        receipt_kind="lifecycle",
        error=error,
        created_at=datetime.now(timezone.utc),
    )
    assert await temp_storage.finalize_outbox_terminal(
        TerminalOutboxFinalization(lifecycle_receipt=receipt)
    )
    result = await temp_storage.get_outbox_item(item.outbox_id)
    assert result.error_summary == error
    assert result.failure_kind_detail is None


@pytest.mark.parametrize("detail", [None, "dispatch_outcome_unknown"])
async def test_mark_terminal_clears_prior_detail_but_preserves_explicit_outcome(
    temp_storage,
    detail,
):
    item = await _outbox(temp_storage)
    assert await temp_storage.mark_outbox_dead_lettered(
        item.outbox_id, failure_kind_detail=detail
    )
    result = await temp_storage.get_outbox_item(item.outbox_id)
    assert result.failure_kind_detail == detail
