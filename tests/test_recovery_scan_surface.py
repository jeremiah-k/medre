"""End-to-end behavioral tests for the operator recovery scan surface.

These exercise the real journey against a real SQLite evidence database
seeded through the public storage API, then driven through the real CLI
parser and command handlers in read-only mode:

* ``medre recover`` scan (``--since`` / ``--limit`` / ``--cursor``),
* ``medre recover --event`` / ``medre inspect event --recovery`` runbook.

The load-bearing contract under test: *current failure means the latest
receipt of a logical delivery is failed/dead_lettered*.  Successful
retries and executed replays must not remain false alarms, unrelated
successes must not hide failures, pages are bounded with honest
continuation, and the command never mutates the database.

Baseline-red selectors (fail on the pre-change tree, pass after):
``test_successful_retry_supersedes_failure`` (the pre-change scan echoed
flags instead of querying, and the runbook listed superseded failures as
current), ``test_pagination_continuation_beyond_default`` and
``test_sparse_failures_beyond_1000_discoverable`` (the pre-change scan
returned a static stub with no query at all), and
``test_scan_does_not_mutate_storage``.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.cli import EXIT_BUILD, EXIT_CONFIG, main
from medre.core.events import CanonicalEvent, DeliveryReceipt, EventMetadata
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    encode_page_cursor,
    resolve_delivery_outcomes,
)
from medre.core.storage.sqlite.storage import SQLiteStorage

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli_raw(*args: str) -> tuple[str, str, int | None]:
    """Run CLI and return (stdout, stderr, exit_code)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code: int | None = None
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as exc:
        code = exc.code
    return stdout.getvalue(), stderr.getvalue(), code


def _run_cli_json(*args: str) -> dict[str, Any]:
    stdout, stderr, code = _run_cli_raw(*args)
    assert code in (None, 0), f"command failed ({code}): {stderr}"
    return json.loads(stdout)


def _event(
    event_id: str, *, at: datetime, kind: str = "message.created"
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=kind,
        schema_version=1,
        timestamp=at,
        source_adapter="matrix.main",
        source_transport_id="srv-1",
        source_channel_id="src-ch",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "recovery journey"},
        metadata=EventMetadata(),
    )


def _receipt(
    event_id: str,
    *,
    receipt_id: str,
    status: str,
    plan: str = "plan-1",
    adapter: str = "meshtastic.radio",
    channel: str | None = None,
    attempt: int = 1,
    sequence: int = 0,
    error: str | None = "TimeoutError: connection timed out",
    failure_kind: str | None = "adapter_transient",
    source: str = "live",
    replay_run_id: str | None = None,
    parent_receipt_id: str | None = None,
    outbox_id: str | None = None,
    at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=sequence,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=plan,
        target_adapter=adapter,
        target_channel=channel,
        route_id="route-1",
        status=status,  # type: ignore[arg-type]
        error=error,
        failure_kind=failure_kind,
        attempt_number=attempt,
        parent_receipt_id=parent_receipt_id,
        source=source,  # type: ignore[arg-type]
        replay_run_id=replay_run_id,
        outbox_id=outbox_id,
        created_at=at or (_T0 + timedelta(seconds=30)),
    )


def _seed(
    path: Path,
    events: list[CanonicalEvent],
    receipts: list[DeliveryReceipt],
) -> None:
    """Create a real evidence DB through the public storage API."""
    asyncio.run(_seed_coro(path, events, receipts))


async def _seed_coro(
    path: Path,
    events: list[CanonicalEvent],
    receipts: list[DeliveryReceipt],
) -> None:
    storage = SQLiteStorage(str(path))
    try:
        await storage.initialize()
        for event in events:
            await storage.append(event)
        for receipt in receipts:
            await storage.append_receipt(receipt)
    finally:
        await storage.close()


def _db_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecars(path: Path) -> list[Path]:
    return [
        p
        for p in (
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-shm"),
            path.with_name(path.name + "-journal"),
        )
        if p.exists()
    ]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "MEDRE_CONFIG", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Recovery parser surface
# ---------------------------------------------------------------------------


def test_recover_no_args_reaches_storage_boundary() -> None:
    """Bare ``medre recover`` is valid broad-scan syntax."""
    _stdout, _stderr, code = _run_cli_raw(
        "recover",
        "--storage-path",
        "/nonexistent",
    )
    assert code in (EXIT_CONFIG, EXIT_BUILD)


def test_recover_event_rejects_scan_only_flags() -> None:
    """Single-event recovery rejects controls that only scope a scan."""
    _stdout, stderr, code = _run_cli_raw(
        "recover",
        "--event",
        "evt-1",
        "--since",
        "2026-01-01T00:00:00+00:00",
        "--limit",
        "20",
        "--cursor",
        encode_page_cursor(0),
        "--storage-path",
        "/nonexistent",
    )
    assert code == EXIT_CONFIG
    assert "scan-only option" in stderr


def test_recovery_outputs_sanitize_persisted_receipt_errors(tmp_path: Path) -> None:
    """Recovery output redacts raw receipt secrets without mutating storage."""
    db_path = tmp_path / "recovery-redaction.db"
    event = _event("evt-redaction", at=_T0)
    secret = "access_token=super-secret-value"
    receipt = _receipt(
        event.event_id,
        receipt_id="rcpt-redaction",
        status="failed",
        error=f"adapter failed: {secret}",
    )
    _seed(db_path, [event], [receipt])

    scan = _run_cli_json("recover", "--storage-path", str(db_path), "--json")
    event_runbook = _run_cli_json(
        "recover",
        "--event",
        event.event_id,
        "--storage-path",
        str(db_path),
        "--json",
    )
    scan_text = json.dumps(scan, sort_keys=True)
    event_text = json.dumps(event_runbook, sort_keys=True)

    assert "super-secret-value" not in scan_text
    assert "super-secret-value" not in event_text
    assert "[REDACTED]" in scan["unresolved"][0]["error"]
    assert "[REDACTED]" in event_runbook["failed_targets"][0]["error"]

    stdout, _stderr, code = _run_cli_raw(
        "recover",
        "--storage-path",
        str(db_path),
    )
    assert code in (None, 0)
    assert "super-secret-value" not in stdout
    assert "[REDACTED]" in stdout


def test_recover_rejects_removed_dry_run_flag() -> None:
    """Recovery previewing belongs to ``replay --mode dry_run``."""
    _stdout, _stderr, code = _run_cli_raw(
        "recover",
        "--event",
        "evt-1",
        "--dry-run",
        "--storage-path",
        "/nonexistent",
    )
    assert code == 2


def test_recover_rejects_removed_failed_only_flag() -> None:
    """The scan is inherently unresolved-failures-only."""
    _stdout, _stderr, code = _run_cli_raw(
        "recover",
        "--failed-only",
        "--storage-path",
        "/nonexistent",
    )
    assert code == 2


def test_recover_rejects_naive_since() -> None:
    """A timestamp without an explicit offset is ambiguous and rejected."""
    _stdout, _stderr, code = _run_cli_raw(
        "recover",
        "--since",
        "2026-01-01T00:00:00",
        "--storage-path",
        "/nonexistent",
    )
    assert code == 2


def test_recover_rejects_malformed_since() -> None:
    _stdout, _stderr, code = _run_cli_raw(
        "recover",
        "--since",
        "not-a-timestamp",
        "--storage-path",
        "/nonexistent",
    )
    assert code == 2


def test_recover_rejects_out_of_range_limit() -> None:
    _stdout, _stderr, code = _run_cli_raw(
        "recover",
        "--limit",
        "0",
        "--storage-path",
        "/nonexistent",
    )
    assert code == 2


# ---------------------------------------------------------------------------
# Current-outcome semantics
# ---------------------------------------------------------------------------


def test_scan_finds_unresolved_failure(tmp_path: Path) -> None:
    db = tmp_path / "one-failure.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [_receipt("evt-1", receipt_id="r1", status="failed")],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    assert report["scope"] == "scan"
    assert len(report["unresolved"]) == 1
    row = report["unresolved"][0]
    assert row["event_id"] == "evt-1"
    assert row["target_adapter"] == "meshtastic.radio"
    assert row["status"] == "failed"
    assert row["attempt_source"] == "live"
    assert row["attempt_number"] == 1
    assert isinstance(row["receipt_sequence"], int)
    assert row["error"]


def test_successful_retry_supersedes_failure(tmp_path: Path) -> None:
    """failed attempt 1 then sent attempt 2 in one lineage: not a current
    failure; the runbook records it as superseded history instead."""
    db = tmp_path / "recovered.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [
            _receipt("evt-1", receipt_id="r1", status="failed", attempt=1),
            _receipt(
                "evt-1",
                receipt_id="r2",
                status="sent",
                attempt=2,
                parent_receipt_id="r1",
                error=None,
                failure_kind=None,
                at=_T0 + timedelta(minutes=1),
            ),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    assert report["unresolved"] == []

    runbook = _run_cli_json(
        "recover", "--storage-path", str(db), "--event", "evt-1", "--json"
    )
    assert runbook["failed_targets"] == []
    assert len(runbook["historical_failures"]) == 1
    hist = runbook["historical_failures"][0]
    assert hist["receipt_id"] == "r1"
    assert hist["superseded_by"]["receipt_id"] == "r2"
    assert hist["superseded_by"]["status"] == "sent"


def test_unrelated_success_does_not_hide_failure(tmp_path: Path) -> None:
    """A sent receipt on another channel of the same event leaves the
    failing channel's delivery unresolved."""
    db = tmp_path / "two-channels.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [
            _receipt(
                "evt-1",
                receipt_id="r-ok",
                status="sent",
                channel="ch-good",
                error=None,
                failure_kind=None,
            ),
            _receipt("evt-1", receipt_id="r-bad", status="failed", channel="ch-bad"),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    rows = report["unresolved"]
    assert len(rows) == 1
    assert rows[0]["target_channel"] == "ch-bad"
    assert rows[0]["receipt_id"] == "r-bad"


def test_later_append_supersedes_higher_attempt_number_in_current_outcome() -> None:
    receipts = [
        _receipt(
            "evt-order",
            receipt_id="old-high-attempt",
            plan="plan-order",
            adapter="matrix",
            channel=None,
            status="failed",
            attempt=4,
            sequence=10,
        ),
        _receipt(
            "evt-order",
            receipt_id="later-suppression",
            plan="plan-order",
            adapter="matrix",
            channel=None,
            status="suppressed",
            attempt=1,
            sequence=11,
        ),
    ]

    resolved = resolve_delivery_outcomes(receipts)

    assert len(resolved) == 1
    assert [receipt.receipt_id for receipt in resolved[0][1]] == [
        "old-high-attempt",
        "later-suppression",
    ]


def test_historical_delivery_grouping_is_event_scoped() -> None:
    receipts = [
        _receipt(
            "evt-a",
            receipt_id="shared-a",
            plan="shared-plan",
            adapter="matrix",
            channel="room",
            status="sent",
            attempt=1,
            sequence=1,
        ),
        _receipt(
            "evt-b",
            receipt_id="shared-b",
            plan="shared-plan",
            adapter="matrix",
            channel="room",
            status="sent",
            attempt=1,
            sequence=2,
        ),
    ]

    resolved = resolve_delivery_outcomes(receipts)

    assert len(resolved) == 2
    assert {key[0] for key, _history in resolved} == {"evt-a", "evt-b"}
    assert {history[0].receipt_id for _key, history in resolved} == {
        "shared-a",
        "shared-b",
    }


def test_retry_chain_latest_attempt_is_the_current_outcome(tmp_path: Path) -> None:
    db = tmp_path / "retry-chain.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [
            _receipt("evt-1", receipt_id="r1", status="failed", attempt=1),
            _receipt(
                "evt-1",
                receipt_id="r2",
                status="failed",
                attempt=2,
                parent_receipt_id="r1",
                at=_T0 + timedelta(minutes=2),
            ),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    assert len(report["unresolved"]) == 1
    assert report["unresolved"][0]["attempt_number"] == 2
    assert report["unresolved"][0]["receipt_id"] == "r2"


def test_queued_latest_attempt_is_in_flight_not_failed(tmp_path: Path) -> None:
    db = tmp_path / "in-flight.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [
            _receipt("evt-1", receipt_id="r1", status="failed", attempt=1),
            _receipt(
                "evt-1",
                receipt_id="r2",
                status="queued",
                attempt=2,
                parent_receipt_id="r1",
                error=None,
                failure_kind=None,
                at=_T0 + timedelta(minutes=1),
            ),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    assert report["unresolved"] == []


def test_dead_lettered_is_terminal_unresolved(tmp_path: Path) -> None:
    db = tmp_path / "dead.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [
            _receipt(
                "evt-1",
                receipt_id="r-dead",
                status="dead_lettered",
                failure_kind="adapter_transient",
            ),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    rows = report["unresolved"]
    assert len(rows) == 1
    assert rows[0]["status"] == "dead_lettered"
    assert rows[0]["disposition"] == "dead_lettered"


def test_executed_replay_resolves_original_failure(tmp_path: Path) -> None:
    """Replay receipt contract: executed (best_effort) replays append
    attempts to the SAME logical delivery — the lifecycle continues the
    attempt chain and the delivery_status authority takes the latest
    receipt with no source filter.  A successful executed replay
    therefore resolves the original live failure; a replayed failure is
    that delivery's current failed attempt; a replay of a DIFFERENT
    delivery never touches this one.  Dry-run replays append no receipts
    at all (replay delivery.py suppresses delivery in DRY_RUN), so they
    can fabricate neither success nor failure."""
    db = tmp_path / "replay-contract.db"
    _seed(
        db,
        [
            _event("evt-replayed-ok", at=_T0),
            _event("evt-replayed-fail", at=_T0 + timedelta(minutes=10)),
        ],
        [
            # Original live failure, later resolved by an executed replay
            # of the same plan/target/channel (attempt 2, source=replay).
            _receipt("evt-replayed-ok", receipt_id="r-live-fail", status="failed"),
            _receipt(
                "evt-replayed-ok",
                receipt_id="r-replay-ok",
                status="sent",
                attempt=2,
                source="replay",
                replay_run_id="run-A",
                error=None,
                failure_kind=None,
                at=_T0 + timedelta(minutes=1),
            ),
            # Original live failure still failing after a replay attempt.
            _receipt(
                "evt-replayed-fail",
                receipt_id="r-replay-fail",
                status="failed",
                attempt=2,
                source="replay",
                replay_run_id="run-B",
                at=_T0 + timedelta(minutes=11),
            ),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    by_event = {row["event_id"]: row for row in report["unresolved"]}
    assert set(by_event) == {"evt-replayed-fail"}
    assert by_event["evt-replayed-fail"]["receipt_id"] == "r-replay-fail"
    assert by_event["evt-replayed-fail"]["attempt_source"] == "replay:run-B"
    assert by_event["evt-replayed-fail"]["attempt_number"] == 2

    # The runbook agrees: resolved-by-replay failure is historical, the
    # still-failing one is current with its replay provenance.
    ok_runbook = _run_cli_json(
        "recover", "--storage-path", str(db), "--event", "evt-replayed-ok", "--json"
    )
    assert ok_runbook["failed_targets"] == []
    assert [h["receipt_id"] for h in ok_runbook["historical_failures"]] == [
        "r-live-fail"
    ]
    assert ok_runbook["historical_failures"][0]["superseded_by"] == {
        "receipt_id": "r-replay-ok",
        "status": "sent",
    }
    fail_runbook = _run_cli_json(
        "recover",
        "--storage-path",
        str(db),
        "--event",
        "evt-replayed-fail",
        "--json",
    )
    assert [ft["receipt_id"] for ft in fail_runbook["failed_targets"]] == [
        "r-replay-fail"
    ]
    assert fail_runbook["failed_targets"][0]["attempt_source"] == "replay:run-B"


def test_unrelated_replay_target_does_not_supersede(tmp_path: Path) -> None:
    """A successful executed replay of one target/channel leaves another
    target's failure on the same event unresolved."""
    db = tmp_path / "replay-unrelated.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [
            _receipt(
                "evt-1",
                receipt_id="r-fail",
                status="failed",
                channel="ch-stuck",
            ),
            _receipt(
                "evt-1",
                receipt_id="r-replay-other",
                status="sent",
                attempt=2,
                channel="ch-other",
                source="replay",
                replay_run_id="run-C",
                error=None,
                failure_kind=None,
                at=_T0 + timedelta(minutes=1),
            ),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    assert [row["receipt_id"] for row in report["unresolved"]] == ["r-fail"]


def test_empty_plan_ids_across_events_do_not_merge(tmp_path: Path) -> None:
    """Two events sharing an empty delivery_plan_id (and channelless
    targets) remain two distinct deliveries."""
    db = tmp_path / "empty-plans.db"
    _seed(
        db,
        [
            _event("evt-a", at=_T0),
            _event("evt-b", at=_T0 + timedelta(minutes=5)),
        ],
        [
            _receipt("evt-a", receipt_id="r-a", status="failed", plan=""),
            _receipt("evt-b", receipt_id="r-b", status="failed", plan=""),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    assert {row["event_id"] for row in report["unresolved"]} == {"evt-a", "evt-b"}


def test_null_and_empty_channel_do_not_split_lineage(tmp_path: Path) -> None:
    """NULL and empty-string channels are one lineage: a later sent
    receipt with '' supersedes the earlier NULL-channel failure."""
    db = tmp_path / "channel-normalization.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [
            _receipt("evt-1", receipt_id="r1", status="failed", channel=None),
            _receipt(
                "evt-1",
                receipt_id="r2",
                status="sent",
                channel="",
                attempt=2,
                error=None,
                failure_kind=None,
                at=_T0 + timedelta(minutes=1),
            ),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    assert report["unresolved"] == []


def test_scan_and_runbook_agree_on_current_failures(tmp_path: Path) -> None:
    """One event mixing a superseded failure with a current one: the scan
    reports exactly the current failure the runbook classifies."""
    db = tmp_path / "mixed.db"
    _seed(
        db,
        [_event("evt-mixed", at=_T0)],
        [
            # Superseded lineage: failed then sent.
            _receipt(
                "evt-mixed",
                receipt_id="r-old",
                status="failed",
                plan="plan-old",
                channel="ch-old",
            ),
            _receipt(
                "evt-mixed",
                receipt_id="r-old-ok",
                status="sent",
                plan="plan-old",
                channel="ch-old",
                attempt=2,
                error=None,
                failure_kind=None,
                at=_T0 + timedelta(minutes=1),
            ),
            # Current failure on a different lineage.
            _receipt(
                "evt-mixed",
                receipt_id="r-now",
                status="failed",
                plan="plan-now",
                channel="ch-now",
            ),
        ],
    )

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    assert [row["receipt_id"] for row in report["unresolved"]] == ["r-now"]

    runbook = _run_cli_json(
        "recover", "--storage-path", str(db), "--event", "evt-mixed", "--json"
    )
    assert [ft["receipt_id"] for ft in runbook["failed_targets"]] == ["r-now"]
    assert [h["receipt_id"] for h in runbook["historical_failures"]] == ["r-old"]


def test_outbox_enriches_disposition_without_inventing_success(tmp_path: Path) -> None:
    """A failed receipt with a retry_wait outbox row reports a scheduled
    retry; the outbox can only enrich, never mark the delivery resolved."""
    db = tmp_path / "outbox-enrich.db"

    async def _seed_with_outbox() -> None:
        storage = SQLiteStorage(str(db))
        try:
            await storage.initialize()
            await storage.append(_event("evt-1", at=_T0))
            # Outbox lifecycle contract: create as pending, claim it
            # (pending -> in_progress), then reach retry_wait via the
            # dedicated transition (only in_progress may enter retry_wait).
            # The transition commits the failed receipt's pointer, exactly
            # like production retry finalization.
            item = await storage.create_outbox_item(
                DeliveryOutboxItem(
                    outbox_id="ob-1",
                    event_id="evt-1",
                    route_id="route-1",
                    delivery_plan_id="plan-1",
                    target_adapter="meshtastic.radio",
                )
            )
            claimed = await storage.claim_due_outbox_items(
                (_T0 + timedelta(seconds=1)).isoformat(), "worker-proof"
            )
            assert [c.outbox_id for c in claimed] == [item.outbox_id]
            await storage.append_receipt(
                _receipt(
                    "evt-1",
                    receipt_id="r1",
                    status="failed",
                    outbox_id=item.outbox_id,
                )
            )
            await storage.mark_outbox_retry_wait(
                item.outbox_id,
                "2026-06-01T12:05:00+00:00",
                receipt_id="r1",
            )
        finally:
            await storage.close()

    asyncio.run(_seed_with_outbox())

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    row = report["unresolved"][0]
    assert row["outbox_status"] == "retry_wait"
    assert row["outbox_next_attempt_at"] == "2026-06-01T12:05:00+00:00"
    assert row["disposition"] == "retry_scheduled"
    # Enrichment is not acceptance: the row is still unresolved/failed.
    assert row["status"] == "failed"


def test_pointerless_failed_receipt_is_not_current(tmp_path: Path) -> None:
    """An outbox-backed failed receipt whose outbox row never committed its
    pointer stays historical under the receipt-authority model: append order
    alone cannot make it the delivery's current unresolved status."""
    db = tmp_path / "outbox-unpointed.db"

    async def _seed_with_outbox() -> None:
        storage = SQLiteStorage(str(db))
        try:
            await storage.initialize()
            await storage.append(_event("evt-1", at=_T0))
            item = await storage.create_outbox_item(
                DeliveryOutboxItem(
                    outbox_id="ob-1",
                    event_id="evt-1",
                    route_id="route-1",
                    delivery_plan_id="plan-1",
                    target_adapter="meshtastic.radio",
                )
            )
            await storage.append_receipt(
                _receipt(
                    "evt-1",
                    receipt_id="r1",
                    status="failed",
                    outbox_id=item.outbox_id,
                )
            )
        finally:
            await storage.close()

    asyncio.run(_seed_with_outbox())

    report = _run_cli_json("recover", "--storage-path", str(db), "--json")
    assert report["unresolved"] == []


def test_event_recovery_rejects_scan_only_options() -> None:
    """Single-event mode never silently ignores scan-only scope controls."""
    for option in (
        ("--since", "2026-09-18T00:00:00+00:00"),
        ("--cursor", encode_page_cursor(1)),
        ("--limit", "25"),
    ):
        _stdout, stderr, code = _run_cli_raw(
            "recover",
            "--storage-path",
            "unused.db",
            "--event",
            "evt-one",
            *option,
        )
        assert code == EXIT_CONFIG
        assert "--event cannot be combined with scan-only option" in stderr
        assert option[0] in stderr


# ---------------------------------------------------------------------------
# --since scope and pagination
# ---------------------------------------------------------------------------


def test_since_filters_on_canonical_event_time(tmp_path: Path) -> None:
    """--since bounds the canonical event timestamp (inclusive), distinct
    from receipt creation time."""
    db = tmp_path / "since.db"
    early, late = _T0, _T0 + timedelta(hours=2)
    # NOTE: the early event's *receipt* is created after the late event's,
    # so filtering on receipt time would invert the result.
    _seed(
        db,
        [_event("evt-early", at=early), _event("evt-late", at=late)],
        [
            _receipt(
                "evt-early",
                receipt_id="r-early",
                status="failed",
                at=late + timedelta(minutes=5),
            ),
            _receipt("evt-late", receipt_id="r-late", status="failed"),
        ],
    )

    since = late.isoformat()
    report = _run_cli_json(
        "recover", "--storage-path", str(db), "--since", since, "--json"
    )
    assert report["filters"]["since"] == since
    assert report["filters"]["since_field"] == "canonical_events.timestamp"
    assert report["filters"]["since_inclusive"] is True
    assert [row["event_id"] for row in report["unresolved"]] == ["evt-late"]

    # Naive and malformed timestamps are CLI errors, not tracebacks.
    for bad in ("2026-06-01T12:00:00", "not-a-timestamp", "2026-06-01"):
        _stdout, stderr, code = _run_cli_raw(
            "recover", "--storage-path", str(db), "--since", bad
        )
        assert code == 2, f"{bad!r} should be rejected with exit 2"
        assert "Traceback" not in stderr


def test_pagination_continuation_beyond_default(tmp_path: Path) -> None:
    db = tmp_path / "pages.db"
    events = [_event(f"evt-{i:03d}", at=_T0 + timedelta(minutes=i)) for i in range(60)]
    receipts = [
        _receipt(f"evt-{i:03d}", receipt_id=f"r-{i:03d}", status="failed")
        for i in range(60)
    ]
    _seed(db, events, receipts)

    page1 = _run_cli_json(
        "recover", "--storage-path", str(db), "--limit", "50", "--json"
    )
    assert len(page1["unresolved"]) == 50
    assert page1["page"]["has_more"] is True
    assert page1["page"]["next_cursor"]

    page2 = _run_cli_json(
        "recover",
        "--storage-path",
        str(db),
        "--limit",
        "50",
        "--cursor",
        page1["page"]["next_cursor"],
        "--json",
    )
    assert len(page2["unresolved"]) == 10
    assert page2["page"]["has_more"] is False
    assert page2["page"]["next_cursor"] is None

    seen = [r["receipt_id"] for r in page1["unresolved"] + page2["unresolved"]]
    assert seen == [f"r-{i:03d}" for i in range(60)]  # no skips, no duplicates


def test_sparse_failures_beyond_1000_discoverable(tmp_path: Path) -> None:
    """Failures past the old 1000-row default bound stay discoverable via
    bounded pages (the old scan never queried at all)."""
    db = tmp_path / "sparse.db"
    total = 1002
    events = [
        _event(f"evt-{i:04d}", at=_T0 + timedelta(seconds=i)) for i in range(total)
    ]
    receipts = [
        _receipt(f"evt-{i:04d}", receipt_id=f"r-{i:04d}", status="failed")
        for i in range(total)
    ]
    _seed(db, events, receipts)

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        argv = ["recover", "--storage-path", str(db), "--limit", "500", "--json"]
        if cursor:
            argv += ["--cursor", cursor]
        report = _run_cli_json(*argv)
        pages += 1
        collected.extend(row["receipt_id"] for row in report["unresolved"])
        if not report["page"]["has_more"]:
            break
        cursor = report["page"]["next_cursor"]
        assert pages <= 5, "pagination did not terminate"

    assert pages == 3
    assert collected == [f"r-{i:04d}" for i in range(total)]


def test_malformed_and_incompatible_cursors_rejected(tmp_path: Path) -> None:
    db = tmp_path / "cursor-errors.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [_receipt("evt-1", receipt_id="r1", status="failed")],
    )

    _stdout, stderr, code = _run_cli_raw(
        "recover", "--storage-path", str(db), "--cursor", "garbage!"
    )
    assert code == 2
    assert "Malformed page cursor" in stderr
    assert "Traceback" not in stderr

    # Forged token with an incompatible version, same encoding scheme.
    import base64

    forged = base64.urlsafe_b64encode(b'{"after_sequence": 0, "v": 99}').decode("ascii")
    _stdout, stderr, code = _run_cli_raw(
        "recover", "--storage-path", str(db), "--cursor", forged
    )
    assert code == 2
    assert "Incompatible page cursor" in stderr
    assert "Traceback" not in stderr

    # A genuine token from the same encoder remains usable.
    genuine = encode_page_cursor(0)
    _stdout2, _stderr2, code2 = _run_cli_raw(
        "recover", "--storage-path", str(db), "--cursor", genuine
    )
    assert code2 in (None, 0)


# ---------------------------------------------------------------------------
# Read-only guarantees and honest output
# ---------------------------------------------------------------------------


def test_missing_db_fails_without_creating_files(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.db"
    _stdout, stderr, code = _run_cli_raw(
        "recover", "--storage-path", str(missing), "--json"
    )
    assert code == EXIT_BUILD
    assert "Storage error" in stderr
    assert "Traceback" not in stderr
    assert not missing.exists()


def test_scan_does_not_mutate_storage(tmp_path: Path) -> None:
    """JSON + human + runbook + inspect leave the database bytes
    untouched.  SQLite may materialize ``-wal``/``-shm`` shared-memory
    sidecars even for read-only WAL connections — that is documented,
    expected behavior; no OTHER sidecar may appear and the evidence file
    itself must be byte-identical."""
    db = tmp_path / "immutable.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [_receipt("evt-1", receipt_id="r1", status="failed")],
    )
    before = _db_digest(db)

    _run_cli_json("recover", "--storage-path", str(db), "--json")
    _run_cli_raw("recover", "--storage-path", str(db))
    _run_cli_json("recover", "--storage-path", str(db), "--event", "evt-1", "--json")
    _run_cli_json("inspect", "event", "evt-1", "--storage-path", str(db), "--recovery")

    assert _db_digest(db) == before
    allowed = {
        db.with_name(db.name + "-wal"),
        db.with_name(db.name + "-shm"),
    }
    assert set(_sidecars(db)) <= allowed


def test_human_output_shows_valid_next_actions(tmp_path: Path) -> None:
    db = tmp_path / "human.db"
    _seed(
        db,
        [
            _event("evt-1", at=_T0),
            _event("evt-2", at=_T0 + timedelta(minutes=1)),
        ],
        [
            _receipt("evt-1", receipt_id="r1", status="failed"),
            _receipt("evt-2", receipt_id="r2", status="failed"),
        ],
    )
    stdout, _stderr, code = _run_cli_raw(
        "recover", "--storage-path", str(db), "--limit", "1"
    )
    assert code in (None, 0)
    assert "evt-1" in stdout
    assert "meshtastic.radio" in stdout
    assert "live" in stdout
    # Valid inspection step (reconstructable from the page).
    assert f"medre inspect event evt-1 --recovery --storage-path {db}" in stdout
    # Replay guidance references the real replay surface, never a
    # fabricated "replay --storage-path".
    assert "medre replay --mode dry_run --event <event_id> --config" in stdout
    assert "replay --storage-path" not in stdout
    # Continuation is explicit when more pages exist.
    assert "Continue with:" in stdout
    assert "--cursor" in stdout
    # Live-view disclosure.
    assert "live view" in stdout


def test_human_empty_scan_states_scope(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    _seed(db, [_event("evt-ok", at=_T0)], [])
    stdout, _stderr, code = _run_cli_raw("recover", "--storage-path", str(db))
    assert code in (None, 0)
    assert "No unresolved current delivery failures" in stdout


def test_json_page_shape_is_stable(tmp_path: Path) -> None:
    db = tmp_path / "shape.db"
    _seed(
        db,
        [_event("evt-1", at=_T0)],
        [_receipt("evt-1", receipt_id="r1", status="failed")],
    )
    stdout, _stderr, _code = _run_cli_raw(
        "recover", "--storage-path", str(db), "--json"
    )
    report = json.loads(stdout)
    assert list(report.keys()) == sorted(report.keys())
    page = report["page"]
    assert page["limit"] == 50
    assert page["count"] == 1
    assert page["has_more"] is False
    assert page["order"] == "receipt_sequence_asc"
    assert page["live_view"] is True
    row = report["unresolved"][0]
    assert list(row.keys()) == sorted(row.keys())
    for typed_field in ("receipt_sequence", "attempt_number"):
        assert isinstance(row[typed_field], int)
    assert isinstance(row["target_channel"], (str, type(None)))
