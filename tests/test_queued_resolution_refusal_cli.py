"""The unstarted-cancellation path refuses observed queued work by status.

Every observed field is supplied, so the exit proves the queued-status
guard fires — not the missing-observation config check that runs earlier
on other request shapes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medre.cli.exit_codes import EXIT_CONFIG
from medre.cli.main import _build_parser
from medre.cli.resolve_commands import _resolve_unstarted
from medre.core.delivery_authority import DeliveryIdentity
from medre.core.events import DeliveryReceipt
from medre.core.storage.sqlite._operator_decision import CONFIRMATION_PENDING_STATUS
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    admit_event,
    append_receipt_with_parent,
    make_outbox_item,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner boundary")

_EVENT = "evt-cli-queued-refusal"
_PLAN = "plan-cli-queued-refusal"
_ADAPTER = "matrix"
_CHANNEL = "!cli-queued-refusal"


async def _seed_queued(path: str) -> tuple[str, int, str]:
    """Create, claim, and queue one row; return identity, generation, version."""
    storage = SQLiteStorage(path)
    await storage.initialize()
    try:
        await admit_event(storage, _EVENT)
        stored = await storage.create_outbox_item(
            make_outbox_item(
                event_id=_EVENT,
                delivery_plan_id=_PLAN,
                target_adapter=_ADAPTER,
                target_channel=_CHANNEL,
            )
        )
        claimed = await storage.claim_due_outbox_items(stored.updated_at, "claimer")
        assert [c.outbox_id for c in claimed] == [stored.outbox_id]
        receipt = DeliveryReceipt(
            receipt_id="rcpt-cli-queued-refusal",
            event_id=stored.event_id,
            delivery_plan_id=stored.delivery_plan_id,
            target_adapter=stored.target_adapter,
            target_channel=stored.target_channel,
            outbox_id=stored.outbox_id,
            attempt_number=stored.attempt_number,
            source="live",
            status="queued",
        )
        await append_receipt_with_parent(storage, receipt)
        assert await storage.mark_outbox_queued(
            stored.outbox_id, receipt_id=receipt.receipt_id
        )
        row = await storage.get_outbox_item(stored.outbox_id)
        assert row is not None and row.status == CONFIRMATION_PENDING_STATUS
        assert row.updated_at is not None
        updated_at = (
            row.updated_at.isoformat()
            if hasattr(row.updated_at, "isoformat")
            else str(row.updated_at)
        )
        return stored.outbox_id, row.attempt_number, updated_at
    finally:
        await storage.close()


async def test_queued_apply_without_resolution_refuses_by_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fully observed queued apply without --resolution is the status refusal."""
    path = str(tmp_path / "state.sqlite")
    outbox_id, generation, updated_at = await _seed_queued(path)
    Path(path).chmod(0o600)
    args = _build_parser().parse_args(
        [
            "resolve",
            "--storage-path",
            path,
            "--event",
            _EVENT,
            "--plan-id",
            _PLAN,
            "--target-adapter",
            _ADAPTER,
            "--target-channel",
            _CHANNEL,
            "--apply",
            "--yes",
            "--decision-id",
            "decision-cli-queued-refusal",
            "--actor-label",
            "local-operator",
            "--outbox-id",
            outbox_id,
            "--expected-generation",
            str(generation),
            "--expected-status",
            "queued",
            "--expected-updated-at",
            updated_at,
        ]
    )
    assert args.resolution is None and args.action is None
    assert DeliveryIdentity(_EVENT, _PLAN, _ADAPTER, _CHANNEL).complete
    with pytest.raises(SystemExit) as excinfo:
        await _resolve_unstarted(args)
    assert excinfo.value.code == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "Queued confirmation-pending work requires --resolution" in err
