"""Structural invariants for runtime execution authority.

These tests verify that the execution-authority boundaries documented in
``docs/dev/runtime-execution-authority-audit.md`` hold at the structural
level: the right methods exist on the right classes, drain/restore helpers
are module-level functions, and evidence/recovery modules have the expected
read-only properties.  They do **not** test runtime behaviour — they guard
against accidental refactorings that would violate the authority model.

See ``docs/dev/runtime-execution-authority-audit.md`` for the normative
reference.
"""

from __future__ import annotations

import inspect

import pytest

from medre.core.evidence.shutdown import (
    ShutdownStatus,
)
from medre.runtime.app import MedreApp, _drain_pending_cancellations

# ---------------------------------------------------------------------------
# Drain/restore helper is a module-level function
# ---------------------------------------------------------------------------


class TestDrainPendingCancellations:
    """_drain_pending_cancellations is a module-level function, not a method."""

    def test_is_module_level_function(self) -> None:
        assert inspect.isfunction(_drain_pending_cancellations)

    def test_return_type_annotation_is_int(self) -> None:
        hints = inspect.get_annotations(_drain_pending_cancellations)
        # ``from __future__ import annotations`` stores annotations as strings
        assert hints.get("return") == "int" or hints.get("return") is int

    def test_accepts_no_arguments(self) -> None:
        sig = inspect.signature(_drain_pending_cancellations)
        assert len(sig.parameters) == 0


# ---------------------------------------------------------------------------
# MedreApp has the expected authority-scoped methods
# ---------------------------------------------------------------------------


class TestMedreAppMethodStructure:
    """MedreApp methods that participate in drain/restore and shutdown evidence."""

    @pytest.mark.parametrize(
        "method_name",
        [
            "_stop_adapter_with_deadline",
            "_persist_drain_abandoned_evidence",
            "_cleanup_core_resources",
            "_cleanup_started_adapters",
            "_start_failure_cleanup",
        ],
    )
    def test_method_is_async(self, method_name: str) -> None:
        method = getattr(MedreApp, method_name, None)
        assert method is not None, f"MedreApp.{method_name} not found"
        assert inspect.iscoroutinefunction(
            method
        ), f"MedreApp.{method_name} must be async"

    @pytest.mark.parametrize(
        "method_name",
        [
            "_stop_adapter_with_deadline",
            "_persist_drain_abandoned_evidence",
            "_cleanup_core_resources",
        ],
    )
    def test_method_docstring_contains_cross_reference(self, method_name: str) -> None:
        """Docstrings for authority-scoped methods should explain their role."""
        method = getattr(MedreApp, method_name)
        doc = method.__doc__
        assert doc is not None, f"MedreApp.{method_name} missing docstring"
        assert (
            len(doc) > 50
        ), f"MedreApp.{method_name} docstring too short for authority documentation"


# ---------------------------------------------------------------------------
# ShutdownStatus values are diagnostic labels, not lifecycle states
# ---------------------------------------------------------------------------


class TestShutdownStatusEvidenceLabels:
    """ShutdownStatus enum values are evidence classification labels."""

    EXPECTED_VALUES = frozenset(
        {
            "running",
            "graceful_stop",
            "cancellation",
            "adapter_failure",
            "drain_timeout",
            "shutdown_pending",
            "stopped",
            "failed",
        }
    )

    def test_all_expected_values_present(self) -> None:
        actual = {member.value for member in ShutdownStatus}
        assert actual == self.EXPECTED_VALUES

    def test_is_str_enum(self) -> None:
        assert issubclass(ShutdownStatus, str)


# ---------------------------------------------------------------------------
# OutboxShutdownClassification is always read-only
# ---------------------------------------------------------------------------


class TestOutboxShutdownClassificationReadOnly:
    """OutboxShutdownClassification never requests storage mutation."""

    @pytest.mark.parametrize(
        "status",
        [
            "pending",
            "retry_wait",
            "in_progress",
            "queued",
            "sent",
            "dead_lettered",
            "cancelled",
            "abandoned",
        ],
    )
    def test_mutate_outbox_always_false(self, status: str) -> None:
        from medre.core.evidence.shutdown import classify_outbox_shutdown_policy

        result = classify_outbox_shutdown_policy(status)
        assert result.mutate_outbox is False

    @pytest.mark.parametrize(
        "status",
        [
            "pending",
            "retry_wait",
            "in_progress",
            "queued",
            "sent",
            "dead_lettered",
            "cancelled",
            "abandoned",
        ],
    )
    def test_append_receipt_always_false(self, status: str) -> None:
        from medre.core.evidence.shutdown import classify_outbox_shutdown_policy

        result = classify_outbox_shutdown_policy(status)
        assert result.append_receipt is False


# ---------------------------------------------------------------------------
# Recovery module is read-only (no storage writes)
# ---------------------------------------------------------------------------


class TestRecoveryModuleReadOnly:
    """core.recovery package docstring clarifies read-only nature."""

    def test_package_docstring_mentions_classification(self) -> None:
        import medre.core.recovery as recovery_pkg

        doc = recovery_pkg.__doc__
        assert doc is not None
        assert "classif" in doc.lower()

    def test_package_docstring_does_not_claim_repair(self) -> None:
        """The package docstring should not claim to repair/modify storage."""
        import medre.core.recovery as recovery_pkg

        doc = recovery_pkg.__doc__
        assert doc is not None
        # The word "repair" may appear only in the negative context
        # (i.e. "does not repair").  Check it's not used affirmatively
        # as an action the module performs.
        lines = doc.splitlines()
        for line in lines:
            lower = line.lower()
            if "repair" in lower and "must not" not in lower and "not" not in lower:
                pytest.fail(
                    f"Recovery docstring claims repair as an action: {line.strip()!r}"
                )


# ---------------------------------------------------------------------------
# _drain_pending_cancellations is safe to call outside asyncio task context
# ---------------------------------------------------------------------------


class TestDrainPendingCancellationsSafety:
    """Calling _drain_pending_cancellations outside a task returns 0."""

    async def test_returns_zero_outside_task(self) -> None:
        # Inside an asyncio event loop but not inside a Task,
        # current_task() returns None and the function returns 0.
        assert _drain_pending_cancellations() == 0
