"""The explicit operator recovery path for confirmation-pending deliveries.

A ``queued`` outbox row — the durable confirmation-pending disposition —
leaves its disposition only through evidence-driven settlement or the
explicit operator resolution. The storage authority commits the ruled
terminal transition under the frozen refusal vocabulary, refuses when
committed receipt evidence contradicts the ruling, records no content, and
never dispatches a send. The CLI mirrors the exact-cancellation surface:
preview, apply, exit codes, and refusal output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from medre.cli.main import _build_parser
from medre.cli.resolve_commands import _resolve_unstarted
from medre.core.delivery_authority import DeliveryIdentity
from medre.core.events import DeliveryReceipt
from medre.core.storage.sqlite._operator_decision import (
    COMPETING_GENERATION,
    CONFIRMATION_PENDING_STATUS,
    OBSERVATION_STALE,
    OPERATOR_RULING_FAILURE_KIND,
    RECEIPT_PROVENANCE_CONTRADICTION,
    ConfirmationPendingResolution,
    OperatorDecisionRefused,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    admit_event,
    append_receipt_with_parent,
    make_outbox_item,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner boundary")

_PLAN = "plan-confirmation-pending"
_ADAPTER = "matrix"
_CHANNEL = "!pending"
_EVENT = "evt-confirmation-pending"


def _receipt(
    stored,
    status: str,
    *,
    receipt_id: str = "queued-acceptance",
    target_channel: str | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=stored.event_id,
        delivery_plan_id=stored.delivery_plan_id,
        target_adapter=stored.target_adapter,
        target_channel=target_channel or stored.target_channel,
        outbox_id=stored.outbox_id,
        attempt_number=stored.attempt_number,
        source="live",
        status=status,
    )


async def _seed_queued(
    storage: SQLiteStorage,
    *,
    event_id: str = _EVENT,
    plan_id: str = _PLAN,
) -> tuple:
    """Create, claim, and queue one row; append its queued receipt."""
    await admit_event(storage, event_id)
    stored = await storage.create_outbox_item(
        make_outbox_item(
            event_id=event_id,
            delivery_plan_id=plan_id,
            target_adapter=_ADAPTER,
            target_channel=_CHANNEL,
        )
    )
    claimed = await storage.claim_due_outbox_items(stored.updated_at, "first-worker")
    assert [c.outbox_id for c in claimed] == [stored.outbox_id]
    receipt = _receipt(stored, "queued")
    await append_receipt_with_parent(storage, receipt)
    assert await storage.mark_outbox_queued(
        stored.outbox_id, receipt_id=receipt.receipt_id
    )
    row = await storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == CONFIRMATION_PENDING_STATUS
    return stored, row


def _decision(stored, row, resolution: str) -> ConfirmationPendingResolution:
    assert row.updated_at is not None
    # The observed version is rendered exactly as the CLI preview renders it.
    updated_at = (
        row.updated_at.isoformat()
        if hasattr(row.updated_at, "isoformat")
        else str(row.updated_at)
    )
    return ConfirmationPendingResolution(
        decision_id=f"decision-{resolution}",
        actor_id="local-operator",
        identity=DeliveryIdentity(
            stored.event_id,
            stored.delivery_plan_id,
            stored.target_adapter,
            stored.target_channel,
        ),
        outbox_id=stored.outbox_id,
        expected_generation=row.attempt_number,
        expected_status=CONFIRMATION_PENDING_STATUS,
        expected_updated_at=updated_at,
        resolution=resolution,
    )


async def test_delivered_resolution_commits_sent(temp_storage: SQLiteStorage) -> None:
    stored, row = await _seed_queued(temp_storage)
    committed = await temp_storage.resolve_confirmation_pending(
        _decision(stored, row, "delivered")
    )
    assert committed is True
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None
    assert after.status == "sent"
    # The ruling keeps the acceptance receipt pointer and clears the
    # failure fact; no receipt is fabricated.
    assert after.receipt_id == "queued-acceptance"
    assert after.failure_kind is None
    assert after.last_attempt_at is not None
    # The terminal settlement retires the frozen dispatch context.
    assert (
        await temp_storage.get_outbox_dispatch_context(
            stored.outbox_id,
            event_id=stored.event_id,
            delivery_plan_id=stored.delivery_plan_id,
            target_adapter=stored.target_adapter,
            target_channel=stored.target_channel,
        )
        is None
    )


async def test_failed_resolution_commits_dead_lettered(
    temp_storage: SQLiteStorage,
) -> None:
    stored, row = await _seed_queued(temp_storage)
    committed = await temp_storage.resolve_confirmation_pending(
        _decision(stored, row, "failed")
    )
    assert committed is True
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None
    assert after.status == "dead_lettered"
    assert after.failure_kind == OPERATOR_RULING_FAILURE_KIND


async def test_cancelled_resolution_commits_cancelled(
    temp_storage: SQLiteStorage,
) -> None:
    stored, row = await _seed_queued(temp_storage)
    committed = await temp_storage.resolve_confirmation_pending(
        _decision(stored, row, "cancelled")
    )
    assert committed is True
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == "cancelled"


async def test_resolution_records_no_content(temp_storage: SQLiteStorage) -> None:
    stored, row = await _seed_queued(temp_storage)
    await temp_storage.resolve_confirmation_pending(_decision(stored, row, "failed"))
    diagnostics = await temp_storage._read_all(
        "SELECT field, diagnostic FROM delivery_evidence_diagnostics "
        "WHERE family='outbox' AND row_id=?",
        (stored.outbox_id,),
    )
    assert all(not entry["diagnostic"] for entry in diagnostics)
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.error_summary is None


async def test_repeat_resolution_refuses_the_settled_row(
    temp_storage: SQLiteStorage,
) -> None:
    stored, row = await _seed_queued(temp_storage)
    decision = _decision(stored, row, "delivered")
    assert await temp_storage.resolve_confirmation_pending(decision) is True
    with pytest.raises(OperatorDecisionRefused, match="Completed or cancelled"):
        await temp_storage.resolve_confirmation_pending(decision)
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == "sent"


async def test_stale_observation_refuses(temp_storage: SQLiteStorage) -> None:
    stored, row = await _seed_queued(temp_storage)
    await temp_storage._write(
        "UPDATE delivery_outbox SET updated_at = ? WHERE outbox_id = ?",
        ("2026-01-02T00:00:00+00:00", stored.outbox_id),
    )
    with pytest.raises(OperatorDecisionRefused, match="Stale"):
        await temp_storage.resolve_confirmation_pending(
            _decision(stored, row, "delivered")
        )
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == CONFIRMATION_PENDING_STATUS


async def test_competing_generation_refuses(temp_storage: SQLiteStorage) -> None:
    stored, row = await _seed_queued(temp_storage)
    await temp_storage.create_outbox_item(
        make_outbox_item(
            event_id=stored.event_id,
            delivery_plan_id=stored.delivery_plan_id,
            target_adapter=stored.target_adapter,
            target_channel=stored.target_channel,
            attempt_number=2,
        )
    )
    with pytest.raises(OperatorDecisionRefused, match=COMPETING_GENERATION):
        await temp_storage.resolve_confirmation_pending(
            _decision(stored, row, "failed")
        )


async def test_sent_receipt_contradiction_refuses(
    temp_storage: SQLiteStorage,
) -> None:
    stored, row = await _seed_queued(temp_storage)
    await append_receipt_with_parent(
        temp_storage,
        _receipt(stored, "sent", receipt_id="late-sent-proof"),
    )
    with pytest.raises(OperatorDecisionRefused, match="already settles"):
        await temp_storage.resolve_confirmation_pending(
            _decision(stored, row, "failed")
        )
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == CONFIRMATION_PENDING_STATUS


async def test_provenance_contradiction_refuses(
    temp_storage: SQLiteStorage,
) -> None:
    stored, row = await _seed_queued(temp_storage)
    await append_receipt_with_parent(
        temp_storage,
        _receipt(
            stored,
            "queued",
            receipt_id="foreign-channel",
            target_channel="!foreign",
        ),
    )
    with pytest.raises(OperatorDecisionRefused, match="provenance contradicts"):
        await temp_storage.resolve_confirmation_pending(
            _decision(stored, row, "failed")
        )


def _cli_args(path: str, *options: str):
    return _build_parser(include_adapter_cli=False).parse_args(
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
            *options,
        ]
    )


async def _seed_cli_state(path: str) -> None:
    store = SQLiteStorage(path)
    await store.initialize()
    await admit_event(store, _EVENT)
    await _seed_queued(store)
    await store.close()


async def test_cli_preview_then_delivered_resolution(tmp_path, capsys) -> None:
    path = str(tmp_path / "state.sqlite")
    await _seed_cli_state(path)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_cli_args(path, "--resolution", "delivered"))
    preview = json.loads(capsys.readouterr().out)
    assert preview["apply_authorized"] is False
    assert preview["decision_capable"] is True
    observed = preview["observed"]
    assert observed["expected_status"] == CONFIRMATION_PENDING_STATUS

    args = _cli_args(
        path,
        "--resolution",
        "delivered",
        "--apply",
        "--yes",
        "--decision-id",
        "resolve-1",
        "--actor-label",
        "local-operator",
        "--outbox-id",
        observed["outbox_id"],
        "--expected-generation",
        str(observed["expected_generation"]),
        "--expected-status",
        observed["expected_status"],
        "--expected-updated-at",
        observed["expected_updated_at"],
    )
    await _resolve_unstarted(args)
    assert json.loads(capsys.readouterr().out)["committed"] is True

    store = await SQLiteStorage.open_readonly(path)
    try:
        items = await store.list_outbox_items_for_event(_EVENT)
        assert {item.status for item in items} == {"sent"}
    finally:
        await store.close()


async def test_cli_settled_apply_refuses(tmp_path, capsys) -> None:
    path = str(tmp_path / "state.sqlite")
    await _seed_cli_state(path)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_cli_args(path, "--resolution", "cancelled"))
    observed = json.loads(capsys.readouterr().out)["observed"]
    args = _cli_args(
        path,
        "--resolution",
        "cancelled",
        "--apply",
        "--yes",
        "--decision-id",
        "resolve-1",
        "--actor-label",
        "local-operator",
        "--outbox-id",
        observed["outbox_id"],
        "--expected-generation",
        str(observed["expected_generation"]),
        "--expected-status",
        observed["expected_status"],
        "--expected-updated-at",
        observed["expected_updated_at"],
    )
    await _resolve_unstarted(args)
    assert json.loads(capsys.readouterr().out)["committed"] is True

    with pytest.raises(SystemExit) as excinfo:
        await _resolve_unstarted(args)
    assert excinfo.value.code == 1
    assert "Resolution refused" in capsys.readouterr().err


async def test_cli_missing_target_exits_not_found(tmp_path, capsys) -> None:
    path = str(tmp_path / "state.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    await admit_event(store, _EVENT)
    await store.close()
    Path(path).chmod(0o600)
    with pytest.raises(SystemExit) as excinfo:
        await _resolve_unstarted(_cli_args(path, "--resolution", "delivered"))
    assert excinfo.value.code == 5


async def test_cli_provenance_contradiction_refuses_apply(tmp_path, capsys) -> None:
    path = str(tmp_path / "state.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    await admit_event(store, _EVENT)
    stored, row = await _seed_queued(store)
    await append_receipt_with_parent(
        store,
        _receipt(
            stored,
            "queued",
            receipt_id="foreign-channel",
            target_channel="!foreign",
        ),
    )
    await store.close()
    Path(path).chmod(0o600)
    await _resolve_unstarted(_cli_args(path, "--resolution", "delivered"))
    preview = json.loads(capsys.readouterr().out)
    assert preview["decision_capable"] is False
    assert RECEIPT_PROVENANCE_CONTRADICTION in preview["refusal_implications"]

    args = _cli_args(
        path,
        "--resolution",
        "delivered",
        "--apply",
        "--yes",
        "--decision-id",
        "resolve-1",
        "--actor-label",
        "local-operator",
        "--outbox-id",
        stored.outbox_id,
        "--expected-generation",
        str(row.attempt_number),
        "--expected-status",
        CONFIRMATION_PENDING_STATUS,
        "--expected-updated-at",
        row.updated_at,
    )
    with pytest.raises(SystemExit) as excinfo:
        await _resolve_unstarted(args)
    assert excinfo.value.code == 1
    assert "Resolution refused" in capsys.readouterr().err


async def test_cli_stale_observation_refuses_apply(tmp_path, capsys) -> None:
    path = str(tmp_path / "state.sqlite")
    await _seed_cli_state(path)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_cli_args(path, "--resolution", "delivered"))
    observed = json.loads(capsys.readouterr().out)["observed"]
    store = SQLiteStorage(path)
    await store.initialize()
    await store._write(
        "UPDATE delivery_outbox SET updated_at = ? WHERE outbox_id = ?",
        ("2026-01-02T00:00:00+00:00", observed["outbox_id"]),
    )
    await store.close()
    args = _cli_args(
        path,
        "--resolution",
        "delivered",
        "--apply",
        "--yes",
        "--decision-id",
        "resolve-1",
        "--actor-label",
        "local-operator",
        "--outbox-id",
        observed["outbox_id"],
        "--expected-generation",
        str(observed["expected_generation"]),
        "--expected-status",
        observed["expected_status"],
        "--expected-updated-at",
        observed["expected_updated_at"],
    )
    with pytest.raises(SystemExit) as excinfo:
        await _resolve_unstarted(args)
    assert excinfo.value.code == 1
    assert (
        OBSERVATION_STALE in capsys.readouterr().err
        or "Stale" in capsys.readouterr().err
    )


async def test_cli_resolution_requires_observed_state(tmp_path, capsys) -> None:
    path = str(tmp_path / "state.sqlite")
    await _seed_cli_state(path)
    Path(path).chmod(0o600)
    with pytest.raises(SystemExit) as excinfo:
        await _resolve_unstarted(
            _cli_args(
                path,
                "--resolution",
                "delivered",
                "--apply",
                "--yes",
                "--decision-id",
                "resolve-1",
                "--actor-label",
                "local-operator",
            )
        )
    assert excinfo.value.code == 2


async def test_cli_unstarted_cancel_rejects_queued_expected_status(
    tmp_path,
) -> None:
    path = str(tmp_path / "state.sqlite")
    await _seed_cli_state(path)
    Path(path).chmod(0o600)
    args = _cli_args(
        path,
        "--apply",
        "--yes",
        "--decision-id",
        "cancel-1",
        "--actor-label",
        "local-operator",
        "--expected-status",
        CONFIRMATION_PENDING_STATUS,
    )
    from medre.cli.exit_codes import EXIT_CONFIG

    with pytest.raises(SystemExit) as excinfo:
        await _resolve_unstarted(args)
    assert excinfo.value.code == EXIT_CONFIG


async def test_operator_ruling_rejects_wrong_receipt_run_provenance(
    temp_storage: SQLiteStorage,
) -> None:
    stored, row = await _seed_queued(temp_storage)
    await temp_storage._write(
        "UPDATE delivery_receipts SET source='replay', replay_run_id='foreign-run' "
        "WHERE receipt_id='queued-acceptance'"
    )
    with pytest.raises(OperatorDecisionRefused, match=RECEIPT_PROVENANCE_CONTRADICTION):
        await temp_storage.resolve_confirmation_pending(
            _decision(stored, row, "failed")
        )
    current = await temp_storage.get_outbox_item(stored.outbox_id)
    assert current is not None and current.status == "queued"


async def test_operator_ruling_refuses_existing_terminal_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    stored, row = await _seed_queued(temp_storage)
    await temp_storage._write(
        "INSERT INTO delivery_receipts "
        "(receipt_id,event_id,delivery_plan_id,target_adapter,target_channel, "
        "outbox_id,attempt_number,status,receipt_kind,created_at) "
        "VALUES ('earlier-cancel',?,?,?,?,? ,1,'cancelled','lifecycle',?)",
        (
            stored.event_id,
            stored.delivery_plan_id,
            stored.target_adapter,
            stored.target_channel,
            stored.outbox_id,
            "2026-11-21T00:00:00+00:00",
        ),
    )
    with pytest.raises(OperatorDecisionRefused, match="Completed or cancelled"):
        await temp_storage.resolve_confirmation_pending(
            _decision(stored, row, "failed")
        )
    current = await temp_storage.get_outbox_item(stored.outbox_id)
    assert current is not None and current.status == "queued"


async def test_queued_without_resolution_never_uses_unstarted_cancel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = str(tmp_path / "queued.sqlite")
    await _seed_cli_state(path)
    Path(path).chmod(0o600)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(
            _cli_args(path, "--expected-status", "queued", "--apply")
        )
    assert exc.value.code != 0
    assert "requires --resolution" in capsys.readouterr().err
    storage = SQLiteStorage(path)
    await storage.initialize()
    try:
        assert (
            await storage._read_one("SELECT status FROM delivery_outbox LIMIT 1")
        ) == {"status": "queued"}
    finally:
        await storage.close()
