"""Tests for delivery planning: DeliveryPlan, DeliveryStrategy, RetryPolicy,
FallbackResolver, RelationResolver, DeliveryOutcome, Diagnostician,
DeliveryFailureKind, RetryExecutor, and receipt lineage semantics.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning import (
    DeliveryPlan,
    DeliveryStrategy,
    FallbackResolver,
    RelationResolver,
    RetryPolicy,
)
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    RetryExecutor,
)
from medre.core.routing import RouteTarget
from medre.core.routing.stats import RouteCounters, RouteStats


def _make_event(
    event_kind: str = "message.created",
    event_id: str = "evt-1",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_transport",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# DeliveryPlan / DeliveryStrategy / RetryPolicy
# ===================================================================


class TestDeliveryPlan:
    """DeliveryPlan construction, fallback chain, and retry policy."""

    def test_construction_with_primary_strategy(self) -> None:
        target = RouteTarget(adapter="fake_presentation")
        strategy = DeliveryStrategy(method="direct")
        plan = DeliveryPlan(
            plan_id="plan-1",
            event_id="evt-1",
            target=target,
            primary_strategy=strategy,
        )
        assert plan.plan_id == "plan-1"
        assert plan.primary_strategy.method == "direct"
        assert plan.fallback_chain == []
        assert plan.retry_policy is None
        assert plan.deadline is None

    def test_fallback_chain(self) -> None:
        """Fallback strategies are stored in order."""
        primary = DeliveryStrategy(method="direct")
        fb1 = DeliveryStrategy(method="propagated")
        fb2 = DeliveryStrategy(method="opportunistic")
        plan = DeliveryPlan(
            plan_id="plan-2",
            event_id="evt-2",
            target=RouteTarget(adapter="t"),
            primary_strategy=primary,
            fallback_chain=[fb1, fb2],
        )
        assert plan.fallback_chain == [fb1, fb2]
        assert plan.fallback_chain[0].method == "propagated"

    def test_retry_policy_defaults(self) -> None:
        policy = RetryPolicy()
        assert policy.max_attempts == 3
        assert policy.backoff_base == 2.0
        assert policy.max_delay_seconds == 60.0
        assert policy.jitter is True

    def test_delivery_strategy_custom_params(self) -> None:
        strategy = DeliveryStrategy(
            method="direct", max_retries=10, timeout_seconds=60.0
        )
        assert strategy.max_retries == 10
        assert strategy.timeout_seconds == 60.0

    def test_plan_with_deadline(self) -> None:
        deadline = datetime(2026, 1, 1, tzinfo=timezone.utc)
        plan = DeliveryPlan(
            plan_id="plan-3",
            event_id="evt-3",
            target=RouteTarget(adapter="t"),
            primary_strategy=DeliveryStrategy(method="direct"),
            deadline=deadline,
            retry_policy=RetryPolicy(max_attempts=3),
        )
        assert plan.deadline is deadline
        assert plan.retry_policy is not None
        assert plan.retry_policy.max_attempts == 3


# ===================================================================
# FallbackResolver
# ===================================================================


class TestFallbackResolver:
    """FallbackResolver downgrades strategies based on adapter capabilities."""

    def test_message_created_uses_direct(self) -> None:
        event = _make_event(event_kind="message.created")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="fake_presentation")
        plan = resolver.resolve_fallback(event, target, AdapterCapabilities())
        assert plan.primary_strategy.method == "direct"

    def test_reaction_suppressed_when_not_supported(self) -> None:
        event = _make_event(event_kind="message.reacted")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        caps = AdapterCapabilities(reactions="unsupported")
        plan = resolver.resolve_fallback(event, target, caps)
        assert plan.primary_strategy.method == "skip"

    def test_reaction_keeps_direct_when_supported(self) -> None:
        event = _make_event(event_kind="message.reacted")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        caps = AdapterCapabilities(reactions="native")
        plan = resolver.resolve_fallback(event, target, caps)
        assert plan.primary_strategy.method == "direct"

    def test_edit_suppressed_when_not_supported(self) -> None:
        event = _make_event(event_kind="message.edited")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        caps = AdapterCapabilities(edits="unsupported")
        plan = resolver.resolve_fallback(event, target, caps)
        assert plan.primary_strategy.method == "skip"

    def test_delete_suppressed_when_not_supported(self) -> None:
        event = _make_event(event_kind="message.deleted")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        caps = AdapterCapabilities(deletes="unsupported")
        plan = resolver.resolve_fallback(event, target, caps)
        assert plan.primary_strategy.method == "skip"

    def test_plan_event_id_matches_source(self) -> None:
        event = _make_event(event_kind="message.created", event_id="evt-x")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        plan = resolver.resolve_fallback(event, target, AdapterCapabilities())
        assert plan.event_id == "evt-x"

    def test_plan_target_matches_input(self) -> None:
        event = _make_event()
        resolver = FallbackResolver()
        target = RouteTarget(adapter="my_target", channel="ch-1")
        plan = resolver.resolve_fallback(event, target, AdapterCapabilities())
        assert plan.target is target


# ===================================================================
# RelationResolver
# ===================================================================


class TestRelationResolver:
    """RelationResolver resolves native refs to canonical event IDs."""

    async def test_resolve_relation_already_resolved(self) -> None:
        """Relation with target_event_id already set is returned unchanged."""
        resolver = RelationResolver(storage=object())
        relation = EventRelation(
            relation_type="reply",
            target_event_id="known-evt",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        result = await resolver.resolve_relation(relation)
        assert result.target_event_id == "known-evt"

    async def test_resolve_relation_raises_without_ref_or_id(self) -> None:
        """Relation with neither target_event_id nor target_native_ref raises ValueError."""
        resolver = RelationResolver(storage=object())
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        with pytest.raises(ValueError, match="target_event_id"):
            await resolver.resolve_relation(relation)

    async def test_resolve_relation_with_native_ref_via_dummy_storage(self) -> None:
        """resolve_relation looks up native ref via storage.resolve_native_ref."""

        class _DummyStorage:
            async def resolve_native_ref(
                self, adapter: str, channel: str, message_id: str
            ) -> str | None:
                if message_id == "msg-1":
                    return "resolved-evt"
                return None

        resolver = RelationResolver(storage=_DummyStorage())
        nref = NativeRef(
            adapter="fake_transport",
            native_channel_id="ch-0",
            native_message_id="msg-1",
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        result = await resolver.resolve_relation(relation)
        assert result.target_event_id == "resolved-evt"

    async def test_resolve_relation_returns_original_when_not_found(self) -> None:
        """If the native ref has no mapping, original relation is returned."""

        class _EmptyStorage:
            async def resolve_native_ref(
                self, adapter: str, channel: str, message_id: str
            ) -> str | None:
                return None

        resolver = RelationResolver(storage=_EmptyStorage())
        nref = NativeRef(adapter="a", native_channel_id="c", native_message_id="nope")
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        result = await resolver.resolve_relation(relation)
        assert result.target_event_id is None

    async def test_create_relation_event(self) -> None:
        """create_relation_event builds a new canonical event with a relation."""
        resolver = RelationResolver(storage=object())
        source = _make_event(event_id="src-1")
        target_nref = NativeRef(
            adapter="fake_presentation",
            native_channel_id="ch-0",
            native_message_id="msg-99",
        )
        new_event = await resolver.create_relation_event(
            source_event=source,
            relation_type="reply",
            target_native_ref=target_nref,
            key=None,
        )
        assert new_event.parent_event_id == "src-1"
        assert "src-1" in new_event.lineage
        assert len(new_event.relations) == 1
        assert new_event.relations[0].relation_type == "reply"
        assert new_event.relations[0].target_native_ref == target_nref
        assert new_event.depth == source.depth + 1


# ===================================================================
# DeliveryOutcome
# ===================================================================


class TestDeliveryOutcome:
    """DeliveryOutcome construction and status semantics."""

    def test_success_outcome(self) -> None:
        outcome = DeliveryOutcome(
            event_id="evt-1",
            target_adapter="discord",
            target_channel="ch-1",
            route_id="route-a",
            delivery_plan_id="plan-1",
            status="success",
            receipt=None,
            error=None,
            duration_ms=12.5,
        )
        assert outcome.status == "success"
        assert outcome.error is None
        assert outcome.receipt is None
        assert outcome.duration_ms == 12.5

    def test_transient_failure_outcome(self) -> None:
        outcome = DeliveryOutcome(
            event_id="evt-2",
            target_adapter="slack",
            target_channel=None,
            route_id="route-b",
            delivery_plan_id="plan-2",
            status="transient_failure",
            receipt=None,
            error="ConnectionRefusedError: connection refused",
            duration_ms=5002.0,
        )
        assert outcome.status == "transient_failure"
        assert outcome.error is not None
        assert "ConnectionRefusedError" in outcome.error

    def test_permanent_failure_outcome(self) -> None:
        outcome = DeliveryOutcome(
            event_id="evt-3",
            target_adapter="irc",
            target_channel="#general",
            route_id="route-c",
            delivery_plan_id="plan-3",
            status="permanent_failure",
            receipt=None,
            error="ValueError: malformed payload",
            duration_ms=3.0,
        )
        assert outcome.status == "permanent_failure"
        assert outcome.target_channel == "#general"

    def test_skipped_outcome(self) -> None:
        outcome = DeliveryOutcome(
            event_id="evt-4",
            target_adapter="matrix",
            target_channel=None,
            route_id="route-d",
            delivery_plan_id="plan-4",
            status="skipped",
            receipt=None,
            error="No renderer found for event_kind=message.reacted",
            duration_ms=0.1,
        )
        assert outcome.status == "skipped"

    def test_all_targets_succeed(self) -> None:
        """Multiple targets all produce success outcomes."""
        targets = ["adapter-a", "adapter-b", "adapter-c"]
        outcomes = [
            DeliveryOutcome(
                event_id="evt-batch",
                target_adapter=t,
                target_channel=None,
                route_id=f"route-{t}",
                delivery_plan_id=f"plan-{t}",
                status="success",
                receipt=None,
                error=None,
                duration_ms=float(i),
            )
            for i, t in enumerate(targets)
        ]
        assert len(outcomes) == 3
        assert all(o.status == "success" for o in outcomes)

    def test_one_target_fails_one_succeeds(self) -> None:
        """Mixed success/failure outcomes in a fanout."""
        outcomes = [
            DeliveryOutcome(
                event_id="evt-mixed",
                target_adapter="good",
                target_channel=None,
                route_id="route-1",
                delivery_plan_id="plan-good",
                status="success",
                receipt=None,
                error=None,
                duration_ms=5.0,
            ),
            DeliveryOutcome(
                event_id="evt-mixed",
                target_adapter="bad",
                target_channel=None,
                route_id="route-2",
                delivery_plan_id="plan-bad",
                status="transient_failure",
                receipt=None,
                error="TimeoutError: timed out",
                duration_ms=30000.0,
            ),
        ]
        succeeded = [o for o in outcomes if o.status == "success"]
        failed = [o for o in outcomes if o.status != "success"]
        assert len(succeeded) == 1
        assert len(failed) == 1
        assert succeeded[0].target_adapter == "good"
        assert failed[0].target_adapter == "bad"

    def test_planner_error_distinct_from_delivery_error(self) -> None:
        """Planner errors produce permanent_failure; delivery errors can be transient."""
        planner_outcome = DeliveryOutcome(
            event_id="evt-err",
            target_adapter="",
            target_channel=None,
            route_id="",
            delivery_plan_id="",
            status="permanent_failure",
            receipt=None,
            error="Planner error: RuntimeError: router misconfigured",
            duration_ms=0.0,
        )
        delivery_outcome = DeliveryOutcome(
            event_id="evt-err",
            target_adapter="adapter-a",
            target_channel="ch-1",
            route_id="route-x",
            delivery_plan_id="plan-x",
            status="transient_failure",
            receipt=None,
            error="ConnectionError: network unreachable",
            duration_ms=1000.0,
        )
        # Planner failure: no adapter/channel context.
        assert planner_outcome.target_adapter == ""
        assert planner_outcome.status == "permanent_failure"

        # Delivery failure: has full target context.
        assert delivery_outcome.target_adapter == "adapter-a"
        assert delivery_outcome.status == "transient_failure"


# ===================================================================
# Diagnostician
# ===================================================================


class TestDiagnostician:
    """Diagnostician records and snapshots diagnostic events."""

    def test_record_planner_failure(self) -> None:
        diag = Diagnostician()
        diag.record_planner_failure("evt-1", "router crash")
        snap = diag.snapshot()
        assert snap["planner_failures"] == {"evt-1": 1}

    def test_record_adapter_failure(self) -> None:
        diag = Diagnostician()
        diag.record_adapter_failure("evt-2", "discord", "ConnectionRefused")
        snap = diag.snapshot()
        assert snap["adapter_failures"] == {"discord": 1}

    def test_record_renderer_failure(self) -> None:
        diag = Diagnostician()
        diag.record_renderer_failure("evt-3", "irc", "no renderer")
        snap = diag.snapshot()
        assert snap["renderer_failures"] == {"irc": 1}

    def test_record_replay_skip(self) -> None:
        diag = Diagnostician()
        diag.record_replay_skip("evt-4", "already delivered")
        snap = diag.snapshot()
        assert snap["replay_skips"] == {"already delivered": 1}

    def test_record_replay_downgrade(self) -> None:
        diag = Diagnostician()
        diag.record_replay_downgrade("evt-5", "full", "summary")
        snap = diag.snapshot()
        assert snap["replay_downgrades"] == {"full->summary": 1}

    def test_record_correlation_miss(self) -> None:
        diag = Diagnostician()
        diag.record_correlation_miss("evt-6", "native-msg-42")
        snap = diag.snapshot()
        assert snap["correlation_misses"] == {"native-msg-42": 1}

    def test_multiple_failures_accumulate(self) -> None:
        diag = Diagnostician()
        diag.record_adapter_failure("evt-7", "slack", "timeout")
        diag.record_adapter_failure("evt-8", "slack", "timeout")
        diag.record_adapter_failure("evt-9", "discord", "reset")
        snap = diag.snapshot()
        assert snap["adapter_failures"] == {"slack": 2, "discord": 1}

    def test_snapshot_isolation(self) -> None:
        diag = Diagnostician()
        diag.record_planner_failure("evt-10", "err")
        snap = diag.snapshot()
        snap["planner_failures"]["evt-10"] = 999
        assert diag.snapshot()["planner_failures"] == {"evt-10": 1}


# ===================================================================
# DeliveryFailureKind
# ===================================================================


class TestDeliveryFailureKind:
    """DeliveryFailureKind taxonomy and retryability semantics."""

    def test_all_members_exist(self) -> None:
        """All expected failure kinds are defined."""
        expected = {
            "PLANNER_FAILURE",
            "RENDERER_FAILURE",
            "ADAPTER_TRANSIENT",
            "ADAPTER_PERMANENT",
            "ADAPTER_MISSING",
            "DEADLINE_EXCEEDED",
            "SHUTDOWN_REJECTION",
            "CAPACITY_REJECTION",
            "LOOP_SUPPRESSED",
            "POLICY_SUPPRESSED",
            "CAPABILITY_SUPPRESSED",
        }
        actual = {m.name for m in DeliveryFailureKind}
        assert actual == expected

    def test_only_adapter_transient_is_retryable(self) -> None:
        """Only ADAPTER_TRANSIENT is retryable."""
        assert DeliveryFailureKind.ADAPTER_TRANSIENT.is_retryable is True

        non_retryable = [
            DeliveryFailureKind.PLANNER_FAILURE,
            DeliveryFailureKind.RENDERER_FAILURE,
            DeliveryFailureKind.ADAPTER_PERMANENT,
            DeliveryFailureKind.ADAPTER_MISSING,
            DeliveryFailureKind.DEADLINE_EXCEEDED,
            DeliveryFailureKind.CAPACITY_REJECTION,
            DeliveryFailureKind.SHUTDOWN_REJECTION,
            DeliveryFailureKind.LOOP_SUPPRESSED,
            DeliveryFailureKind.POLICY_SUPPRESSED,
            DeliveryFailureKind.CAPABILITY_SUPPRESSED,
        ]
        for kind in non_retryable:
            assert kind.is_retryable is False, f"{kind.name} should not be retryable"

    def test_enum_values_are_strings(self) -> None:
        """Enum values are lowercase snake_case strings."""
        assert DeliveryFailureKind.PLANNER_FAILURE.value == "planner_failure"
        assert DeliveryFailureKind.RENDERER_FAILURE.value == "renderer_failure"
        assert DeliveryFailureKind.ADAPTER_TRANSIENT.value == "adapter_transient"
        assert DeliveryFailureKind.ADAPTER_PERMANENT.value == "adapter_permanent"
        assert DeliveryFailureKind.ADAPTER_MISSING.value == "adapter_missing"
        assert DeliveryFailureKind.DEADLINE_EXCEEDED.value == "deadline_exceeded"
        assert DeliveryFailureKind.POLICY_SUPPRESSED.value == "policy_suppressed"

    def test_classify_transient_errors(self) -> None:
        """Transient exception types classify as ADAPTER_TRANSIENT."""
        transient_exc = [
            TimeoutError("timed out"),
            ConnectionError("refused"),
            ConnectionRefusedError("refused"),
            ConnectionResetError("reset"),
            ConnectionAbortedError("aborted"),
            BrokenPipeError("broken"),
            OSError("os error"),
        ]
        for exc in transient_exc:
            kind = RetryExecutor.classify_failure(exc)
            assert (
                kind is DeliveryFailureKind.ADAPTER_TRANSIENT
            ), f"{type(exc).__name__} should classify as ADAPTER_TRANSIENT"

    def test_classify_permanent_errors(self) -> None:
        """Non-transient exceptions classify as ADAPTER_PERMANENT."""
        exc = RuntimeError("business logic error")
        kind = RetryExecutor.classify_failure(exc)
        assert kind is DeliveryFailureKind.ADAPTER_PERMANENT

    def test_classify_planner_failure(self) -> None:
        kind = RetryExecutor.classify_failure(RuntimeError("x"), planner_failed=True)
        assert kind is DeliveryFailureKind.PLANNER_FAILURE

    def test_classify_renderer_failure(self) -> None:
        kind = RetryExecutor.classify_failure(RuntimeError("x"), renderer_failed=True)
        assert kind is DeliveryFailureKind.RENDERER_FAILURE

    def test_classify_adapter_missing(self) -> None:
        kind = RetryExecutor.classify_failure(
            RuntimeError("x"), adapter_registered=False
        )
        assert kind is DeliveryFailureKind.ADAPTER_MISSING

    def test_classify_deadline_exceeded(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        kind = RetryExecutor.classify_failure(RuntimeError("x"), deadline=past)
        assert kind is DeliveryFailureKind.DEADLINE_EXCEEDED

    def test_classify_no_deadline_not_exceeded(self) -> None:
        """When deadline is None, deadline_exceeded is never returned."""
        kind = RetryExecutor.classify_failure(TimeoutError("timeout"), deadline=None)
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT


# ===================================================================
# RetryExecutor
# ===================================================================


class TestRetryExecutor:
    """RetryExecutor backoff, exhaustion, and receipt construction."""

    def test_compute_backoff_doubles_each_attempt(self) -> None:
        """Backoff doubles: base=2 → 2, 4, 8, 16, ..."""
        policy = RetryPolicy(backoff_base=2.0, jitter=False, max_delay_seconds=1000.0)
        executor = RetryExecutor(policy)
        assert executor.compute_backoff(1) == timedelta(seconds=2.0)
        assert executor.compute_backoff(2) == timedelta(seconds=4.0)
        assert executor.compute_backoff(3) == timedelta(seconds=8.0)
        assert executor.compute_backoff(4) == timedelta(seconds=16.0)

    def test_compute_backoff_capped_at_max_delay(self) -> None:
        """Backoff is capped at max_delay_seconds."""
        policy = RetryPolicy(backoff_base=2.0, jitter=False, max_delay_seconds=10.0)
        executor = RetryExecutor(policy)
        # attempt 1: 2, 2: 4, 3: 8, 4: 10 (capped), 5: 10 (capped)
        assert executor.compute_backoff(4) == timedelta(seconds=10.0)
        assert executor.compute_backoff(5) == timedelta(seconds=10.0)

    def test_compute_backoff_with_jitter(self) -> None:
        """Jitter produces backoff values within expected bounds."""
        policy = RetryPolicy(backoff_base=4.0, jitter=True, max_delay_seconds=1000.0)
        executor = RetryExecutor(policy)
        for attempt in range(1, 6):
            backoff = executor.compute_backoff(attempt)
            # With jitter: delay in [base*0.5, base) for each step
            base_delay = 4.0 * (2 ** (attempt - 1))
            assert timedelta(seconds=base_delay * 0.5) <= backoff
            assert backoff <= timedelta(seconds=base_delay)

    def test_compute_backoff_jitter_is_deterministic(self) -> None:
        """Repeated calls with the same policy and attempt return identical values."""
        policy = RetryPolicy(backoff_base=4.0, jitter=True, max_delay_seconds=1000.0)
        executor = RetryExecutor(policy)
        for attempt in range(1, 6):
            values = [executor.compute_backoff(attempt) for _ in range(20)]
            assert (
                len(set(values)) == 1
            ), f"Attempt {attempt}: jitter is nondeterministic, got {set(values)}"

    def test_compute_backoff_jitter_different_attempts_differ(self) -> None:
        """Different attempt numbers produce different jittered backoffs."""
        policy = RetryPolicy(backoff_base=2.0, jitter=True, max_delay_seconds=1000.0)
        executor = RetryExecutor(policy)
        backoffs = {
            attempt: executor.compute_backoff(attempt) for attempt in range(1, 6)
        }
        # Each attempt should produce a distinct jittered value (before capping)
        assert len(set(backoffs.values())) == len(
            backoffs
        ), f"All attempts should produce distinct backoffs, got {backoffs}"

    def test_compute_backoff_jitter_different_policies_differ(self) -> None:
        """Different policies produce different jittered backoffs for the same attempt."""
        policy_a = RetryPolicy(backoff_base=2.0, jitter=True, max_delay_seconds=1000.0)
        policy_b = RetryPolicy(backoff_base=3.0, jitter=True, max_delay_seconds=1000.0)
        executor_a = RetryExecutor(policy_a)
        executor_b = RetryExecutor(policy_b)
        # Same attempt, different policy → different hash seed → different result
        for attempt in range(1, 4):
            assert executor_a.compute_backoff(attempt) != executor_b.compute_backoff(
                attempt
            )

    def test_is_exhausted_within_max_attempts(self) -> None:
        """Not exhausted when attempts remaining."""
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        assert executor.is_exhausted(1) is False
        assert executor.is_exhausted(2) is False

    def test_is_exhausted_at_max_attempts(self) -> None:
        """Exhausted exactly at max_attempts."""
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        assert executor.is_exhausted(3) is True
        assert executor.is_exhausted(4) is True

    def test_next_attempt_number(self) -> None:
        policy = RetryPolicy()
        executor = RetryExecutor(policy)
        assert executor.next_attempt_number(1) == 2
        assert executor.next_attempt_number(5) == 6

    def test_build_retry_receipt(self) -> None:
        """Retry receipt has status=failed and next_retry_at populated."""
        policy = RetryPolicy(backoff_base=2.0, jitter=False, max_delay_seconds=60.0)
        executor = RetryExecutor(policy)
        receipt = executor.build_retry_receipt(
            event_id="evt-1",
            delivery_plan_id="plan-1",
            target_adapter="adapter-a",
            previous_receipt_id="rcpt-prev",
            attempt_number=2,
            error="ConnectionError: timeout",
        )
        assert receipt.status == "failed"
        assert receipt.event_id == "evt-1"
        assert receipt.delivery_plan_id == "plan-1"
        assert receipt.target_adapter == "adapter-a"
        assert receipt.attempt_number == 2
        assert receipt.parent_receipt_id == "rcpt-prev"
        assert receipt.next_retry_at is not None
        assert receipt.error == "ConnectionError: timeout"
        # backoff for attempt 2: base * 2^1 = 4.0 seconds
        assert receipt.next_retry_at > receipt.created_at

    def test_build_retry_receipt_defaults_source_live(self) -> None:
        """build_retry_receipt defaults source='live' and replay_run_id=None."""
        policy = RetryPolicy(backoff_base=2.0, jitter=False, max_delay_seconds=60.0)
        executor = RetryExecutor(policy)
        receipt = executor.build_retry_receipt(
            event_id="evt-default-src",
            delivery_plan_id="plan-src",
            target_adapter="adapter-x",
            previous_receipt_id=None,
            attempt_number=1,
            error="timeout",
        )
        assert receipt.source == "live"
        assert receipt.replay_run_id is None

    def test_build_retry_receipt_replay_source_propagates(self) -> None:
        """build_retry_receipt propagates source='replay' and replay_run_id."""
        policy = RetryPolicy(backoff_base=2.0, jitter=False, max_delay_seconds=60.0)
        executor = RetryExecutor(policy)
        receipt = executor.build_retry_receipt(
            event_id="evt-replay-src",
            delivery_plan_id="plan-replay",
            target_adapter="adapter-y",
            previous_receipt_id="rcpt-prev",
            attempt_number=2,
            error="ConnectionError",
            source="replay",
            replay_run_id="run-42",
        )
        assert receipt.source == "replay"
        assert receipt.replay_run_id == "run-42"
        assert receipt.status == "failed"
        assert receipt.attempt_number == 2

    def test_build_dead_letter_receipt(self) -> None:
        """Dead-letter receipt has status=dead_lettered and no next_retry_at."""
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        receipt = executor.build_dead_letter_receipt(
            event_id="evt-2",
            delivery_plan_id="plan-2",
            target_adapter="adapter-b",
            previous_receipt_id="rcpt-last",
            attempt_number=4,
            error="Retry exhausted after 3 attempts",
        )
        assert receipt.status == "dead_lettered"
        assert receipt.event_id == "evt-2"
        assert receipt.delivery_plan_id == "plan-2"
        assert receipt.target_adapter == "adapter-b"
        assert receipt.attempt_number == 4
        assert receipt.parent_receipt_id == "rcpt-last"
        assert receipt.next_retry_at is None
        assert receipt.error is not None
        assert "exhausted" in receipt.error

    def test_retry_exhaustion_flow(self) -> None:
        """Simulate a full retry exhaustion flow: 3 attempts then dead letter."""
        policy = RetryPolicy(max_attempts=3, jitter=False)
        executor = RetryExecutor(policy)

        # Attempt 1 fails
        assert executor.is_exhausted(1) is False
        r1 = executor.build_retry_receipt(
            event_id="evt-flow",
            delivery_plan_id="plan-flow",
            target_adapter="t",
            previous_receipt_id=None,
            attempt_number=1,
            error="ConnectionError",
        )
        assert r1.attempt_number == 1
        assert r1.parent_receipt_id is None

        # Attempt 2 fails
        assert executor.is_exhausted(2) is False
        r2 = executor.build_retry_receipt(
            event_id="evt-flow",
            delivery_plan_id="plan-flow",
            target_adapter="t",
            previous_receipt_id=r1.receipt_id,
            attempt_number=2,
            error="ConnectionError",
        )
        assert r2.attempt_number == 2
        assert r2.parent_receipt_id == r1.receipt_id

        # Attempt 3 fails — still one more attempt
        assert executor.is_exhausted(3) is True

        # Dead letter
        dl = executor.build_dead_letter_receipt(
            event_id="evt-flow",
            delivery_plan_id="plan-flow",
            target_adapter="t",
            previous_receipt_id=r2.receipt_id,
            attempt_number=4,
            error="Retry exhausted",
        )
        assert dl.status == "dead_lettered"
        assert dl.attempt_number == 4
        assert dl.parent_receipt_id == r2.receipt_id

    def test_policy_property(self) -> None:
        """RetryExecutor exposes its policy."""
        policy = RetryPolicy(max_attempts=7)
        executor = RetryExecutor(policy)
        assert executor.policy is policy
        assert executor.policy.max_attempts == 7

    def test_build_retry_receipt_has_target_channel(self) -> None:
        """build_retry_receipt accepts and persists target_channel."""
        policy = RetryPolicy(backoff_base=2.0, jitter=False, max_delay_seconds=60.0)
        executor = RetryExecutor(policy)
        receipt = executor.build_retry_receipt(
            event_id="evt-ch",
            delivery_plan_id="plan-ch",
            target_adapter="adapter-ch",
            previous_receipt_id=None,
            attempt_number=1,
            error="timeout",
            target_channel="!room:test",
        )
        assert receipt.target_channel == "!room:test"

    def test_build_retry_receipt_has_retry_policy_fields(self) -> None:
        """build_retry_receipt includes retry policy fields from executor's policy."""
        policy = RetryPolicy(
            max_attempts=7,
            backoff_base=3.0,
            max_delay_seconds=90.0,
            jitter=False,
        )
        executor = RetryExecutor(policy)
        receipt = executor.build_retry_receipt(
            event_id="evt-policy",
            delivery_plan_id="plan-policy",
            target_adapter="adapter-pol",
            previous_receipt_id=None,
            attempt_number=1,
            error="timeout",
        )
        assert receipt.retry_max_attempts == 7
        assert receipt.retry_backoff_base == 3.0
        assert receipt.retry_max_delay == 90.0
        assert receipt.retry_jitter is False

    def test_build_retry_receipt_preserves_source_retry(self) -> None:
        """build_retry_receipt with source='retry' preserves the value."""
        policy = RetryPolicy(backoff_base=2.0, jitter=False, max_delay_seconds=60.0)
        executor = RetryExecutor(policy)
        receipt = executor.build_retry_receipt(
            event_id="evt-retry-src",
            delivery_plan_id="plan-retry-src",
            target_adapter="adapter-rs",
            previous_receipt_id=None,
            attempt_number=1,
            error="timeout",
            source="retry",
        )
        assert receipt.source == "retry"

    def test_build_retry_receipt_preserves_target_channel(self) -> None:
        """build_retry_receipt with target_channel='!room:test' preserves it."""
        policy = RetryPolicy(backoff_base=2.0, jitter=False, max_delay_seconds=60.0)
        executor = RetryExecutor(policy)
        receipt = executor.build_retry_receipt(
            event_id="evt-tc",
            delivery_plan_id="plan-tc",
            target_adapter="adapter-tc",
            previous_receipt_id=None,
            attempt_number=1,
            error="timeout",
            target_channel="!room:test",
        )
        assert receipt.target_channel == "!room:test"

    def test_build_dead_letter_receipt_preserves_target_channel(self) -> None:
        """build_dead_letter_receipt with target_channel='!room:dl' preserves it."""
        policy = RetryPolicy(max_attempts=3)
        executor = RetryExecutor(policy)
        receipt = executor.build_dead_letter_receipt(
            event_id="evt-dl-tc",
            delivery_plan_id="plan-dl-tc",
            target_adapter="adapter-dl-tc",
            previous_receipt_id=None,
            attempt_number=4,
            error="Retry exhausted",
            target_channel="!room:dl",
        )
        assert receipt.target_channel == "!room:dl"

    def test_build_dead_letter_receipt_has_retry_policy_fields(self) -> None:
        """build_dead_letter_receipt includes retry policy fields."""
        policy = RetryPolicy(
            max_attempts=5,
            backoff_base=3.0,
            max_delay_seconds=120.0,
            jitter=True,
        )
        executor = RetryExecutor(policy)
        receipt = executor.build_dead_letter_receipt(
            event_id="evt-dl-pol",
            delivery_plan_id="plan-dl-pol",
            target_adapter="adapter-dl-pol",
            previous_receipt_id=None,
            attempt_number=6,
            error="Retry exhausted after 5 attempts",
        )
        assert receipt.retry_max_attempts is not None
        assert receipt.retry_backoff_base is not None
        assert receipt.retry_max_attempts == 5
        assert receipt.retry_backoff_base == 3.0

    def test_build_retry_receipt_source_docstring(self) -> None:
        """build_retry_receipt docstring mentions live, retry, and replay."""
        doc = RetryExecutor.build_retry_receipt.__doc__
        assert doc is not None
        assert '"live"' in doc
        assert '"retry"' in doc
        assert '"replay"' in doc


# ===================================================================
# DeliveryOutcome with failure_kind
# ===================================================================


class TestDeliveryOutcomeWithFailureKind:
    """DeliveryOutcome includes failure_kind from the taxonomy."""

    def test_success_outcome_no_failure_kind(self) -> None:
        outcome = DeliveryOutcome(
            event_id="e1",
            target_adapter="a",
            target_channel=None,
            route_id="r1",
            delivery_plan_id="p1",
            status="success",
            failure_kind=None,
        )
        assert outcome.failure_kind is None
        assert outcome.status == "success"

    def test_transient_failure_with_failure_kind(self) -> None:
        outcome = DeliveryOutcome(
            event_id="e2",
            target_adapter="a",
            target_channel=None,
            route_id="r2",
            delivery_plan_id="p2",
            status="transient_failure",
            failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT,
            error="TimeoutError: timed out",
        )
        assert outcome.failure_kind is DeliveryFailureKind.ADAPTER_TRANSIENT
        assert outcome.failure_kind.is_retryable is True

    def test_permanent_failure_with_failure_kind(self) -> None:
        outcome = DeliveryOutcome(
            event_id="e3",
            target_adapter="a",
            target_channel=None,
            route_id="r3",
            delivery_plan_id="p3",
            status="permanent_failure",
            failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
            error="ValueError: malformed",
        )
        assert outcome.failure_kind is DeliveryFailureKind.ADAPTER_PERMANENT
        assert outcome.failure_kind.is_retryable is False

    def test_planner_failure_kind(self) -> None:
        outcome = DeliveryOutcome(
            event_id="e4",
            target_adapter="",
            target_channel=None,
            route_id="",
            delivery_plan_id="",
            status="permanent_failure",
            failure_kind=DeliveryFailureKind.PLANNER_FAILURE,
            error="Planner error: RuntimeError",
        )
        assert outcome.failure_kind is DeliveryFailureKind.PLANNER_FAILURE

    def test_renderer_failure_kind(self) -> None:
        outcome = DeliveryOutcome(
            event_id="e5",
            target_adapter="a",
            target_channel=None,
            route_id="r5",
            delivery_plan_id="p5",
            status="permanent_failure",
            failure_kind=DeliveryFailureKind.RENDERER_FAILURE,
            error="Rendering failed: no renderer",
        )
        assert outcome.failure_kind is DeliveryFailureKind.RENDERER_FAILURE

    def test_policy_suppressed_failure_kind(self) -> None:
        """POLICY_SUPPRESSED is a permanent, non-retryable failure kind."""
        outcome = DeliveryOutcome(
            event_id="e-ps",
            target_adapter="a",
            target_channel=None,
            route_id="r-ps",
            delivery_plan_id="p-ps",
            status="permanent_failure",
            failure_kind=DeliveryFailureKind.POLICY_SUPPRESSED,
            error="route policy denied for target",
        )
        assert outcome.failure_kind is DeliveryFailureKind.POLICY_SUPPRESSED
        assert outcome.failure_kind.is_retryable is False
        assert outcome.failure_kind.value == "policy_suppressed"

    def test_positional_args_without_failure_kind(self) -> None:
        """DeliveryOutcome works with positional args (no failure_kind)."""
        outcome = DeliveryOutcome("e6", "a", None, "r6", "p6", "skipped")
        assert outcome.status == "skipped"
        assert outcome.failure_kind is None
        assert outcome.receipt is None
        assert outcome.error is None
        assert outcome.duration_ms == 0.0


# ===================================================================
# Track 6: Retry/dead-letter observability and deterministic outcomes
# ===================================================================


class TestRetryDeadLetterObservability:
    """Verify observability snapshots across retry exhaustion and dead-letter."""

    def test_full_retry_exhaustion_flow_with_snapshots(self) -> None:
        """Simulate 3-attempt retry exhaustion with Diagnostician snapshots."""
        policy = RetryPolicy(max_attempts=3, jitter=False)
        executor = RetryExecutor(policy)
        diag = Diagnostician()

        # Attempt 1 fails
        assert executor.is_exhausted(1) is False
        r1 = executor.build_retry_receipt(
            event_id="evt-obs-1",
            delivery_plan_id="plan-obs",
            target_adapter="adapter-obs",
            previous_receipt_id=None,
            attempt_number=1,
            error="ConnectionError: timeout",
        )
        diag.record_adapter_failure(
            "evt-obs-1", "adapter-obs", "ConnectionError: timeout"
        )

        # Attempt 2 fails
        assert executor.is_exhausted(2) is False
        r2 = executor.build_retry_receipt(
            event_id="evt-obs-1",
            delivery_plan_id="plan-obs",
            target_adapter="adapter-obs",
            previous_receipt_id=r1.receipt_id,
            attempt_number=2,
            error="ConnectionError: timeout",
        )
        diag.record_adapter_failure(
            "evt-obs-1", "adapter-obs", "ConnectionError: timeout"
        )

        # Attempt 3 fails — exhausted
        assert executor.is_exhausted(3) is True

        # Dead letter
        dl = executor.build_dead_letter_receipt(
            event_id="evt-obs-1",
            delivery_plan_id="plan-obs",
            target_adapter="adapter-obs",
            previous_receipt_id=r2.receipt_id,
            attempt_number=4,
            error="Retry exhausted after 3 attempts",
        )
        assert dl.status == "dead_lettered"
        assert dl.parent_receipt_id == r2.receipt_id
        assert dl.next_retry_at is None

        # Verify receipt lineage chain
        assert r1.parent_receipt_id is None
        assert r2.parent_receipt_id == r1.receipt_id
        assert dl.parent_receipt_id == r2.receipt_id

        # Observability snapshot
        snap = diag.snapshot()
        assert (
            snap["adapter_failures"]["adapter-obs"] == 2
        )  # Only 2 recorded (not at attempt 3)
        assert snap["planner_failures"] == {}
        assert snap["renderer_failures"] == {}

    def test_retry_receipt_backoff_chain_deterministic(self) -> None:
        """Retry receipt chain with jitter=False produces exact backoff times."""
        policy = RetryPolicy(
            max_attempts=5, backoff_base=2.0, jitter=False, max_delay_seconds=60.0
        )
        executor = RetryExecutor(policy)

        receipts = []
        prev_id = None
        for attempt in range(1, 6):
            r = executor.build_retry_receipt(
                event_id="evt-chain",
                delivery_plan_id="plan-chain",
                target_adapter="chain-target",
                previous_receipt_id=prev_id,
                attempt_number=attempt,
                error="ConnectionError",
            )
            receipts.append(r)
            prev_id = r.receipt_id

        # Verify chain
        assert receipts[0].parent_receipt_id is None
        for i in range(1, 5):
            assert receipts[i].parent_receipt_id == receipts[i - 1].receipt_id

        # Verify backoff progression: base * 2^(attempt-1)
        # attempt 1: 2.0, attempt 2: 4.0, attempt 3: 8.0, attempt 4: 16.0, attempt 5: 32.0
        for i, r in enumerate(receipts):
            expected_backoff = 2.0 * (2**i)
            actual_backoff = (r.next_retry_at - r.created_at).total_seconds()
            assert (
                abs(actual_backoff - expected_backoff) < 0.01
            ), f"Attempt {i+1}: expected backoff {expected_backoff}s, got {actual_backoff}s"

    def test_dead_letter_receipt_observability(self) -> None:
        """Dead-letter receipt flow produces complete observability trail."""
        policy = RetryPolicy(max_attempts=2, jitter=False)
        executor = RetryExecutor(policy)
        diag = Diagnostician()

        # Attempt 1
        r1 = executor.build_retry_receipt(
            event_id="evt-dl-obs",
            delivery_plan_id="plan-dl",
            target_adapter="dl-target",
            previous_receipt_id=None,
            attempt_number=1,
            error="ConnectionError: refused",
        )
        diag.record_adapter_failure(
            "evt-dl-obs", "dl-target", "ConnectionError: refused"
        )

        # Attempt 2 — still under max
        assert executor.is_exhausted(2) is True

        # Dead letter
        dl = executor.build_dead_letter_receipt(
            event_id="evt-dl-obs",
            delivery_plan_id="plan-dl",
            target_adapter="dl-target",
            previous_receipt_id=r1.receipt_id,
            attempt_number=3,
            error="Retry exhausted after 2 attempts",
        )

        # Verify dead-letter receipt properties
        assert dl.status == "dead_lettered"
        assert dl.attempt_number == 3
        assert dl.next_retry_at is None
        assert "exhausted" in (dl.error or "")

        # Snapshot: only the intermediate failures recorded, not the dead letter itself
        snap = diag.snapshot()
        assert snap["adapter_failures"]["dl-target"] == 1

    def test_classify_failure_coverage(self) -> None:
        """All failure kinds produce correct retryability classification."""
        # ADAPTER_TRANSIENT is retryable
        assert (
            RetryExecutor.classify_failure(
                ConnectionError("net"),
                adapter_registered=True,
            )
            is DeliveryFailureKind.ADAPTER_TRANSIENT
        )

        # PLANNER_FAILURE
        assert (
            RetryExecutor.classify_failure(
                RuntimeError("x"),
                planner_failed=True,
            )
            is DeliveryFailureKind.PLANNER_FAILURE
        )

        # RENDERER_FAILURE
        assert (
            RetryExecutor.classify_failure(
                RuntimeError("x"),
                renderer_failed=True,
            )
            is DeliveryFailureKind.RENDERER_FAILURE
        )

        # ADAPTER_MISSING
        assert (
            RetryExecutor.classify_failure(
                RuntimeError("x"),
                adapter_registered=False,
            )
            is DeliveryFailureKind.ADAPTER_MISSING
        )

        # DEADLINE_EXCEEDED
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        assert (
            RetryExecutor.classify_failure(
                TimeoutError("x"),
                deadline=past,
            )
            is DeliveryFailureKind.DEADLINE_EXCEEDED
        )

        # ADAPTER_PERMANENT for non-transient
        assert (
            RetryExecutor.classify_failure(
                RuntimeError("business error"),
            )
            is DeliveryFailureKind.ADAPTER_PERMANENT
        )


# ===================================================================
# RouteStats / RouteCounters
# ===================================================================


class TestRouteCounters:
    """RouteCounters frozen dataclass."""

    def test_default_zeros(self) -> None:
        c = RouteCounters()
        assert c.delivered == 0
        assert c.failed == 0
        assert c.skipped == 0
        assert c.loop_prevented == 0
        assert c.policy_suppressed == 0

    def test_custom_values(self) -> None:
        c = RouteCounters(
            delivered=5, failed=1, skipped=2, loop_prevented=3, policy_suppressed=4
        )
        assert c.delivered == 5
        assert c.loop_prevented == 3
        assert c.policy_suppressed == 4

    def test_frozen(self) -> None:
        c = RouteCounters()
        with pytest.raises((TypeError, AttributeError)):
            c.delivered = 99  # type: ignore[misc]


class TestRouteStats:
    """RouteStats: recording, snapshot determinism."""

    def test_record_delivered(self) -> None:
        stats = RouteStats()
        stats.record_delivered("r1")
        stats.record_delivered("r1")
        stats.record_delivered("r2")
        snap = stats.snapshot()
        assert snap["r1"]["delivered"] == 2
        assert snap["r2"]["delivered"] == 1

    def test_record_failed(self) -> None:
        stats = RouteStats()
        stats.record_failed("r1", "timeout")
        snap = stats.snapshot()
        assert snap["r1"]["failed"] == 1
        assert snap["r1"]["last_error"] == "timeout"

    def test_record_skipped(self) -> None:
        stats = RouteStats()
        stats.record_skipped("r1")
        snap = stats.snapshot()
        assert snap["r1"]["skipped"] == 1

    def test_record_loop_prevented(self) -> None:
        stats = RouteStats()
        stats.record_loop_prevented("r1")
        stats.record_loop_prevented("r1")
        snap = stats.snapshot()
        assert snap["r1"]["loop_prevented"] == 2

    def test_record_policy_suppressed(self) -> None:
        stats = RouteStats()
        stats.record_policy_suppressed("r1")
        stats.record_policy_suppressed("r1")
        stats.record_policy_suppressed("r2")
        snap = stats.snapshot()
        assert snap["r1"]["policy_suppressed"] == 2
        assert snap["r2"]["policy_suppressed"] == 1
        # Other counters untouched
        assert snap["r1"]["delivered"] == 0
        assert snap["r1"]["failed"] == 0
        assert snap["r1"]["loop_prevented"] == 0

    def test_snapshot_deterministic_order(self) -> None:
        """Snapshot keys are sorted alphabetically."""
        stats = RouteStats()
        stats.record_delivered("zebra")
        stats.record_delivered("alpha")
        stats.record_delivered("mid")
        snap = stats.snapshot()
        assert list(snap.keys()) == ["alpha", "mid", "zebra"]

    def test_snapshot_empty(self) -> None:
        stats = RouteStats()
        assert stats.snapshot() == {}

    def test_snapshot_no_last_error_when_none(self) -> None:
        """No last_error key when only successes recorded."""
        stats = RouteStats()
        stats.record_delivered("r1")
        snap = stats.snapshot()
        assert "last_error" not in snap["r1"]

    def test_counters_independent_per_route(self) -> None:
        """Each route has independent counters."""
        stats = RouteStats()
        stats.record_delivered("r1")
        stats.record_failed("r2", "err")
        stats.record_loop_prevented("r3")
        snap = stats.snapshot()
        assert snap["r1"] == {
            "delivered": 1,
            "failed": 0,
            "skipped": 0,
            "loop_prevented": 0,
            "policy_suppressed": 0,
            "capability_suppressed": 0,
        }
        assert snap["r2"]["failed"] == 1
        assert snap["r3"]["loop_prevented"] == 1


# ===================================================================
# Policy-suppressed failure kind: classification and reporting
# ===================================================================


class TestPolicySuppressedClassification:
    """Verify policy_suppressed classification and reporting."""

    def test_infer_failure_kind_policy_suppressed_text(self) -> None:
        """Error text containing 'policy_suppressed' infers policy_suppressed."""
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("policy_suppressed for target", "failed")
            == "policy_suppressed"
        )

    def test_infer_failure_kind_route_policy_denied(self) -> None:
        """Error text containing 'route policy denied' infers policy_suppressed."""
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("route policy denied for target", "failed")
            == "policy_suppressed"
        )

    def test_infer_failure_kind_case_insensitive(self) -> None:
        """Inference is case-insensitive."""
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("Route Policy Denied", "failed") == "policy_suppressed"
        )

    def test_failure_category_permanent(self) -> None:
        """policy_suppressed maps to the 'permanent' category."""
        from medre.core.observability.classification import failure_category

        assert failure_category("policy_suppressed") == "permanent"

    def test_failure_category_not_retryable(self) -> None:
        """policy_suppressed is not in RETRYABLE_KINDS."""
        from medre.core.observability.classification import RETRYABLE_KINDS

        assert "policy_suppressed" not in RETRYABLE_KINDS

    def test_failure_category_not_operational(self) -> None:
        """policy_suppressed is not in OPERATIONAL_KINDS."""
        from medre.core.observability.classification import OPERATIONAL_KINDS

        assert "policy_suppressed" not in OPERATIONAL_KINDS

    def test_derive_failure_kind_detail_policy_suppressed(self) -> None:
        """_derive_failure_kind_detail returns policy_suppressed from route policy denied error."""
        from medre.runtime.reporting import _derive_failure_kind_detail

        assert (
            _derive_failure_kind_detail("policy_suppressed", "route policy denied")
            == "policy_suppressed"
        )

    def test_derive_failure_kind_detail_generic_error(self) -> None:
        """_derive_failure_kind_detail returns policy_suppressed when only failure_kind matches."""
        from medre.runtime.reporting import _derive_failure_kind_detail

        assert (
            _derive_failure_kind_detail("policy_suppressed", "some other error")
            == "policy_suppressed"
        )

    def test_derive_failure_kind_detail_none_kind(self) -> None:
        """_derive_failure_kind_detail returns None when failure_kind is None."""
        from medre.runtime.reporting import _derive_failure_kind_detail

        assert _derive_failure_kind_detail(None, "route policy denied") is None

    def test_reporting_dict_policy_suppressed_not_retryable(self) -> None:
        """delivery_receipt_to_report_dict marks policy_suppressed as not retryable."""
        from medre.core.events.canonical import DeliveryReceipt
        from medre.runtime.reporting import delivery_receipt_to_report_dict

        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id="rcpt-test",
            event_id="e1",
            delivery_plan_id="p1",
            target_adapter="a",
            target_channel=None,
            status="suppressed",
            error="route policy denied for target",
            next_retry_at=None,
            created_at=datetime.now(timezone.utc),
            attempt_number=1,
            parent_receipt_id=None,
            source="live",
            replay_run_id=None,
            failure_kind="policy_suppressed",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["retryable"] is False
        assert report["failure_kind"] == "policy_suppressed"
        assert report["failure_kind_detail"] == "policy_suppressed"
