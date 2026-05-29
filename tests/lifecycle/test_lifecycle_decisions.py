"""Pure sync unit tests for DeliveryLifecycleService decision logic.

Exercises classification, retry determination, dead-letter checks,
attempt context, retry field extraction, and terminal-state identification.
No I/O — all tests are synchronous and need no storage fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.contracts.adapter import (
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    RetryPolicy,
)

from .conftest import _make_lifecycle, _make_plan, _make_receipt

# ===================================================================
# Attempt context computation
# ===================================================================


class TestComputeAttemptContext:
    """Verify attempt_number and parent_receipt_id from previous_receipt."""

    def test_first_attempt(self) -> None:
        """No previous receipt → attempt=1, parent=None."""
        lifecycle = _make_lifecycle()
        attempt, parent = lifecycle.compute_attempt_context(None)
        assert attempt == 1
        assert parent is None

    def test_retry_attempt_increments(self) -> None:
        """Previous receipt attempt=2 → attempt=3, parent=previous id."""
        lifecycle = _make_lifecycle()
        prev = _make_receipt(receipt_id="rcpt-prev", attempt_number=2)
        attempt, parent = lifecycle.compute_attempt_context(prev)
        assert attempt == 3
        assert parent == "rcpt-prev"

    def test_attempt_number_one_from_first_receipt(self) -> None:
        """Previous receipt attempt=1 → attempt=2."""
        lifecycle = _make_lifecycle()
        prev = _make_receipt(receipt_id="rcpt-first", attempt_number=1)
        attempt, parent = lifecycle.compute_attempt_context(prev)
        assert attempt == 2
        assert parent == "rcpt-first"


# ===================================================================
# Retry field extraction
# ===================================================================


class TestExtractRetryFields:
    """Verify retry policy field extraction from delivery plan."""

    def test_no_retry_policy(self) -> None:
        """Plan without retry policy → all None."""
        lifecycle = _make_lifecycle()
        plan = _make_plan(retry_policy=None)
        fields = lifecycle.extract_retry_fields(plan)
        assert fields["retry_max_attempts"] is None
        assert fields["retry_backoff_base"] is None
        assert fields["retry_max_delay"] is None
        assert fields["retry_jitter"] is None

    def test_with_retry_policy(self) -> None:
        """Plan with retry policy → fields populated."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(
            max_attempts=5,
            backoff_base=3.0,
            max_delay_seconds=120.0,
            jitter=False,
        )
        plan = _make_plan(retry_policy=policy)
        fields = lifecycle.extract_retry_fields(plan)
        assert fields["retry_max_attempts"] == 5
        assert fields["retry_backoff_base"] == 3.0
        assert fields["retry_max_delay"] == 120.0
        assert fields["retry_jitter"] is False


# ===================================================================
# Failure classification
# ===================================================================


class TestClassifyFailure:
    """Verify RetryExecutor.classify_failure passthrough."""

    def test_transient_error(self) -> None:
        """AdapterSendError(transient=True) → ADAPTER_TRANSIENT."""
        lifecycle = _make_lifecycle()
        kind = lifecycle.classify_failure(
            AdapterSendError("timeout", transient=True),
            adapter_registered=True,
        )
        assert kind == DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_permanent_error(self) -> None:
        """AdapterPermanentError → ADAPTER_PERMANENT."""
        lifecycle = _make_lifecycle()
        kind = lifecycle.classify_failure(
            AdapterPermanentError("malformed"),
            adapter_registered=True,
        )
        assert kind == DeliveryFailureKind.ADAPTER_PERMANENT

    def test_connection_error_transient(self) -> None:
        """ConnectionError → ADAPTER_TRANSIENT."""
        lifecycle = _make_lifecycle()
        kind = lifecycle.classify_failure(
            ConnectionError("refused"),
            adapter_registered=True,
        )
        assert kind == DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_generic_runtime_error_permanent(self) -> None:
        """Generic RuntimeError → ADAPTER_PERMANENT."""
        lifecycle = _make_lifecycle()
        kind = lifecycle.classify_failure(
            RuntimeError("unknown"),
            adapter_registered=True,
        )
        assert kind == DeliveryFailureKind.ADAPTER_PERMANENT


# ===================================================================
# Retryable / permanent classification
# ===================================================================


class TestIsRetryable:
    """Verify is_retryable delegates to DeliveryFailureKind."""

    def test_transient_is_retryable(self) -> None:
        lifecycle = _make_lifecycle()
        assert lifecycle.is_retryable(DeliveryFailureKind.ADAPTER_TRANSIENT) is True

    def test_permanent_not_retryable(self) -> None:
        lifecycle = _make_lifecycle()
        assert lifecycle.is_retryable(DeliveryFailureKind.ADAPTER_PERMANENT) is False

    def test_renderer_failure_not_retryable(self) -> None:
        lifecycle = _make_lifecycle()
        assert lifecycle.is_retryable(DeliveryFailureKind.RENDERER_FAILURE) is False


# ===================================================================
# Dead-letter determination
# ===================================================================


class TestShouldDeadLetter:
    """Verify dead-letter transition logic."""

    def test_failed_exhausted_policy(self) -> None:
        """Failed + exhausted policy → dead-letter."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1)
        plan = _make_plan(retry_policy=policy)
        assert lifecycle.should_dead_letter("failed", plan, 1) is True

    def test_failed_with_retries_remaining(self) -> None:
        """Failed + retries remaining → no dead-letter."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3)
        plan = _make_plan(retry_policy=policy)
        assert lifecycle.should_dead_letter("failed", plan, 1) is False

    def test_sent_no_dead_letter(self) -> None:
        """Sent status → no dead-letter regardless of policy."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1)
        plan = _make_plan(retry_policy=policy)
        assert lifecycle.should_dead_letter("sent", plan, 1) is False

    def test_failed_no_policy(self) -> None:
        """Failed + no retry policy → no dead-letter."""
        lifecycle = _make_lifecycle()
        plan = _make_plan(retry_policy=None)
        assert lifecycle.should_dead_letter("failed", plan, 1) is False

    def test_failed_exhausted_at_max(self) -> None:
        """Failed at max_attempts → dead-letter."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3)
        plan = _make_plan(retry_policy=policy)
        assert lifecycle.should_dead_letter("failed", plan, 3) is True


# ===================================================================
# Next retry time computation
# ===================================================================


class TestComputeNextRetryAt:
    """Verify next_retry_at calculation for retryable transient failures."""

    def test_retryable_transient_returns_time(self) -> None:
        """Transient failure with policy and retries remaining → next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            1,
            now,
        )
        assert result is not None
        assert result > now

    def test_permanent_failure_no_retry_time(self) -> None:
        """Permanent failure -> no next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_PERMANENT,
            plan,
            1,
            now,
        )
        assert result is None

    def test_exhausted_no_retry_time(self) -> None:
        """Exhausted retry -> no next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1, backoff_base=1.0)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            1,
            now,
        )
        assert result is None

    def test_no_policy_no_retry_time(self) -> None:
        """No retry policy -> no next_retry_at."""
        lifecycle = _make_lifecycle()
        plan = _make_plan(retry_policy=None)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            1,
            now,
        )
        assert result is None

    def test_sent_no_retry_time(self) -> None:
        """Sent status → no next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        result = lifecycle.compute_next_retry_at(
            "sent",
            None,
            plan,
            1,
            now,
        )
        assert result is None

    def test_backoff_increases_with_attempts(self) -> None:
        """Higher attempt numbers produce later next_retry_at."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=5, backoff_base=1.0, jitter=False)
        plan = _make_plan(retry_policy=policy)
        now = datetime.now(tz=timezone.utc)

        r1 = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            1,
            now,
        )
        r2 = lifecycle.compute_next_retry_at(
            "failed",
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            plan,
            2,
            now,
        )
        assert r1 is not None
        assert r2 is not None
        # Attempt 2 backoff (2s) > attempt 1 backoff (1s).
        assert r2 > r1


# ===================================================================
# Terminal-state determination
# ===================================================================


class TestIsTerminalOutboxStatus:
    """Verify terminal outbox status identification."""

    def test_sent_is_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("sent") is True

    def test_dead_lettered_is_terminal(self) -> None:
        assert (
            DeliveryLifecycleService.is_terminal_outbox_status("dead_lettered") is True
        )

    def test_cancelled_is_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("cancelled") is True

    def test_abandoned_is_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("abandoned") is True

    def test_pending_is_not_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("pending") is False

    def test_in_progress_is_not_terminal(self) -> None:
        assert (
            DeliveryLifecycleService.is_terminal_outbox_status("in_progress") is False
        )

    def test_retry_wait_is_not_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("retry_wait") is False

    def test_queued_is_not_terminal(self) -> None:
        assert DeliveryLifecycleService.is_terminal_outbox_status("queued") is False
