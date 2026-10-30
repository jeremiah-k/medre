"""Only a private local file may receive a compare-and-swap cancellation."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from medre.cli.main import _build_parser
from medre.cli.resolve_commands import _resolve_unstarted
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner boundary")


def _args(path: str, *options: str):
    return _build_parser(include_adapter_cli=False).parse_args(
        [
            "resolve",
            "--storage-path",
            path,
            "--event",
            "test-001",
            "--plan-id",
            "plan",
            "--target-adapter",
            "matrix",
            "--target-channel",
            "!left",
            *options,
        ]
    )


async def _seed(path: str, sample_event):
    store = SQLiteStorage(path)
    await store.initialize()
    await store.append(sample_event)
    for channel in ("!left", "!right"):
        await store.create_outbox_item(
            make_outbox_item(
                delivery_plan_id="plan",
                target_adapter="matrix",
                target_channel=channel,
                event_id=sample_event.event_id,
            )
        )
    await store.close()


async def test_preview_then_cancel_exact_one_leg_and_repeat(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed(path, sample_event)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_args(path))
    preview = json.loads(capsys.readouterr().out)
    assert preview["apply_authorized"] is False
    selected = preview["observed"]
    assert selected["target_channel"] == "!left"

    args = _args(
        path,
        "--apply",
        "--yes",
        "--decision-id",
        "choice-1",
        "--actor-label",
        "operator-provenance",
        "--outbox-id",
        selected["outbox_id"],
        "--expected-generation",
        str(selected["expected_generation"]),
        "--expected-status",
        selected["expected_status"],
        "--expected-updated-at",
        selected["expected_updated_at"],
    )
    await _resolve_unstarted(args)
    assert json.loads(capsys.readouterr().out)["committed"] is True
    await _resolve_unstarted(args)
    assert json.loads(capsys.readouterr().out)["committed"] is False

    store = await SQLiteStorage.open_readonly(path)
    try:
        outboxes = await store.list_outbox_items_for_event(sample_event.event_id)
        assert {x.target_channel: x.status for x in outboxes} == {
            "!left": "cancelled",
            "!right": "pending",
        }
        decisions = await store.list_operator_decisions(sample_event.event_id)
        assert len(decisions) == 1
        assert decisions[0]["decision_id"] == "choice-1"
    finally:
        await store.close()


async def test_stale_preview_refuses_a_late_native_claim(
    tmp_path, sample_event, capsys
):
    path = str(tmp_path / "state.sqlite")
    await _seed(path, sample_event)
    Path(path).chmod(0o600)
    await _resolve_unstarted(_args(path))
    observed = json.loads(capsys.readouterr().out)["observed"]
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.claim_due_outbox_items(
            now="2030-01-01T00:00:00", worker_id="native-owner", limit=10
        )
    finally:
        await store.close()
    args = _args(
        path,
        "--apply",
        "--yes",
        "--decision-id",
        "stale",
        "--actor-label",
        "operator",
        "--outbox-id",
        observed["outbox_id"],
        "--expected-generation",
        str(observed["expected_generation"]),
        "--expected-status",
        observed["expected_status"],
        "--expected-updated-at",
        observed["expected_updated_at"],
    )
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(args)
    assert exc.value.code == 1
    store = await SQLiteStorage.open_readonly(path)
    try:
        assert await store.list_operator_decisions(sample_event.event_id) == []
    finally:
        await store.close()


async def test_insecure_file_or_symlink_refused_before_open(tmp_path, sample_event):
    path = str(tmp_path / "state.sqlite")
    await _seed(path, sample_event)
    Path(path).chmod(0o644)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(_args(path))
    assert exc.value.code == 3
    Path(path).chmod(0o600)
    link = tmp_path / "alias.sqlite"
    link.symlink_to(Path(path))
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(_args(str(link)))
    assert exc.value.code == 3


async def test_apply_needs_explicit_confirmation_before_write(tmp_path, sample_event):
    path = str(tmp_path / "state.sqlite")
    await _seed(path, sample_event)
    Path(path).chmod(0o600)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(_args(path, "--apply", "--actor-label", "operator"))
    assert exc.value.code == 2


async def test_preview_refuses_competing_generations(tmp_path, sample_event):
    path = str(tmp_path / "state.sqlite")
    await _seed(path, sample_event)
    Path(path).chmod(0o600)
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.create_outbox_item(
            make_outbox_item(
                delivery_plan_id="plan",
                target_adapter="matrix",
                target_channel="!left",
                event_id=sample_event.event_id,
                attempt_number=2,
            )
        )
    finally:
        await store.close()
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(_args(path))
    assert exc.value.code == 5


async def test_empty_target_channel_does_not_select_no_channel(tmp_path, sample_event):
    path = str(tmp_path / "state.sqlite")
    await _seed(path, sample_event)
    Path(path).chmod(0o600)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(_args(path, "--target-channel", ""))
    assert exc.value.code == 2


@pytest.mark.parametrize("apply", [False, True])
async def test_parent_traversal_cannot_hide_a_symlink(
    tmp_path, sample_event, monkeypatch, capsys, apply
):
    """Ambiguous paths are refused before either storage opener is called."""
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "state.sqlite"
    await _seed(str(path), sample_event)
    path.chmod(0o600)
    link = directory / "shortcut"
    link.symlink_to(tmp_path, target_is_directory=True)

    def unexpected_open(*args, **kwargs):
        raise AssertionError("Ambiguous path reached a storage opener")

    monkeypatch.setattr("medre.cli.resolve_commands.SQLiteStorage", unexpected_open)
    monkeypatch.setattr(
        "medre.cli.resolve_commands._open_readonly_storage", unexpected_open
    )
    options = ("--apply",) if apply else ()
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(_args(f"{link}/../state.sqlite", *options))
    assert exc.value.code == 3
    assert "parent traversal" in capsys.readouterr().err


async def test_unsafe_ancestor_refused_even_with_a_private_parent(
    tmp_path, sample_event, capsys
):
    """A writable ancestor cannot provide authority for a private descendant."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    private = shared / "private"
    private.mkdir(mode=0o700)
    path = private / "state.sqlite"
    await _seed(str(path), sample_event)
    path.chmod(0o600)
    shared.chmod(0o777)
    try:
        with pytest.raises(SystemExit) as exc:
            await _resolve_unstarted(_args(str(path)))
        assert exc.value.code == 3
        assert "ancestors" in capsys.readouterr().err
    finally:
        shared.chmod(0o700)


async def test_openers_receive_the_validated_absolute_path(
    tmp_path, sample_event, monkeypatch, capsys
):
    """Preview and apply open the exact absolute name checked for permission."""
    from medre.cli import resolve_commands

    path = tmp_path / "state.sqlite"
    await _seed(str(path), sample_event)
    path.chmod(0o600)
    monkeypatch.chdir(tmp_path)
    reader = resolve_commands._open_readonly_storage
    opened = []

    async def checked_reader(name):
        opened.append(name)
        return await reader(name)

    def checked_writer(name):
        opened.append(name)
        return SQLiteStorage(name)

    monkeypatch.setattr(resolve_commands, "_open_readonly_storage", checked_reader)
    monkeypatch.setattr(resolve_commands, "SQLiteStorage", checked_writer)
    await _resolve_unstarted(_args("./state.sqlite"))
    observed = json.loads(capsys.readouterr().out)["observed"]
    await _resolve_unstarted(_apply_args("./state.sqlite", observed))
    assert json.loads(capsys.readouterr().out)["committed"] is True
    assert opened == [str(path), str(path)]


def _apply_args(path, observed):
    return _args(
        path,
        "--apply",
        "--yes",
        "--decision-id",
        "failure-test",
        "--actor-label",
        "operator",
        "--outbox-id",
        observed["outbox_id"],
        "--expected-generation",
        str(observed["expected_generation"]),
        "--expected-status",
        observed["expected_status"],
        "--expected-updated-at",
        observed["expected_updated_at"],
    )


@pytest.mark.parametrize("failure", ["initialize", "audit_write"])
async def test_storage_failure_uses_cli_exit_and_preserves_decision_atomicity(
    tmp_path, sample_event, capsys, failure, monkeypatch
):
    """Storage failures use exit 3; failed audit insertion cancels no target."""
    path = tmp_path / "state.sqlite"
    await _seed(str(path), sample_event)
    path.chmod(0o600)
    await _resolve_unstarted(_args(str(path)))
    observed = json.loads(capsys.readouterr().out)["observed"]
    if failure == "initialize":
        path.write_bytes(b"not a SQLite database")
    else:
        initialize = SQLiteStorage.initialize

        async def inject_after_validated_open(self):
            await initialize(self)
            await self._write(
                "CREATE TRIGGER reject_decision BEFORE INSERT ON "
                "operator_delivery_decisions "
                "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END",
                (),
            )

        monkeypatch.setattr(SQLiteStorage, "initialize", inject_after_validated_open)
    with pytest.raises(SystemExit) as exc:
        await _resolve_unstarted(_apply_args(str(path), observed))
    assert exc.value.code == 3
    output = capsys.readouterr()
    assert not output.out
    assert "Storage error:" in output.err
    if failure == "audit_write":
        # Remove only the test's runtime fault before validating persisted shape.
        raw = sqlite3.connect(path)
        try:
            raw.execute("DROP TRIGGER reject_decision")
            raw.commit()
        finally:
            raw.close()
        store = await SQLiteStorage.open_readonly(str(path))
        try:
            assert await store.list_operator_decisions(sample_event.event_id) == []
            items = await store.list_outbox_items_for_event(sample_event.event_id)
            assert all(item.status == "pending" for item in items)
        finally:
            await store.close()
