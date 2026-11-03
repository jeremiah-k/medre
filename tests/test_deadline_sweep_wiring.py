# medre-test-tier: compatibility
"""Wiring of the guarded deadline sweep into the retry worker poll cycle.

Covers the operator-facing surface of ``retire_expired_unstarted_outbox``
runtime enablement: the default-off policy leaves poll-cycle behavior
unchanged, an enabled policy runs one bounded sweep step per due cycle
through the poll cycle, claimed or otherwise unproven rows are never
touched, a sweep failure is isolated from the delivery poll, and
out-of-range declarations fail closed at every config entry path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.model import (
    DEADLINE_SWEEP_MAX_INTERVAL_SECONDS,
    DEADLINE_SWEEP_MAX_LIMIT,
    DeadlineSweepConfig,
    RetryConfig,
)
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.initial_outbox import _pipeline, _stage
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import make_outbox_item

pytestmark = pytest.mark.usefixtures("isolated_config_env")


def _future(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _direct_snapshot(deadline: str | None) -> OutboxDispatchSnapshotWrite:
    """Freeze the reduced direct dispatch context exactly as staging builds it."""
    return OutboxDispatchSnapshotWrite(
        kind="direct",
        context={
            "delivery_strategy": "direct",
            "capability_level": "native",
            "capability_field": None,
            "capability_reason": None,
            "deadline": deadline,
        },
    )


async def _stage_expired(
    store: SQLiteStorage, event_id: str, *, plan_deadline: datetime
) -> str:
    """Admit one event and stage one expired, unstarted pending generation."""
    await store.admit_ingress(
        make_storage_event(event_id=event_id),
        None,
        "live",
        content_deadline=_future(3600),
    )
    item = make_outbox_item(
        event_id=event_id,
        delivery_plan_id=f"plan-{event_id}",
    )
    created = await store.create_outbox_item(
        item,
        dispatch_snapshot=_direct_snapshot(plan_deadline.isoformat()),
    )
    return created.outbox_id


# ---------------------------------------------------------------------------
# Default-off: the default configuration is behavior-preserving
# ---------------------------------------------------------------------------


async def test_default_policy_is_disabled(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default declaration is off and the poll cycle never sweeps."""
    sweep_spy = AsyncMock(return_value=[])
    monkeypatch.setattr(temp_storage, "retire_expired_unstarted_outbox", sweep_spy)
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
    sweep_spy.assert_not_called()
    assert worker.state.deadline_sweep_runs == 0
    assert worker.state.deadline_sweep_errors == 0
    assert worker.state.deadline_retired == 0


async def test_default_off_poll_delivery_is_unchanged(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the default config a staged initial obligation still delivers.

    The default-off pin: the poll cycle performs the same claim, delivery and
    count refresh as before the sweep wiring existed, and the sweep surface
    is never touched.
    """
    sweep_spy = AsyncMock(return_value=[])
    monkeypatch.setattr(temp_storage, "retire_expired_unstarted_outbox", sweep_spy)
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(),
        initial_dispatch=True,
        interval_seconds=10,
    )
    await _stage(temp_storage, runner, "default-off-delivery")
    async with asyncio.TaskGroup() as group:
        await worker._process_due(datetime.now(timezone.utc), group=group)
    sweep_spy.assert_not_called()
    assert worker.state.deadline_sweep_runs == 0
    assert worker.state.deadline_retired == 0
    delivered = await temp_storage.list_outbox_items_for_event("default-off-delivery")
    assert any(row.status == "sent" for row in delivered)
    assert not any(row.status == "abandoned" for row in delivered)


async def test_worker_without_a_sweep_capability_polls_normally(
    temp_storage: SQLiteStorage,
) -> None:
    """An enabled policy stays dormant on a backend without the transition."""
    runner, _, _ = _pipeline(temp_storage)

    class _NoSweepStorage:
        """Minimal storage double exposing no deadline-cleanup surface."""

        async def claim_due_outbox_items(
            self,
            now: str,
            worker_id: str,
            lease_seconds: int = 30,
            limit: int = 20,
            *,
            excluded_adapters: tuple[str, ...] = (),
            initial_only: bool = False,
            one_per_adapter: bool = False,
        ) -> list[DeliveryOutboxItem]:
            return []

        async def count_outbox_by_status(self) -> dict[str, int]:
            return {"pending": 0}

    worker = RetryWorker(
        _NoSweepStorage(),  # type: ignore[arg-type]
        runner,
        None,
        retry_config=RetryConfig(deadline_sweep=DeadlineSweepConfig(enabled=True)),
        initial_dispatch=True,
        interval_seconds=10,
    )
    await worker._process_due(datetime.now(timezone.utc))
    assert worker.state.deadline_sweep_runs == 0
    assert worker.state.deadline_sweep_errors == 0


# ---------------------------------------------------------------------------
# Enabled: the sweep retires only expired, provably unstarted work
# ---------------------------------------------------------------------------


async def test_enabled_sweep_retires_expired_unstarted_work_in_poll_cycle(
    temp_storage: SQLiteStorage,
) -> None:
    """One poll with the policy on retires the expired unstarted generation."""
    outbox_id = await _stage_expired(
        temp_storage, "wiring-expired", plan_deadline=_future(-60)
    )
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            deadline_sweep=DeadlineSweepConfig(enabled=True, interval_seconds=60.0)
        ),
        initial_dispatch=True,
        interval_seconds=10,
    )
    await worker._process_due(datetime.now(timezone.utc))
    item = await temp_storage.get_outbox_item(outbox_id)
    assert item is not None
    assert item.status == "abandoned"
    assert item.failure_kind == "deadline_exceeded"
    assert item.worker_id is None and item.lease_until is None
    assert worker.state.deadline_sweep_runs == 1
    assert worker.state.deadline_retired == 1
    assert worker.state.deadline_sweep_errors == 0


async def test_sweep_spares_claimed_work_in_the_same_cycle(
    temp_storage: SQLiteStorage,
) -> None:
    """A claimed in_progress row is never retired by the poll-cycle sweep."""
    claimed_id = await _stage_expired(
        temp_storage, "wiring-claimed", plan_deadline=_future(-60)
    )
    claimed = await temp_storage.claim_due_outbox_items(
        now=datetime.now(timezone.utc).isoformat(),
        worker_id="wiring-holder",
        lease_seconds=7200,
        limit=10,
    )
    assert [row.outbox_id for row in claimed] == [claimed_id]
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            deadline_sweep=DeadlineSweepConfig(enabled=True, interval_seconds=60.0)
        ),
        initial_dispatch=True,
        interval_seconds=10,
    )
    await worker._process_due(datetime.now(timezone.utc))
    item = await temp_storage.get_outbox_item(claimed_id)
    assert item is not None
    assert item.status == "in_progress"
    assert item.worker_id == "wiring-holder"
    assert worker.state.deadline_sweep_runs == 1
    assert worker.state.deadline_retired == 0


async def test_delivery_retries_interleave_with_the_sweep(
    temp_storage: SQLiteStorage,
) -> None:
    """The same cycle retires expired work and still delivers live work."""
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            deadline_sweep=DeadlineSweepConfig(enabled=True, interval_seconds=60.0)
        ),
        initial_dispatch=True,
        interval_seconds=10,
    )
    # The live obligation is admitted and drained first so the expired row
    # staged afterwards never occupies the one-slot ingress batch.
    await _stage(temp_storage, runner, "wiring-interleave-live")
    expired_id = await _stage_expired(
        temp_storage, "wiring-interleave-expired", plan_deadline=_future(-60)
    )
    async with asyncio.TaskGroup() as group:
        await worker._process_due(datetime.now(timezone.utc), group=group)
    expired_item = await temp_storage.get_outbox_item(expired_id)
    assert expired_item is not None
    assert expired_item.status == "abandoned"
    live = await temp_storage.list_outbox_items_for_event("wiring-interleave-live")
    assert any(row.status == "sent" for row in live)
    assert worker.state.deadline_retired == 1
    assert worker.state.deadline_sweep_errors == 0


async def test_sweep_cadence_gates_repeat_polls(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep runs at most once per interval and again after it elapses."""
    sweep_spy = AsyncMock(return_value=[])
    monkeypatch.setattr(temp_storage, "retire_expired_unstarted_outbox", sweep_spy)
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            deadline_sweep=DeadlineSweepConfig(enabled=True, interval_seconds=60.0)
        ),
        initial_dispatch=True,
        interval_seconds=10,
    )
    now = datetime.now(timezone.utc)
    await worker._process_due(now)
    await worker._process_due(now + timedelta(seconds=1))
    await worker._process_due(now + timedelta(seconds=59))
    assert sweep_spy.await_count == 1
    await worker._process_due(now + timedelta(seconds=60))
    assert sweep_spy.await_count == 2
    assert worker.state.deadline_sweep_runs == 2


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_sweep_failure_is_isolated_from_the_poll_cycle(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising sweep neither aborts the cycle nor the next sweep step."""
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            deadline_sweep=DeadlineSweepConfig(enabled=True, interval_seconds=60.0)
        ),
        initial_dispatch=True,
        interval_seconds=10,
    )
    failing = AsyncMock(side_effect=RuntimeError("sweep writer exploded"))
    monkeypatch.setattr(worker, "_deadline_sweep", failing)
    await _stage(temp_storage, runner, "wiring-isolation-live")
    now = datetime.now(timezone.utc)
    async with asyncio.TaskGroup() as group:
        await worker._process_due(now, group=group)
    failing.assert_awaited_once()
    assert worker.state.deadline_sweep_errors == 1
    assert worker.state.deadline_sweep_runs == 0
    live = await temp_storage.list_outbox_items_for_event("wiring-isolation-live")
    assert any(row.status == "sent" for row in live)

    # A later poll, after the cadence elapses, runs the sweep again and the
    # worker continues past the earlier failure.
    recovered = AsyncMock(return_value=[])
    monkeypatch.setattr(worker, "_deadline_sweep", recovered)
    await worker._process_due(now + timedelta(seconds=60))
    recovered.assert_awaited_once()
    assert worker.state.deadline_sweep_errors == 1
    assert worker.state.deadline_sweep_runs == 1


async def test_repeated_sweep_failures_stay_isolated(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistently failing sweep never surfaces as a worker crash."""
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            deadline_sweep=DeadlineSweepConfig(enabled=True, interval_seconds=0.05)
        ),
        initial_dispatch=True,
        interval_seconds=10,
    )
    failing = AsyncMock(side_effect=RuntimeError("still broken"))
    monkeypatch.setattr(worker, "_deadline_sweep", failing)
    now = datetime.now(timezone.utc)
    for seconds in (0, 0.1, 0.2):
        await worker._process_due(now + timedelta(seconds=seconds))
    assert worker.state.deadline_sweep_errors == 3
    assert worker.state.deadline_sweep_runs == 0


# ---------------------------------------------------------------------------
# Config validation: out-of-range declarations fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, DEADLINE_SWEEP_MAX_LIMIT + 1, True, 1.5])
def test_out_of_range_limit_fails_closed(limit: object) -> None:
    """The per-sweep limit accepts only integers inside the closed 1..500 range."""
    with pytest.raises(ConfigValidationError, match="deadline_sweep.limit"):
        DeadlineSweepConfig(limit=limit).validate()  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [1, DEADLINE_SWEEP_MAX_LIMIT])
def test_limit_boundaries_are_accepted(limit: int) -> None:
    """Both ends of the storage transition's closed range load unchanged."""
    policy = DeadlineSweepConfig(enabled=True, limit=limit)
    policy.validate()
    assert RetryConfig(deadline_sweep=policy).validate().deadline_sweep is policy


@pytest.mark.parametrize("interval", [0, -5.0, True, "60"])
def test_invalid_interval_fails_closed(interval: object) -> None:
    """The cadence must be a positive finite number."""
    with pytest.raises(ConfigValidationError, match="deadline_sweep.interval_seconds"):
        DeadlineSweepConfig(interval_seconds=interval).validate()  # type: ignore[arg-type]


def test_validation_runs_through_the_retry_section() -> None:
    """Every RetryConfig entry path revalidates the nested sweep policy."""
    bad = RetryConfig(
        deadline_sweep=DeadlineSweepConfig(limit=DEADLINE_SWEEP_MAX_LIMIT + 1)
    )
    with pytest.raises(ConfigValidationError, match="deadline_sweep.limit"):
        bad.validate()


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("limit: 501\n", "deadline_sweep.limit"),
        ("limit: 0\n", "deadline_sweep.limit"),
        ("limit: true\n", "deadline_sweep.limit"),
        ("interval_seconds: 0\n", "deadline_sweep.interval_seconds"),
        ("interval_seconds: 1e12\n", "deadline_sweep.interval_seconds"),
    ],
)
def test_loader_rejects_out_of_range_sweep_declarations(
    tmp_path: Path, section: str, message: str
) -> None:
    """The YAML loader fails closed on out-of-range sweep declarations."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"retry:\n  deadline_sweep:\n    {section}")
    with pytest.raises(ConfigValidationError, match=message):
        load_config(str(config_file))


def test_loader_rejects_unknown_sweep_keys(tmp_path: Path) -> None:
    """A typo inside the sweep section is rejected before any value is read."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("retry:\n  deadline_sweep:\n    limmit: 50\n")
    with pytest.raises(ConfigValidationError, match="unknown key"):
        load_config(str(config_file))


def test_loader_parses_an_enabled_sweep_section(tmp_path: Path) -> None:
    """A valid declaration flows through the loader unchanged."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "retry:\n"
        "  deadline_sweep:\n"
        "    enabled: true\n"
        "    limit: 500\n"
        "    interval_seconds: 30.0\n"
    )
    config, _, _ = load_config(str(config_file))
    policy = config.retry.deadline_sweep
    assert policy.enabled is True
    assert policy.limit == 500
    assert policy.interval_seconds == 30.0


def test_sweep_interval_ceiling_rejects_overflow() -> None:
    """A policy cannot repeatedly abort the dispatch poll before claiming work."""
    with pytest.raises(ConfigValidationError, match="deadline_sweep.interval_seconds"):
        DeadlineSweepConfig(
            interval_seconds=DEADLINE_SWEEP_MAX_INTERVAL_SECONDS + 1
        ).validate()


def test_config_schema_accepts_enabled_sweep_and_rejects_invalid_bounds() -> None:
    """Schema-driven authors and the runtime loader accept the same declaration."""
    import json
    from jsonschema import Draft202012Validator

    schema_path = Path(__file__).parents[1] / "docs/schemas/runtime-config.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text()))
    valid = {
        "retry": {
            "deadline_sweep": {
                "enabled": True,
                "limit": 500,
                "interval_seconds": 60,
            }
        }
    }
    assert not list(validator.iter_errors(valid))
    invalid_policies = (
        {"limit": 0},
        {"limit": 501},
        {"interval_seconds": 0},
        {"interval_seconds": 1e12},
        {"unknown": 1},
    )
    for policy in invalid_policies:
        assert list(validator.iter_errors({"retry": {"deadline_sweep": policy}}))


async def test_live_runtime_uses_loaded_deadline_sweep_policy(tmp_path: Path) -> None:
    """The real app constructor forwards the loaded operator declaration."""
    from dataclasses import replace

    from medre.config.paths import resolve
    from tests.helpers.startup_cleanup import (
        _build_app,
        _config_with_one_fake_adapter,
    )

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "retry:\n  deadline_sweep:\n    enabled: true\n"
        "    limit: 17\n    interval_seconds: 35\n"
    )
    loaded, _, _ = load_config(str(config_file))
    config = replace(_config_with_one_fake_adapter(), retry=loaded.retry)
    app = _build_app(config, resolve(environ={"MEDRE_HOME": str(tmp_path)}))
    await app.start()
    try:
        worker = app._retry_worker
        assert worker is not None
        assert worker._deadline_sweep_enabled is True
        assert worker._deadline_sweep_limit == 17
        assert worker._deadline_sweep_interval == 35
    finally:
        await app.stop()


async def test_sweep_cadence_overflow_isolated_from_delivery_poll(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an unvalidated oversized interval cannot abort due-work claims."""
    sweep_spy = AsyncMock(return_value=[])
    monkeypatch.setattr(temp_storage, "retire_expired_unstarted_outbox", sweep_spy)
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(deadline_sweep=DeadlineSweepConfig(enabled=True)),
        initial_dispatch=True,
    )
    worker._deadline_sweep_interval = 1e20
    await worker._process_due(datetime.now(timezone.utc))
    assert worker.state.deadline_sweep_errors == 1
    sweep_spy.assert_not_called()
