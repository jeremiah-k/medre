"""Post-ceiling evidence remains appendable without resurrecting diagnostic content."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.core.events import DeliveryReceipt
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _admit_plain, _install_elapsed_policy
from tests.helpers.handoff_resolution import _observation, _seed_reserved_claim


async def _diagnostics(store: SQLiteStorage, family: str, row_id: str) -> list[str]:
    rows = await store._read_all(
        "SELECT diagnostic FROM delivery_evidence_diagnostics "
        "WHERE family=? AND row_id=? ORDER BY diag_id",
        (family, row_id),
    )
    return [row["diagnostic"] for row in rows]


@pytest.mark.parametrize("purged", [False, True])
async def test_late_receipt_keeps_authority_without_diagnostic_content(
    temp_storage: SQLiteStorage, purged: bool
) -> None:
    event_id = f"late-receipt-{purged}"
    await _admit_plain(temp_storage, event_id)
    now = await _install_elapsed_policy(temp_storage, event_id)
    if purged:
        await temp_storage.enforce_content_ceilings(now=now)
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id=f"receipt-{event_id}",
            event_id=event_id,
            delivery_plan_id="plan",
            target_adapter="fake",
            target_channel=None,
            receipt_kind="attempt",
            status="failed",
            failure_kind="adapter_transient",
            error="PRIVATE RECEIPT ERROR",
            capability_reason="PRIVATE CAPABILITY REASON",
            rendering_evidence="PRIVATE RENDERING EVIDENCE",
            created_at=datetime.now(timezone.utc),
        )
    )
    assert (
        await temp_storage._read_one(
            "SELECT receipt_id FROM delivery_receipts WHERE receipt_id=?",
            (f"receipt-{event_id}",),
        )
        is not None
    )
    assert await _diagnostics(temp_storage, "receipt", f"receipt-{event_id}") == []


async def test_pre_ceiling_receipt_diagnostic_remains_available(
    temp_storage: SQLiteStorage,
) -> None:
    await _admit_plain(temp_storage, "pre-ceiling-receipt")
    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id="pre-ceiling-receipt",
            event_id="pre-ceiling-receipt",
            delivery_plan_id="plan",
            target_adapter="fake",
            receipt_kind="attempt",
            status="failed",
            failure_kind="adapter_transient",
            error="LEGITIMATE DIAGNOSTIC",
        )
    )
    assert await _diagnostics(temp_storage, "receipt", "pre-ceiling-receipt") == [
        "LEGITIMATE DIAGNOSTIC"
    ]


@pytest.mark.parametrize("purged", [False, True])
async def test_late_observation_and_outbox_failures_do_not_retain_text(
    temp_storage: SQLiteStorage, purged: bool
) -> None:
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        outbox_id=f"late-obs-outbox-{purged}",
        event_id=f"late-obs-{purged}",
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    if purged:
        await temp_storage.enforce_content_ceilings(now=now)
    obs = msgspec.structs.replace(
        _observation(f"late-obs-record-{purged}", provenance),
        error="PRIVATE OBSERVATION ERROR",
    )
    assert await temp_storage.append_delivery_observation(obs)
    assert await _diagnostics(temp_storage, "observation", obs.observation_id) == []
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        failure_kind="adapter_transient",
        failure_kind_detail="PRIVATE FAILURE DETAIL",
        error_summary="PRIVATE ERROR SUMMARY",
        attempt_number=provenance.attempt_number,
    )
    assert await _diagnostics(temp_storage, "outbox", stored.outbox_id) == []


async def test_late_ingress_failure_does_not_recreate_diagnostic(
    temp_storage: SQLiteStorage,
) -> None:
    await _admit_plain(temp_storage, "late-ingress-diagnostic")
    now = await _install_elapsed_policy(temp_storage, "late-ingress-diagnostic")
    await temp_storage.enforce_content_ceilings(now=now)
    claimed = await temp_storage.claim_ingress_work(worker_id="diagnostics", limit=10)
    assert any(item.event_id == "late-ingress-diagnostic" for item in claimed)
    assert await temp_storage.release_ingress_work(
        "late-ingress-diagnostic",
        worker_id="diagnostics",
        error="PRIVATE INGRESS ERROR",
    )
    assert await _diagnostics(temp_storage, "ingress", "late-ingress-diagnostic") == []


async def test_upgraded_uncertainty_is_backfilled_before_diagnostics_are_erased(
    tmp_path,
) -> None:
    """A pre-fact attempt retains its late-ACK correlation across upgrade/restart."""

    from tests.helpers.handoff_resolution import (
        _bind_and_progress,
        _observation,
        _seed_reserved_claim,
    )

    path = str(tmp_path / "legacy-uncertain.sqlite")
    original = SQLiteStorage(path)
    await original.initialize()
    try:
        stored, provenance = await _seed_reserved_claim(original)
        await _bind_and_progress(original, provenance, ["accepted", "unsent", "unsent"])
        assert await original.mark_outbox_retry_wait(
            stored.outbox_id,
            next_attempt_at=(
                datetime.now(timezone.utc) + timedelta(minutes=1)
            ).isoformat(),
            failure_kind="adapter_transient",
            failure_kind_detail="uncertain_native_handoff",
            attempt_number=provenance.attempt_number,
        )
    finally:
        # A failed setup must still release the handle before the raw DDL
        # step below; an open database here fails later cleanup noisily.
        await original.close()
    # sqlite3 connections only commit inside ``with``; they never close, so
    # an un-closed downgrade handle leaks an unclosed database that fails a
    # later test at garbage-collection time.
    raw = sqlite3.connect(path)
    try:
        raw.execute("DROP TABLE delivery_handoff_uncertainty")
        raw.execute("DROP TABLE radio_accepted_part_facts")
        raw.commit()
    finally:
        raw.close()
    upgraded = SQLiteStorage(path)
    await upgraded.initialize()
    try:
        now = await _install_elapsed_policy(upgraded, provenance.event_id)
        await upgraded.enforce_content_ceilings(now=now)
        assert await _diagnostics(upgraded, "outbox", stored.outbox_id) == []
        state = await upgraded._read_one(
            "SELECT attempt_number, state FROM delivery_handoff_uncertainty "
            "WHERE outbox_id=?",
            (stored.outbox_id,),
        )
        assert state == {
            "attempt_number": provenance.attempt_number,
            "state": "uncertain_native_handoff",
        }
        assert await upgraded._read_one(
            "SELECT native_message_id FROM radio_accepted_part_facts "
            "WHERE outbox_id=? AND attempt_number=1 AND ordinal=1",
            (stored.outbox_id,),
        ) == {"native_message_id": "native-1"}
        assert await upgraded.append_radio_packet_observation(
            _observation(
                "ack-after-upgrade-and-purge",
                provenance,
                radio_packet={
                    "ordinal": 1,
                    "origin_outbox_id": stored.outbox_id,
                    "origin_attempt_number": provenance.attempt_number,
                },
            )
        )
    finally:
        await upgraded.close()
    restarted = SQLiteStorage(path)
    await restarted.initialize()
    try:
        assert (
            await restarted._read_one(
                "SELECT observation_id FROM delivery_observations WHERE observation_id=?",
                ("ack-after-upgrade-and-purge",),
            )
            is not None
        )
        assert await restarted._read_all("PRAGMA foreign_key_check") == []
    finally:
        await restarted.close()


async def test_readonly_ceiling_audit_opens_exact_hash_named_database(tmp_path) -> None:
    """A URI-reserved filename character cannot redirect audit to a neighbor."""
    correct_path = str(tmp_path / "bridge #1.sqlite")
    decoy_path = str(tmp_path / "bridge ")
    for path, name in ((correct_path, "right-event"), (decoy_path, "wrong-event")):
        storage = SQLiteStorage(path)
        await storage.initialize()
        try:
            await _admit_plain(storage, name)
        finally:
            await storage.close()
    reader = await SQLiteStorage.open_readonly(correct_path)
    try:
        assert await reader._read_one("SELECT event_id FROM canonical_events") == {
            "event_id": "right-event"
        }
        assert await reader.audit_content_ceilings() is not None
        assert await reader.verify_content_ceiling_receipts() is not None
        with pytest.raises(StorageError, match="readonly|read-only"):
            await reader._write(
                "DELETE FROM canonical_events WHERE event_id=?", ("right-event",)
            )
    finally:
        await reader.close()


async def test_empty_ingress_clear_after_enforcement_does_not_recreate_a_row(
    temp_storage: SQLiteStorage,
) -> None:
    await _admit_plain(temp_storage, "late-ingress-empty-clear")
    now = await _install_elapsed_policy(temp_storage, "late-ingress-empty-clear")
    await temp_storage.enforce_content_ceilings(now=now)
    claimed = await temp_storage.claim_ingress_work(worker_id="clear-worker", limit=10)
    assert any(item.event_id == "late-ingress-empty-clear" for item in claimed)
    assert await temp_storage.release_ingress_work(
        "late-ingress-empty-clear", worker_id="clear-worker", error=""
    )
    assert await _diagnostics(temp_storage, "ingress", "late-ingress-empty-clear") == []


async def test_ceiling_purge_does_not_revive_superseded_uncertainty(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(
        temp_storage, outbox_id="superseded-after-purge", event_id="superseded-event"
    )
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        failure_kind="adapter_transient",
        failure_kind_detail="uncertain_native_handoff",
        attempt_number=provenance.attempt_number,
    )
    await temp_storage._write(
        "UPDATE delivery_handoff_uncertainty SET state='superseded_native_handoff' "
        "WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await _diagnostics(temp_storage, "outbox", stored.outbox_id) == []
    assert await temp_storage._read_one(
        "SELECT attempt_number, state FROM delivery_handoff_uncertainty "
        "WHERE outbox_id=?",
        (stored.outbox_id,),
    ) == {
        "attempt_number": provenance.attempt_number,
        "state": "superseded_native_handoff",
    }
