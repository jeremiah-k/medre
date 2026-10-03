"""Independent collection gates for RF relations and peer-power tests."""

import importlib
import asyncio
import inspect
import runpy
import subprocess
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest


@pytest.mark.parametrize("hub_configured", [False, True])
def test_lxmf_relations_do_not_require_peer_power_control(
    monkeypatch: pytest.MonkeyPatch, hub_configured: bool,
) -> None:
    # Preserve helper endpoint snapshots before installing synthetic inputs.
    importlib.import_module("tests.helpers.lxmf_live_peer")
    monkeypatch.setenv("LXMF_PAIR", "1")
    monkeypatch.setenv("MEDRE_LIVE_QUICK", "0")
    for name in (
        "LXMF_MEDRE_RNS_CONFIG", "LXMF_PEER_RNS_CONFIG",
        "LXMF_MEDRE_IDENTITY", "LXMF_PEER_IDENTITY",
    ):
        monkeypatch.setenv(name, "test-endpoint")
    for name in ("LXMF_PEER_HUB", "LXMF_PEER_HUB_PORT"):
        if hub_configured:
            monkeypatch.setenv(name, "test-hub")
        else:
            monkeypatch.delenv(name, raising=False)
    namespace = runpy.run_path(str(Path(__file__).with_name("test_lxmf_pair_live.py")))
    relations = namespace["TestLxmfPairRelationsAndIsolation"]
    power = namespace["test_rf_off_control_and_restored_delivery"]
    assert not any(mark.args[0] for mark in relations.pytestmark if mark.name == "skipif")
    assert any(mark.args[0] for mark in power.pytestmark if mark.name == "skipif") is not hub_configured


@pytest.fixture
def power_case(monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Execute the real power testcase with simulated external endpoints."""
    namespace = runpy.run_path(str(Path(__file__).with_name("test_lxmf_pair_live.py")))
    case = namespace["test_rf_off_control_and_restored_delivery"]
    globals_ = case.__globals__
    app = SimpleNamespace(adapters={
        "lab_src": SimpleNamespace(simulate_inbound=AsyncMock()),
        "lx_radio": SimpleNamespace(session=SimpleNamespace(delivery_state_counts=lambda: {})),
    })

    class Listener:
        def __init__(self, seconds: float) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def packets_until(self, predicate, timeout: float) -> list:
            return [{"content": "N6-ghost"}] if timeout == 300.0 else []

    async def ready(predicate, **kwargs) -> bool:
        result = predicate()
        return bool(await result) if inspect.isawaitable(result) else bool(result)

    stop = AsyncMock()
    for key, value in {
        "_launch": AsyncMock(return_value=app), "_stop_app": stop,
        "_await_peer_recall": AsyncMock(return_value=True), "_PEER_DEST": lambda: "peer",
        "_events_with_body": AsyncMock(return_value=[object()]),
        "_PeerListener": Listener, "_nonce": lambda label: label, "wait_until": ready,
    }.items():
        monkeypatch.setitem(globals_, key, value)
    return case, globals_, stop


async def test_power_off_failure_restores_hub_and_stops_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, power_case: tuple,
) -> None:
    case, globals_, stop = power_case
    actions = []

    def run(args, **kwargs):
        if "-a" in args:
            actions.append(args[-1])
            if args[-1] == "off":
                raise subprocess.TimeoutExpired(args, 20)
        return SimpleNamespace(stdout="power", stderr="")

    monkeypatch.setitem(globals_, "subprocess", SimpleNamespace(run=run))
    with pytest.raises(subprocess.TimeoutExpired):
        await case(tmp_path)
    assert actions == ["off", "on"]
    stop.assert_awaited_once()


async def test_power_off_cancellation_settles_worker_before_restoration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, power_case: tuple,
) -> None:
    case, globals_, stop = power_case
    started, release = Event(), Event()
    actions = []

    def run(args, **kwargs):
        if "-a" in args:
            if args[-1] == "off":
                actions.append("off-start")
                started.set()
                assert release.wait(2), "test worker was not released"
                actions.append("off-end")
            else:
                actions.append("on")
        return SimpleNamespace(stdout="power", stderr="")

    monkeypatch.setitem(globals_, "subprocess", SimpleNamespace(run=run))
    task = asyncio.create_task(case(tmp_path))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert actions == ["off-start", "off-end", "on"]
    stop.assert_awaited_once()


async def test_queued_ghost_alone_cannot_prove_fresh_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, power_case: tuple,
) -> None:
    case, globals_, stop = power_case
    power = {"state": "on"}

    def run(args, **kwargs):
        if "-a" in args:
            power["state"] = args[-1]
        return SimpleNamespace(stdout="0000" if power["state"] == "off" else "power")

    monkeypatch.setitem(globals_, "subprocess", SimpleNamespace(run=run))
    with pytest.raises(AssertionError, match="fresh nonce not delivered"):
        await case(tmp_path)
    assert power["state"] == "on"
    stop.assert_awaited_once()
