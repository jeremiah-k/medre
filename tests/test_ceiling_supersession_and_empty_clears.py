"""Same-attempt supersession and empty-clear fences around the ceiling purge.

The purge derives uncertainty state from the latest legacy diagnostic, while
the content-free typed ledger can hold a newer explicit supersession whose
diagnostic text was fence-denied. These tests pin the two directions of that
precedence, the cross-attempt boundary, and the rule that no diagnostic row -
empty or not - may be (re)created once a ceiling has elapsed or enforcement
has committed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import msgspec

from medre.core.events import DeliveryReceipt
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    _admit_plain,
    _admit_with_ceiling,
    _install_elapsed_policy,
)
from tests.helpers.handoff_resolution import (
    _WORKER,
    _due_row,
    _observation,
    _seed_reserved_claim,
)


def _next_attempt() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


async def _diagnostics(store: SQLiteStorage, family: str, row_id: str) -> list[str]:
    rows = await store._read_all(
        "SELECT diagnostic FROM delivery_evidence_diagnostics "
        "WHERE family=? AND row_id=? ORDER BY diag_id",
        (family, row_id),
    )
    return [row["diagnostic"] for row in rows]


async def _typed(store: SQLiteStorage, outbox_id: str) -> dict[str, object] | None:
    return await store._read_one(
        "SELECT attempt_number, state FROM delivery_handoff_uncertainty "
        "WHERE outbox_id=?",
        (outbox_id,),
    )


async def test_purge_keeps_typed_supersession_when_explicit_diagnostic_is_fenced(
    temp_storage: SQLiteStorage,
) -> None:
    """A fence-denied explicit classification must not be revived by the purge.

    The uncertainty diagnostic predates the ceiling; the explicit failure
    detail arrives after it elapsed, so its diagnostic text is denied while
    the content-free typed supersession records. The purge must derive its
    state from the surviving (older) diagnostic without overwriting the
    typed supersession.
    """
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-fenced-explicit",
        event_id="evt-fenced-explicit",
    )
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_transient",
        failure_kind_detail="uncertain_native_handoff",
        attempt_number=provenance.attempt_number,
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await _due_row(temp_storage, stored.outbox_id, _WORKER)
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_permanent",
        failure_kind_detail="adapter_permanent",
        attempt_number=provenance.attempt_number,
        expected_worker_id=_WORKER,
    )
    # The explicit text is fenced; only the older uncertainty remains.
    assert await _diagnostics(temp_storage, "outbox", stored.outbox_id) == [
        "uncertain_native_handoff"
    ]
    assert await _typed(temp_storage, stored.outbox_id) == {
        "attempt_number": provenance.attempt_number,
        "state": "superseded_native_handoff",
    }
    await temp_storage.enforce_content_ceilings(now=now)
    assert await _diagnostics(temp_storage, "outbox", stored.outbox_id) == []
    assert await _typed(temp_storage, stored.outbox_id) == {
        "attempt_number": provenance.attempt_number,
        "state": "superseded_native_handoff",
    }
    assert await temp_storage._read_one(
        "SELECT event_id FROM event_content_ceiling_log WHERE event_id=?",
        (provenance.event_id,),
    ) == {"event_id": provenance.event_id}


async def test_purge_advances_uncertainty_to_a_newer_attempt_despite_supersession(
    temp_storage: SQLiteStorage,
) -> None:
    """Supersession closes one attempt; a newer attempt's uncertainty stands.

    The typed row is keyed by outbox identity, so the same-attempt protection
    must be scoped to the attempt: a legacy diagnostic reporting uncertainty
    for the outbox's newer generation must advance the ledger past a stale
    superseded state recorded for the older generation.
    """
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-cross-attempt",
        event_id="evt-cross-attempt",
    )
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_transient",
        failure_kind_detail="uncertain_native_handoff",
        attempt_number=provenance.attempt_number,
    )
    await _due_row(temp_storage, stored.outbox_id, _WORKER)
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_permanent",
        failure_kind_detail="adapter_permanent",
        attempt_number=provenance.attempt_number,
        expected_worker_id=_WORKER,
    )
    assert await _typed(temp_storage, stored.outbox_id) == {
        "attempt_number": provenance.attempt_number,
        "state": "superseded_native_handoff",
    }
    await _due_row(temp_storage, stored.outbox_id, _WORKER)
    reserved = await temp_storage.reserve_outbox_attempt(
        stored.outbox_id, _WORKER, provenance.attempt_number
    )
    assert reserved == provenance.attempt_number + 1
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_transient",
        failure_kind_detail="uncertain_native_handoff",
        attempt_number=reserved,
    )
    # Test setup only: rewind the typed ledger to the older generation's
    # supersession so the purge meets a stale superseded state while the
    # current generation's diagnostic reports uncertainty (the guarded
    # upgrade can land in this state).
    await temp_storage._write(
        "UPDATE delivery_handoff_uncertainty SET attempt_number=?, "
        "state='superseded_native_handoff' WHERE outbox_id=?",
        (provenance.attempt_number, stored.outbox_id),
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await _diagnostics(temp_storage, "outbox", stored.outbox_id) == []
    assert await _typed(temp_storage, stored.outbox_id) == {
        "attempt_number": reserved,
        "state": "uncertain_native_handoff",
    }


async def test_outbox_empty_clear_after_enforcement_writes_no_row(
    temp_storage: SQLiteStorage,
) -> None:
    """An empty outbox clear cannot recreate a residual after enforcement.

    The outbox statement's empty-value exemption is bounded by its
    only-when-a-nonempty-diagnostic-exists precondition, and enforcement has
    purged every attributed diagnostic, so neither an empty nor a late
    non-empty detail may land.
    """
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-empty-clear",
        event_id="evt-empty-clear",
    )
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_transient",
        failure_kind_detail="PRIVATE FAILURE DETAIL",
        attempt_number=provenance.attempt_number,
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    await _due_row(temp_storage, stored.outbox_id, _WORKER)
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_transient",
        failure_kind_detail="",
        attempt_number=provenance.attempt_number,
        expected_worker_id=_WORKER,
    )
    assert await _diagnostics(temp_storage, "outbox", stored.outbox_id) == []
    await _due_row(temp_storage, stored.outbox_id, _WORKER)
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_transient",
        failure_kind_detail="PRIVATE LATE DETAIL",
        attempt_number=provenance.attempt_number,
        expected_worker_id=_WORKER,
    )
    assert await _diagnostics(temp_storage, "outbox", stored.outbox_id) == []


async def test_empty_receipt_diagnostic_after_enforcement_writes_no_row(
    temp_storage: SQLiteStorage,
) -> None:
    """Receipt diagnostic writers skip empty values; no residual row appears."""
    event_id = "evt-empty-receipt-clear"
    await _admit_plain(temp_storage, event_id)
    now = await _install_elapsed_policy(temp_storage, event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    receipt_id = f"receipt-{event_id}"
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id=receipt_id,
            event_id=event_id,
            delivery_plan_id="plan",
            target_adapter="fake",
            target_channel=None,
            receipt_kind="attempt",
            status="failed",
            failure_kind="adapter_transient",
            error="",
            capability_reason="",
            rendering_evidence="",
            created_at=datetime.now(timezone.utc),
        )
    )
    assert await _diagnostics(temp_storage, "receipt", receipt_id) == []


async def test_empty_observation_diagnostic_after_enforcement_writes_no_row(
    temp_storage: SQLiteStorage,
) -> None:
    """Observation diagnostic writers skip empty values after enforcement."""
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-empty-observation",
        event_id="evt-empty-observation",
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    obs = msgspec.structs.replace(
        _observation("obs-empty-observation-clear", provenance), error=""
    )
    assert await temp_storage.append_delivery_observation(obs)
    assert await _diagnostics(temp_storage, "observation", obs.observation_id) == []


async def test_pre_ceiling_empty_clears_still_record(
    temp_storage: SQLiteStorage,
) -> None:
    """Ordinary pre-ceiling clear behaviour is unchanged by the fences.

    Without a ceiling policy at all, and with a ceiling that has not yet
    elapsed, an empty ingress clear appends its row and an outbox clear
    appends behind a nonempty same-attempt diagnostic.
    """
    event_id = "evt-ingress-clear-no-policy"
    await _admit_plain(temp_storage, event_id)
    claimed = await temp_storage.claim_ingress_work(
        worker_id="ingress-clear-worker", limit=10
    )
    assert any(item.event_id == event_id for item in claimed)
    assert await temp_storage.release_ingress_work(
        event_id, worker_id="ingress-clear-worker", error=""
    )
    assert await _diagnostics(temp_storage, "ingress", event_id) == [""]

    future_event = "evt-ingress-clear-future-ceiling"
    await _admit_with_ceiling(temp_storage, future_event)
    claimed = await temp_storage.claim_ingress_work(
        worker_id="ingress-clear-worker", limit=10
    )
    assert any(item.event_id == future_event for item in claimed)
    assert await temp_storage.release_ingress_work(
        future_event, worker_id="ingress-clear-worker", error=""
    )
    assert await _diagnostics(temp_storage, "ingress", future_event) == [""]

    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-pre-ceiling-clear",
        event_id="evt-pre-ceiling-clear",
    )
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_transient",
        failure_kind_detail="PRIVATE FAILURE DETAIL",
        attempt_number=provenance.attempt_number,
    )
    await _due_row(temp_storage, stored.outbox_id, _WORKER)
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_transient",
        failure_kind_detail="",
        attempt_number=provenance.attempt_number,
        expected_worker_id=_WORKER,
    )
    assert await _diagnostics(temp_storage, "outbox", stored.outbox_id) == [
        "PRIVATE FAILURE DETAIL",
        "",
    ]


async def test_uncertainty_after_enforcement_keeps_typed_evidence(
    temp_storage: SQLiteStorage,
) -> None:
    """Uncertainty arising after erasure is typed directly, not via a diagnostic.

    The diagnostic write is fence-denied, but the content-free uncertainty
    ledger records the adapter timeout itself and stays readable for late
    reconciliation; a later sweep leaves it in place.
    """
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-late-uncertainty",
        event_id="evt-late-uncertainty",
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_next_attempt(),
        failure_kind="adapter_transient",
        failure_kind_detail="uncertain_native_handoff",
        attempt_number=provenance.attempt_number,
    )
    assert await _diagnostics(temp_storage, "outbox", stored.outbox_id) == []
    assert await _typed(temp_storage, stored.outbox_id) == {
        "attempt_number": provenance.attempt_number,
        "state": "uncertain_native_handoff",
    }
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.failure_kind_detail == "uncertain_native_handoff"
    await temp_storage.enforce_content_ceilings(now=now)
    assert await _typed(temp_storage, stored.outbox_id) == {
        "attempt_number": provenance.attempt_number,
        "state": "uncertain_native_handoff",
    }
