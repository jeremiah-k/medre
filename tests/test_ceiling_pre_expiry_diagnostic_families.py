"""Diagnostic retention is keyed to elapsed ceilings, not policy existence.

Two guards live here:

- An event whose ceiling is installed but has not elapsed must still
  retain separated receipt diagnostics written before expiry. A writer
  that suppressed diagnostics for every event carrying a policy row
  would erase legitimate pre-ceiling evidence.
- The sweep's diagnostic purge covers all four attribution families.
  Seeding only receipt and observation diagnostics would let the purge
  skip the outbox and ingress families while the zero-count assertion
  still passed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from medre.core.events import DeliveryReceipt
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    MARKER,
    _admit_with_ceiling,
    _read_ceiling,
    _seed_every_copy,
)


async def _diagnostics(store: SQLiteStorage, family: str, row_id: str) -> list[str]:
    rows = await store._read_all(
        "SELECT diagnostic FROM delivery_evidence_diagnostics "
        "WHERE family=? AND row_id=? ORDER BY diag_id",
        (family, row_id),
    )
    return [row["diagnostic"] for row in rows]


async def test_receipt_diagnostic_survives_with_an_unelapsed_policy(
    temp_storage: SQLiteStorage,
) -> None:
    event_id = "pre-expiry-policy-receipt"
    await _admit_with_ceiling(temp_storage, event_id)
    policy = await temp_storage._read_one(
        "SELECT hard_ceiling FROM event_content_access_policies WHERE event_id=?",
        (event_id,),
    )
    assert policy is not None
    ceiling = datetime.fromisoformat(str(policy["hard_ceiling"]))
    assert ceiling > datetime.now(timezone.utc)

    await temp_storage.append_receipt(
        DeliveryReceipt(
            receipt_id=f"receipt-{event_id}",
            event_id=event_id,
            delivery_plan_id="plan",
            target_adapter="fake",
            receipt_kind="attempt",
            status="failed",
            failure_kind="adapter_transient",
            error="LEGITIMATE PRE-EXPIRY DIAGNOSTIC",
        )
    )
    assert await _diagnostics(temp_storage, "receipt", f"receipt-{event_id}") == [
        "LEGITIMATE PRE-EXPIRY DIAGNOSTIC"
    ]


async def test_sweep_purges_the_outbox_and_ingress_diagnostic_families(
    temp_storage: SQLiteStorage,
) -> None:
    event_id = "ceiling-family-sweep"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    # The every-copy fixture seeds receipt and observation diagnostics; add
    # the two families whose attribution arms the assertion would otherwise
    # exercise against an empty table.
    await temp_storage._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES ('outbox', ?, 1, "
        "'error_summary', ?, ?)",
        (outbox_id, f"outbox family {MARKER}", now),
    )
    await temp_storage._write(
        "INSERT INTO delivery_evidence_diagnostics (family, row_id, attempt, "
        "field, diagnostic, created_at) VALUES ('ingress', ?, 1, "
        "'last_error', ?, ?)",
        (event_id, f"ingress family {MARKER}", now),
    )

    counted = await temp_storage._read_one(
        "SELECT COUNT(*) AS diagnostics FROM delivery_evidence_diagnostics d "
        "WHERE (d.family='receipt' AND d.row_id IN (SELECT receipt_id "
        "FROM delivery_receipts WHERE event_id=?)) "
        "OR (d.family='observation' AND d.row_id IN (SELECT observation_id "
        "FROM delivery_observations WHERE event_id=?)) "
        "OR (d.family='outbox' AND d.row_id=?) "
        "OR (d.family='ingress' AND d.row_id=?)",
        (event_id, event_id, outbox_id, event_id),
    )
    assert counted is not None and counted["diagnostics"] == 4

    ceiling = await _read_ceiling(temp_storage, event_id)
    enforced = await temp_storage.enforce_content_ceilings(
        now=ceiling + timedelta(seconds=1)
    )
    assert [item.event_id for item in enforced] == [event_id]

    residual = await temp_storage._read_one(
        "SELECT COUNT(*) AS diagnostics FROM delivery_evidence_diagnostics d "
        "WHERE (d.family='receipt' AND d.row_id IN (SELECT receipt_id "
        "FROM delivery_receipts WHERE event_id=?)) "
        "OR (d.family='observation' AND d.row_id IN (SELECT observation_id "
        "FROM delivery_observations WHERE event_id=?)) "
        "OR (d.family='outbox' AND d.row_id=?) "
        "OR (d.family='ingress' AND d.row_id=?)",
        (event_id, event_id, outbox_id, event_id),
    )
    assert residual is not None and residual["diagnostics"] == 0
