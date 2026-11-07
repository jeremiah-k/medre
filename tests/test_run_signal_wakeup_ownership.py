# medre-test-tier: compatibility
"""Process wakeup-fd ownership and signal receipt accounting."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from types import SimpleNamespace

import pytest

from medre.cli import run_commands
from tests.helpers.async_utils import wait_until
from tests.helpers.compatibility_configs import CONFIG_SINGLE_ADAPTER


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_existing_host_wakeup_fd_is_not_stolen() -> None:
    host_read, host_write = os.pipe()
    os.set_blocking(host_read, False)
    os.set_blocking(host_write, False)
    previous_fd = signal.set_wakeup_fd(host_write)
    signals = run_commands._RunSignals()
    try:
        signals.install(run_commands._request_shutdown)
        assert run_commands._wakeup_fd_installed is False
        assert run_commands._wakeup_read_fd is None
        assert run_commands._wakeup_write_fd is None
        assert signal.set_wakeup_fd(host_write) == host_write
        signals.restore()
        assert signal.set_wakeup_fd(host_write) == host_write
    finally:
        signals.restore()
        signal.set_wakeup_fd(previous_fd)
        os.close(host_write)
        os.close(host_read)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_late_host_replacement_survives_run_cleanup() -> None:
    host_read, host_write = os.pipe()
    os.set_blocking(host_read, False)
    os.set_blocking(host_write, False)
    previous_fd = signal.set_wakeup_fd(-1)
    signals = run_commands._RunSignals()
    try:
        signals.install(run_commands._request_shutdown)
        assert run_commands._wakeup_fd_installed is True
        assert signal.set_wakeup_fd(host_write) == run_commands._wakeup_write_fd
        signals.restore()
        assert signal.set_wakeup_fd(host_write) == host_write
    finally:
        signals.restore()
        signal.set_wakeup_fd(previous_fd)
        os.close(host_write)
        os.close(host_read)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_draining_is_owned_by_run_loop_not_signal_handler(monkeypatch) -> None:
    run_commands._reset_shutdown_evidence()
    try:
        drained = []
        monkeypatch.setattr(
            run_commands, "_drain_wakeup_pipe", lambda: drained.append(1)
        )
        monkeypatch.setattr(run_commands, "_shutdown_wake_callback", None)
        run_commands._request_shutdown(signal.SIGINT, None)
        assert run_commands.shutdown_requested
        assert run_commands.shutdown_signal_count == 1
        assert drained == []
        run_commands._drain_wakeup_pipe()
        assert drained == [1]
    finally:
        run_commands._reset_shutdown_evidence()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_host_receives_signal_bytes_captured_during_ownership_probe(monkeypatch):
    host_read, host_write = os.pipe()
    os.set_blocking(host_read, False)
    os.set_blocking(host_write, False)
    set_fd = signal.set_wakeup_fd
    previous_fd = set_fd(host_write)
    signals = run_commands._RunSignals()
    injected = False

    def inject_during_probe(fd):
        nonlocal injected
        previous = set_fd(fd)
        if previous == host_write and fd != host_write and not injected:
            injected = True
            os.write(fd, bytes([signal.SIGINT, signal.SIGTERM]))
        return previous

    monkeypatch.setattr(signal, "set_wakeup_fd", inject_during_probe)
    try:
        signals.install(run_commands._request_shutdown)
        assert injected
        assert os.read(host_read, 10) == bytes([signal.SIGINT, signal.SIGTERM])
        assert run_commands._wakeup_fd_installed is False
    finally:
        signals.restore()
        set_fd(previous_fd)
        os.close(host_read)
        os.close(host_write)


@pytest.mark.parametrize("signal_together", [False, True])
async def test_app_wait_failure_surfaces_without_polling(monkeypatch, signal_together):
    run_commands._reset_shutdown_evidence()
    monkeypatch.setattr(run_commands, "_SHUTDOWN_POLL_SECONDS", 30.0)
    entered, release, wake = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def fail():
        entered.set()
        await release.wait()
        if signal_together:
            run_commands.shutdown_requested = True
            wake.set()
        raise RuntimeError("app wait failed")

    waiting = asyncio.create_task(
        run_commands._wait_for_run_shutdown(
            SimpleNamespace(wait_for_shutdown=fail), wake
        )
    )
    try:
        await entered.wait()
        release.set()
        with pytest.raises(RuntimeError, match="app wait failed"):
            await asyncio.wait_for(waiting, 2)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        run_commands._reset_shutdown_evidence()


# ---------------------------------------------------------------------------
# Wakeup-pipe install, ownership, and drain
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_wakeup_pipe_install_failure_downgrades_to_handler_counts(monkeypatch) -> None:
    """A refused wakeup fd leaves no pipe; handler-count evidence survives."""
    real_set_wakeup_fd = signal.set_wakeup_fd

    def refuse(fd: int) -> int:
        if fd != -1:
            raise ValueError("wakeup fd unavailable")
        return real_set_wakeup_fd(fd)

    monkeypatch.setattr(signal, "set_wakeup_fd", refuse)
    run_commands._reset_shutdown_evidence()
    signals = run_commands._RunSignals()
    try:
        signals.install(run_commands._request_shutdown)
        assert run_commands._wakeup_fd_installed is False
        assert run_commands._wakeup_read_fd is None
        assert run_commands._wakeup_write_fd is None
        # The handler path still records requests, and the run loop's drain
        # without a pipe is a no-op rather than an error.
        run_commands._request_shutdown(signal.SIGINT, None)
        assert run_commands.shutdown_signal_count == 1
        run_commands._drain_wakeup_pipe()
        assert run_commands.shutdown_delivered_count == 0
    finally:
        signals.restore()
        run_commands._reset_shutdown_evidence()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_close_wakeup_pipe_without_installed_ownership_preserves_host_fd() -> None:
    """Closing an unregistered pipe leaves the process wakeup fd alone.

    Simulates a close racing a failed install: the pipe ends were recorded
    but the process wakeup fd was never (or no longer) ours to detach.
    """
    host_read, host_write = os.pipe()
    os.set_blocking(host_read, False)
    os.set_blocking(host_write, False)
    previous_fd = signal.set_wakeup_fd(host_write)
    orphan_read, orphan_write = os.pipe()
    run_commands._wakeup_read_fd = orphan_read
    run_commands._wakeup_write_fd = orphan_write
    run_commands._wakeup_fd_installed = False
    try:
        run_commands._close_wakeup_pipe()
        assert run_commands._wakeup_read_fd is None
        assert run_commands._wakeup_write_fd is None
        # The host descriptor is still the process wakeup fd; nothing detached.
        assert signal.set_wakeup_fd(host_write) == host_write
    finally:
        run_commands._close_wakeup_pipe()
        signal.set_wakeup_fd(previous_fd)
        os.close(host_write)
        os.close(host_read)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_forward_probe_wakeups_returns_once_the_probe_pipe_is_drained() -> None:
    """Probe forwarding ends at EOF instead of spinning on an empty pipe."""
    probe_read, probe_write = os.pipe()
    host_read, host_write = os.pipe()
    for fd in (probe_read, probe_write, host_read, host_write):
        os.set_blocking(fd, False)
    try:
        os.write(probe_write, bytes([signal.SIGINT]))
        os.close(probe_write)
        probe_write = None  # closed; the finally skips None entries
        run_commands._forward_probe_wakeups(probe_read, host_write)
        assert os.read(host_read, 8) == bytes([signal.SIGINT])
    finally:
        for fd in (probe_read, probe_write, host_write, host_read):
            if fd is not None:
                os.close(fd)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_pipe_exhaustion_downgrades_to_handler_counts(monkeypatch) -> None:
    """Descriptor exhaustion degrades the receipt without breaking handlers."""

    def exhausted() -> tuple[int, int]:
        raise OSError("too many open files")

    monkeypatch.setattr(run_commands.os, "pipe", exhausted)
    run_commands._reset_shutdown_evidence()
    signals = run_commands._RunSignals()
    try:
        signals.install(run_commands._request_shutdown)
        assert run_commands._wakeup_fd_installed is False
        assert run_commands._wakeup_read_fd is None
        assert run_commands._wakeup_write_fd is None
        # The handler path still records requests.
        run_commands._request_shutdown(signal.SIGINT, None)
        assert run_commands.shutdown_signal_count == 1
    finally:
        signals.restore()
        run_commands._reset_shutdown_evidence()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_drain_counts_only_shutdown_signal_deliveries() -> None:
    """Each buffered shutdown byte counts once; foreign bytes do not count.

    Writes simulate the C-level handler's one byte per delivered signal.
    """
    run_commands._reset_shutdown_evidence()
    run_commands._install_wakeup_pipe()
    try:
        assert run_commands._wakeup_write_fd is not None
        os.write(
            run_commands._wakeup_write_fd,
            bytes([signal.SIGINT, signal.SIGINT, signal.SIGTERM, 42]),
        )
        run_commands._drain_wakeup_pipe()
        assert run_commands.shutdown_delivered_count == 3
        # A drained pipe contributes nothing further.
        run_commands._drain_wakeup_pipe()
        assert run_commands.shutdown_delivered_count == 3
        # A concurrent closer releases process ownership before closing the
        # write end; the drain loop then ends cleanly at EOF instead of
        # relying on the empty-pipe error path.
        signal.set_wakeup_fd(-1)
        run_commands._wakeup_fd_installed = False
        os.close(run_commands._wakeup_write_fd)
        run_commands._wakeup_write_fd = None
        run_commands._drain_wakeup_pipe()
        assert run_commands.shutdown_delivered_count == 3
    finally:
        run_commands._close_wakeup_pipe()
        run_commands._reset_shutdown_evidence()


# ---------------------------------------------------------------------------
# Handler wake path
# ---------------------------------------------------------------------------


async def test_signal_wake_reaches_the_running_loop() -> None:
    """The handler's wake callback starts teardown at servicing time."""
    loop = asyncio.get_running_loop()
    woke = asyncio.Event()
    run_commands._reset_shutdown_evidence()
    run_commands._shutdown_wake_callback = lambda: loop.call_soon_threadsafe(woke.set)
    try:
        run_commands._request_shutdown(signal.SIGINT, None)
        assert await wait_until(woke.is_set, timeout=5.0)
    finally:
        run_commands._shutdown_wake_callback = None
        run_commands._reset_shutdown_evidence()


async def test_closed_loop_wake_is_swallowed() -> None:
    """A wake racing loop closure cannot crash the signal handler."""

    def closed_loop_wake() -> None:
        raise RuntimeError("Event loop is closed")

    run_commands._reset_shutdown_evidence()
    run_commands._shutdown_wake_callback = closed_loop_wake
    try:
        run_commands._request_shutdown(signal.SIGINT, None)  # must not raise
    finally:
        run_commands._shutdown_wake_callback = None
        run_commands._reset_shutdown_evidence()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wakeup fd")
def test_install_rolls_back_when_signal_registration_fails(monkeypatch) -> None:
    """A refused registration restores handlers and releases the pipe."""
    real_signal = signal.signal
    previous_int = signal.getsignal(signal.SIGINT)

    def refuse_sigterm(signum: int, handler: object) -> object:
        if signum == signal.SIGTERM:
            raise OSError("SIGTERM registration refused")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", refuse_sigterm)
    signals = run_commands._RunSignals()
    with pytest.raises(OSError, match="SIGTERM registration refused"):
        signals.install(run_commands._request_shutdown)
    assert run_commands._wakeup_fd_installed is False
    assert run_commands._wakeup_read_fd is None
    assert run_commands._shutdown_wake_callback is None
    # SIGINT was rolled back to its previous disposition during restore.
    assert signal.getsignal(signal.SIGINT) == previous_int


# ---------------------------------------------------------------------------
# _wait_for_run_shutdown ownership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wake_pre_set", [False, True])
async def test_clean_app_completion_rearms_and_stays_non_terminal(
    monkeypatch, wake_pre_set
):
    """A clean completion is not terminal; the app waiter is rearmed once."""
    run_commands._reset_shutdown_evidence()
    monkeypatch.setattr(run_commands, "_SHUTDOWN_POLL_SECONDS", 0.1)
    calls = 0
    rearmed = asyncio.Event()
    wake = asyncio.Event()
    if wake_pre_set:
        wake.set()

    async def wait_for_shutdown(timeout=None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return  # clean completion without a shutdown request
        rearmed.set()
        await asyncio.Event().wait()

    waiting = asyncio.create_task(
        run_commands._wait_for_run_shutdown(
            SimpleNamespace(wait_for_shutdown=wait_for_shutdown), wake
        )
    )
    try:
        assert await wait_until(rearmed.is_set, timeout=5.0)
        assert calls == 2
        # A serviced signal then ends the loop; the pending waiter is owned.
        run_commands.shutdown_requested = True
        wake.set()
        await asyncio.wait_for(waiting, 5.0)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        run_commands._reset_shutdown_evidence()
    assert calls == 2  # no third call: the rearm is paced, not a spin


async def test_signal_racing_clean_completion_still_retrieves_the_app_result():
    """A completion racing a serviced signal still retrieves the app result."""
    run_commands._reset_shutdown_evidence()
    completed = asyncio.Event()
    release = asyncio.Event()
    wake = asyncio.Event()

    async def wait_for_shutdown(timeout=None) -> None:
        completed.set()
        await release.wait()

    waiting = asyncio.create_task(
        run_commands._wait_for_run_shutdown(
            SimpleNamespace(wait_for_shutdown=wait_for_shutdown), wake
        )
    )
    try:
        await completed.wait()
        # The signal lands as the clean completion is processed; the loop must
        # still retrieve the app result before exiting.
        release.set()
        run_commands.shutdown_requested = True
        wake.set()
        await asyncio.wait_for(waiting, 5.0)
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        run_commands._reset_shutdown_evidence()


# ---------------------------------------------------------------------------
# In-process run through a real SIGINT
# ---------------------------------------------------------------------------


class _FakeShutdownApp:
    """Minimal app surface driving `_run` to the shutdown receipt."""

    def __init__(self) -> None:
        self.build_failures: list[str] = []
        self.adapters: list[object] = [object()]
        self.route_eligibility = None
        self.live_health = None
        self.boot_summary = None
        self.adapter_states: dict[str, object] = {}
        self.started_adapter_ids: list[str] = []
        self._failed_adapter_ids: list[str] = []
        self.storage = None
        self._runtime_accounting = None
        self.stop_calls = 0

    async def start(self) -> None:
        return None

    async def wait_for_shutdown(self, timeout=None) -> None:
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stop_calls += 1


#: Hang guard for the in-process run; the wake path answers in milliseconds.
_SIGNAL_ACK_BOUND_SECONDS = 10.0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX os.kill delivery")
async def test_run_wakes_teardown_from_a_real_sigint_and_counts_the_delivery(
    tmp_path, monkeypatch, isolated_config_env
):
    """A real SIGINT wakes the loop and the pipe records the delivery."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_SINGLE_ADAPTER)
    app = _FakeShutdownApp()

    class _FakeBuilder:
        def __init__(self, config: object, paths: object) -> None:
            self._app = app

        def build(self) -> _FakeShutdownApp:
            return self._app

    monkeypatch.setattr("medre.runtime.builder.RuntimeBuilder", _FakeBuilder)
    printed: list[str] = []
    monkeypatch.setattr(
        run_commands, "_print", lambda line="", **_: printed.append(line)
    )
    baseline_fd = signal.set_wakeup_fd(-1)
    signal.set_wakeup_fd(baseline_fd)

    run_commands._reset_shutdown_evidence()
    run_task = asyncio.create_task(run_commands._run(str(config_path), None))
    try:
        # Handlers and the wakeup pipe are owned before the press.
        assert await wait_until(
            lambda: run_commands._shutdown_wake_callback is not None,
            timeout=_SIGNAL_ACK_BOUND_SECONDS,
        )
        os.kill(os.getpid(), signal.SIGINT)
        assert await wait_until(
            lambda: run_commands.shutdown_requested,
            timeout=_SIGNAL_ACK_BOUND_SECONDS,
        )
        await asyncio.wait_for(run_task, 30.0)
        assert app.stop_calls == 1  # owned teardown ran exactly once
        assert run_commands.shutdown_signal_count == 1
        assert run_commands.shutdown_delivered_count == 1  # pipe counted the delivery
        receipts = [line for line in printed if "Shutdown requested" in line]
        assert len(receipts) == 1
        assert "1 time(s)" in receipts[0]
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
        run_commands._reset_shutdown_evidence()

    observed_fd = signal.set_wakeup_fd(-1)
    assert observed_fd == baseline_fd  # process wakeup fd ownership restored
    signal.set_wakeup_fd(observed_fd)
    assert run_commands._wakeup_fd_installed is False
