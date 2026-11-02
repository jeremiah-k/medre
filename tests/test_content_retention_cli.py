"""Operator-facing retention status and activation command contracts.

Covers ``medre storage retention status`` and ``medre storage retention
activate`` through the full CLI parser: the content-free aggregate status
report, the honest heartbeat observation, the fail-closed refusals with the
sibling exit-code split, the append-only epoch activation semantics, and the
live-storage safety of the small activation transaction.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from medre.cli import main
from medre.cli.exit_codes import EXIT_BUILD, EXIT_CONFIG
from medre.cli.storage_commands import _RETENTION_REFUSAL
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.content_retention import seed_eligible_content as _seed_eligible_at

_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> tuple[str, str]:
    """Run the CLI and return (stdout, stderr). Catches SystemExit."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit:
        pass
    return stdout.getvalue(), stderr.getvalue()


def _run_cli_exit(*args: str) -> tuple[int, str, str]:
    """Run the CLI expecting a SystemExit; return (exit_code, stdout, stderr)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code: int = 0
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    return code, stdout.getvalue(), stderr.getvalue()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _schema(name: str) -> dict:
    return json.loads((_ROOT / "docs/schemas" / name).read_text())


def _status_validator() -> Draft202012Validator:
    schema = _schema("content-retention-status.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _activation_validator() -> Draft202012Validator:
    schema = _schema("content-retention-activation.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _with_store(
    db: str, body: Callable[[SQLiteStorage], Awaitable[Any]], *, initialize: bool = True
) -> Any:
    """Run ``body`` against a writable store handle and always close it."""

    async def _go() -> Any:
        store = SQLiteStorage(db)
        if initialize:
            await store.initialize()
        try:
            return await body(store)
        finally:
            await store.close()

    return asyncio.run(_go())


def _seed(db: str, event_id: str, created_at: str) -> None:
    def _body(store: SQLiteStorage) -> Awaitable[None]:
        return _seed_eligible_at(store, event_id, created_at)

    _with_store(db, _body)


async def _noop(store: SQLiteStorage) -> None:
    return None


# ---------------------------------------------------------------------------
# storage retention status
# ---------------------------------------------------------------------------


def test_status_without_epoch_reports_no_active_epoch(tmp_path: Path) -> None:
    db = str(tmp_path / "no-epoch.db")

    def _body(store: SQLiteStorage) -> Awaitable[None]:
        return _seed_eligible_at(
            store, "historic-marker-evt", "2026-10-31T10:00:00+00:00"
        )

    _with_store(db, _body)
    out, err = _run_cli("storage", "retention", "status", "--storage-path", db)
    assert err == ""
    report = json.loads(out)
    assert report["format"] == "medre-content-retention-status"
    assert report["version"] == 1
    assert report["scope"] == "core_sqlite"
    assert report["policy"] == {"status": "no_active_epoch"}
    assert report["heartbeat_observation"] == "unknown"
    assert report["heartbeat_stale"] is None
    assert report["heartbeat_grace_seconds"] == 3600
    assert report["heartbeat_assumed_interval_seconds"] == 300
    assert report["collected_events"] == 0
    assert report["preview"]["status"] == "no_active_epoch"
    assert report["preview"]["events_examined"] == 0
    assert report["preview"]["eligible"] == 0
    assert report["preview"]["has_more"] is False
    assert report["retention_authorized"] is False
    assert report["deletion_authorized"] is False
    assert report["physical_erasure_claimed"] is False
    assert report["unmeasured"]
    _status_validator().validate(report)
    dumped = json.dumps(report)
    assert "DO-NOT-DELETE" not in dumped
    assert "historic-marker-evt" not in dumped


def test_status_with_epoch_surfaces_policy_and_fresh_heartbeat(tmp_path: Path) -> None:
    db = str(tmp_path / "epoch.db")

    async def _body(store: SQLiteStorage) -> None:
        await _seed_eligible_at(store, "old-marker-evt", "2026-10-31T10:00:00+00:00")
        await store.open_retention_epoch(activated_by="operator", now=_utc_now())
        await _seed_eligible_at(store, "fresh-marker-evt", "2026-11-02T10:00:00+00:00")
        await store.observe_retention_heartbeat(epoch=1, now=_utc_now())

    _with_store(db, _body)
    out, err = _run_cli("storage", "retention", "status", "--storage-path", db)
    assert err == ""
    report = json.loads(out)
    policy = report["policy"]
    assert policy["epoch"] == 1
    assert policy["mode"] == "relay-only"
    assert policy["activated_at"]
    assert policy["admission_watermark"] == "2026-10-31T10:00:00+00:00"
    assert policy["last_observed_active_at"] is not None
    assert policy["activated_by"] == "operator"
    assert report["heartbeat_observation"] == "fresh"
    assert report["heartbeat_stale"] is False
    assert report["heartbeat_grace_seconds"] == 3600
    assert report["heartbeat_assumed_interval_seconds"] == 300
    # The embedded preview rollup counts only post-watermark events and
    # drops the preview's next_after continuation cursor: no identifiers.
    preview = report["preview"]
    assert preview["epoch"] == 1
    assert preview["events_examined"] == 1
    assert preview["eligible"] == 1
    assert preview["blocked"] == 0
    assert preview["has_more"] is False
    assert "next_after" not in preview
    assert preview["retention_authorized"] is False
    assert preview["deletion_authorized"] is False
    assert preview["physical_erasure_claimed"] is False
    assert report["collected_events"] == 0
    _status_validator().validate(report)
    dumped = json.dumps(report)
    assert "DO-NOT-DELETE" not in dumped
    assert "old-marker-evt" not in dumped
    assert "fresh-marker-evt" not in dumped


def test_status_reports_stale_heartbeat_past_the_grace_window(tmp_path: Path) -> None:
    db = str(tmp_path / "stale.db")
    stale_now = _utc_now() - timedelta(hours=2)

    async def _body(store: SQLiteStorage) -> None:
        await store.open_retention_epoch(activated_by="operator", now=stale_now)
        await store.observe_retention_heartbeat(epoch=1, now=stale_now)

    _with_store(db, _body)
    out, _ = _run_cli("storage", "retention", "status", "--storage-path", db)
    report = json.loads(out)
    assert report["heartbeat_observation"] == "stale"
    assert report["heartbeat_stale"] is True
    _status_validator().validate(report)


def test_status_treats_activation_as_last_activity_without_heartbeat(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "no-heartbeat.db")
    stale_now = _utc_now() - timedelta(hours=2)

    async def _body(store: SQLiteStorage) -> None:
        # No heartbeat recorded yet: the activation instant stands in, and a
        # long-idle epoch is honestly reported stale rather than fresh.
        await store.open_retention_epoch(activated_by="operator", now=stale_now)

    _with_store(db, _body)
    out, _ = _run_cli("storage", "retention", "status", "--storage-path", db)
    report = json.loads(out)
    assert report["policy"]["last_observed_active_at"] is None
    assert report["heartbeat_observation"] == "stale"
    assert report["heartbeat_stale"] is True


@pytest.mark.parametrize("heartbeat", ["not-a-timestamp", "2026-11-01T00:00:00"])
def test_status_reports_unknown_when_freshness_cannot_be_assessed(
    tmp_path: Path,
    heartbeat: str,
) -> None:
    db = str(tmp_path / "corrupt-heartbeat.db")

    async def _body(store: SQLiteStorage) -> None:
        await store.open_retention_epoch(activated_by="operator", now=_utc_now())
        await store._write(
            "UPDATE content_retention_epochs " "SET last_observed_active_at=?",
            (heartbeat,),
        )

    _with_store(db, _body)
    out, _ = _run_cli("storage", "retention", "status", "--storage-path", db)
    report = json.loads(out)
    assert report["heartbeat_observation"] == "unknown"
    assert report["heartbeat_stale"] is None
    _status_validator().validate(report)


def test_status_refuses_structurally_invalid_empty_heartbeat(tmp_path: Path) -> None:
    db = str(tmp_path / "empty-heartbeat.db")

    async def corrupt(store: SQLiteStorage) -> None:
        await store.open_retention_epoch(activated_by="operator", now=_utc_now())
        await store._write(
            "UPDATE content_retention_epochs SET last_observed_active_at=''"
        )

    _with_store(db, corrupt)
    code, out, err = _run_cli_exit(
        "storage", "retention", "status", "--storage-path", db
    )
    assert code == EXIT_CONFIG
    assert out == ""
    assert _RETENTION_REFUSAL in err
    assert db not in err


def test_status_counts_collected_receipts_after_real_collections(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "collected.db")

    async def _body(store: SQLiteStorage) -> None:
        await store.open_retention_epoch(activated_by="operator", now=_utc_now())
        await _seed_eligible_at(store, "gone-marker-evt", "2026-11-02T10:00:00+00:00")
        await _seed_eligible_at(store, "kept-marker-evt", "2026-11-02T11:00:00+00:00")
        assert (await store.collect_event_content("gone-marker-evt", dry_run=False))[
            "status"
        ] == "collected"

    _with_store(db, _body)
    out, _ = _run_cli("storage", "retention", "status", "--storage-path", db)
    report = json.loads(out)
    assert report["collected_events"] == 1
    assert report["preview"]["events_examined"] == 2
    assert report["preview"]["eligible"] == 1
    assert report["preview"]["already_collected"] == 1
    _status_validator().validate(report)
    dumped = json.dumps(report)
    assert "DO-NOT-DELETE" not in dumped
    assert "gone-marker-evt" not in dumped
    assert "kept-marker-evt" not in dumped


def test_status_fails_closed_on_missing_storage_without_creating_files(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.db"
    code, out, err = _run_cli_exit(
        "storage", "retention", "status", "--storage-path", str(missing)
    )
    assert code == EXIT_BUILD
    assert "existing SQLite database required" in err
    assert out == ""
    assert not missing.exists()


def test_status_fails_closed_with_path_free_refusal_on_unreadable_storage(
    tmp_path: Path,
) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 32)
    code, out, err = _run_cli_exit(
        "storage", "retention", "status", "--storage-path", str(garbage)
    )
    assert code == EXIT_CONFIG
    assert err == f"{_RETENTION_REFUSAL}\n"
    assert out == ""
    # The refusal never leaks the storage path.
    assert "garbage" not in err


def test_status_fails_closed_on_incomplete_storage(tmp_path: Path) -> None:
    partial = tmp_path / "partial.db"
    conn = sqlite3.connect(str(partial))
    try:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    code, out, err = _run_cli_exit(
        "storage", "retention", "status", "--storage-path", str(partial)
    )
    assert code == EXIT_CONFIG
    assert err == f"{_RETENTION_REFUSAL}\n"
    assert out == ""


# ---------------------------------------------------------------------------
# storage retention activate
# ---------------------------------------------------------------------------


def test_activate_requires_yes(tmp_path: Path) -> None:
    db = str(tmp_path / "guarded.db")
    _with_store(db, _noop)
    code, out, err = _run_cli_exit(
        "storage", "retention", "activate", "--storage-path", db
    )
    assert code == EXIT_CONFIG
    assert "--yes" in err
    assert out == ""

    async def _read() -> None:
        readonly = await SQLiteStorage.open_readonly(db)
        try:
            assert await readonly.latest_retention_epoch() is None
        finally:
            await readonly.close()

    asyncio.run(_read())


def test_activate_opens_operator_epoch_at_the_admission_high_water(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "activate.db")

    async def _body(store: SQLiteStorage) -> None:
        await _seed_eligible_at(store, "pre-marker-evt-1", "2026-11-01T10:00:00+00:00")
        await _seed_eligible_at(store, "pre-marker-evt-2", "2026-11-02T10:00:00+00:00")

    _with_store(db, _body)
    code, out, err = _run_cli_exit(
        "storage", "retention", "activate", "--storage-path", db, "--yes"
    )
    assert code == 0
    assert err == ""
    result = json.loads(out)
    assert result["format"] == "medre-content-retention-activation"
    assert result["version"] == 1
    assert result["epoch"] == 1
    assert result["mode"] == "relay-only"
    assert result["activated_by"] == "operator"
    assert result["admission_watermark"] == "2026-11-02T10:00:00+00:00"
    assert result["physical_erasure_claimed"] is False
    _activation_validator().validate(result)
    dumped = json.dumps(result)
    assert "DO-NOT-DELETE" not in dumped
    assert "pre-marker-evt-1" not in dumped
    assert "pre-marker-evt-2" not in dumped
    # Pre-activation history is excluded: the preview examines none of it.
    out, _ = _run_cli("storage", "retention", "status", "--storage-path", db)
    report = json.loads(out)
    assert report["policy"]["admission_watermark"] == "2026-11-02T10:00:00+00:00"
    assert report["preview"]["events_examined"] == 0
    # A post-activation event is inside the boundary.
    _seed(db, "post-marker-evt", "2026-11-03T10:00:00+00:00")
    out, _ = _run_cli("storage", "retention", "status", "--storage-path", db)
    report = json.loads(out)
    assert report["preview"]["events_examined"] == 1
    assert report["preview"]["eligible"] == 1


def test_activate_appends_a_new_epoch_each_time(tmp_path: Path) -> None:
    db = str(tmp_path / "append.db")
    _seed(db, "early-marker-evt", "2026-11-01T10:00:00+00:00")
    code, out, _ = _run_cli_exit(
        "storage", "retention", "activate", "--storage-path", db, "--yes"
    )
    first = json.loads(out)
    assert code == 0
    assert first["epoch"] == 1
    assert first["admission_watermark"] == "2026-11-01T10:00:00+00:00"
    # Activation is not idempotent: a second command appends epoch 2 and
    # moves the watermark forward to the new admission high-water.
    _seed(db, "later-marker-evt", "2026-11-02T10:00:00+00:00")
    code, out, _ = _run_cli_exit(
        "storage", "retention", "activate", "--storage-path", db, "--yes"
    )
    second = json.loads(out)
    assert code == 0
    assert second["epoch"] > first["epoch"]
    assert second["admission_watermark"] > first["admission_watermark"]
    _activation_validator().validate(first)
    _activation_validator().validate(second)
    out, _ = _run_cli("storage", "retention", "status", "--storage-path", db)
    report = json.loads(out)
    assert report["policy"]["epoch"] == second["epoch"]
    # An empty store keeps the previous (absent) watermark across epochs.
    empty_db = str(tmp_path / "empty-epochs.db")
    _with_store(empty_db, _noop)
    code, out, _ = _run_cli_exit(
        "storage", "retention", "activate", "--storage-path", empty_db, "--yes"
    )
    assert code == 0
    first_empty = json.loads(out)
    assert first_empty["admission_watermark"] is None
    code, out, _ = _run_cli_exit(
        "storage", "retention", "activate", "--storage-path", empty_db, "--yes"
    )
    assert code == 0
    second_empty = json.loads(out)
    assert second_empty["epoch"] == first_empty["epoch"] + 1
    assert second_empty["admission_watermark"] is None


def test_activate_is_safe_against_live_storage(tmp_path: Path) -> None:
    db = str(tmp_path / "live.db")

    async def _body(store: SQLiteStorage) -> None:
        await _seed_eligible_at(store, "live-marker-evt", "2026-11-01T10:00:00+00:00")
        # The writable handle stays open while the CLI opens its own
        # connection from another thread: one small transaction serializes
        # correctly against the live store.
        results: dict[str, tuple[int, str, str]] = {}

        def _run() -> None:
            results["cli"] = _run_cli_exit(
                "storage", "retention", "activate", "--storage-path", db, "--yes"
            )

        thread = threading.Thread(target=_run)
        thread.start()
        thread.join(timeout=60)
        assert not thread.is_alive()
        code, out, err = results["cli"]
        assert code == 0
        assert err == ""
        assert json.loads(out)["epoch"] == 1
        epoch = await store.latest_retention_epoch()
        assert epoch is not None
        assert epoch.epoch == 1
        assert epoch.activated_by == "operator"
        assert epoch.admission_watermark == "2026-11-01T10:00:00+00:00"
        assert await store.count_collected_events() == 0

    _with_store(db, _body)


def test_activate_refuses_missing_file_without_mutation(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    code, out, err = _run_cli_exit(
        "storage", "retention", "activate", "--storage-path", str(missing), "--yes"
    )
    assert code == EXIT_BUILD
    assert "existing SQLite database required" in err
    assert out == ""
    assert not missing.exists()


def test_activate_refuses_non_database_file_without_mutation(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    empty.touch()
    code, out, err = _run_cli_exit(
        "storage", "retention", "activate", "--storage-path", str(empty), "--yes"
    )
    assert code == EXIT_BUILD
    assert "existing SQLite database required" in err
    assert out == ""
    assert empty.stat().st_size == 0
    assert not (tmp_path / "empty.db-wal").exists()


def test_retention_group_requires_a_subcommand(tmp_path: Path) -> None:
    code, out, err = _run_cli_exit(
        "storage", "retention", "--storage-path", str(tmp_path / "x.db")
    )
    assert code == EXIT_CONFIG
    assert out == ""


# ---------------------------------------------------------------------------
# Docs guards
# ---------------------------------------------------------------------------


def test_cli_inventory_documents_the_retention_commands() -> None:
    text = (_ROOT / "docs/ops/configuration.md").read_text()
    section = re.search(
        r"^## CLI Commands(.*?)(?=^## |\Z)", text, re.DOTALL | re.MULTILINE
    )
    assert section, "configuration.md lost its CLI Commands section"
    fence = re.search(r"```text\n(.*?)```", section.group(1), re.DOTALL)
    assert fence, "configuration.md CLI inventory ```text block is missing"
    inventory = fence.group(1)
    assert "medre storage retention status" in inventory
    assert "medre storage retention activate" in inventory
    assert "--yes" in inventory
    assert "--storage-path" in inventory


def test_retention_group_help_describes_both_commands() -> None:
    stdout, stderr = _run_cli("storage", "retention", "--help")
    assert stderr == ""
    assert "read-only" in stdout
    assert "--yes" in stdout
    # The activation help states the stopped-storage maintenance path.
    assert "storage collect --apply" in stdout


def test_retention_leaf_help_lists_the_operator_flags() -> None:
    stdout, stderr = _run_cli("storage", "retention", "status", "--help")
    assert stderr == ""
    assert "--storage-path" in stdout
    stdout, stderr = _run_cli("storage", "retention", "activate", "--help")
    assert stderr == ""
    assert "--storage-path" in stdout
    assert "--yes" in stdout


@pytest.mark.parametrize("interval", [1, 300, 600, 86400])
def test_status_and_worker_share_epoch_freshness_rule(interval, monkeypatch):
    from unittest.mock import MagicMock

    from medre.cli import storage_commands
    from medre.core.storage.content_retention import ContentRetentionPolicy
    from medre.runtime.collection import ContentCollectionWorker

    monkeypatch.setattr(
        storage_commands, "_HEARTBEAT_ASSUMED_INTERVAL_SECONDS", interval
    )
    worker = ContentCollectionWorker(
        MagicMock(),
        MagicMock(),
        ContentRetentionPolicy(mode="relay-only", interval_seconds=interval),
    )
    assert (
        storage_commands._heartbeat_observation(None)[2]
        == worker._epoch_staleness_threshold()
    )
