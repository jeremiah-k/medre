"""The CLI mirror of the confirmation-pending recovery path.

The ``medre`` resolve surface mirrors the storage authority's
confirmation-pending disposition: preview, apply, exit codes, and refusal
output. Every command first establishes the local permission boundary — a
private owner-only database file and parent (``_check_private_local_store``)
— so the module is gated on POSIX: the boundary check refuses non-POSIX
platforms outright, and its ``st_mode`` owner-only bits have no Windows
representation. The storage-authority side of the contract lives in
``test_confirmation_pending_recovery.py`` without the gate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from medre.cli.exit_codes import EXIT_FAILED
from medre.cli.main import _build_parser
from medre.cli.resolve_commands import _resolve_unstarted
from medre.core.storage.sqlite._operator_decision import (
    CONFIRMATION_PENDING_STATUS,
    OBSERVATION_STALE,
    RECEIPT_PROVENANCE_CONTRADICTION,
    SETTLED_TARGET_UNDECIDABLE,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.confirmation_pending import (
    EVENT_ID,
    PLAN_ID,
    TARGET_ADAPTER,
    TARGET_CHANNEL,
    build_receipt,
    seed_queued_row,
)
from tests.helpers.storage_outbox import admit_event, append_receipt_with_parent

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner boundary")


def _cli_args(path: str, *options: str):
    return _build_parser(include_adapter_cli=False).parse_args(
        [
            "resolve",
            "--storage-path",
            path,
            "--event",
            EVENT_ID,
            "--plan-id",
            PLAN_ID,
            "--target-adapter",
            TARGET_ADAPTER,
            "--target-channel",
            TARGET_CHANNEL,
            *options,
        ]
    )


async def _seed_cli_state(path: str) -> None:
    store = SQLiteStorage(path)
    await store.initialize()
    await admit_event(store, EVENT_ID)
    await seed_queued_row(store)
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
        items = await store.list_outbox_items_for_event(EVENT_ID)
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
    await admit_event(store, EVENT_ID)
    await store.close()
    Path(path).chmod(0o600)
    with pytest.raises(SystemExit) as excinfo:
        await _resolve_unstarted(_cli_args(path, "--resolution", "delivered"))
    assert excinfo.value.code == 5


async def test_cli_provenance_contradiction_refuses_apply(tmp_path, capsys) -> None:
    path = str(tmp_path / "state.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    await admit_event(store, EVENT_ID)
    stored, row = await seed_queued_row(store)
    await append_receipt_with_parent(
        store,
        build_receipt(
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


async def test_queued_preview_without_resolution_refuses_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The queued gate also covers the preview mode, before any read.

    A ``--expected-status queued`` observation without ``--resolution`` is a
    configuration error in both modes: the unstarted-cancellation preview
    only lists ``pending``/``retry_wait`` rows, so the old behavior answered
    a misleading "absent or ambiguous" not-found instead of naming the
    required flag.
    """
    path = str(tmp_path / "queued-preview.sqlite")
    await _seed_cli_state(path)
    Path(path).chmod(0o600)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(_cli_args(path, "--expected-status", "queued"))
    assert exc.value.code == 2
    assert "requires --resolution" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Preview/apply parity with the write-transaction refusals
# ---------------------------------------------------------------------------


async def test_cli_preview_refuses_terminal_receipt_before_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A terminal receipt at the effective attempt is previewed as refused.

    The preview must mirror the write transaction exactly: reporting a
    decision-capable observation for a row the authority will refuse would
    send the operator through an apply that can only fail.
    """
    path = str(tmp_path / "terminal.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    await admit_event(store, EVENT_ID)
    stored, row = await seed_queued_row(store)
    await append_receipt_with_parent(
        store,
        build_receipt(stored, "cancelled", receipt_id="early-cancel"),
    )
    await store.close()
    Path(path).chmod(0o600)
    await _resolve_unstarted(_cli_args(path, "--resolution", "failed"))
    preview = json.loads(capsys.readouterr().out)
    assert preview["decision_capable"] is False
    assert SETTLED_TARGET_UNDECIDABLE in preview["refusal_implications"]

    args = _cli_args(
        path,
        "--resolution",
        "failed",
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
    assert excinfo.value.code == EXIT_FAILED
    assert "Resolution refused" in capsys.readouterr().err
    assert capsys.readouterr().out == ""
