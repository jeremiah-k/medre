# medre-test-tier: compatibility
"""The evidence chain the operator walkthrough pages depend on.

``docs/ops/first-run-walkthrough.md`` and ``docs/ops/incident-walkthrough.md``
connect existing read-only surfaces into one path and keep eight knowledge
categories distinct. These tests pin the facts the pages state about those
surfaces, in the order the pages use them:

- reports bind to the running installation: readiness and evidence repeat
  the exact ``medre version`` string in ``medre_version``;
- a smoke-seeded SQLite database read back through ``inspect receipts``,
  ``inspect work-status``, and ``recover`` distinguishes adapter acceptance
  from recipient observation and never renders a delivered or healthy
  aggregate from local facts;
- an uncertain radio hand-off is visible per part through
  ``inspect radio-attempt`` with content withheld, and ``work-status`` keeps
  the native-send boundary honest for reserved generations;
- a deliberately stale confirmation-pending resolution through the real CLI
  refuses with exit code 1, prints the stale-state refusal, and mutates
  nothing.

Already pinned elsewhere and cited by the pages, not duplicated here:
readiness ``runtime``/``recipient`` ``not_observed`` rows
(``test_readiness_report.py``), the ceiling audit/verify states and
fail-closed ``no_receipt`` exit (``test_content_ceiling_audit_e2e.py``),
diagnostics build-vs-live rendering (``test_cli_diagnostics_render.py``),
and work-status metadata-only honesty (``test_cli_work_status.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path

import pytest

from medre.cli.main import _get_version, main
from medre.core.events.delivery import DELIVERY_CONFIRMATION_LEVEL_VALUES
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.confirmation_pending import EVENT_ID as CP_EVENT
from tests.helpers.confirmation_pending import PLAN_ID as CP_PLAN
from tests.helpers.confirmation_pending import TARGET_ADAPTER as CP_ADAPTER
from tests.helpers.confirmation_pending import TARGET_CHANNEL as CP_CHANNEL
from tests.helpers.confirmation_pending import seed_queued_row
from tests.helpers.handoff_resolution import (
    _WORKER,
    _bind_and_progress,
    _seed_reserved_claim,
)
from tests.helpers.walkthrough import seed_via_smoke_cli


@pytest.fixture(autouse=True)
def _clean_state_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear state-discovery env vars so every read hits explicit paths."""
    for var in (
        "MEDRE_HOME",
        "MEDRE_CONFIG",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


def _run_cli(*argv: str) -> tuple[int, str, str]:
    """Run one CLI invocation through dispatch, capturing stdout/stderr."""
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        try:
            main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


async def _seed_uncertain_radio(db_path: str) -> str:
    """Seed one reserved radio generation with an unknown packet part."""
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    try:
        stored, provenance = await _seed_reserved_claim(storage)
        await _bind_and_progress(
            storage, provenance, ["accepted", "unknown", "unsent"], worker=_WORKER
        )
        return stored.outbox_id
    finally:
        await storage.close()


async def _seed_queued_row_fields(path: str) -> tuple[str, str, object]:
    """Seed one confirmation-pending row; return identity and version fields."""
    storage = SQLiteStorage(path)
    await storage.initialize()
    try:
        _stored, row = await seed_queued_row(storage)
        return row.outbox_id, row.delivery_plan_id, row.updated_at
    finally:
        await storage.close()


async def _read_queued_rows(path: str) -> list[object]:
    """Read the event's outbox rows through a strict read-only connection."""
    storage = await SQLiteStorage.open_readonly(path)
    try:
        return list(await storage.list_outbox_items_for_event(CP_EVENT))
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# Version binding
# ---------------------------------------------------------------------------


def test_reports_repeat_the_running_version(tmp_path: Path) -> None:
    """Readiness and evidence JSON carry the same version `medre version` prints."""
    event_id, db_path = seed_via_smoke_cli(tmp_path)
    db_path = str(db_path)
    config_path = str(tmp_path / "smoke_storage_config.yaml")

    readiness_code, readiness_out, _ = _run_cli(
        "readiness", "--config", config_path, "--json"
    )
    evidence_code, evidence_out, _ = _run_cli(
        "evidence", "--storage-path", db_path, "--event", event_id, "--json"
    )
    assert readiness_code == 0
    assert evidence_code == 0

    expected = _get_version()
    readiness = json.loads(readiness_out)
    evidence = json.loads(evidence_out)
    assert readiness["medre_version"] == expected
    assert evidence["medre_version"] == expected


# ---------------------------------------------------------------------------
# Adapter acceptance vs recipient observation on one seeded bridge
# ---------------------------------------------------------------------------


def test_receipts_record_adapter_acceptance_never_recipient_delivery(
    tmp_path: Path,
) -> None:
    """Every seeded receipt is acceptance evidence with no recipient claim."""
    event_id, db_path = seed_via_smoke_cli(tmp_path)
    db_path = str(db_path)

    code, out, _ = _run_cli(
        "inspect", "receipts", "--event", event_id, "--storage-path", db_path
    )
    assert code == 0
    receipts = json.loads(out)
    assert receipts, "the smoke seed must leave at least one receipt"
    for receipt in receipts:
        assert receipt["status"] == "sent"
        assert receipt["confirmation_level"] in DELIVERY_CONFIRMATION_LEVEL_VALUES
        assert receipt["adapter_message_id"]
        for key in receipt:
            assert "recipient" not in key.lower()


def test_work_status_states_local_boundaries_on_a_healthy_bridge(
    tmp_path: Path,
) -> None:
    """With every delivery locally terminal, recipient facts stay unknown."""
    event_id, db_path = seed_via_smoke_cli(tmp_path)
    db_path = str(db_path)

    code, out, _ = _run_cli("inspect", "work-status", "--storage-path", db_path)
    assert code == 0
    assert "Recipient delivery: unknown" in out
    assert "Connectivity: unknown" in out
    assert "native queue 0" in out
    assert "uncertain outcome 0" in out
    assert "terminal 1" in out
    # No healthy/delivered aggregate may be rendered from local facts.
    assert "healthy" not in out
    assert "delivered" not in out

    code, out, _ = _run_cli("recover", "--event", event_id, "--storage-path", db_path)
    assert code == 0
    assert "does not establish recipient delivery" in out
    assert "Failed targets: none" in out


# ---------------------------------------------------------------------------
# The uncertain radio hand-off category
# ---------------------------------------------------------------------------


def test_radio_attempt_shows_unknown_part_without_handoff(tmp_path: Path) -> None:
    """One accepted part and one unknown part, content withheld throughout."""
    db_path = str(tmp_path / "radio-walkthrough.db")
    outbox_id = asyncio.run(_seed_uncertain_radio(db_path))

    code, out, _ = _run_cli(
        "inspect", "radio-attempt", outbox_id, "1", "--storage-path", db_path
    )
    assert code == 0
    report = json.loads(out)
    assert report["progress"]["content_withheld"] is True
    parts = {part["ordinal"]: part for part in report["progress"]["parts"]}
    assert parts[1]["state"] == "accepted"
    assert parts[1]["handoff_recorded"] is True
    assert parts[2]["state"] == "unknown"
    assert parts[2]["handoff_recorded"] is False
    assert parts[3]["state"] == "unsent"

    code, out, _ = _run_cli("inspect", "work-status", "--storage-path", db_path)
    assert code == 0
    assert "reserved/active 1" in out
    assert (
        "Native send: unknown for reserved generations without attempt evidence" in out
    )
    assert "Recipient delivery: unknown" in out

    code, out, _ = _run_cli(
        "recover", "--event", "evt-handoff-resolution", "--storage-path", db_path
    )
    assert code == 0
    assert "An attempt owns dispatch; inspect its lease before intervention." in out


# ---------------------------------------------------------------------------
# The deliberately failed recovery: stale resolution refuses, mutates nothing
# ---------------------------------------------------------------------------


def test_stale_resolution_apply_refuses_and_leaves_row_queued(tmp_path: Path) -> None:
    """Wrong expected generation: exit 1, refusal text, unchanged outbox row."""
    path = str(tmp_path / "stale-resolution.sqlite")
    outbox_id, plan_id, updated_at = asyncio.run(_seed_queued_row_fields(path))
    Path(path).chmod(0o600)
    assert updated_at is not None
    updated_at_text = (
        updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
    )

    code, out, err = _run_cli(
        "resolve",
        "--storage-path",
        path,
        "--event",
        CP_EVENT,
        "--plan-id",
        CP_PLAN,
        "--target-adapter",
        CP_ADAPTER,
        "--target-channel",
        CP_CHANNEL,
        "--resolution",
        "delivered",
        "--apply",
        "--yes",
        "--decision-id",
        "stale-walkthrough-decision",
        "--actor-label",
        "local-operator",
        "--outbox-id",
        outbox_id,
        "--expected-generation",
        "99",
        "--expected-status",
        "queued",
        "--expected-updated-at",
        updated_at_text,
    )
    assert code == 1
    assert "Stale observed state" in err
    assert out == ""

    rows = asyncio.run(_read_queued_rows(path))
    assert len(rows) == 1
    assert rows[0].outbox_id == outbox_id
    assert rows[0].status == "queued"
    assert rows[0].delivery_plan_id == plan_id
