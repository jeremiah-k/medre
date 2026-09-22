"""Focused contracts for Matrix stale-sync and Megolm recovery supervision."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.session import (
    MatrixSession,
    _StaleSyncError,
    _SyncRecycleFailed,
)
from medre.config.adapters.errors import MatrixConfigError
from medre.config.sample import generate_sample_config
from tests.helpers.matrix_session import make_matrix_config

_RUNTIME_DEFAULTS: dict[str, int | float] = {
    "sync_stale_timeout_seconds": 300.0,
    "megolm_key_request_rate_limit_per_minute": 30,
    "megolm_key_request_max_inflight": 4,
}


def test_runtime_supervision_defaults_match_schema_example_and_sample() -> None:
    root = Path(__file__).resolve().parents[1]
    config = make_matrix_config().validate()
    schema = json.loads(
        (root / "docs/schemas/adapter-config.schema.json").read_text(encoding="utf-8")
    )
    matrix_schema = next(
        branch for branch in schema["oneOf"] if branch.get("title") == "MatrixConfig"
    )
    properties = matrix_schema["properties"]
    example = json.loads(
        (root / "docs/schemas/examples/adapter-config-example.json").read_text(
            encoding="utf-8"
        )
    )
    sample = generate_sample_config()

    for field_name, expected in _RUNTIME_DEFAULTS.items():
        assert getattr(config, field_name) == expected
        assert properties[field_name]["default"] == expected
        assert example[field_name] == expected
        assert f"# {field_name}: {expected}" in sample


def test_runtime_supervision_defaults_validate() -> None:
    config = make_matrix_config().validate()
    assert config.sync_stale_timeout_seconds == 300.0
    assert config.megolm_key_request_rate_limit_per_minute == 30
    assert config.megolm_key_request_max_inflight == 4


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"sync_timeout_ms": -1}, "sync_timeout_ms"),
        ({"sync_timeout_ms": True}, "sync_timeout_ms"),
        ({"sync_stale_timeout_seconds": -1}, "sync_stale_timeout_seconds"),
        ({"sync_stale_timeout_seconds": float("inf")}, "sync_stale_timeout_seconds"),
        (
            {"sync_timeout_ms": 30_000, "sync_stale_timeout_seconds": 45.0},
            "must exceed",
        ),
        ({"megolm_key_request_rate_limit_per_minute": 0}, "rate_limit"),
        ({"megolm_key_request_max_inflight": 0}, "max_inflight"),
    ],
)
def test_runtime_supervision_invalid_values_rejected(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(MatrixConfigError, match=match):
        make_matrix_config(**overrides).validate()


def test_zero_stale_timeout_explicitly_disables_active_recovery() -> None:
    config = make_matrix_config(sync_stale_timeout_seconds=0).validate()
    assert config.sync_stale_timeout_seconds == 0


async def test_stale_sync_attempt_is_stopped_before_restart() -> None:
    config = make_matrix_config(sync_timeout_ms=1, sync_stale_timeout_seconds=300.0)
    ticks = iter((100.0, 401.0, 401.0))
    session = MatrixSession(config, clock=lambda: next(ticks))
    stop = MagicMock()

    async def _sync_forever(**_kwargs: object) -> None:
        await asyncio.Event().wait()

    session._client = SimpleNamespace(
        sync_forever=_sync_forever, stop_sync_forever=stop
    )
    with pytest.raises(_StaleSyncError):
        await asyncio.wait_for(session._run_sync_forever_attempt(), timeout=0.5)

    stop.assert_called_once()
    assert session._stale_sync_recoveries == 1
    assert session._last_stale_sync_at is not None


async def test_stale_sync_recycle_fails_closed_when_inner_loop_ignores_cancel() -> None:
    session = MatrixSession(make_matrix_config())
    release = asyncio.Event()
    started = asyncio.Event()

    async def _resists_cancel() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(_resists_cancel())
    await started.wait()
    client = SimpleNamespace(stop_sync_forever=MagicMock())
    try:
        with patch(
            "medre.adapters.matrix.session._SYNC_RECYCLE_CANCEL_TIMEOUT_SECONDS", 0.01
        ):
            with pytest.raises(_SyncRecycleFailed):
                await session._recycle_stale_sync_task(task, client)
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=0.5)


async def test_stale_sync_stop_error_still_cancels_sync_owner() -> None:
    session = MatrixSession(make_matrix_config())
    started = asyncio.Event()

    async def _wait_forever() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_wait_forever())
    await started.wait()
    stop = MagicMock(side_effect=RuntimeError("stop failed"))
    client = SimpleNamespace(stop_sync_forever=stop)

    await session._recycle_stale_sync_task(task, client)

    stop.assert_called_once()
    assert task.cancelled()


async def test_failed_stale_recycle_does_not_count_completed_recovery() -> None:
    config = make_matrix_config(sync_timeout_ms=0, sync_stale_timeout_seconds=0.001)
    ticks = iter((100.0, 100.0, 101.0, 101.0))
    session = MatrixSession(config, clock=lambda: next(ticks))
    release = asyncio.Event()
    started = asyncio.Event()
    inner_tasks: list[asyncio.Task[None]] = []

    async def _resists_cancel(**_kwargs: object) -> None:
        task = asyncio.current_task()
        assert task is not None
        inner_tasks.append(task)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    session._client = SimpleNamespace(
        sync_forever=_resists_cancel,
        stop_sync_forever=MagicMock(),
    )
    try:
        with patch(
            "medre.adapters.matrix.session._SYNC_RECYCLE_CANCEL_TIMEOUT_SECONDS", 0.01
        ):
            with pytest.raises(_SyncRecycleFailed):
                await asyncio.wait_for(session._run_sync_forever_attempt(), timeout=0.5)
        assert started.is_set()
        assert session._stale_sync_recoveries == 0
        assert session._last_stale_sync_at == 101.0
    finally:
        release.set()
        if inner_tasks:
            await asyncio.wait_for(
                asyncio.gather(*inner_tasks, return_exceptions=True), timeout=0.5
            )


def test_megolm_rate_limit_is_independent_from_warning_dedup() -> None:
    session = MatrixSession(
        make_matrix_config(megolm_key_request_rate_limit_per_minute=2)
    )
    session._undecryptable_dedup["!room:test:session"] = 123.0
    assert session._reserve_room_key_request(1000.0) is True
    assert session._reserve_room_key_request(1001.0) is True
    assert session._reserve_room_key_request(1002.0) is False
    assert session._room_key_request_rate_limited == 1
    assert "!room:test:session" in session._undecryptable_dedup


async def test_megolm_retry_attempts_consume_network_rate_limit() -> None:
    session = MatrixSession(
        make_matrix_config(
            encryption_mode="e2ee_required",
            megolm_key_request_rate_limit_per_minute=1,
        )
    )
    session._crypto_enabled = True
    error_type = type("ToDeviceError", (), {})
    response = error_type()
    response.errcode = "M_LIMIT_EXCEEDED"
    to_device = AsyncMock(return_value=response)
    session._client = SimpleNamespace(
        device_id="DEVICE",
        user_id="@bot:example.com",
        to_device=to_device,
    )
    event = SimpleNamespace(as_key_request=MagicMock(return_value={"request": 1}))

    with patch("medre.adapters.matrix.session._sleep", new=AsyncMock()):
        await session._request_missing_room_key(
            event=event,
            event_id="$event",
            room_id="!room:example.com",
            session_id_tag="redacted",
        )

    assert to_device.await_count == 1
    assert session._room_key_request_attempts == 1
    assert session._room_key_request_rate_limited == 1


async def test_megolm_same_session_recovery_cannot_replace_tracked_task() -> None:
    session = MatrixSession(make_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    session._live_sync_started = True
    pending = asyncio.create_task(asyncio.Event().wait())
    key = "!room:example.com:new-session"
    session._room_key_request_tasks[key] = pending
    # Simulate an expired warning-dedup entry: task ownership must remain
    # independent from warning policy.
    session._undecryptable_dedup[key] = -1_000.0
    session._client = SimpleNamespace(
        device_id="DEVICE", user_id="@bot:example.com", to_device=AsyncMock()
    )
    event = SimpleNamespace(
        event_id="$event",
        session_id="new-session",
        as_key_request=MagicMock(return_value={"request": 1}),
    )
    room = SimpleNamespace(room_id="!room:example.com")
    try:
        await session._on_megolm_event(room, event)
        assert session._room_key_request_tasks[key] is pending
        assert session._room_key_request_inflight_rejected == 0
        session._client.to_device.assert_not_awaited()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_megolm_max_inflight_rejects_new_recovery_without_network_send() -> None:
    session = MatrixSession(
        make_matrix_config(
            encryption_mode="e2ee_required",
            megolm_key_request_max_inflight=1,
        )
    )
    session._crypto_enabled = True
    session._live_sync_started = True
    pending = asyncio.create_task(asyncio.Event().wait())
    session._room_key_request_tasks["existing"] = pending
    session._client = SimpleNamespace(
        device_id="DEVICE",
        user_id="@bot:example.com",
        to_device=AsyncMock(),
    )
    event = SimpleNamespace(
        event_id="$event",
        session_id="new-session",
        as_key_request=MagicMock(return_value={"request": 1}),
    )
    room = SimpleNamespace(room_id="!room:example.com")
    try:
        await session._on_megolm_event(room, event)
        assert session._room_key_request_inflight_rejected == 1
        session._client.to_device.assert_not_awaited()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


def test_adapter_stopped_diagnostics_preserve_supervision_shape() -> None:
    from medre.adapters.matrix.adapter import MatrixAdapter

    diag = MatrixAdapter(make_matrix_config()).diagnostics()
    assert diag["stale_sync_recoveries"] == 0
    assert diag["last_stale_sync_at"] is None
    assert diag["megolm_recovery_rate_limited"] == 0
    assert diag["megolm_recovery_inflight_rejected"] == 0
    assert diag["megolm_recovery_inflight"] == 0


async def test_sync_reconnect_jitter_never_exceeds_backoff_cap() -> None:
    from medre.adapters.matrix import session as session_module

    session = MatrixSession(make_matrix_config())
    session._client = SimpleNamespace()
    session._reconnect_attempts = 100_000  # long outages must not overflow backoff

    async def _stop_after_observing_delay(delay: float) -> None:
        assert delay == session_module._BACKOFF_CAP
        raise asyncio.CancelledError

    with (
        patch.object(
            MatrixSession,
            "_run_sync_forever_attempt",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(
            "medre.adapters.matrix.session.random.uniform",
            return_value=session_module._BACKOFF_CAP
            * session_module._BACKOFF_JITTER_FRACTION,
        ),
        patch.object(
            session_module,
            "asyncio",
            SimpleNamespace(
                sleep=_stop_after_observing_delay,
                CancelledError=asyncio.CancelledError,
            ),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await session._sync_with_reconnect()


async def test_non_retryable_sync_exception_fails_closed_without_reconnect() -> None:
    """Unexpected programming/contract errors must not retry forever."""
    session = MatrixSession(make_matrix_config())

    with patch.object(
        MatrixSession,
        "_run_sync_forever_attempt",
        new=AsyncMock(side_effect=TypeError("bad callback contract")),
    ):
        await session._sync_with_reconnect()

    assert isinstance(session.last_sync_error, TypeError)
    assert session.reconnect_attempts == 0
    assert session.reconnecting is False


async def test_shutdown_race_does_not_record_terminal_sync_failure() -> None:
    """A sync exception after stop is requested belongs to normal shutdown."""
    session = MatrixSession(make_matrix_config())

    async def _fail_after_stop_request() -> None:
        session._stop_requested = True
        raise RuntimeError("request closed during shutdown")

    with patch.object(
        MatrixSession,
        "_run_sync_forever_attempt",
        new=_fail_after_stop_request,
    ):
        await session._sync_with_reconnect()

    assert session.last_sync_error is None
    assert session.reconnect_attempts == 0
    assert session.reconnecting is False


async def test_stop_continues_cleanup_when_provider_stop_hook_raises() -> None:
    """A provider stop hint failure cannot skip cancellation/close cleanup."""
    session = MatrixSession(make_matrix_config())
    client = SimpleNamespace(
        stop_sync_forever=MagicMock(side_effect=RuntimeError("stop failed")),
        close=AsyncMock(),
        logged_in=True,
        olm=None,
        store=None,
    )
    session._client = client
    session._closed = False

    await session.stop(timeout=0.2)

    client.stop_sync_forever.assert_called_once_with()
    client.close.assert_awaited_once_with()
    assert session.closed is True
    assert session._client is None


async def test_warning_dedup_does_not_suppress_later_key_recovery() -> None:
    session = MatrixSession(
        make_matrix_config(
            encryption_mode="e2ee_required",
            megolm_key_request_rate_limit_per_minute=5,
        )
    )
    session._crypto_enabled = True
    session._live_sync_started = True
    session._client = SimpleNamespace(
        device_id="DEVICE",
        user_id="@bot:example.com",
        to_device=AsyncMock(return_value=SimpleNamespace()),
    )
    room = SimpleNamespace(room_id="!room:example.com")
    event = SimpleNamespace(
        event_id="$event",
        session_id="session-1",
        as_key_request=MagicMock(return_value={"request": 1}),
    )
    key = "!room:example.com:session-1"
    session._undecryptable_dedup[key] = __import__("time").monotonic()

    await session._on_megolm_event(room, event)
    task = session._room_key_request_tasks[key]
    await asyncio.wait_for(task, timeout=0.5)

    session._client.to_device.assert_awaited_once()
    assert session._suppressed_rate_limited_undecryptable == 1
    assert session._room_key_request_attempts == 1
