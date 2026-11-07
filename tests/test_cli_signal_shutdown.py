# medre-test-tier: compatibility
"""Real-process signal contract for the run command's shutdown path.

These tests spawn the actual CLI entry (``medre.cli.run_commands._run``) in a
child process, observe its output line by line from the parent, and deliver
real signals with ``os.kill``. They pin the operator-visible shutdown contract
that the in-process suites cannot see end to end:

- one SIGINT or SIGTERM after readiness enters teardown within the runtime's
  one-second polling interval (plus generous scheduling slack),
- teardown completes with every adapter stopped and honest drain evidence,
- repeated signals during teardown never interrupt owned cleanup,
- a signal before readiness never wedges the process,
- synchronous work occupying the loop thread defers the acknowledgment until
  a deterministic parent-controlled release; owned teardown then completes,
- the first of two adjacent presses starts teardown at servicing time rather
  than waiting out the poll tick, and a press landing during teardown is still
  counted,
- presses that coalesce behind one CPython servicing round (delivered while
  the loop thread is un-serviced inside a C call) are all counted through the
  wakeup pipe, so the receipt never undercounts what the operator pressed.

Deadlines here are hang guards, not latency requirements, except where a test
explicitly bounds the acknowledgment latency. Coordination uses output-line
handshakes, never sleeps.
"""

from __future__ import annotations

import os
import platform
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import medre
from tests.helpers.compatibility_configs import CONFIG_SINGLE_ADAPTER

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="subprocess os.kill signal delivery exercises POSIX handlers",
)

_READY_MARKERS = ("Runtime ready (", "Runtime running (")
_STARTUP_HELD_MARKER = "STARTUP_HELD"
_STOP_HELD_MARKER = "STOP_HELD"
_SHUTDOWN_MARKER = "Runtime shutting down"
_STOPPED_MARKER = "Runtime stopped (1 adapter(s))"
_DRAIN_OK_MARKER = "drain completed"
_DRAIN_TIMEOUT_MARKER = "drain timed out"
_BLOCK_ENTERED_MARKER = "BLOCK_ENTERED"
_C_BLOCK_ENTERED_MARKER = "C_BLOCK_ENTERED"
_C_BLOCK_DONE_MARKER = "C_BLOCK_DONE"

#: Generous bound for the healthy signal-to-teardown path: the run loop polls
#: shutdown once per second, so a healthy acknowledgment lands well inside a
#: few seconds even on a loaded runner, while a wedged loop does not.
_ACK_BOUND_SECONDS = 10.0

_CHILD_SCRIPT = """\
import asyncio
import sys

config_path, mode, expected_src = sys.argv[1:4]

import medre

if not medre.__file__.startswith(expected_src):
    print(f"IMPORT_PROVENANCE_ERROR {medre.__file__}", flush=True)
    raise SystemExit(99)

if mode == "block":
    from medre.runtime.app import MedreApp

    _original_wait = MedreApp.wait_for_shutdown

    async def blocking_wait(self, timeout=None):
        if not getattr(self, "_block_entered", False):
            self._block_entered = True
            print("BLOCK_ENTERED", flush=True)
            sys.stdin.readline()
        return await _original_wait(self, timeout=timeout)

    MedreApp.wait_for_shutdown = blocking_wait
elif mode == "cblock":
    # Loop thread blocked inside a C call that never returns on EINTR
    # (POSIX: pthread_cond_timedwait must not fail with EINTR). Signal
    # deliveries during the window coalesce into one Python-level handler
    # invocation when the call returns — the un-serviced window behind the
    # field reports of a swallowed first Ctrl+C.
    import ctypes
    import time as _time

    from medre.runtime.app import MedreApp

    _original_wait = MedreApp.wait_for_shutdown
    _libc = ctypes.PyDLL(None)

    class _Timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    _cond = ctypes.c_buffer(96)
    _mtx = ctypes.c_buffer(64)
    assert _libc.pthread_cond_init(_cond, None) == 0
    assert _libc.pthread_mutex_init(_mtx, None) == 0

    async def c_blocked_wait(self, timeout=None):
        if not getattr(self, "_cblock_entered", False):
            self._cblock_entered = True
            print("C_BLOCK_ENTERED", flush=True)
            assert _libc.pthread_mutex_lock(_mtx) == 0
            deadline = _Timespec(int(_time.time()) + 3, 0)
            _libc.pthread_cond_timedwait(_cond, _mtx, ctypes.byref(deadline))
            _libc.pthread_mutex_unlock(_mtx)
            print("C_BLOCK_DONE", flush=True)
        return await _original_wait(self, timeout=timeout)

    MedreApp.wait_for_shutdown = c_blocked_wait
elif mode == "stop":
    from medre.runtime.app import MedreApp

    _original_stop = MedreApp.stop

    async def held_stop(self, *args, **kwargs):
        print("STOP_HELD", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        return await _original_stop(self, *args, **kwargs)

    MedreApp.stop = held_stop
elif mode == "startup":
    from medre.core.engine.pipeline.runner import PipelineRunner

    _original_start = PipelineRunner.start

    async def held_start(self):
        await _original_start(self)
        print("STARTUP_HELD", flush=True)
        await asyncio.Event().wait()

    PipelineRunner.start = held_start

from medre.cli import run_commands
if mode == "stop":
    # The fallback poll must be too long to satisfy the wakeup-path bound.
    run_commands._SHUTDOWN_POLL_SECONDS = 30.0

try:
    asyncio.run(run_commands._run(config_path, None))
except KeyboardInterrupt:
    # Mirrors the real CLI entry: a signal during the pre-handler window
    # (config load, build) cancels the runner's main task; the owned
    # rollback inside startup cleanup has already run by this point.
    pass
"""


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_SINGLE_ADAPTER)
    return config_path


class _ChildRun:
    """Run the real CLI entry in a child process with line-level observation."""

    def __init__(self, config_path: Path, tmp_path: Path, mode: str = "normal"):
        src_root = str(Path(medre.__file__).resolve().parents[1])
        repo_root = str(Path(__file__).resolve().parents[1])
        inherited = os.environ.get("PYTHONPATH")
        child_path = os.pathsep.join(p for p in (src_root, repo_root, inherited) if p)
        env = {
            **os.environ,
            "MEDRE_HOME": str(tmp_path / "home"),
            "PYTHONPATH": child_path,
        }
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD_SCRIPT,
                str(config_path),
                mode,
                src_root,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        self.output: list[str] = []
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._eof = threading.Event()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._closed = False

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line.rstrip("\n"))
        self._lines.put(None)
        self._eof.set()

    def _consume(self, line: str | None, timeout_hint: str) -> None:
        if line is None:
            self.fail_child(f"child exited before {timeout_hint}")
        self.output.append(line)

    def fail_child(self, reason: str) -> None:
        """Kill the child if it is alive, then fail with captured output."""
        self.kill_if_running()
        pytest.fail(f"{reason}\n--- child output ---\n" + "\n".join(self.output))

    def wait_for_line(self, markers: tuple[str, ...] | str, deadline_s: float) -> str:
        wanted = (markers,) if isinstance(markers, str) else markers
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=0.1)
            except queue.Empty:
                continue
            self._consume(line, f"emitting any of {wanted}")
            if any(marker in line for marker in wanted):
                return line
        self.fail_child(f"no line matching {wanted} within {deadline_s}s")

    def send_signal(self, signum: int) -> float:
        os.kill(self.proc.pid, signum)
        return time.monotonic()

    def wait_exit(self, deadline_s: float) -> int:
        deadline = time.monotonic() + deadline_s
        exited = False
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                exited = True
                break
            try:
                line = self._lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is not None:
                self.output.append(line)
        if not exited:
            self.fail_child(f"child did not exit within {deadline_s}s")
        if not self._eof.wait(10.0):
            self.fail_child("output reader never reached end of stream")
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                self.output.append(line)
        self._close_stream()
        return self.proc.returncode

    def release(self) -> None:
        """Release a parent-controlled child phase without a scheduling sleep."""
        assert self.proc.stdin is not None
        self.proc.stdin.write("continue\n")
        self.proc.stdin.flush()

    def _close_stream(self) -> None:
        if not self._closed and self.proc.stdout is not None:
            self._closed = True
            self.proc.stdout.close()
            if self.proc.stdin is not None:
                self.proc.stdin.close()

    def kill_if_running(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._eof.wait(10.0)
        self._close_stream()


def _assert_clean_exit(child: _ChildRun) -> None:
    """The healthy teardown evidence: bounded exit, drained, every owner stopped."""
    joined = "\n".join(child.output)
    assert "Traceback" not in joined
    assert _DRAIN_OK_MARKER in joined
    assert _DRAIN_TIMEOUT_MARKER not in joined
    assert _STOPPED_MARKER in joined


def _assert_signal_receipt(child: _ChildRun, expected_count: int) -> None:
    """Teardown reports how many signals arrived and when the first one did.

    This is the timestamp the field log lacked: the reaction time between the
    first request and teardown entry is measured inside the process.
    """
    receipt = [
        line
        for line in child.output
        if "Shutdown requested" in line and "time(s)" in line
    ]
    assert len(receipt) == 1
    assert f"{expected_count} time(s)" in receipt[0]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_one_signal_enters_teardown_with_bounded_latency(tmp_path, signum):
    """A single signal after readiness reaches teardown inside the poll bound."""
    child = _ChildRun(_write_config(tmp_path), tmp_path)
    try:
        child.wait_for_line(_READY_MARKERS, 60)
        sent_at = child.send_signal(signum)
        child.wait_for_line(_SHUTDOWN_MARKER, _ACK_BOUND_SECONDS + 20)
        latency = time.monotonic() - sent_at
        assert child.wait_exit(30) == 0
    finally:
        child.kill_if_running()
    assert latency < _ACK_BOUND_SECONDS
    _assert_clean_exit(child)
    _assert_signal_receipt(child, 1)


def test_repeated_signals_during_teardown_do_not_interrupt_cleanup(tmp_path):
    """A second signal while teardown runs must not corrupt owned cleanup."""
    child = _ChildRun(_write_config(tmp_path), tmp_path, "stop")
    try:
        child.wait_for_line(_READY_MARKERS, 60)
        child.send_signal(signal.SIGINT)
        child.wait_for_line(_STOP_HELD_MARKER, 30)
        child.send_signal(signal.SIGINT)
        child.release()
        assert child.wait_exit(30) == 0
    finally:
        child.kill_if_running()
    _assert_clean_exit(child)
    # The repeated press is coalesced into the same owned teardown and is
    # observed by the handler before the receipt snapshot.
    _assert_signal_receipt(child, 2)


def test_signal_before_readiness_never_wedges_the_process(tmp_path):
    """A signal during owned startup rolls back initialized resources."""
    child = _ChildRun(_write_config(tmp_path), tmp_path, "startup")
    try:
        child.wait_for_line(_STARTUP_HELD_MARKER, 60)
        child.send_signal(signal.SIGINT)
        assert child.wait_exit(30) == 0
    finally:
        child.kill_if_running()
    joined = "\n".join(child.output)
    assert "Traceback" not in joined
    assert not any(marker in joined for marker in _READY_MARKERS)
    assert "Storage closed during startup cleanup" in joined


def test_sync_loop_work_defers_acknowledgment_until_released(tmp_path):
    """Synchronous loop work delays shutdown until its parent-controlled release."""
    child = _ChildRun(_write_config(tmp_path), tmp_path, "block")
    try:
        child.wait_for_line(_READY_MARKERS, 60)
        child.wait_for_line(_BLOCK_ENTERED_MARKER, 30)
        child.send_signal(signal.SIGINT)
        assert child.proc.poll() is None
        assert not any(_SHUTDOWN_MARKER in line for line in child.output)
        child.release()
        child.wait_for_line(_SHUTDOWN_MARKER, _ACK_BOUND_SECONDS)
        assert child.wait_exit(30) == 0
    finally:
        child.kill_if_running()
    _assert_clean_exit(child)
    _assert_signal_receipt(child, 1)


#: The stop-mode fallback poll is 30s. A generous 5s bound catches a lost
#: wakeup while accommodating output/logging latency on busy CI runners.
_FIRST_PRESS_TEARDOWN_BOUND_SECONDS = 5.0


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_first_of_adjacent_presses_starts_teardown_immediately(tmp_path, signum):
    """An adjacent pair begins teardown from the first press, not the second.

    Teardown is pinned open so the second press lands inside it and is
    counted, mirroring the field pattern where teardown spans the presses.
    """
    child = _ChildRun(_write_config(tmp_path), tmp_path, "stop")
    try:
        child.wait_for_line(_READY_MARKERS, 60)
        sent_at = child.send_signal(signum)
        child.wait_for_line(_STOP_HELD_MARKER, _ACK_BOUND_SECONDS)
        first_press_latency = time.monotonic() - sent_at
        assert first_press_latency < _FIRST_PRESS_TEARDOWN_BOUND_SECONDS
        child.send_signal(signum)
        child.release()
        assert child.wait_exit(30) == 0
    finally:
        child.kill_if_running()
    _assert_clean_exit(child)
    _assert_signal_receipt(child, 2)


@pytest.mark.skipif(
    not (
        sys.platform == "linux"
        and platform.libc_ver()[0] == "glibc"
        and platform.machine().lower() in {"x86_64", "amd64"}
    ),
    reason="native pthread test uses glibc x86-64 mutex/condition layouts",
)
def test_coalesced_presses_are_counted_through_the_wakeup_pipe(tmp_path):
    """Presses delivered inside one un-serviced window are all counted.

    While the loop thread is blocked in a C call that never returns on EINTR,
    CPython services pending signals only once, after the call returns, and
    coalesces every delivery behind it. Before the wakeup pipe existed the
    receipt then reported one request for two presses and the first-request
    timestamp pointed at the servicing round instead of the presses. The
    pipe records one byte per delivery, so both presses are reported.
    """
    child = _ChildRun(_write_config(tmp_path), tmp_path, "cblock")
    try:
        child.wait_for_line(_READY_MARKERS, 60)
        child.wait_for_line(_C_BLOCK_ENTERED_MARKER, 30)
        # Land both deliveries inside the three-second un-serviced window.
        # Entry is proven by the marker; the offsets keep the presses clear
        # of the call's entry window and of each other. The native window lasts
        # two to three seconds, leaving ample margin after the second press. The spacing is
        # allowlisted in test_test_suite_structure.py: an un-serviced
        # native call exposes no observable condition to wait on, so
        # wait_until/Event cannot replace it without changing what the
        # test reproduces.
        time.sleep(0.15)
        child.send_signal(signal.SIGINT)
        time.sleep(0.2)
        child.send_signal(signal.SIGINT)
        child.wait_for_line(_C_BLOCK_DONE_MARKER, 30)
        child.wait_for_line(_SHUTDOWN_MARKER, _ACK_BOUND_SECONDS)
        assert child.wait_exit(30) == 0
    finally:
        child.kill_if_running()
    _assert_clean_exit(child)
    _assert_signal_receipt(child, 2)
