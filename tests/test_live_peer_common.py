"""Common live-peer process ownership: readiness, release, failure paths.

Proves the shared ``PeerProcess`` owner with plain child processes — no radios,
no SDK imports. Assertions observe release and responsiveness (process reaped,
stdio closed, bounded waits) rather than transport-specific implementation.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.helpers.live_peer_common import (
    PeerProcess,
    async_packets_until,
    async_peer_context,
    poll_packets_until,
    read_jsonl,
)

_READY_TIMEOUT = 5.0


def _write_ready_child(ready_path: Path, scratch_path: Path, line: str) -> list[str]:
    """Child that writes evidence, signals readiness, and idles until killed."""
    return [
        sys.executable,
        "-c",
        (
            "import sys, time\n"
            "scratch, ready = sys.argv[1], sys.argv[2]\n"
            f"open(scratch, 'a').write({line!r} + '\\n')\n"
            "open(ready, 'w').write('1')\n"
            "time.sleep(60)\n"
        ),
        str(scratch_path),
        str(ready_path),
    ]


def _dying_child() -> list[str]:
    """Child that exits non-zero without ever signalling readiness."""
    return [sys.executable, "-c", "import sys; sys.exit(3)"]


def _silent_child() -> list[str]:
    """Child that stays alive but never signals readiness."""
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def test_start_returns_once_ready_file_exists(tmp_path: Path) -> None:
    ready = tmp_path / "peer.ready"
    owner = PeerProcess(ready, _READY_TIMEOUT)
    argv = _write_ready_child(ready, tmp_path / "peer.jsonl", '{"id": 1}')
    try:
        owner.start(argv)
        assert owner.poll() is None
    finally:
        owner.terminate()


def test_early_child_exit_raises_and_releases(tmp_path: Path) -> None:
    ready = tmp_path / "peer.ready"
    owner = PeerProcess(ready, _READY_TIMEOUT)
    with pytest.raises(AssertionError, match="died before readiness"):
        owner.start(_dying_child())
    assert owner.proc is not None
    assert owner.proc.poll() is not None
    assert owner.stdio_closed


def test_silent_child_raises_within_deadline_and_is_killed(tmp_path: Path) -> None:
    ready = tmp_path / "peer.ready"
    owner = PeerProcess(ready, 1.0)
    started = time.monotonic()
    with pytest.raises(AssertionError, match="never signalled ready"):
        owner.start(_silent_child())
    assert time.monotonic() - started < 5.0
    assert owner.proc is not None
    assert owner.proc.poll() is not None
    assert owner.stdio_closed


def test_exceptional_body_exit_releases_child(tmp_path: Path) -> None:
    """A body failure still reaps the child and closes capture handles."""
    ready = tmp_path / "peer.ready"
    owner = PeerProcess(ready, _READY_TIMEOUT)
    owner.start(_write_ready_child(ready, tmp_path / "peer.jsonl", "{}"))
    proc = owner.proc
    assert proc is not None
    try:
        raise RuntimeError("test body failure")
    except RuntimeError:
        owner.terminate()
    assert proc.poll() is not None
    assert owner.stdio_closed


def test_terminate_is_idempotent(tmp_path: Path) -> None:
    ready = tmp_path / "peer.ready"
    owner = PeerProcess(ready, _READY_TIMEOUT)
    owner.start(_write_ready_child(ready, tmp_path / "peer.jsonl", "{}"))
    owner.terminate()
    owner.terminate()
    assert owner.stdio_closed


def test_read_jsonl_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_jsonl(tmp_path / "absent.jsonl") == []


def test_poll_packets_until_returns_on_predicate(tmp_path: Path) -> None:
    scratch = tmp_path / "peer.jsonl"
    scratch.write_text('{"id": 1}\n{"id": 2}\n')
    packets = poll_packets_until(
        lambda: read_jsonl(scratch),
        lambda pkts: len(pkts) >= 2,
        timeout=2.0,
        interval=0.05,
    )
    assert [p["id"] for p in packets] == [1, 2]


def test_poll_packets_until_times_out_with_last_read(tmp_path: Path) -> None:
    scratch = tmp_path / "peer.jsonl"
    scratch.write_text('{"id": 1}\n')
    packets = poll_packets_until(
        lambda: read_jsonl(scratch),
        lambda pkts: len(pkts) >= 5,
        timeout=0.3,
        interval=0.05,
    )
    assert [p["id"] for p in packets] == [1]


def test_full_lifecycle_with_incremental_collection(tmp_path: Path) -> None:
    """Spawn → readiness → incremental read → drain-style teardown."""
    ready = tmp_path / "peer.ready"
    scratch = tmp_path / "peer.jsonl"
    owner = PeerProcess(ready, _READY_TIMEOUT)
    owner.start(_write_ready_child(ready, scratch, '{"id": 7}'))
    try:
        packets = poll_packets_until(
            lambda: read_jsonl(scratch),
            lambda pkts: bool(pkts),
            timeout=2.0,
            interval=0.05,
        )
        assert packets == [{"id": 7}]
    finally:
        owner.terminate()
    assert owner.proc is not None
    assert owner.proc.poll() is not None
    assert owner.stdio_closed


async def test_blocking_packet_wait_rejects_runtime_event_loop() -> None:
    with pytest.raises(RuntimeError, match="Blocking peer observation"):
        poll_packets_until(lambda: [], bool, timeout=0.1)
    assert poll_packets_until(lambda: [{"id": 1}], bool, timeout=0) == [{"id": 1}]


async def test_threaded_observation_allows_runtime_to_produce_packet() -> None:
    loop = asyncio.get_running_loop()
    reading = asyncio.Event()
    packets: list[dict] = []

    def read_packets() -> list[dict]:
        loop.call_soon_threadsafe(reading.set)
        return list(packets)

    async def deliver() -> None:
        await reading.wait()
        packets.append({"id": "runtime-delivery"})

    producer = asyncio.create_task(deliver())
    try:
        observed = await asyncio.to_thread(
            poll_packets_until, read_packets, bool, 1.0, 0.001
        )
        assert observed == [{"id": "runtime-delivery"}]
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


async def test_async_peer_lifecycle_keeps_runtime_responsive() -> None:
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    phases = []

    async def runtime_tick(phase):
        phases.append(phase)

    class Listener:
        def __enter__(self):
            assert threading.get_ident() != main_thread
            asyncio.run_coroutine_threadsafe(runtime_tick("enter"), loop).result(1)
            return self

        def __exit__(self, *_):
            assert threading.get_ident() != main_thread
            asyncio.run_coroutine_threadsafe(runtime_tick("exit"), loop).result(1)

    async with async_peer_context(Listener()):
        phases.append("body")
    assert phases == ["enter", "body", "exit"]


async def test_cancelled_peer_start_is_released_before_return() -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    closed = threading.Event()
    exits = []

    class Listener:
        def __enter__(self):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2)
            return self

        def __exit__(self, *_):
            exits.append(1)
            closed.set()

    async def run():
        async with async_peer_context(Listener()):
            pytest.fail("cancelled startup entered body")

    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert exits == [1]


@pytest.mark.parametrize(
    "args", [[], ["probe"], ["sendn", '["nonce"]'], ["sendts", "peer", "text"]]
)
def test_meshcore_peer_rejects_missing_arguments_before_connecting(args, monkeypatch):
    from tests.helpers import meshcore_live_peer

    def no_process(*_args, **_kwargs):
        pytest.fail("invalid peer arguments spawned a hardware client")

    monkeypatch.setattr(meshcore_live_peer.subprocess, "run", no_process)
    with pytest.raises(ValueError, match="BLE address"):
        meshcore_live_peer.run_meshcore_peer(args, 95)


async def test_native_meshcore_peer_uses_discovered_device_and_owned_reconnect(
    monkeypatch,
):
    import ast
    from types import SimpleNamespace

    from tests.helpers.meshcore_live_peer import _PEER_SCRIPT

    device = object()
    client = object()
    calls = []

    async def discover(address, **kwargs):
        calls.append((address, kwargs))
        return device

    async def connect(**kwargs):
        assert kwargs["device"] is device
        assert kwargs["auto_reconnect"] is False
        return client

    monkeypatch.setenv("MESHCORE_PEER_BLE_ADAPTER", "hci-test")
    monkeypatch.setitem(
        sys.modules,
        "bleak",
        SimpleNamespace(BleakScanner=SimpleNamespace(find_device_by_address=discover)),
    )
    monkeypatch.setitem(
        sys.modules,
        "meshcore",
        SimpleNamespace(
            MeshCore=SimpleNamespace(create_ble=connect), EventType=object()
        ),
    )
    tree = ast.parse(_PEER_SCRIPT)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_connect"
    )
    namespace = {"asyncio": asyncio, "os": os, "ADDRESS": "owned-peer"}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "<native-peer>", "exec"),
        namespace,
    )
    assert await namespace["_connect"]() is client
    assert calls == [("owned-peer", {"timeout": 8.0, "adapter": "hci-test"})]


@pytest.mark.parametrize("body_fails", [False, True])
async def test_repeated_cancellation_during_exit_joins_owner(body_fails):
    loop = asyncio.get_running_loop()
    exiting = asyncio.Event()
    release = threading.Event()
    exits = []

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            loop.call_soon_threadsafe(exiting.set)
            assert release.wait(3)
            exits.append(exc[0])

    async def run():
        async with async_peer_context(Listener()):
            if body_fails:
                raise ValueError("body failed")

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(exiting.wait(), 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exits == [ValueError if body_fails else None]


@pytest.mark.parametrize("suppress", [False, True])
async def test_async_context_preserves_body_exception_semantics(suppress):
    exits = []

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            exits.append(exc)
            return suppress

    async def run():
        async with async_peer_context(Listener()):
            raise ValueError("body failed")

    if suppress:
        await run()
    else:
        with pytest.raises(ValueError, match="body failed"):
            await run()
    assert len(exits) == 1
    assert exits[0][0] is ValueError


async def test_async_snapshot_poll_cancellation_joins_inflight_read():
    loop = asyncio.get_running_loop()
    reading = asyncio.Event()
    release = threading.Event()
    reads = []

    def snapshot(_predicate, timeout):
        assert timeout == 0
        loop.call_soon_threadsafe(reading.set)
        assert release.wait(3)
        reads.append(1)
        return []

    task = asyncio.create_task(async_packets_until(snapshot, bool, 100))
    try:
        await asyncio.wait_for(reading.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reads == [1]


async def test_async_snapshot_poll_keeps_runtime_responsive():
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    packets = []

    def snapshot(_predicate, timeout):
        assert timeout == 0
        loop.call_soon_threadsafe(ready.set)
        return list(packets)

    async def deliver():
        await ready.wait()
        packets.append({"id": 1})

    producer = asyncio.create_task(deliver())
    try:
        assert await async_packets_until(snapshot, bool, 2, 0.001) == [{"id": 1}]
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


def test_jsonl_ignores_only_uncommitted_trailing_record(tmp_path):
    scratch = tmp_path / "peer.jsonl"
    scratch.write_text('{"id": 1}\n{"id":')
    assert read_jsonl(scratch) == [{"id": 1}]
    with scratch.open("a") as file:
        file.write(" 2}\n")
    assert read_jsonl(scratch) == [{"id": 1}, {"id": 2}]
    scratch.write_text('{"id":\n')
    with pytest.raises(json.JSONDecodeError):
        read_jsonl(scratch)


def test_stale_ready_does_not_authorize_unready_child(tmp_path):
    ready = tmp_path / "peer.ready"
    ready.write_text("stale")
    owner = PeerProcess(ready, 0.1)
    with pytest.raises(AssertionError, match="never signalled ready"):
        owner.start(_silent_child())
    assert owner.stdio_closed
    assert owner.proc is not None and owner.proc.poll() is not None


def test_second_start_does_not_orphan_first_child(tmp_path):
    ready = tmp_path / "peer.ready"
    owner = PeerProcess(ready, _READY_TIMEOUT)
    try:
        owner.start(_write_ready_child(ready, tmp_path / "peer.jsonl", "{}"))
        proc = owner.proc
        with pytest.raises(RuntimeError, match="already running"):
            owner.start(_silent_child())
        assert owner.proc is proc and owner.poll() is None
    finally:
        owner.terminate()


@pytest.mark.parametrize(
    "args",
    [
        ["probe", " "],
        ["sendn", "owned", "not JSON"],
        ["sendn", "owned", "[]"],
        ["sendn", "owned", "[1]"],
        ["sendts", "owned", "text", "wrong", "2"],
        ["listen", "owned", "nan", "scratch", "ready"],
        ["listen", "owned", "0", "scratch", "ready"],
        ["listen", "owned", "1", "scratch", "scratch"],
    ],
)
def test_meshcore_peer_rejects_invalid_values_before_connecting(args, monkeypatch):
    from tests.helpers import meshcore_live_peer

    def no_process(*_args, **_kwargs):
        pytest.fail("invalid peer arguments spawned a hardware client")

    monkeypatch.setattr(meshcore_live_peer.subprocess, "run", no_process)
    with pytest.raises(ValueError):
        meshcore_live_peer.run_meshcore_peer(args, 95)


@pytest.mark.parametrize("timeout,interval", [(-1, 0.1), (float("nan"), 0.1), (1, 0)])
async def test_invalid_poll_window_rejected_before_read(timeout, interval):
    def read(*_):
        pytest.fail("invalid window must not start a read")

    with pytest.raises(ValueError):
        await async_packets_until(read, bool, timeout, interval)
    with pytest.raises(ValueError):
        poll_packets_until(read, bool, timeout, interval)


async def test_failed_cancelled_entry_does_not_call_exit():
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    class Listener:
        def __enter__(self):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
            raise ValueError("entry failed after its own cleanup")

        def __exit__(self, *_):
            pytest.fail("failed entry must own cleanup")

    async def run():
        async with async_peer_context(Listener()):
            pytest.fail("failed entry yielded")

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_failed_observer_cannot_supply_absence_evidence(tmp_path):
    owner = PeerProcess(tmp_path / "peer.ready", 1)
    with pytest.raises(AssertionError, match="died before readiness"):
        owner.start(_dying_child())
    with pytest.raises(AssertionError, match="exited early"):
        owner.raise_if_failed()
