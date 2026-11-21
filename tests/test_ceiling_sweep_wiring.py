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

    async def failing(*, now=None, limit=50):
        raise RuntimeError("synthetic ceiling sweep failure")

    monkeypatch.setattr(temp_storage, "enforce_content_ceilings", failing)
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
    await worker._process_due(datetime.now(timezone.utc))
    assert worker.state.ceiling_sweep_errors == 1
    assert worker.state.ceiling_sweep_runs == 0


def test_ceiling_sweep_config_rejects_invalid_declarations() -> None:
    """Out-of-range declarations fail closed at the config boundary."""
    with pytest.raises(ConfigValidationError, match="ceiling_sweep.enabled"):
        CeilingSweepConfig(enabled="yes").validate()
    with pytest.raises(ConfigValidationError, match="ceiling_sweep.limit"):
        CeilingSweepConfig(limit=501).validate()
    with pytest.raises(ConfigValidationError, match="ceiling_sweep.interval_seconds"):
        CeilingSweepConfig(interval_seconds=0).validate()
