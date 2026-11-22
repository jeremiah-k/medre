"""Wiring of the guarded ceiling-enforcement sweep into the retry worker.

Covers the operator-facing surface of ``enforce_content_ceilings`` runtime
enablement: the default-off policy leaves poll-cycle behavior unchanged, an
enabled policy runs one bounded enforcement step per due cycle and reports
what the guarded storage transition committed, a sweep failure is isolated
from the delivery poll, and out-of-range declarations fail closed at every
config entry path. The purge behavior itself is qualified per store in
``tests/test_content_ceiling_enforcement.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.model import CeilingSweepConfig, RetryConfig
from medre.core.storage.sqlite._ceiling_enforcement import CeilingEnforcement
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.initial_outbox import _pipeline


async def test_default_ceiling_policy_is_disabled(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default declaration is off and the poll cycle never enforces."""
    sweep_spy_entries = []

    async def spy(*, now=None, limit=50):
        sweep_spy_entries.append(limit)
        return []

    monkeypatch.setattr(temp_storage, "enforce_content_ceilings", spy)
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(),
        initial_dispatch=True,
        interval_seconds=10,
    )
    now = datetime.now(timezone.utc)
    for seconds in (0, 1, 2):
        await worker._process_due(now + timedelta(seconds=seconds))
    assert sweep_spy_entries == []
    assert worker.state.ceiling_sweep_runs == 0
    assert worker.state.ceiling_sweep_errors == 0
    assert worker.state.ceiling_enforced == 0


async def test_enabled_ceiling_policy_enforces_through_poll_cycle(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled policy runs one bounded enforcement step per due cycle.

    The worker reports what the guarded storage transition committed; the
    real purge behavior is qualified at the storage level.
    """
    sweep_spy = AsyncMock(
        return_value=[
            CeilingEnforcement(
                event_id="evt-1",
                ceiling="2026-01-01T00:00:00+00:00",
                enforced_at="2026-01-01T00:00:01+00:00",
                rows_removed=1,
                logical_bytes_removed=10,
                blobs_removed=0,
                blob_bytes_removed=0,
                residual_pass=False,
            )
        ]
    )
    monkeypatch.setattr(temp_storage, "enforce_content_ceilings", sweep_spy)
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            ceiling_sweep=CeilingSweepConfig(enabled=True, limit=10)
        ),
        initial_dispatch=True,
        interval_seconds=10,
    )
    now = datetime.now(timezone.utc)
    await worker._process_due(now)
    assert sweep_spy.await_count == 1
    assert sweep_spy.await_args.kwargs["limit"] == 10
    assert worker.state.ceiling_sweep_runs == 1
    assert worker.state.ceiling_enforced == 1
    # The cadence throttles the next due cycle.
    await worker._process_due(now + timedelta(seconds=1))
    assert sweep_spy.await_count == 1


async def test_ceiling_sweep_failure_is_isolated(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing enforcement step never takes the delivery poll down."""

    sweep_mock = AsyncMock(side_effect=RuntimeError("synthetic ceiling sweep failure"))
    monkeypatch.setattr(temp_storage, "enforce_content_ceilings", sweep_mock)
    claims = AsyncMock(wraps=temp_storage.claim_due_outbox_items)
    monkeypatch.setattr(temp_storage, "claim_due_outbox_items", claims)
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            ceiling_sweep=CeilingSweepConfig(enabled=True, limit=10)
        ),
        initial_dispatch=True,
        interval_seconds=10,
    )
    now = datetime.now(timezone.utc)
    await worker._process_due(now)
    assert worker.state.ceiling_sweep_errors == 1
    assert worker.state.ceiling_sweep_runs == 0
    assert claims.await_count == 1  # failed sweep did not skip delivery
    # The worker binds the sweep step at construction, so recovery flips
    # the same mock instead of rebinding the storage attribute.
    sweep_mock.side_effect = None
    sweep_mock.return_value = []
    # The due timer uses the supplied clock; no arbitrary sleeps.
    await worker._process_due(now + timedelta(seconds=61))
    assert worker.state.ceiling_sweep_runs == 1
    assert worker.state.ceiling_sweep_errors == 1
    assert claims.await_count == 2


def test_ceiling_sweep_config_rejects_invalid_declarations() -> None:
    """Out-of-range declarations fail closed at the config boundary."""
    with pytest.raises(ConfigValidationError, match="ceiling_sweep.enabled"):
        CeilingSweepConfig(enabled="yes").validate()
    with pytest.raises(ConfigValidationError, match="ceiling_sweep.limit"):
        CeilingSweepConfig(limit=501).validate()
    with pytest.raises(ConfigValidationError, match="ceiling_sweep.interval_seconds"):
        CeilingSweepConfig(interval_seconds=0).validate()
    # Do not render rejected huge integers: CPython limits int-to-string conversion.
    huge = 10**5000
    with pytest.raises(ConfigValidationError, match="ceiling_sweep.limit"):
        CeilingSweepConfig(limit=huge).validate()
    with pytest.raises(ConfigValidationError, match="ceiling_sweep.interval_seconds"):
        CeilingSweepConfig(interval_seconds=huge).validate()


async def test_ceiling_only_worker_does_not_claim_delivery(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default-off retry policy can run maintenance without transport work."""
    sweep = AsyncMock(return_value=[])
    claims = AsyncMock(return_value=[])
    monkeypatch.setattr(temp_storage, "enforce_content_ceilings", sweep)
    monkeypatch.setattr(temp_storage, "claim_due_outbox_items", claims)
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            enabled=False, ceiling_sweep=CeilingSweepConfig(enabled=True)
        ),
        initial_dispatch=False,
    )
    assert worker._enabled is True
    await worker._process_due(datetime.now(timezone.utc))
    sweep.assert_awaited_once()
    claims.assert_not_awaited()


async def test_maintenance_wake_does_not_bypass_retry_scan(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling wake before the retry scan must not claim retry work."""
    sweep = AsyncMock(return_value=[])
    claims = AsyncMock(return_value=[])
    monkeypatch.setattr(temp_storage, "enforce_content_ceilings", sweep)
    monkeypatch.setattr(temp_storage, "claim_due_outbox_items", claims)
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            enabled=True,
            interval_seconds=3600,
            ceiling_sweep=CeilingSweepConfig(enabled=True, interval_seconds=60),
        ),
        initial_dispatch=False,
    )
    now = datetime.now(timezone.utc)
    worker._next_retry_scan = now + timedelta(seconds=3600)
    worker._next_ceiling_sweep_at = now
    async with asyncio.TaskGroup() as group:
        await worker._process_due(now, group=group)
    sweep.assert_awaited_once()
    claims.assert_not_awaited()


async def test_ceiling_sweep_due_after_deadline_maintenance_is_not_skipped(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long deadline step cannot hide a sweep due before claim processing."""
    sweep = AsyncMock(return_value=[])
    monkeypatch.setattr(temp_storage, "enforce_content_ceilings", sweep)
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            enabled=False, ceiling_sweep=CeilingSweepConfig(enabled=True)
        ),
        initial_dispatch=False,
    )
    # The poll's captured timestamp precedes the scheduled sweep, but the
    # real deadline-maintenance boundary is now well beyond that instant.
    before_deadline_maintenance = datetime.now(timezone.utc) - timedelta(minutes=2)
    worker._next_ceiling_sweep_at = before_deadline_maintenance + timedelta(minutes=1)
    await worker._run_ceiling_sweep(before_deadline_maintenance)
    sweep.assert_awaited_once()
    assert worker.state.ceiling_sweep_runs == 1
