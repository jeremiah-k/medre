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
        "INSERT INTO delivery_evidence_diagnostics "
        "(family, row_id, attempt, field, diagnostic, created_at) "
        "VALUES ('ingress', 'diagnostic-transition', 0, 'last_error', "
        "'historical failure', '2026-10-30T00:00:00+00:00')"
    )
    first = await temp_storage.claim_ingress_work(worker_id="first")
    assert first[0].last_error == "historical failure"
    assert await getattr(temp_storage, transition)(
        "diagnostic-transition", worker_id="first", error=error
    )
    second = await temp_storage.claim_ingress_work(worker_id="second")
    assert second[0].last_error == (error or None)
    rows = await temp_storage._read_all(
        "SELECT diagnostic FROM delivery_evidence_diagnostics "
        "WHERE family='ingress' AND row_id='diagnostic-transition' ORDER BY diag_id"
    )
    assert [row["diagnostic"] for row in rows] == ["historical failure", error]


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


async def _seeded_outbox(store, outbox_id, event_id):
    """Seed an in-progress outbox row with attempt-one diagnostics.

    The free-form text lives only in ``delivery_evidence_diagnostics``; the
    durable ``failure_kind`` fact stays inline.
    """
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={}, metadata={}
    )
    await store.admit_ingress(event, None, "live")
    item = await store.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            route_id="diagnostic-route",
            event_id=event_id,
            delivery_plan_id="diagnostic-plan",
            target_adapter="fake_transport",
            status="in_progress",
        )
    )
    assert await store.mark_outbox_retry_wait(
        outbox_id,
        "2026-10-31T01:00:00+00:00",
        failure_kind="adapter_transient",
        failure_kind_detail="seeded detail",
        error_summary="seeded summary",
    )
    claimed = await store.claim_due_outbox_items(
        "2026-11-01T00:00:00+00:00", "seed-worker"
    )
    assert [c.outbox_id for c in claimed] == [outbox_id]
    return item


async def _inline_failure_columns(store, outbox_id):
    return await store._read_one(
        "SELECT failure_kind FROM delivery_outbox WHERE outbox_id=?",
        (outbox_id,),
    )


async def _outbox_side_rows(store, outbox_id):
    return await store._read_all(
        "SELECT field, diagnostic, attempt FROM delivery_evidence_diagnostics "
        "WHERE family='outbox' AND row_id=? ORDER BY diag_id",
        (outbox_id,),
    )


@pytest.mark.parametrize("mark", ["mark_outbox_sent", "mark_outbox_queued"])
async def test_settled_transitions_suppress_seeded_diagnostics(temp_storage, mark):
    item = await _seeded_outbox(temp_storage, "ob-seeded-settle", "evt-seeded-settle")
    assert await getattr(temp_storage, mark)(item.outbox_id, attempt_number=1)
    settled = await temp_storage.get_outbox_item(item.outbox_id)
    assert settled is not None
    assert settled.failure_kind_detail is None
    assert settled.error_summary is None
    # The seeded attempt-one diagnostics remain appended history; a settled
    # row never resurfaces them.
    rows = await _outbox_side_rows(temp_storage, item.outbox_id)
    assert {row["attempt"] for row in rows} == {1}


async def test_terminal_finalization_suppresses_seeded_diagnostics(temp_storage):
    item = await _seeded_outbox(temp_storage, "ob-seeded-final", "evt-seeded-final")
    receipt = DeliveryReceipt(
        receipt_id="legacy-terminal-receipt",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        outbox_id=item.outbox_id,
        attempt_number=item.attempt_number,
        status="dead_lettered",
        receipt_kind="lifecycle",
        created_at=datetime.now(timezone.utc),
    )
    assert await temp_storage.finalize_outbox_terminal(
        TerminalOutboxFinalization(lifecycle_receipt=receipt)
    )
    terminal = await temp_storage.get_outbox_item(item.outbox_id)
    assert terminal is not None
    assert terminal.failure_kind_detail is None
    assert terminal.error_summary is None
    # The terminal clear appends explicit empty markers for both fields; the
    # seeded history remains appended above them.
    rows = await _outbox_side_rows(temp_storage, item.outbox_id)
    assert [(row["field"], row["diagnostic"], row["attempt"]) for row in rows] == [
        ("failure_kind_detail", "seeded detail", 1),
        ("error_summary", "seeded summary", 1),
        ("failure_kind_detail", "", 1),
        ("error_summary", "", 1),
    ]


async def test_terminal_finalization_supersedes_seeded_text_in_history(
    temp_storage,
):
    item = await _seeded_outbox(
        temp_storage, "ob-seeded-supersede", "evt-seeded-supersede"
    )
    receipt = DeliveryReceipt(
        receipt_id="legacy-supersede-receipt",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        outbox_id=item.outbox_id,
        attempt_number=item.attempt_number,
        status="dead_lettered",
        receipt_kind="lifecycle",
        failure_kind="adapter_permanent",
        error="TERMINAL-ERROR",
        created_at=datetime.now(timezone.utc),
    )
    assert await temp_storage.finalize_outbox_terminal(
        TerminalOutboxFinalization(lifecycle_receipt=receipt)
    )
    inline = await _inline_failure_columns(temp_storage, item.outbox_id)
    # The durable failure_kind fact stays inline.
    assert inline["failure_kind"] == "adapter_permanent"
    terminal = await temp_storage.get_outbox_item(item.outbox_id)
    assert terminal is not None
    assert terminal.error_summary == "TERMINAL-ERROR"
    assert terminal.failure_kind_detail is None
    # Side-table rows written along the way remain: the append-only history
    # carries the seeded attempt wording plus the terminal wording.
    outbox_rows = await _outbox_side_rows(temp_storage, item.outbox_id)
    assert [
        (row["field"], row["diagnostic"], row["attempt"]) for row in outbox_rows
    ] == [
        ("failure_kind_detail", "seeded detail", 1),
        ("error_summary", "seeded summary", 1),
        ("failure_kind_detail", "", 1),
        ("error_summary", "TERMINAL-ERROR", 1),
    ]
    receipt_rows = await temp_storage._read_all(
        "SELECT field, diagnostic FROM delivery_evidence_diagnostics "
        "WHERE family='receipt' AND row_id=?",
        ("legacy-supersede-receipt",),
    )
    assert [(row["field"], row["diagnostic"]) for row in receipt_rows] == [
        ("error", "TERMINAL-ERROR")
    ]
