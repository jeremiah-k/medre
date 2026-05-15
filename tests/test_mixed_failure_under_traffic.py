"""Long-running fake bridge test: failure isolation under sustained traffic.

Processes a sequence of 10 messages with mixed success/failure outcomes
to prove that one failure does **not** poison later messages.  Uses the
direct ``PipelineRunner.handle_ingress`` path with fake adapters and
deterministic failure injection.

Topology
--------
Route ``traffic-route``:
    source ``"src"`` → targets [``"stable"``, ``"flaky"``, ``"ghost"``]

* ``stable`` — :class:`FakePresentationAdapter` (always succeeds).
* ``flaky`` — :class:`FaultyPresentationAdapter` configured with
  ``failure_mode="fail_n_then_succeed"`` and ``fail_count=2``.  The first
  two ``deliver()`` calls raise ``RuntimeError``; every subsequent call
  succeeds.
* ``ghost`` — **not registered** in the adapters dict.  Every delivery
  produces ``ADAPTER_MISSING``.

Message sequence
----------------
1. **A** — normal success on stable; flaky fails (call 1); ghost = MISSING.
2. **B** — normal success on stable; flaky fails (call 2); ghost = MISSING.
3. **C** — normal success on stable; flaky **succeeds** (call 3); ghost = MISSING.
4. **D** — normal success on stable; flaky succeeds; ghost = MISSING.
5. **E** — duplicate ``source_native_ref`` of A → **suppressed** (loop_prevented).
6. **F** — normal success on stable; flaky succeeds; ghost = MISSING.
7. **G** — normal success on stable; flaky succeeds; ghost = MISSING.
8. **H** — normal success on stable; flaky succeeds; ghost = MISSING.
9. **I** — normal success on stable; flaky succeeds; ghost = MISSING.
10. **J** — normal success on stable; flaky succeeds; ghost = MISSING.

Assertions verify:
* No failed message prevents later successes.
* ``RuntimeAccounting`` counters are exact integers.
* ``DeliveryOutcome`` statuses and ``failure_kind`` values are correct.
* Successful deliveries produce receipts with ``status="sent"``.
* The duplicate (E) increments ``loop_prevented`` and returns no outcomes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from typing import cast

from medre.adapters.fake_presentation import (
    FakePresentationAdapter,
    FaultyPresentationAdapter,
)
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.events.canonical import CanonicalEvent, NativeRef
from medre.core.events.metadata import EventMetadata
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import DeliveryFailureKind, DeliveryOutcome
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend

from tests.helpers.pipeline import make_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event_with_native_ref(
    event_id: str,
    native_ref: NativeRef,
    source_adapter: str = "src",
    event_kind: str = "message.created",
    payload: dict | None = None,
) -> CanonicalEvent:
    """Create a CanonicalEvent with a source_native_ref for dedup testing."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"text": "hello"},
        metadata=EventMetadata(),
        source_native_ref=native_ref,
    )


def _build_runner(
    storage: SQLiteStorage,
    accounting: RuntimeAccounting,
) -> PipelineRunner:
    """Build a PipelineRunner wired for the mixed-failure traffic test."""
    stable = FakePresentationAdapter(adapter_id="stable")
    flaky = FaultyPresentationAdapter(
        adapter_id="flaky",
        failure_mode="fail_n_then_succeed",
        fail_count=2,
    )

    route = Route(
        id="traffic-route",
        source=RouteSource(
            adapter="src",
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[
            RouteTarget(adapter="stable"),
            RouteTarget(adapter="flaky"),
            RouteTarget(adapter="ghost"),
        ],
    )
    router = Router(routes=[route])

    rp = RenderingPipeline()
    rp.register(TextRenderer(), priority=100)

    config = PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={"stable": stable, "flaky": flaky},
        event_bus=EventBus(),
        rendering_pipeline=rp,
        runtime_accounting=accounting,
    )
    return PipelineRunner(config)


def _outcome_map(
    outcomes: list[DeliveryOutcome],
) -> dict[str, DeliveryOutcome]:
    """Index delivery outcomes by target_adapter for easy assertion."""
    return {o.target_adapter: o for o in outcomes}


# ===================================================================
# The main traffic test
# ===================================================================


class TestMixedFailureUnderTraffic:
    """Prove that one failure does not poison later messages."""

    async def test_10_message_mixed_failure_sequence(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        accounting = RuntimeAccounting()
        runner = _build_runner(temp_storage, accounting)
        await runner.start()

        # References we will reuse for the duplicate test.
        native_ref_a = NativeRef(
            adapter="src",
            native_channel_id="ch-0",
            native_message_id="native-A",
        )

        try:
            # ============================================================
            # Message A — flaky call 1 (fails); ghost always MISSING
            # ============================================================
            event_a = _make_event_with_native_ref(
                event_id="msg-A",
                native_ref=native_ref_a,
            )
            outcomes_a = await runner.handle_ingress(event_a)
            om_a = _outcome_map(outcomes_a)

            assert om_a["stable"].status == "success"
            assert om_a["flaky"].status == "permanent_failure"
            assert om_a["flaky"].failure_kind is DeliveryFailureKind.ADAPTER_PERMANENT
            assert "RuntimeError" in (om_a["flaky"].error or "")
            assert om_a["ghost"].status == "permanent_failure"
            assert om_a["ghost"].failure_kind is DeliveryFailureKind.ADAPTER_MISSING

            # ============================================================
            # Message B — flaky call 2 (fails again)
            # ============================================================
            event_b = make_event(event_id="msg-B", source_adapter="src")
            outcomes_b = await runner.handle_ingress(event_b)
            om_b = _outcome_map(outcomes_b)

            assert om_b["stable"].status == "success"
            assert om_b["flaky"].status == "permanent_failure"
            assert om_b["flaky"].failure_kind is DeliveryFailureKind.ADAPTER_PERMANENT
            assert om_b["ghost"].status == "permanent_failure"
            assert om_b["ghost"].failure_kind is DeliveryFailureKind.ADAPTER_MISSING

            # ============================================================
            # Message C — flaky call 3 (PAST fail_count → succeeds!)
            # This is the key assertion: the earlier failures did NOT
            # poison the flaky adapter.
            # ============================================================
            event_c = make_event(event_id="msg-C", source_adapter="src")
            outcomes_c = await runner.handle_ingress(event_c)
            om_c = _outcome_map(outcomes_c)

            assert om_c["stable"].status == "success"
            assert om_c["flaky"].status == "success", (
                "flaky adapter should have recovered after fail_count=2; "
                "earlier failures must not poison later messages"
            )
            assert om_c["flaky"].failure_kind is None
            assert om_c["ghost"].status == "permanent_failure"

            # ============================================================
            # Messages D, G, H, I, J — all succeed on stable+flaky
            # (grouped; each is individually submitted to prove
            # sequential isolation).
            # ============================================================
            for label in ("D", "G", "H", "I", "J"):
                evt = make_event(event_id=f"msg-{label}", source_adapter="src")
                om = _outcome_map(await runner.handle_ingress(evt))
                assert om["stable"].status == "success"
                assert om["flaky"].status == "success", (
                    f"msg-{label}: flaky must succeed after recovery"
                )
                assert om["ghost"].status == "permanent_failure"

            # ============================================================
            # Message E — duplicate native_ref of A → loop_prevented
            # ============================================================
            event_e = _make_event_with_native_ref(
                event_id="msg-E-dup",
                native_ref=native_ref_a,
            )
            outcomes_e = await runner.handle_ingress(event_e)
            assert outcomes_e == [], (
                "Duplicate source_native_ref should produce no outcomes"
            )

            # ============================================================
            # Message F — success continues after duplicate suppression
            # ============================================================
            event_f = make_event(event_id="msg-F", source_adapter="src")
            outcomes_f = await runner.handle_ingress(event_f)
            om_f = _outcome_map(outcomes_f)

            assert om_f["stable"].status == "success"
            assert om_f["flaky"].status == "success", (
                "Suppression of msg-E must not affect msg-F"
            )
            assert om_f["ghost"].status == "permanent_failure"

            # ============================================================
            # Final accounting verification
            # ============================================================
            snap = accounting.snapshot()

            # 10 messages submitted; 1 suppressed as duplicate.
            assert snap["inbound_accepted"] == 9
            assert snap["loop_prevented"] == 1

            # 9 accepted events × 3 targets = 27 outbound attempts.
            assert snap["outbound_attempts"] == 27

            # stable: 9 successes.
            # flaky: 2 failures (msg-A, msg-B) + 7 successes = 9 attempts.
            # ghost: 9 failures (ADAPTER_MISSING every time).
            # Total delivered: 9 (stable) + 7 (flaky) = 16.
            assert snap["outbound_delivered"] == 16

            # Total failed: 2 (flaky) + 9 (ghost) = 11.
            assert snap["outbound_failed"] == 11

            # Consistency: delivered + failed == attempts.
            assert (
                snap["outbound_delivered"] + snap["outbound_failed"]
                == snap["outbound_attempts"]
            ), "delivered + failed must equal total attempts"

            # Unrelated counters remain zero.
            assert snap["replay_processed"] == 0
            assert snap["replay_rejected"] == 0
            assert snap["capacity_rejections"] == 0

            # All values are deterministic ints (not floats, not None).
            for key, value in snap.items():
                assert isinstance(value, int), (
                    f"accounting[{key!r}] = {value!r}; expected int"
                )

        finally:
            await runner.stop()


# ===================================================================
# Per-message receipt verification
# ===================================================================


class TestReceiptConsistency:
    """Verify delivery receipts persisted in storage match outcomes."""

    async def test_failed_receipts_have_error_field(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Every failed delivery produces a receipt with status='failed'."""
        accounting = RuntimeAccounting()
        runner = _build_runner(temp_storage, accounting)
        await runner.start()

        try:
            event = make_event(event_id="receipt-check-1", source_adapter="src")
            outcomes = await runner.handle_ingress(event)

            for outcome in outcomes:
                if outcome.status == "permanent_failure":
                    # Failed receipt must exist in storage.
                    rows = await temp_storage._read_all(
                        "SELECT * FROM delivery_receipts "
                        "WHERE event_id = ? AND target_adapter = ?",
                        (outcome.event_id, outcome.target_adapter),
                    )
                    assert len(rows) >= 1, (
                        f"Expected failed receipt for {outcome.target_adapter}"
                    )
                    assert rows[0]["status"] == "failed"
                    assert rows[0]["error"] is not None

                elif outcome.status == "success":
                    # Successful receipt with status='sent'.
                    rows = await temp_storage._read_all(
                        "SELECT * FROM delivery_receipts "
                        "WHERE event_id = ? AND target_adapter = ?",
                        (outcome.event_id, outcome.target_adapter),
                    )
                    assert len(rows) >= 1, (
                        f"Expected sent receipt for {outcome.target_adapter}"
                    )
                    assert rows[0]["status"] == "sent"
        finally:
            await runner.stop()

    async def test_successful_receipts_have_native_refs(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Every successful delivery stores an outbound native ref."""
        accounting = RuntimeAccounting()
        runner = _build_runner(temp_storage, accounting)
        await runner.start()

        try:
            event = make_event(event_id="nref-check-1", source_adapter="src")
            outcomes = await runner.handle_ingress(event)

            successful = [o for o in outcomes if o.status == "success"]
            assert len(successful) >= 1

            for outcome in successful:
                refs = await temp_storage._read_all(
                    "SELECT * FROM native_message_refs "
                    "WHERE event_id = ? AND adapter = ? AND direction = 'outbound'",
                    (outcome.event_id, outcome.target_adapter),
                )
                assert len(refs) >= 1, (
                    f"Expected outbound native_ref for {outcome.target_adapter}"
                )
        finally:
            await runner.stop()


# ===================================================================
# Failure isolation edge cases
# ===================================================================


class TestFailureIsolation:
    """Edge-case assertions that failures are truly isolated."""

    async def test_ghost_always_fails_independently(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ADAPTER_MISSING outcome never affects stable or flaky."""
        accounting = RuntimeAccounting()
        runner = _build_runner(temp_storage, accounting)
        await runner.start()

        try:
            for i in range(5):
                event = make_event(
                    event_id=f"isolation-{i}",
                    source_adapter="src",
                )
                outcomes = await runner.handle_ingress(event)
                om = _outcome_map(outcomes)

                # stable always succeeds regardless of ghost.
                assert om["stable"].status == "success"

                # ghost always fails with ADAPTER_MISSING.
                assert om["ghost"].status == "permanent_failure"
                assert om["ghost"].failure_kind is DeliveryFailureKind.ADAPTER_MISSING
        finally:
            await runner.stop()

    async def test_duplicate_does_not_affect_subsequent_messages(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A suppressed duplicate (loop_prevented) has zero effect on later events."""
        accounting = RuntimeAccounting()
        runner = _build_runner(temp_storage, accounting)
        await runner.start()

        native_ref = NativeRef(
            adapter="src",
            native_channel_id="ch-0",
            native_message_id="dup-edge-1",
        )

        try:
            # First message — succeeds normally.
            event_1 = _make_event_with_native_ref(
                event_id="edge-1",
                native_ref=native_ref,
            )
            outcomes_1 = await runner.handle_ingress(event_1)
            assert len(outcomes_1) == 3

            # Duplicate — suppressed.
            event_2 = _make_event_with_native_ref(
                event_id="edge-2-dup",
                native_ref=native_ref,
            )
            outcomes_2 = await runner.handle_ingress(event_2)
            assert outcomes_2 == []

            # Third message (fresh native ref) — delivered despite prior duplicate.
            # flaky adapter is on call 2 (fail_count=2), so it still fails.
            fresh_ref = NativeRef(
                adapter="src",
                native_channel_id="ch-0",
                native_message_id="fresh-3",
            )
            event_3 = _make_event_with_native_ref(
                event_id="edge-3",
                native_ref=fresh_ref,
            )
            outcomes_3 = await runner.handle_ingress(event_3)
            om_3 = _outcome_map(outcomes_3)
            assert om_3["stable"].status == "success"
            # flaky: 2nd call, still within fail_count → permanent_failure
            assert om_3["flaky"].failure_kind == DeliveryFailureKind.ADAPTER_PERMANENT

            # Verify accounting: 2 accepted, 1 loop_prevented.
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 2
            assert snap["loop_prevented"] == 1
        finally:
            await runner.stop()

    async def test_flaky_recovery_after_initial_failures(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """FaultyPresentationAdapter recovers and stays healthy after fail_count."""
        accounting = RuntimeAccounting()
        runner = _build_runner(temp_storage, accounting)
        await runner.start()

        try:
            # Send enough messages to push past fail_count=2.
            for i in range(10):
                event = make_event(
                    event_id=f"recovery-{i}",
                    source_adapter="src",
                )
                outcomes = await runner.handle_ingress(event)
                om = _outcome_map(outcomes)

                if i < 2:
                    # First 2 calls: flaky fails.
                    assert om["flaky"].status == "permanent_failure", (
                        f"flaky should fail on call {i + 1}"
                    )
                else:
                    # Calls 3+: flaky succeeds.
                    assert om["flaky"].status == "success", (
                        f"flaky should succeed on call {i + 1} "
                        f"(past fail_count=2)"
                    )

            # Verify accounting over 10 messages.
            # stable: 10 success
            # flaky: 2 fail + 8 success
            # ghost: 10 fail (ADAPTER_MISSING — not registered)
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 10
            assert snap["outbound_attempts"] == 30  # 10 × 3 targets
            assert snap["outbound_delivered"] == 18  # 10 stable + 8 flaky
            assert snap["outbound_failed"] == 12  # 2 flaky + 10 ghost
        finally:
            await runner.stop()
