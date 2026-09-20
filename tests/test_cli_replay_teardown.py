"""``best_effort`` replay teardown: one drain deadline, preserved primary
errors, and the shared diagnostics-key observation seam.

Covers the CLI teardown contract end to end at its seam:

* the pre-stop drain and ``app.stop()`` share ONE
  ``shutdown_drain_timeout_seconds`` deadline — congestion cannot spend
  the documented budget twice (durable-ingress "Capacity and shutdown
  handoff");
* the replay body's own failure or cancellation is never replaced by a
  secondary teardown failure, and drain failures are never silently
  swallowed;
* the drain observes only the shared diagnostics keys and treats an
  adapter exposing nothing as "no grace", never as delivery truth.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from medre.cli.replay_commands import (
    _drain_inflight_deliveries,
    _teardown_replay_runtime,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.supervision.diagnostic_contract import (
    PENDING_DELIVERY_COUNT,
    QUEUE_PENDING,
)
from tests.helpers.fake_runtime import (
    build_and_start,
    clean_stop,
    make_two_adapter_config_with_route,
)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


class _StubApp:
    """Minimal app surface for drain/teardown seam tests."""

    def __init__(self, diagnostics: dict[str, dict] | None = None) -> None:
        self.started_adapter_ids = list((diagnostics or {}).keys())
        self.adapters = {
            aid: SimpleNamespace(diagnostics=(lambda d=diag: d))
            for aid, diag in (diagnostics or {}).items()
        }
        self.stop_calls: list[dict] = []

    async def stop(self, *, drain_deadline: float | None = None) -> None:
        self.stop_calls.append({"drain_deadline": drain_deadline})


def _diagnostics(pending: int, key: str) -> dict:
    if key == PENDING_DELIVERY_COUNT:
        return {"session": {PENDING_DELIVERY_COUNT: pending}}
    return {key: pending}


async def test_pending_clears_returns_before_deadline() -> None:
    app = _StubApp({"lx": _diagnostics(0, PENDING_DELIVERY_COUNT)})
    started = time.monotonic()
    await _drain_inflight_deliveries(app, timeout=5.0)
    assert time.monotonic() - started < 1.0


async def test_never_pending_times_out_bounded() -> None:
    app = _StubApp({"lx": _diagnostics(3, PENDING_DELIVERY_COUNT)})
    started = time.monotonic()
    await _drain_inflight_deliveries(app, timeout=0.5)
    assert 0.4 <= time.monotonic() - started < 2.0


async def test_queue_pending_key_observed() -> None:
    app = _StubApp({"mt": _diagnostics(1, QUEUE_PENDING)})
    started = time.monotonic()
    await _drain_inflight_deliveries(app, timeout=0.5)
    assert time.monotonic() - started >= 0.4  # it waited on the key


async def test_adapter_without_diagnostics_gets_no_grace() -> None:
    app = _StubApp({})
    app.adapters["bare"] = SimpleNamespace()  # no diagnostics()
    app.started_adapter_ids = ["bare"]
    started = time.monotonic()
    await _drain_inflight_deliveries(app, timeout=5.0)
    assert time.monotonic() - started < 1.0


async def test_stop_receives_the_one_deadline() -> None:
    """The deadline stop() honors is the one the drain consumed from."""
    app = _StubApp({"lx": _diagnostics(0, PENDING_DELIVERY_COUNT)})
    await _teardown_replay_runtime(app, drain_timeout=7.0)
    assert len(app.stop_calls) == 1
    passed = app.stop_calls[0]["drain_deadline"]
    assert passed is not None
    # One budget: the deadline given to stop() is at most one full
    # drain window from now — never a fresh full timer on top of the
    # already-consumed drain.
    assert passed - time.monotonic() <= 7.0


async def test_stop_failure_propagates_when_body_succeeded() -> None:
    class _FailingStopApp(_StubApp):
        async def stop(self, *, drain_deadline: float | None = None) -> None:
            raise RuntimeError("shutdown exploded")

    app = _FailingStopApp({"lx": _diagnostics(0, PENDING_DELIVERY_COUNT)})
    with pytest.raises(RuntimeError, match="shutdown exploded"):
        await _teardown_replay_runtime(app, drain_timeout=1.0)


async def test_body_failure_is_not_masked_by_stop_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingStopApp(_StubApp):
        async def stop(self, *, drain_deadline: float | None = None) -> None:
            raise RuntimeError("shutdown exploded")

    app = _FailingStopApp({"lx": _diagnostics(0, PENDING_DELIVERY_COUNT)})
    with caplog.at_level(logging.ERROR, logger="medre.cli.replay_commands"):
        try:
            raise RuntimeError("replay body failed")
        except RuntimeError:
            await _teardown_replay_runtime(app, drain_timeout=1.0)
    # Primary preserved (no teardown exception surfaced) and the
    # secondary failure is visible, not swallowed.
    assert any("shutdown exploded" in r.message for r in caplog.records)


async def test_drain_failure_is_logged_and_never_silent(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _explode(app: Any, timeout: float) -> None:
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(
        "medre.cli.replay_commands._drain_inflight_deliveries", _explode
    )
    app = _StubApp()
    with caplog.at_level(logging.WARNING, logger="medre.cli.replay_commands"):
        try:
            raise RuntimeError("replay body failed")
        except RuntimeError:
            await _teardown_replay_runtime(app, drain_timeout=1.0)
    assert any("drain failed" in r.message for r in caplog.records)
    assert len(app.stop_calls) == 1


async def test_drain_failure_fatal_after_stop_when_body_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _explode(app: Any, timeout: float) -> None:
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(
        "medre.cli.replay_commands._drain_inflight_deliveries", _explode
    )
    app = _StubApp()
    with pytest.raises(RuntimeError, match="drain exploded"):
        await _teardown_replay_runtime(app, drain_timeout=1.0)
    assert len(app.stop_calls) == 1


async def test_drain_cancellation_still_stops_then_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _cancel(app: Any, timeout: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr("medre.cli.replay_commands._drain_inflight_deliveries", _cancel)
    app = _StubApp()
    with pytest.raises(asyncio.CancelledError):
        await _teardown_replay_runtime(app, drain_timeout=1.0)
    assert len(app.stop_calls) == 1


async def test_cancellation_from_stop_propagates() -> None:
    class _CancellingApp(_StubApp):
        async def stop(self, *, drain_deadline: float | None = None) -> None:
            raise asyncio.CancelledError()

    app = _CancellingApp({"lx": _diagnostics(0, PENDING_DELIVERY_COUNT)})
    with pytest.raises(asyncio.CancelledError):
        await _teardown_replay_runtime(app, drain_timeout=1.0)


async def test_stop_with_expired_deadline_skips_drain_wait(
    tmp_paths: MedrePaths,
) -> None:
    """The runtime owner enforces a caller-supplied absolute deadline."""
    config, _route = make_two_adapter_config_with_route()
    app = await build_and_start(config, tmp_paths)
    try:
        started = time.monotonic()
        await app.stop(drain_deadline=started - 1.0)  # already expired
        assert time.monotonic() - started < 5.0  # no fresh full budget
    finally:
        if app._state.value not in ("stopped", "failed"):
            await clean_stop(app)
