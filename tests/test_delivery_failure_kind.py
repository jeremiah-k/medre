"""Delivery failure taxonomy and classification tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from medre.core.planning.delivery_plan import DeliveryFailureKind, RetryExecutor


def test_all_delivery_failure_kinds_exist() -> None:
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
        "OUTBOX_NOT_OWNED",
        "REPLAY_DUPLICATE_SUPPRESSED",
    }

    assert {member.name for member in DeliveryFailureKind} == expected


def test_only_adapter_transient_is_retryable() -> None:
    assert DeliveryFailureKind.ADAPTER_TRANSIENT.is_retryable is True
    for kind in DeliveryFailureKind:
        if kind is not DeliveryFailureKind.ADAPTER_TRANSIENT:
            assert kind.is_retryable is False, f"{kind.name} should not be retryable"


def test_delivery_failure_kind_values_are_strings() -> None:
    assert DeliveryFailureKind.PLANNER_FAILURE.value == "planner_failure"
    assert DeliveryFailureKind.RENDERER_FAILURE.value == "renderer_failure"
    assert DeliveryFailureKind.ADAPTER_TRANSIENT.value == "adapter_transient"
    assert DeliveryFailureKind.ADAPTER_PERMANENT.value == "adapter_permanent"
    assert DeliveryFailureKind.ADAPTER_MISSING.value == "adapter_missing"
    assert DeliveryFailureKind.DEADLINE_EXCEEDED.value == "deadline_exceeded"
    assert DeliveryFailureKind.POLICY_SUPPRESSED.value == "policy_suppressed"


def test_transient_errors_classify_as_adapter_transient() -> None:
    transient_errors = [
        TimeoutError("timed out"),
        ConnectionError("refused"),
        ConnectionRefusedError("refused"),
        ConnectionResetError("reset"),
        ConnectionAbortedError("aborted"),
        BrokenPipeError("broken"),
        OSError("os error"),
    ]

    for error in transient_errors:
        assert RetryExecutor.classify_failure(error) is (
            DeliveryFailureKind.ADAPTER_TRANSIENT
        )


def test_other_errors_classify_as_adapter_permanent() -> None:
    assert RetryExecutor.classify_failure(RuntimeError("business logic error")) is (
        DeliveryFailureKind.ADAPTER_PERMANENT
    )


def test_planner_failure_classification_takes_precedence() -> None:
    assert (
        RetryExecutor.classify_failure(RuntimeError("x"), planner_failed=True)
        is DeliveryFailureKind.PLANNER_FAILURE
    )


def test_renderer_failure_classification_takes_precedence() -> None:
    assert (
        RetryExecutor.classify_failure(RuntimeError("x"), renderer_failed=True)
        is DeliveryFailureKind.RENDERER_FAILURE
    )


def test_unregistered_adapter_classifies_as_missing() -> None:
    assert (
        RetryExecutor.classify_failure(RuntimeError("x"), adapter_registered=False)
        is DeliveryFailureKind.ADAPTER_MISSING
    )


def test_expired_deadline_classifies_as_deadline_exceeded() -> None:
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert (
        RetryExecutor.classify_failure(RuntimeError("x"), deadline=past)
        is DeliveryFailureKind.DEADLINE_EXCEEDED
    )


def test_missing_deadline_does_not_classify_as_deadline_exceeded() -> None:
    assert (
        RetryExecutor.classify_failure(TimeoutError("timeout"), deadline=None)
        is DeliveryFailureKind.ADAPTER_TRANSIENT
    )
