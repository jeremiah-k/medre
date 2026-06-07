"""Persistence authority tests: recovery.

Focused tests proving the recovery authority model where gaps exist from
Waves 1–2, without duplicating existing near-limit test files.

Covers:
  1. Recovery builders are pure functions — they do not accept storage
     objects and cannot write to storage.
  2. Recovery classification produces labels, not persisted state.
  3. Recovery diagnostics never create success receipts or delete/mutate
     evidence.
  4. recover CLI uses open_readonly — no writes possible.
"""

from __future__ import annotations

import inspect

import pytest

from medre.core.recovery.builder import (
    build_recovery_summary,
    build_startup_recovery_ledger,
)
from medre.core.recovery.classification import classify_startup_reclamation
from medre.core.recovery.models import (
    RecoveryOwnershipAction,
    RecoveryOwnershipStatus,
    RecoverySummary,
    StartupRecoveryLedger,
)
from medre.core.recovery.recovery_source import RecoverySource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    outbox_id: str = "ob-1",
    status: str = "pending",
    event_id: str = "ev-1",
    delivery_plan_id: str = "plan-1",
    next_attempt_at: str | None = None,
    lease_until: str | None = None,
    updated_at: str | None = None,
    worker_id: str | None = None,
) -> dict[str, str | None]:
    return {
        "outbox_id": outbox_id,
        "status": status,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "next_attempt_at": next_attempt_at,
        "lease_until": lease_until,
        "updated_at": updated_at,
        "worker_id": worker_id,
    }


def _get_function_param_names(func: object) -> list[str]:
    """Get parameter names of a function, excluding 'self'."""
    if not callable(func):
        return []
    sig = inspect.signature(func)  # type: ignore[arg-type]
    return [name for name in sig.parameters if name != "self"]


# ===================================================================
# 1. Recovery builders are pure functions
# ===================================================================


class TestRecoveryBuilderPurity:
    """Recovery builders do not accept storage objects and cannot write
    to storage.

    build_startup_recovery_ledger and build_recovery_summary are pure
    functions over pre-fetched data. They must not accept storage objects.
    """

    def test_build_startup_recovery_ledger_has_no_storage_param(self) -> None:
        """build_startup_recovery_ledger does not accept a storage parameter."""
        params = _get_function_param_names(build_startup_recovery_ledger)
        storage_like = [
            p for p in params if "storage" in p.lower() or "db" in p.lower()
        ]
        assert (
            storage_like == []
        ), f"build_startup_recovery_ledger must not accept storage-like params: {storage_like}"

    def test_build_recovery_summary_has_no_storage_param(self) -> None:
        """build_recovery_summary does not accept a storage parameter."""
        params = _get_function_param_names(build_recovery_summary)
        storage_like = [
            p for p in params if "storage" in p.lower() or "db" in p.lower()
        ]
        assert (
            storage_like == []
        ), f"build_recovery_summary must not accept storage-like params: {storage_like}"

    def test_classify_startup_reclamation_has_no_storage_param(self) -> None:
        """classify_startup_reclamation does not accept a storage parameter."""
        params = _get_function_param_names(classify_startup_reclamation)
        storage_like = [
            p for p in params if "storage" in p.lower() or "db" in p.lower()
        ]
        assert (
            storage_like == []
        ), f"classify_startup_reclamation must not accept storage-like params: {storage_like}"

    def test_builders_return_frozen_data(self) -> None:
        """Builder output types are frozen/immutable dataclasses."""
        import dataclasses

        for cls in (RecoveryOwnershipAction, StartupRecoveryLedger, RecoverySummary):
            assert dataclasses.is_dataclass(cls)
            assert cls.__dataclass_params__.frozen, f"{cls.__name__} must be frozen"


# ===================================================================
# 2. Recovery classification produces labels, not persisted state
# ===================================================================


class TestRecoveryClassificationIsLabels:
    """Recovery status labels are diagnostic classifications, not persisted state.

    RecoveryOwnershipStatus values are enum labels used in recovery ledgers.
    They are never written to storage.
    """

    @pytest.mark.parametrize(
        "status",
        list(RecoveryOwnershipStatus),
    )
    def test_recovery_status_is_string_enum(
        self, status: RecoveryOwnershipStatus
    ) -> None:
        """Each RecoveryOwnershipStatus is a string enum value."""
        assert isinstance(status, str)
        assert isinstance(status, RecoveryOwnershipStatus)

    def test_no_sent_status_in_recovery_ownership(self) -> None:
        """RecoveryOwnershipStatus does not contain a 'sent' status.

        Recovery never fabricates a delivery success.
        """
        member_values = {m.value for m in RecoveryOwnershipStatus}
        assert "sent" not in member_values
        assert "delivered" not in member_values


# ===================================================================
# 3. Recovery diagnostics never create success receipts
# ===================================================================


class TestRecoveryNoSuccessReceipts:
    """Recovery diagnostics do not create success receipts or mutate evidence.

    build_startup_recovery_ledger produces a StartupRecoveryLedger with
    RecoveryOwnershipAction entries. Each action is a diagnostic
    classification, not a delivery outcome. No receipts are created.
    """

    def test_recovery_action_is_diagnostic(self) -> None:
        """RecoveryOwnershipAction records diagnostic classifications."""
        action = RecoveryOwnershipAction(
            recovery_run_id="run-1",
            startup_timestamp="2026-01-01T00:00:00+00:00",
            outbox_id="ob-test",
            prior_status="pending",
            observed_status="pending",
            ownership_action=RecoveryOwnershipStatus.RECOVERABLE.value,
            reason="immediately_claimable",
            worker_identity=None,
            recovery_source=RecoverySource.SNAPSHOT_DIAGNOSTICS.value,
            timestamp="2026-01-01T00:00:00+00:00",
            delivery_plan_id="plan-test",
            event_id="evt-test",
        )
        # It is a dataclass with no receipt_id field
        assert not hasattr(action, "receipt_id")
        assert not hasattr(action, "delivery_status")

    def test_terminal_items_are_unrecoverable(self) -> None:
        """Terminal items (sent, dead_lettered) classified as terminal."""
        from datetime import datetime, timezone

        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        for terminal_status in ("sent", "dead_lettered"):
            item = _make_item(
                status=terminal_status, updated_at="2026-01-01T00:00:00+00:00"
            )
            label, _reason = classify_startup_reclamation(
                item,
                known_event_ids={"ev-1"},
                now=now,
            )
            assert label == "terminal", (
                f"Terminal status {terminal_status!r} must be classified as 'terminal', "
                f"got {label!r}"
            )

    def test_snapshot_diagnostics_source_is_not_real_recovery(self) -> None:
        """SNAPSHOT_DIAGNOSTICS source indicates no runtime recovery occurred."""
        assert RecoverySource.SNAPSHOT_DIAGNOSTICS == "snapshot_diagnostics"
        # This source explicitly documents: "Not proof of delivery or mutation."
        assert (
            RecoverySource.SNAPSHOT_DIAGNOSTICS is not RecoverySource.STARTUP_RECOVERY
        )


# ===================================================================
# 4. recover CLI uses open_readonly
# ===================================================================


class TestRecoverCLIReadOnly:
    """recover CLI commands open storage in read-only mode.

    The recover CLI uses _open_readonly_storage which calls
    SQLiteStorage.open_readonly — no writes are possible.
    """

    def test_recover_commands_module_imports_readonly_helper(self) -> None:
        """recover_commands imports _open_readonly_storage."""
        from medre.cli import recover_commands

        source = inspect.getsource(recover_commands)
        assert (
            "_open_readonly_storage" in source
        ), "recover_commands must use _open_readonly_storage for storage access"

    def test_recover_commands_docstring_states_readonly(self) -> None:
        """recover_commands module docstring explicitly states read-only."""
        from medre.cli import recover_commands

        doc = recover_commands.__doc__
        assert doc is not None
        assert "read-only" in doc.lower() or "readonly" in doc.lower()
