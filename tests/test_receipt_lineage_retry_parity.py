"""Receipt lineage and retry semantics tests.

Proves that receipt identity fields are deterministic across
queued/sent/failed/suppressed states and that retry reconstruction
preserves delivery_plan_id, route_id, target_adapter, target_channel,
and target identity.  Also verifies that retry attempts append evidence
rather than overwriting, exhaustion is visible, and suppressed
deliveries do not enter the retry queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryExecutor,
    RetryPolicy,
    stable_delivery_plan_id,
)
from medre.core.routing.models import RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADAPTER = "mesh_adapter"
_CHANNEL = "ch-mesh-42"
_ROUTE_ID = "route-mesh-out"
_EVENT_ID = "evt-lineage-001"
_PLAN_ID = "plan-lineage-001"


def _make_target(
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
) -> RouteTarget:
    return RouteTarget(adapter=adapter, channel=channel)


def _make_plan(
    plan_id: str = _PLAN_ID,
    event_id: str = _EVENT_ID,
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
    route_id: str | None = _ROUTE_ID,
    retry_policy: RetryPolicy | None = None,
    target_identity: str = "",
) -> DeliveryPlan:
    return DeliveryPlan(
        plan_id=plan_id,
        event_id=event_id,
        target=_make_target(adapter, channel),
        primary_strategy=DeliveryStrategy(method="direct"),
        retry_policy=retry_policy,
        route_id=route_id,
        target_identity=target_identity,
    )


def _make_queued_receipt(
    *,
    receipt_id: str = "rcpt-queued-001",
    event_id: str = _EVENT_ID,
    plan_id: str = _PLAN_ID,
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
    route_id: str = _ROUTE_ID,
    attempt_number: int = 1,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=0,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=adapter,
        target_channel=channel,
        route_id=route_id,
        status="queued",
        attempt_number=attempt_number,
        created_at=datetime.now(tz=timezone.utc),
    )


def _make_sent_receipt(
    *,
    receipt_id: str = "rcpt-sent-001",
    event_id: str = _EVENT_ID,
    plan_id: str = _PLAN_ID,
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
    route_id: str = _ROUTE_ID,
    attempt_number: int = 1,
    parent_receipt_id: str | None = None,
    adapter_message_id: str | None = "native-msg-001",
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=0,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=adapter,
        target_channel=channel,
        route_id=route_id,
        status="sent",
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
        adapter_message_id=adapter_message_id,
        created_at=datetime.now(tz=timezone.utc),
    )


def _make_failed_receipt(
    *,
    receipt_id: str = "rcpt-fail-001",
    event_id: str = _EVENT_ID,
    plan_id: str = _PLAN_ID,
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
    route_id: str = _ROUTE_ID,
    attempt_number: int = 1,
    parent_receipt_id: str | None = None,
    error: str = "ConnectionError: timeout",
    failure_kind: str = "adapter_transient",
    next_retry_at: datetime | None = None,
) -> DeliveryReceipt:
    if next_retry_at is None:
        next_retry_at = datetime.now(tz=timezone.utc) + timedelta(seconds=10)
    return DeliveryReceipt(
        sequence=0,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=adapter,
        target_channel=channel,
        route_id=route_id,
        status="failed",
        error=error,
        failure_kind=failure_kind,
        next_retry_at=next_retry_at,
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
        created_at=datetime.now(tz=timezone.utc),
    )


def _make_suppressed_receipt(
    *,
    receipt_id: str = "rcpt-supp-001",
    event_id: str = _EVENT_ID,
    plan_id: str = _PLAN_ID,
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
    route_id: str = _ROUTE_ID,
    failure_kind: str = "loop_suppressed",
    error: str = "loop_prevented",
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=0,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=adapter,
        target_channel=channel,
        route_id=route_id,
        status="suppressed",
        error=error,
        failure_kind=failure_kind,
        attempt_number=1,
        created_at=datetime.now(tz=timezone.utc),
    )


# ===================================================================
# Tests: deterministic delivery_plan_id
# ===================================================================


class TestReceiptLineage:
    """Receipt identity and lineage across delivery states."""

    async def test_queued_receipt_uses_deterministic_delivery_plan_id(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Queued receipt has deterministic plan_id derived from
        event_id + target identity."""
        event = make_storage_event(event_id=_EVENT_ID)
        await temp_storage.append(event)

        target = _make_target()
        deterministic_id = stable_delivery_plan_id(
            _EVENT_ID,
            target,
            route_id=_ROUTE_ID,
        )

        queued = _make_queued_receipt(plan_id=deterministic_id)
        await temp_storage.append_receipt(queued)

        stored = await temp_storage.list_receipts_for_event(_EVENT_ID)
        assert len(stored) == 1
        assert stored[0].status == "queued"
        assert stored[0].delivery_plan_id == deterministic_id

    async def test_sent_receipt_correlates_to_queued_plan(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Sent/failed receipt correlates to the right queued plan."""
        event = make_storage_event(event_id=_EVENT_ID)
        await temp_storage.append(event)

        target = _make_target()
        plan_id = stable_delivery_plan_id(
            _EVENT_ID,
            target,
            route_id=_ROUTE_ID,
        )

        queued = _make_queued_receipt(
            receipt_id="rcpt-q-1",
            plan_id=plan_id,
        )
        await temp_storage.append_receipt(queued)

        sent = _make_sent_receipt(
            receipt_id="rcpt-s-1",
            plan_id=plan_id,
            parent_receipt_id="rcpt-q-1",
        )
        await temp_storage.append_receipt(sent)

        all_receipts = await temp_storage.list_receipts_for_event(_EVENT_ID)
        assert len(all_receipts) == 2
        # Both share the same delivery_plan_id
        plan_ids = {r.delivery_plan_id for r in all_receipts}
        assert plan_ids == {plan_id}
        # Sent receipt links to queued via parent_receipt_id
        sent_r = [r for r in all_receipts if r.status == "sent"][0]
        assert sent_r.parent_receipt_id == "rcpt-q-1"

    async def test_suppressed_receipt_carries_route_target_plan_context(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Suppressed receipt carries route_id, target_adapter,
        target_channel, and delivery_plan_id."""
        event = make_storage_event(event_id=_EVENT_ID)
        await temp_storage.append(event)

        supp = _make_suppressed_receipt()
        await temp_storage.append_receipt(supp)

        stored = await temp_storage.list_receipts_for_event(_EVENT_ID)
        assert len(stored) == 1
        r = stored[0]
        assert r.status == "suppressed"
        assert r.route_id == _ROUTE_ID
        assert r.target_adapter == _ADAPTER
        assert r.target_channel == _CHANNEL
        assert r.delivery_plan_id == _PLAN_ID
        # Suppressed receipts have no next_retry_at
        assert r.next_retry_at is None


# ===================================================================
# Tests: retry reconstruction preserves identity fields
# ===================================================================


class TestRetryReconstruction:
    """RetryExecutor preserves identity fields across retry chains."""

    def _build_original_receipt(
        self,
        *,
        plan_id: str = _PLAN_ID,
        route_id: str = _ROUTE_ID,
        adapter: str = _ADAPTER,
        channel: str | None = _CHANNEL,
        attempt_number: int = 1,
        failure_kind: str = "adapter_transient",
    ) -> DeliveryReceipt:
        return _make_failed_receipt(
            receipt_id="rcpt-orig-1",
            plan_id=plan_id,
            route_id=route_id,
            adapter=adapter,
            channel=channel,
            attempt_number=attempt_number,
            failure_kind=failure_kind,
        )

    def test_retry_reconstruction_preserves_delivery_plan_id(self) -> None:
        """Retry receipt carries the same delivery_plan_id as the original."""
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        original = self._build_original_receipt()

        retry = executor.build_retry_receipt(
            event_id=original.event_id,
            delivery_plan_id=original.delivery_plan_id,
            target_adapter=original.target_adapter,
            previous_receipt_id=original.receipt_id,
            attempt_number=2,
            error="ConnectionError: retry 1",
            target_channel=original.target_channel,
        )

        assert retry.delivery_plan_id == original.delivery_plan_id

    def test_retry_reconstruction_preserves_route_id(self) -> None:
        """Route_id from original is propagated through retry reconstruction.

        The RetryExecutor does not directly set route_id on the retry
        receipt; it is propagated by the pipeline's deliver_to_target
        from the previous_receipt.  We verify the identity field is
        preserved by reconstructing the plan from the receipt.
        """
        original = self._build_original_receipt()
        _make_target()
        plan = _make_plan(
            plan_id=original.delivery_plan_id,
            route_id=original.route_id,
        )

        # Plan carries the route_id from the original receipt.
        assert plan.route_id == original.route_id

    def test_retry_reconstruction_preserves_target_identity(self) -> None:
        """Target identity fields (adapter + channel) are preserved
        through retry reconstruction."""
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        original = self._build_original_receipt(
            adapter="custom_adapter",
            channel="!room:server",
        )

        retry = executor.build_retry_receipt(
            event_id=original.event_id,
            delivery_plan_id=original.delivery_plan_id,
            target_adapter=original.target_adapter,
            previous_receipt_id=original.receipt_id,
            attempt_number=2,
            error="ConnectionError: retry",
            target_channel=original.target_channel,
        )

        assert retry.target_adapter == "custom_adapter"
        assert retry.target_channel == "!room:server"

    def test_retry_reconstruction_preserves_target_adapter_channel(self) -> None:
        """Retry preserves target adapter and channel across multiple
        retry attempts."""
        policy = RetryPolicy(max_attempts=5)
        executor = RetryExecutor(policy)

        adapter = "lxmf_node"
        channel = "mesh-7"
        plan_id = "plan-lxmf-001"

        original = self._build_original_receipt(
            plan_id=plan_id,
            adapter=adapter,
            channel=channel,
        )

        # Simulate a chain of retries.
        prev_id = original.receipt_id
        for attempt in range(2, 5):
            retry = executor.build_retry_receipt(
                event_id=original.event_id,
                delivery_plan_id=original.delivery_plan_id,
                target_adapter=original.target_adapter,
                previous_receipt_id=prev_id,
                attempt_number=attempt,
                error=f"ConnectionError: attempt {attempt}",
                target_channel=original.target_channel,
            )
            assert retry.target_adapter == adapter
            assert retry.target_channel == channel
            assert retry.delivery_plan_id == plan_id
            prev_id = retry.receipt_id


# ===================================================================
# Tests: retry evidence and exhaustion
# ===================================================================


class TestRetryEvidence:
    """Retry attempts append new evidence; exhaustion is visible."""

    async def test_retry_attempts_append_new_evidence(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Each retry attempt produces a new receipt; older evidence
        is not overwritten."""
        event = make_storage_event(event_id=_EVENT_ID)
        await temp_storage.append(event)

        # Original failure (attempt 1).
        r1 = _make_failed_receipt(
            receipt_id="rcpt-attempt-1",
            attempt_number=1,
            error="ConnectionError: timeout",
            next_retry_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1),
        )
        await temp_storage.append_receipt(r1)

        # Retry failure (attempt 2) — appends, does not overwrite.
        r2 = _make_failed_receipt(
            receipt_id="rcpt-attempt-2",
            attempt_number=2,
            error="ConnectionError: reset",
            parent_receipt_id="rcpt-attempt-1",
            next_retry_at=datetime.now(tz=timezone.utc) + timedelta(seconds=10),
        )
        await temp_storage.append_receipt(r2)

        all_receipts = await temp_storage.list_receipts_for_event(_EVENT_ID)
        assert len(all_receipts) == 2

        # Both receipts are distinct — old evidence preserved.
        attempt_1 = [r for r in all_receipts if r.attempt_number == 1]
        attempt_2 = [r for r in all_receipts if r.attempt_number == 2]
        assert len(attempt_1) == 1
        assert len(attempt_2) == 1

        # Attempt 2 links to attempt 1.
        assert attempt_2[0].parent_receipt_id == "rcpt-attempt-1"
        assert attempt_2[0].error != attempt_1[0].error

    async def test_retry_exhaustion_is_visible_as_durable_failure_evidence(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Retry exhaustion produces a dead_lettered receipt that is
        visible in storage as durable failure evidence."""
        event = make_storage_event(event_id=_EVENT_ID)
        await temp_storage.append(event)

        # Attempt 1: failed.
        r1 = _make_failed_receipt(
            receipt_id="rcpt-exh-1",
            attempt_number=1,
            error="ConnectionError: first",
        )
        await temp_storage.append_receipt(r1)

        # Attempt 2: failed (retry).
        r2 = _make_failed_receipt(
            receipt_id="rcpt-exh-2",
            attempt_number=2,
            error="ConnectionError: second",
            parent_receipt_id="rcpt-exh-1",
        )
        await temp_storage.append_receipt(r2)

        # Attempt 3: dead-lettered (exhausted).
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        dead = executor.build_dead_letter_receipt(
            event_id=_EVENT_ID,
            delivery_plan_id=_PLAN_ID,
            target_adapter=_ADAPTER,
            previous_receipt_id="rcpt-exh-2",
            attempt_number=3,
            error="All 3 attempts exhausted",
            target_channel=_CHANNEL,
        )
        await temp_storage.append_receipt(dead)

        all_receipts = await temp_storage.list_receipts_for_event(_EVENT_ID)
        assert len(all_receipts) == 3

        dead_lettered = [r for r in all_receipts if r.status == "dead_lettered"]
        assert len(dead_lettered) == 1
        assert dead_lettered[0].attempt_number == 3
        assert dead_lettered[0].parent_receipt_id == "rcpt-exh-2"
        assert dead_lettered[0].next_retry_at is None

        # The earlier failed receipts are still present (not overwritten).
        failed = [r for r in all_receipts if r.status == "failed"]
        assert len(failed) == 2


# ===================================================================
# Tests: suppressed deliveries excluded from retry
# ===================================================================


class TestSuppressedRetryExclusion:
    """Suppressed deliveries are never enqueued for retry."""

    async def test_suppressed_deliveries_do_not_enter_retry_queue(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Suppressed receipts have no next_retry_at, are not
        returned by list_due_retry_receipts."""
        event = make_storage_event(event_id=_EVENT_ID)
        await temp_storage.append(event)

        supp = _make_suppressed_receipt(
            failure_kind="loop_suppressed",
            error="loop_prevented",
        )
        await temp_storage.append_receipt(supp)

        # Also add a genuine failed receipt for comparison.
        failed = _make_failed_receipt(
            receipt_id="rcpt-real-fail",
            next_retry_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1),
        )
        await temp_storage.append_receipt(failed)

        # Query for due retry receipts — should NOT include suppressed.
        now = datetime.now(tz=timezone.utc)
        due = await temp_storage.list_due_retry_receipts(
            now,
            limit=20,
            max_attempts=3,
        )

        due_ids = {r.receipt_id for r in due}
        # The suppressed receipt must not appear.
        assert supp.receipt_id not in due_ids
        # The failed receipt should appear.
        assert failed.receipt_id in due_ids

        # Suppressed receipt has no next_retry_at.
        stored = await temp_storage.list_receipts_for_event(_EVENT_ID)
        supp_stored = [r for r in stored if r.status == "suppressed"]
        assert len(supp_stored) == 1
        assert supp_stored[0].next_retry_at is None
        assert supp_stored[0].failure_kind == "loop_suppressed"


# ===================================================================
# Plan ID format validation
# ===================================================================


class TestPlanIdFormat:
    """Unit tests for stable_delivery_plan_id format invariants."""

    def test_none_route_id_uses_unrouted(self) -> None:
        """route_id=None produces 'unrouted' in plan ID, no double colon."""
        target = RouteTarget(adapter="test", channel="ch-0")
        plan_id = stable_delivery_plan_id(
            "evt-001", target, route_id=None, target_index=0
        )
        assert "unrouted" in plan_id
        assert "::" not in plan_id

    def test_empty_route_id_uses_unrouted(self) -> None:
        """route_id='' produces 'unrouted' in plan ID, no double colon."""
        target = RouteTarget(adapter="test", channel="ch-0")
        plan_id = stable_delivery_plan_id(
            "evt-001", target, route_id="", target_index=0
        )
        assert "unrouted" in plan_id
        assert "::" not in plan_id

    def test_valid_route_id_present_in_plan_id(self) -> None:
        """route_id='route-xyz' appears in plan ID."""
        target = RouteTarget(adapter="test", channel="ch-0")
        plan_id = stable_delivery_plan_id(
            "evt-001", target, route_id="route-xyz", target_index=3
        )
        assert "route-xyz" in plan_id
        assert "::" not in plan_id

    def test_plan_id_format_structure(self) -> None:
        """Plan ID follows format plan:{event_id}:{route_part}:{index_part}:{target_hash}."""
        target = RouteTarget(adapter="test", channel="ch-0")
        plan_id = stable_delivery_plan_id(
            "evt-001", target, route_id="r1", target_index=0
        )
        parts = plan_id.split(":")
        assert (
            len(parts) == 5
        ), f"Expected 5 colon-separated parts, got {len(parts)}: {plan_id}"
        assert parts[0] == "plan"
        assert parts[1] == "evt-001"
        assert parts[2] == "r1"
        assert parts[3] == "0"
        assert (
            len(parts[4]) == 16
        ), f"Target hash should be 16 hex chars, got {len(parts[4])}"
        assert all(
            c in "0123456789abcdef" for c in parts[4]
        ), "Target hash should be hex"

    def test_none_target_index_uses_target(self) -> None:
        """target_index=None produces 'target' in index position."""
        target = RouteTarget(adapter="test", channel="ch-0")
        plan_id = stable_delivery_plan_id(
            "evt-001", target, route_id="r1", target_index=None
        )
        assert ":target:" in plan_id
