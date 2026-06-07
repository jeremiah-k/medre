"""Integration tests proving graceful shutdown preserves non-terminal outbox rows.

Design policy: non-terminal outbox rows are durable/resumable across
graceful shutdown.  This module turns that policy into integration-level
regression tests that operate at the storage layer (SQLite close/reopen)
plus shutdown-evidence model integration.

Tests prove:
- ``pending`` remains pending across simulated shutdown.
- ``retry_wait`` remains retry_wait across simulated shutdown.
- ``in_progress`` remains in_progress across simulated shutdown (lease intact).
- ``queued`` remains queued across simulated shutdown.
- ``sent`` and ``dead_lettered`` remain terminal across simulated shutdown.
- No graceful shutdown creates ``cancelled``/``abandoned`` rows for non-terminal
  statuses.
- ``build_shutdown_evidence`` fed from live storage outbox counts produces
  ``shutdown_status="shutdown_pending"``, ``resume_expected=True``,
  ``outbox_shutdown_policy="resumable"`` when non-terminal work exists.
- ``classify_outbox_shutdown_policy`` returns ``resume_on_restart=True`` for
  all non-terminal rows found in storage after reopen.

All tests are deterministic and fake-only; no network/hardware/live claims.
``retry_stopped`` event coverage is provided by
``tests/test_retry_event_buffer_wiring.py`` and is NOT duplicated here.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from medre.core.engine.pipeline.delivery_state import (
    NON_TERMINAL_OUTBOX_STATUSES,
    TERMINAL_OUTBOX_STATUSES,
)
from medre.core.evidence.shutdown import (
    build_shutdown_evidence,
    classify_outbox_shutdown_policy,
)
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Canonical status vocab constants from delivery_state.py.
# Deterministic param order via tuple(sorted(...)).
_NON_TERMINAL_STATUSES: tuple[str, ...] = tuple(sorted(NON_TERMINAL_OUTBOX_STATUSES))
_TERMINAL_STATUSES: tuple[str, ...] = tuple(sorted(TERMINAL_OUTBOX_STATUSES))


def _make_outbox_item(
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "adapter_a",
    target_channel: str | None = "ch-0",
    attempt_number: int = 1,
    status: str = "pending",
    next_attempt_at: str | None = None,
) -> DeliveryOutboxItem:
    """Build a minimal DeliveryOutboxItem for tests."""
    return DeliveryOutboxItem(
        outbox_id=f"obox-{uuid.uuid4()}",
        event_id=f"evt-{uuid.uuid4()}",
        route_id="route-1",
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        attempt_number=attempt_number,
        status=status,
        next_attempt_at=next_attempt_at,
    )


async def _seed_item_in_status(
    storage: SQLiteStorage,
    status: str,
    plan_id: str,
) -> DeliveryOutboxItem:
    """Create an outbox item directly in the given status without using claim.

    This avoids ``claim_due_outbox_items`` which would grab ALL due items,
    making it unsuitable for multi-item tests.  Instead, we use direct
    ``create_outbox_item`` with explicit status/worker fields to seed items
    in their target status.

    For ``in_progress``, ``queued``, ``retry_wait``, ``sent``, ``dead_lettered``
    the item is created as ``in_progress`` with worker fields, then transitioned
    to the target status via mark_* methods.
    """
    if status == "pending":
        item = _make_outbox_item(
            delivery_plan_id=plan_id,
            target_channel=f"ch-{plan_id}",
        )
        created = await storage.create_outbox_item(item)
        assert created.status == "pending"
        return created

    # For all other statuses, create directly as in_progress with worker fields,
    # then transition to target.  This avoids the claim-DUE-grabs-everything
    # issue in multi-item tests.
    item = DeliveryOutboxItem(
        outbox_id=f"obox-{uuid.uuid4()}",
        event_id=f"evt-{uuid.uuid4()}",
        route_id="route-1",
        delivery_plan_id=plan_id,
        target_adapter="adapter_a",
        target_channel=f"ch-{plan_id}",
        attempt_number=1,
        status="in_progress",
        worker_id="worker-seed",
        locked_at="2026-01-01T00:00:00",
        lease_until="2026-01-01T00:05:00",
    )
    created = await storage.create_outbox_item(item)
    oid = created.outbox_id

    if status == "in_progress":
        assert created.status == "in_progress"
        return created

    if status == "queued":
        await storage.mark_outbox_queued(oid)
        fetched = await storage.get_outbox_item(oid)
        assert fetched is not None
        assert fetched.status == "queued"
        return fetched

    if status == "retry_wait":
        await storage.mark_outbox_retry_wait(
            oid,
            next_attempt_at="2026-01-01T01:00:00",
            failure_kind="adapter_transient",
            error_summary="simulated transient",
        )
        fetched = await storage.get_outbox_item(oid)
        assert fetched is not None
        assert fetched.status == "retry_wait"
        return fetched

    if status == "sent":
        await storage.mark_outbox_sent(oid, receipt_id="rcpt-sent")
        fetched = await storage.get_outbox_item(oid)
        assert fetched is not None
        assert fetched.status == "sent"
        return fetched

    if status == "dead_lettered":
        await storage.mark_outbox_dead_lettered(oid, failure_kind="adapter_permanent")
        fetched = await storage.get_outbox_item(oid)
        assert fetched is not None
        assert fetched.status == "dead_lettered"
        return fetched

    if status == "cancelled":
        # cancelled items are created via in_progress → mark_outbox_cancelled
        await storage.mark_outbox_cancelled(oid, error_summary="shutdown_test")
        fetched = await storage.get_outbox_item(oid)
        assert fetched is not None
        assert fetched.status == "cancelled"
        return fetched

    if status == "abandoned":
        # abandoned items are created via in_progress → mark_outbox_abandoned
        await storage.mark_outbox_abandoned(oid, error_summary="shutdown_test")
        fetched = await storage.get_outbox_item(oid)
        assert fetched is not None
        assert fetched.status == "abandoned"
        return fetched

    raise ValueError(f"Unsupported status for helper: {status!r}")


# ---------------------------------------------------------------------------
# Test Group 1: Individual status preservation across close/reopen
# ---------------------------------------------------------------------------


class TestPendingPreservedAcrossShutdown:
    """pending outbox rows survive simulated graceful shutdown (close/reopen)."""

    async def test_pending_status_preserved(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        # Phase 1: create pending item.
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            item = _make_outbox_item(
                delivery_plan_id="plan-pend-surv", target_channel="ch-p"
            )
            await storage.create_outbox_item(item)
            oid = item.outbox_id
        finally:
            await storage.close()

        # Phase 2: reopen and verify.
        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            fetched = await storage2.get_outbox_item(oid)
            assert fetched is not None
            assert fetched.status == "pending"
            assert fetched.delivery_plan_id == "plan-pend-surv"
        finally:
            await storage2.close()


class TestRetryWaitPreservedAcrossShutdown:
    """retry_wait outbox rows survive simulated graceful shutdown."""

    async def test_retry_wait_status_preserved(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            created = await _seed_item_in_status(storage, "retry_wait", "plan-rw-surv")
            oid = created.outbox_id
            assert created.status == "retry_wait"
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            fetched = await storage2.get_outbox_item(oid)
            assert fetched is not None
            assert fetched.status == "retry_wait"
            assert fetched.next_attempt_at is not None
        finally:
            await storage2.close()


class TestInProgressPreservedAcrossShutdown:
    """in_progress outbox rows (with active lease) survive simulated shutdown."""

    async def test_in_progress_status_preserved_with_lease(
        self, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            created = await _seed_item_in_status(storage, "in_progress", "plan-ip-surv")
            oid = created.outbox_id
            assert created.status == "in_progress"
            assert created.worker_id == "worker-seed"
            assert created.lease_until is not None
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            fetched = await storage2.get_outbox_item(oid)
            assert fetched is not None
            assert fetched.status == "in_progress"
            # Lease fields survive — the row is resumable on restart.
            assert fetched.worker_id == "worker-seed"
            assert fetched.lease_until is not None
            assert fetched.locked_at is not None
        finally:
            await storage2.close()


class TestQueuedPreservedAcrossShutdown:
    """queued outbox rows survive simulated graceful shutdown."""

    async def test_queued_status_preserved(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            created = await _seed_item_in_status(storage, "queued", "plan-q-surv")
            oid = created.outbox_id
            assert created.status == "queued"
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            fetched = await storage2.get_outbox_item(oid)
            assert fetched is not None
            assert fetched.status == "queued"
        finally:
            await storage2.close()


# ---------------------------------------------------------------------------
# Test Group 2: Terminal statuses preserved; no spurious mutation
# ---------------------------------------------------------------------------


class TestTerminalStatusesPreservedAcrossShutdown:
    """Terminal outbox rows are untouched by simulated graceful shutdown."""

    @pytest.mark.parametrize("terminal_status", _TERMINAL_STATUSES)
    async def test_terminal_status_preserved(
        self, terminal_status: str, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            created = await _seed_item_in_status(
                storage, terminal_status, f"plan-term-{terminal_status}"
            )
            oid = created.outbox_id
            assert created.status == terminal_status
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            fetched = await storage2.get_outbox_item(oid)
            assert fetched is not None
            assert fetched.status == terminal_status
        finally:
            await storage2.close()


class TestMixedStatusesPreservedAcrossShutdown:
    """All outbox rows in mixed statuses survive simulated shutdown."""

    async def test_all_statuses_preserved(self, tmp_path: Path) -> None:
        """Create items in every non-terminal + terminal status, close/reopen,
        verify each retains its original status."""
        db_path = str(tmp_path / "test.db")
        all_statuses = _NON_TERMINAL_STATUSES + _TERMINAL_STATUSES
        oid_by_status: dict[str, str] = {}

        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            for status in all_statuses:
                created = await _seed_item_in_status(
                    storage, status, f"plan-mixed-{status}"
                )
                oid_by_status[status] = created.outbox_id
                assert created.status == status
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            for status, oid in oid_by_status.items():
                fetched = await storage2.get_outbox_item(oid)
                assert (
                    fetched is not None
                ), f"Item for status={status!r} not found after reopen"
                assert (
                    fetched.status == status
                ), f"Expected status={status!r} but got {fetched.status!r}"
        finally:
            await storage2.close()


class TestNoSpuriousCancelledOrAbandoned:
    """Simulated shutdown does NOT create cancelled/abandoned rows."""

    async def test_close_reopen_creates_no_cancelled_or_abandoned(
        self,
        tmp_path: Path,
    ) -> None:
        """Create only non-terminal items, close/reopen, verify no
        cancelled or abandoned rows exist."""
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            for status in _NON_TERMINAL_STATUSES:
                await _seed_item_in_status(storage, status, f"plan-no-cancel-{status}")
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            counts = await storage2.count_outbox_by_status()
            assert (
                counts.get("cancelled", 0) == 0
            ), f"Unexpected cancelled rows: {counts.get('cancelled')}"
            assert (
                counts.get("abandoned", 0) == 0
            ), f"Unexpected abandoned rows: {counts.get('abandoned')}"
            # All non-terminal counts should be exactly 1 each.
            for status in _NON_TERMINAL_STATUSES:
                assert (
                    counts.get(status, 0) == 1
                ), f"Expected 1 {status} row, got {counts.get(status, 0)}"
        finally:
            await storage2.close()

    async def test_counts_identical_before_and_after_shutdown(
        self, tmp_path: Path
    ) -> None:
        """outbox_by_status counts are byte-identical before and after close/reopen."""
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            # Create 2 pending + 1 retry_wait + 1 queued + 1 in_progress + 2 sent.
            for i in range(2):
                await _seed_item_in_status(storage, "pending", f"plan-cnt-p-{i}")
            await _seed_item_in_status(storage, "retry_wait", "plan-cnt-rw")
            await _seed_item_in_status(storage, "queued", "plan-cnt-q")
            await _seed_item_in_status(storage, "in_progress", "plan-cnt-ip")
            for i in range(2):
                await _seed_item_in_status(storage, "sent", f"plan-cnt-s-{i}")

            counts_before = await storage.count_outbox_by_status()
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            counts_after = await storage2.count_outbox_by_status()
        finally:
            await storage2.close()

        assert (
            counts_before == counts_after
        ), f"Counts changed across shutdown: before={counts_before}, after={counts_after}"


# ---------------------------------------------------------------------------
# Test Group 3: Shutdown evidence from live storage data
# ---------------------------------------------------------------------------


class TestShutdownEvidenceFromLiveStorageCounts:
    """build_shutdown_evidence fed from real storage outbox counts produces
    correct shutdown_pending/resumable evidence."""

    async def test_evidence_shutdown_pending_when_nonterminal_work_exists(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-terminal items in storage → shutdown_pending, resume_expected=True."""
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            await _seed_item_in_status(storage, "pending", "plan-ev-p")
            await _seed_item_in_status(storage, "retry_wait", "plan-ev-rw")
            await _seed_item_in_status(storage, "sent", "plan-ev-s")

            counts = await storage.count_outbox_by_status()
        finally:
            await storage.close()

        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts=counts,
        )
        assert evidence.shutdown_status == "shutdown_pending"
        assert evidence.resume_expected is True
        assert evidence.outbox_shutdown_policy == "resumable"
        assert evidence.pending_outbox_counts is not None
        assert evidence.pending_outbox_counts.get("pending") == 1
        assert evidence.pending_outbox_counts.get("retry_wait") == 1
        assert evidence.pending_retry_work_total == 2

    async def test_evidence_graceful_stop_when_all_terminal(
        self, tmp_path: Path
    ) -> None:
        """Only terminal items in storage → graceful_stop, resume_expected=False."""
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            await _seed_item_in_status(storage, "sent", "plan-ev-ts1")
            await _seed_item_in_status(storage, "dead_lettered", "plan-ev-dl1")

            counts = await storage.count_outbox_by_status()
        finally:
            await storage.close()

        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts=counts,
        )
        assert evidence.shutdown_status == "graceful_stop"
        assert evidence.resume_expected is False
        assert evidence.outbox_shutdown_policy == "resumable"
        assert evidence.pending_retry_work_total == 0

    async def test_evidence_resume_expected_true_for_each_nonterminal(
        self,
        tmp_path: Path,
    ) -> None:
        """Each non-terminal status alone triggers resume_expected=True."""
        for status in _NON_TERMINAL_STATUSES:
            status_db_path = tmp_path / f"test-{status}.db"
            storage = SQLiteStorage(db_path=str(status_db_path))
            try:
                await storage.initialize()
                await _seed_item_in_status(storage, status, f"plan-ev-resume-{status}")
                counts = await storage.count_outbox_by_status()
            finally:
                await storage.close()

            evidence = build_shutdown_evidence(
                runtime_state="stopped",
                outbox_counts=counts,
            )
            assert (
                evidence.shutdown_status == "shutdown_pending"
            ), f"status={status!r}: expected shutdown_pending"
            assert (
                evidence.resume_expected is True
            ), f"status={status!r}: expected resume_expected=True"
            assert evidence.outbox_shutdown_policy == "resumable"

    async def test_evidence_shutdown_pending_with_in_progress(
        self,
        tmp_path: Path,
    ) -> None:
        """in_progress rows count as pending work for shutdown evidence."""
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            await _seed_item_in_status(storage, "in_progress", "plan-ev-ip")
            counts = await storage.count_outbox_by_status()
        finally:
            await storage.close()

        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts=counts,
        )
        assert evidence.shutdown_status == "shutdown_pending"
        assert evidence.resume_expected is True
        assert evidence.pending_outbox_counts is not None
        assert evidence.pending_outbox_counts.get("in_progress") == 1

    async def test_evidence_to_dict_json_safe_from_storage(
        self,
        tmp_path: Path,
    ) -> None:
        """Evidence built from storage counts is JSON-safe."""
        import json

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            await _seed_item_in_status(storage, "pending", "plan-ev-json")
            await _seed_item_in_status(storage, "sent", "plan-ev-json-s")
            counts = await storage.count_outbox_by_status()
        finally:
            await storage.close()

        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts=counts,
        )
        data = evidence.to_dict()
        result = json.dumps(data, sort_keys=True)
        parsed = json.loads(result)
        assert parsed["shutdown_status"] == "shutdown_pending"
        assert parsed["resume_expected"] is True
        assert parsed["outbox_shutdown_policy"] == "resumable"


# ---------------------------------------------------------------------------
# Test Group 4: classify_outbox_shutdown_policy integration from storage items
# ---------------------------------------------------------------------------


class TestClassifyOutboxPolicyFromStorageItems:
    """classify_outbox_shutdown_policy returns correct classification for
    rows that actually exist in storage after simulated shutdown."""

    @pytest.mark.parametrize("status", _NON_TERMINAL_STATUSES)
    async def test_nonterminal_classified_resumable(
        self, status: str, tmp_path: Path
    ) -> None:
        """Each non-terminal status in storage is classified as resumable."""
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            created = await _seed_item_in_status(storage, status, f"plan-cls-{status}")
            assert created.status == status
        finally:
            await storage.close()

        # After reopen, verify status and classify.
        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            fetched = await storage2.get_outbox_item(created.outbox_id)
            assert fetched is not None
            assert fetched.status == status

            classification = classify_outbox_shutdown_policy(fetched.status)
            assert classification.resume_on_restart is True
            assert classification.classification.startswith("resumable_")
            assert classification.mutate_outbox is False
            assert classification.append_receipt is False
        finally:
            await storage2.close()

    @pytest.mark.parametrize("status", _TERMINAL_STATUSES)
    async def test_terminal_classified_no_resume(
        self, status: str, tmp_path: Path
    ) -> None:
        """Each terminal status in storage is classified as non-resumable."""
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            created = await _seed_item_in_status(
                storage, status, f"plan-cls-term-{status}"
            )
            assert created.status == status
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            fetched = await storage2.get_outbox_item(created.outbox_id)
            assert fetched is not None
            assert fetched.status == status

            classification = classify_outbox_shutdown_policy(fetched.status)
            assert classification.resume_on_restart is False
            assert classification.classification.startswith("terminal_")
            assert classification.mutate_outbox is False
            assert classification.append_receipt is False
        finally:
            await storage2.close()

    async def test_all_nonterminal_items_reclaimable_after_reopen(
        self,
        tmp_path: Path,
    ) -> None:
        """After simulated shutdown, pending items are claimable by a new
        worker — proving they are genuine resumable work."""
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            # Create a pending item (claimable) and a queued item (not
            # directly claimable).  Also create a retry_wait item — BUT we
            # must NOT call claim, because claim grabs ALL due items.
            # So we seed them directly.
            await _seed_item_in_status(storage, "pending", "plan-reclaim-p")

            # For retry_wait: create via in_progress → retry_wait.
            # _seed_item_in_status already does this without claim.
            await _seed_item_in_status(storage, "retry_wait", "plan-reclaim-rw")

            # queued is NOT directly claimable.
            await _seed_item_in_status(storage, "queued", "plan-reclaim-q")
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            # Claim due items — should get pending and retry_wait items.
            claimed = await storage2.claim_due_outbox_items(
                now="2026-01-01T00:05:00",
                worker_id="worker-restart",
                lease_seconds=60,
                limit=10,
            )
            claimed_plans = {c.delivery_plan_id for c in claimed}
            assert "plan-reclaim-p" in claimed_plans
            # retry_wait items ARE claimable when their next_attempt_at has
            # passed.  The _seed helper set next_attempt_at via
            # mark_outbox_retry_wait to "2026-01-01T01:00:00", but claim
            # uses the `now` parameter.  Since now="2026-01-01T00:05:00"
            # is BEFORE next_attempt_at="2026-01-01T01:00:00", the
            # retry_wait item is NOT yet due for claim.  This is correct
            # behavior — the item is legitimately waiting for its scheduled
            # retry time.
            #
            # Verify the retry_wait item is still present and still
            # retry_wait (preserved across restart).
            all_items = await storage2.list_outbox_items()
            rw_item = [i for i in all_items if i.delivery_plan_id == "plan-reclaim-rw"]
            assert len(rw_item) == 1
            assert rw_item[0].status == "retry_wait"

            # queued is NOT directly claimable.
            assert "plan-reclaim-q" not in claimed_plans

            # All claimed items should be in_progress now.
            for c in claimed:
                assert c.status == "in_progress"
                assert c.worker_id == "worker-restart"
        finally:
            await storage2.close()

    async def test_retry_wait_claimable_when_due_after_reopen(
        self, tmp_path: Path
    ) -> None:
        """After simulated shutdown, a retry_wait item whose next_attempt_at
        has passed IS claimable by a new worker."""
        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path=db_path)
        try:
            await storage.initialize()
            await _seed_item_in_status(storage, "retry_wait", "plan-rw-due")
        finally:
            await storage.close()

        storage2 = SQLiteStorage(db_path=db_path)
        try:
            await storage2.initialize()
            # Claim with now PAST the next_attempt_at.
            claimed = await storage2.claim_due_outbox_items(
                now="2026-01-01T02:00:00",
                worker_id="worker-restart-rw",
                lease_seconds=60,
                limit=10,
            )
            claimed_plans = {c.delivery_plan_id for c in claimed}
            assert "plan-rw-due" in claimed_plans
            assert claimed[0].status == "in_progress"
            assert claimed[0].worker_id == "worker-restart-rw"
        finally:
            await storage2.close()
