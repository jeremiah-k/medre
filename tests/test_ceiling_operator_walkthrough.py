"""Operator walkthrough contract for the hard content-privacy ceiling.

Pins, through the real CLI and the real loader, exactly what the operator
procedure in ``docs/ops/content-ceiling-audit.md`` tells an operator to
observe and run:

- ``medre storage ceiling audit --event`` reports each ceiling state
  distinctly (``not_elapsed``, ``elapsed_pending``, ``enforced_clean``,
  ``unknown``), every report denies physical erasure, and ``verify``
  passes only receipted, clean events and fails closed otherwise.
- The documented opt-in rollout profile loads through ``load_config``
  with the intended values while the loader default stays disabled.
- A failed enforcement tick leaves the store observably expired-not-purged
  (audit ``elapsed_pending``, verify exit 1, body rows intact, no partial
  purge) and the next pass completes it.
- One bounded pass cannot finish a backlog larger than its page: the
  documented completion condition (one audit window with no
  ``elapsed_pending`` and no ``residual_enforced`` findings, plus every
  verify page passing) is what detects the work a single pass leaves
  behind.
- The enforcement transition purges without dispatching: outbox statuses,
  receipts, and settlements are never fabricated by a purge (the
  worker-level fence — the maintenance worker never claims delivery work —
  is pinned in ``tests/test_ceiling_sweep_wiring.py``).

Every seeded value is synthetic.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from medre.config.loader import load_config
from medre.core.storage.backend import OutboxDispatchSnapshotWrite
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    _admit_plain,
    _admit_with_ceiling,
    _count,
    _install_elapsed_policy,
    _seed_every_copy,
    _seed_late_write,
)
from tests.helpers.cli import _run_cli_raw
from tests.helpers.storage_outbox import make_outbox_item

_AUDIT_FORMAT = "medre-content-ceiling-audit"
_VERIFY_FORMAT = "medre-content-ceiling-verify"


def _cli_json(argv: tuple[str, ...]) -> tuple[int, dict]:
    """Run one ceiling CLI invocation and return (exit code, parsed JSON)."""
    stdout, stderr, code = _run_cli_raw(*argv)
    assert stdout, f"ceiling CLI printed nothing on stdout (stderr: {stderr})"
    return code or 0, json.loads(stdout)


async def _backdate(store: SQLiteStorage, event_id: str, *, seconds: int = 5) -> None:
    elapsed = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    await store._write(
        "UPDATE event_content_access_policies SET hard_ceiling=? WHERE event_id=?",
        (elapsed.isoformat(timespec="microseconds"), event_id),
    )


async def _seed_state_store(db_path: Path) -> None:
    """Seed one event per state on a real store, then close the writer."""
    store = SQLiteStorage(db_path=str(db_path))
    await store.initialize()
    try:
        await _admit_with_ceiling(store, "walk-fresh", seconds=3600)
        await _admit_with_ceiling(store, "walk-expired", seconds=3600)
        await _backdate(store, "walk-expired")
        await _admit_plain(store, "walk-seeded")
        await _install_elapsed_policy(store, "walk-seeded")
    finally:
        await store.close()


async def _enforce_once(db_path: str, *, event_ids: set[str]) -> None:
    """Run the guarded enforcement transition once, then close the writer."""
    store = SQLiteStorage(db_path=db_path)
    await store.initialize()
    try:
        enforced = await store.enforce_content_ceilings(limit=50)
        assert {row.event_id for row in enforced} == event_ids
        if enforced:
            assert all(row.residual_pass is False for row in enforced)
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# The four states through the real CLI
# ---------------------------------------------------------------------------


def test_audit_cli_reports_each_ceiling_state_distinctly(tmp_path: Path) -> None:
    db = tmp_path / "walkthrough.sqlite"
    asyncio.run(_seed_state_store(db))
    path = str(db)

    code, fresh = _cli_json(
        ("storage", "ceiling", "audit", "--storage-path", path, "--event", "walk-fresh")
    )
    assert code == 0
    assert fresh["format"] == _AUDIT_FORMAT
    assert fresh["state"] == "not_elapsed"
    assert fresh["receipt"] is None
    assert fresh["residual_stores_total"] >= 1

    code, expired = _cli_json(
        (
            "storage",
            "ceiling",
            "audit",
            "--storage-path",
            path,
            "--event",
            "walk-expired",
        )
    )
    assert code == 0
    assert expired["state"] == "elapsed_pending"
    assert expired["receipt"] is None

    code, window = _cli_json(
        ("storage", "ceiling", "audit", "--storage-path", path, "--limit", "25")
    )
    assert code == 0
    assert window["receipts"] == 0
    assert [row["event_id"] for row in window["elapsed_pending"]] == [
        "walk-expired",
        "walk-seeded",
    ]
    assert all(row["residual_stores_total"] >= 1 for row in window["elapsed_pending"])

    code, unknown = _cli_json(
        ("storage", "ceiling", "audit", "--storage-path", path, "--event", "walk-ghost")
    )
    assert code == 0
    assert unknown["state"] == "unknown"

    # Every report states the erasure boundary explicitly.
    for report in (fresh, expired, window, unknown):
        assert report["physical_erasure_claimed"] is False
        assert "sqlite_free_pages_and_wal" in report["unmeasured"]

    # Enforcement is the guarded storage transition the opt-in sweep calls.
    asyncio.run(_enforce_once(path, event_ids={"walk-expired", "walk-seeded"}))

    code, clean = _cli_json(
        (
            "storage",
            "ceiling",
            "audit",
            "--storage-path",
            path,
            "--event",
            "walk-expired",
        )
    )
    assert code == 0
    assert clean["state"] == "enforced_clean"
    assert clean["residual_stores_total"] == 0

    code, verify = _cli_json(
        ("storage", "ceiling", "verify", "--storage-path", path, "--limit", "25")
    )
    assert code == 0
    assert verify["format"] == _VERIFY_FORMAT
    assert verify["verdict"] == "passed"
    assert verify["receipts_passed"] == 2
    assert all(row["status"] == "passed" for row in verify["receipts"])

    # Verification fails closed without a receipt: absence of proof is not proof.
    code, failed = _cli_json(
        (
            "storage",
            "ceiling",
            "verify",
            "--storage-path",
            path,
            "--event",
            "walk-fresh",
        )
    )
    assert code == 1
    assert failed["verdict"] == "failed"
    assert failed["event_state"] == "no_receipt"


# ---------------------------------------------------------------------------
# The documented opt-in rollout profile through the real loader
# ---------------------------------------------------------------------------


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    config_path = tmp_path / "profile.yaml"
    config_path.write_text(text)
    config, _source, _paths = load_config(str(config_path))
    return config


def test_opt_in_profile_loads_and_loader_default_stays_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = _load(tmp_path, monkeypatch, "runtime: {}\n")
    assert default.retry.ceiling_sweep.enabled is False
    assert default.retry.ceiling_sweep.limit == 50
    assert default.retry.ceiling_sweep.interval_seconds == 60.0

    # The profile exactly as documented in docs/ops/content-ceiling-audit.md.
    profile = _load(
        tmp_path,
        monkeypatch,
        "retry:\n"
        "  enabled: false\n"
        "  ceiling_sweep:\n"
        "    enabled: true\n"
        "    limit: 50\n"
        "    interval_seconds: 60\n",
    )
    assert profile.retry.enabled is False
    assert profile.retry.ceiling_sweep.enabled is True
    assert profile.retry.ceiling_sweep.limit == 50
    assert profile.retry.ceiling_sweep.interval_seconds == 60.0


# ---------------------------------------------------------------------------
# A failed enforcement tick is observably expired-not-purged
# ---------------------------------------------------------------------------


async def _seed_and_fail_enforcement(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, int]:
    """Seed one elapsed target, fail a purge mid-tick, and report row counts.

    The failed tick's transaction rolls back; the counts returned are the
    body rows and ceiling receipts observed after the rollback.
    """
    store = SQLiteStorage(db_path=db_path)
    await store.initialize()
    try:
        await _admit_with_ceiling(store, "walk-fail", seconds=3600)
        await _backdate(store, "walk-fail")

        def exploding_purge(db_conn, event_id: str):
            raise RuntimeError("synthetic verification failure")

        monkeypatch.setattr(
            "medre.core.storage.sqlite._ceiling_enforcement._measure_and_purge",
            exploding_purge,
        )
        with pytest.raises(RuntimeError, match="synthetic verification failure"):
            await store.enforce_content_ceilings(limit=25)
        monkeypatch.undo()
        bodies = await _count(store, "SELECT COUNT(*) AS n FROM event_body_content")
        receipts = await _count(
            store, "SELECT COUNT(*) AS n FROM event_content_ceiling_log"
        )
        return bodies, receipts
    finally:
        await store.close()


def test_failed_enforcement_leaves_expired_not_purged_observably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "failed-tick.sqlite"
    path = str(db)
    bodies, receipts = asyncio.run(_seed_and_fail_enforcement(path, monkeypatch))

    # No partial purge: the body row survives with no receipt.
    assert bodies == 1
    assert receipts == 0

    code, audit = _cli_json(
        (
            "storage",
            "ceiling",
            "audit",
            "--storage-path",
            path,
            "--event",
            "walk-fail",
        )
    )
    assert code == 0
    assert audit["state"] == "elapsed_pending"
    assert audit["receipt"] is None

    code, verify = _cli_json(
        ("storage", "ceiling", "verify", "--storage-path", path, "--event", "walk-fail")
    )
    assert code == 1
    assert verify["event_state"] == "no_receipt"

    # The next pass completes the purge and the observables flip.
    asyncio.run(_enforce_once(path, event_ids={"walk-fail"}))

    code, clean = _cli_json(
        (
            "storage",
            "ceiling",
            "audit",
            "--storage-path",
            path,
            "--event",
            "walk-fail",
        )
    )
    assert code == 0
    assert clean["state"] == "enforced_clean"
    code, passed = _cli_json(
        ("storage", "ceiling", "verify", "--storage-path", path, "--event", "walk-fail")
    )
    assert code == 0
    assert passed["verdict"] == "passed"


# ---------------------------------------------------------------------------
# The rehearsal completion condition across enforcement pages
# ---------------------------------------------------------------------------


async def _seed_paged_backlog(db_path: str, *, count: int = 51) -> dict[str, str]:
    """Seed more elapsed targets than one limit=50 pass can enforce.

    Each target carries the full synthetic residual profile so the seeded
    store matches the documented rehearsal's shaped copy. Returns the
    seeded outbox identities for late-write seeding.
    """
    store = SQLiteStorage(db_path=db_path)
    await store.initialize()
    outbox_ids: dict[str, str] = {}
    try:
        elapsed = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(
            timespec="microseconds"
        )
        for i in range(count):
            event_id = f"walk-page-{i:02d}"
            await _admit_with_ceiling(store, event_id, seconds=3600)
            outbox_ids[event_id] = await _seed_every_copy(store, event_id)
            await store._write(
                "UPDATE event_content_access_policies "
                "SET hard_ceiling=? WHERE event_id=?",
                (elapsed, event_id),
            )
    finally:
        await store.close()
    return outbox_ids


async def _enforce_pass(db_path: str) -> list[str]:
    """Run one limit=50 enforcement pass and return its summaries."""
    store = SQLiteStorage(db_path=db_path)
    await store.initialize()
    try:
        enforced = await store.enforce_content_ceilings(limit=50)
        return [row.event_id for row in enforced]
    finally:
        await store.close()


async def _audit_window(db_path: str) -> dict:
    """Run the read-only audit with the rehearsal's 25-event window."""
    store = SQLiteStorage(db_path=db_path)
    await store.initialize()
    try:
        return await store.audit_content_ceilings(limit=25)
    finally:
        await store.close()


async def _seed_late_writes(db_path: str, targets: dict[str, str]) -> None:
    """Land pre-ceiling-authorized late writes on receipted events."""
    store = SQLiteStorage(db_path=db_path)
    await store.initialize()
    try:
        receipts = await _count(
            store, "SELECT COUNT(*) AS n FROM event_content_ceiling_log"
        )
        assert receipts == 51, receipts
        for event_id, outbox_id in targets.items():
            await _seed_late_write(store, event_id, outbox_id)
    finally:
        await store.close()


def test_completion_condition_detects_work_one_pass_leaves_behind(
    tmp_path: Path,
) -> None:
    """A 51-event backlog needs repeated bounded passes and full paging.

    Pins the documented rehearsal completion condition against the real
    paging semantics: one pass enforces at most 50 elapsed events, and the
    residual repair walks one bounded receipt page per pass, so a late
    write on a later receipt survives a pass that empties
    ``elapsed_pending`` while the first verify page still passes.
    """
    db = tmp_path / "paged-rehearsal.sqlite"
    path = str(db)
    outbox_ids = asyncio.run(_seed_paged_backlog(path))

    # One pass enforces at most 50 of the 51 elapsed events.
    assert len(asyncio.run(_enforce_pass(path))) == 50

    # The documented observable reports the leftover work.
    window = asyncio.run(_audit_window(path))
    assert [row["event_id"] for row in window["elapsed_pending"]] == ["walk-page-50"]
    assert window["elapsed_pending_has_more"] is False

    # The next pass completes the new-event backlog to the full observable.
    assert asyncio.run(_enforce_pass(path)) == ["walk-page-50"]
    clean = asyncio.run(_audit_window(path))
    assert clean["elapsed_pending"] == []
    assert clean["residual_enforced"] == []
    assert clean["elapsed_pending_has_more"] is False
    assert clean["residual_enforced_has_more"] is False

    # Late writes land on two receipted events: one inside the receipt
    # page a pass revisits, one beyond it.
    asyncio.run(
        _seed_late_writes(
            path,
            {
                "walk-page-25": outbox_ids["walk-page-25"],
                "walk-page-50": outbox_ids["walk-page-50"],
            },
        )
    )
    trapped = asyncio.run(_audit_window(path))
    assert trapped["elapsed_pending"] == []
    assert sorted(row["event_id"] for row in trapped["residual_enforced"]) == [
        "walk-page-25",
        "walk-page-50",
    ]

    # The first verify page passes while residual content waits beyond it:
    # one clean page never proves the unchecked history clean.
    code, verify = _cli_json(
        ("storage", "ceiling", "verify", "--storage-path", path, "--limit", "25")
    )
    assert code == 0
    assert verify["verdict"] == "passed"
    assert verify["receipts_examined"] == 25
    assert verify["receipts_has_more"] is True
    assert verify["receipts_next_after_event"] == "walk-page-24"

    # One pass repairs only the receipts its residual page reaches.
    assert asyncio.run(_enforce_pass(path)) == ["walk-page-25"]
    after_one = asyncio.run(_audit_window(path))
    assert after_one["elapsed_pending"] == []
    assert [row["event_id"] for row in after_one["residual_enforced"]] == [
        "walk-page-50"
    ]

    # The next pass reaches the rest; the observable is then fully met.
    assert asyncio.run(_enforce_pass(path)) == ["walk-page-50"]
    done = asyncio.run(_audit_window(path))
    assert done["elapsed_pending"] == []
    assert done["residual_enforced"] == []
    assert done["elapsed_pending_has_more"] is False
    assert done["residual_enforced_has_more"] is False

    # Every verify page passes across the whole receipt history.
    after = ""
    pages = 0
    while True:
        argv = (
            "storage",
            "ceiling",
            "verify",
            "--storage-path",
            path,
            "--limit",
            "25",
        )
        if after:
            argv = argv + ("--after-event", after)
        code, page = _cli_json(argv)
        assert code == 0
        assert page["verdict"] == "passed"
        pages += 1
        after = page["receipts_next_after_event"] or ""
        if not page["receipts_has_more"]:
            break
    assert pages == 3


# ---------------------------------------------------------------------------
# The purge never dispatches or fabricates settlement
# ---------------------------------------------------------------------------


async def _seed_no_send_store(db_path: str) -> str:
    """Seed one elapsed target with a staged pending generation."""
    store = SQLiteStorage(db_path=db_path)
    await store.initialize()
    try:
        await _admit_with_ceiling(store, "walk-nosend", seconds=3600)
        await _backdate(store, "walk-nosend")
        created = await store.create_outbox_item(
            make_outbox_item(
                delivery_plan_id="plan-walk-nosend",
                event_id="walk-nosend",
                target_adapter="radio",
                status="pending",
            ),
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="direct",
                context={"delivery_strategy": "direct", "capability_level": "native"},
            ),
        )
        return created.outbox_id
    finally:
        await store.close()


async def _enforce_and_assert_no_send(db_path: str, outbox_id: str) -> None:
    """Enforce the elapsed target and pin that the purge never dispatches."""
    store = SQLiteStorage(db_path=db_path)
    await store.initialize()
    try:
        receipts_before = await _count(
            store, "SELECT COUNT(*) AS n FROM delivery_receipts"
        )
        enforced = await store.enforce_content_ceilings(limit=25)
        assert [row.event_id for row in enforced] == ["walk-nosend"]

        row = await store._read_one(
            "SELECT status, attempt_number, active_attempt FROM delivery_outbox "
            "WHERE outbox_id=?",
            (outbox_id,),
        )
        assert row is not None
        # The dispatch context is retired, but the generation is never
        # claimed, marked sent, or settled by the purge.
        assert row["status"] == "pending"
        assert row["active_attempt"] is None
        assert await _count(store, "SELECT COUNT(*) AS n FROM delivery_receipts") == (
            receipts_before
        )
        assert (
            await _count(store, "SELECT COUNT(*) AS n FROM delivery_outbox_snapshots")
            == 0
        )
        assert (
            await _count(store, "SELECT COUNT(*) AS n FROM event_content_ceiling_log")
            == 1
        )
    finally:
        await store.close()


def test_enforcement_purges_without_sending_or_fabricating_settlement(
    tmp_path: Path,
) -> None:
    db = tmp_path / "no-send.sqlite"
    path = str(db)
    outbox_id = asyncio.run(_seed_no_send_store(path))
    asyncio.run(_enforce_and_assert_no_send(path, outbox_id))
