"""Shared ownership primitives for the native live-peer listeners.

One small owner for the process/readiness/teardown skeleton that the
Meshtastic, MeshCore, and LXMF peer listeners previously re-implemented
(with drift in stream-close guards and scratch-path naming). Everything
transport-specific stays in the per-transport modules: the child script,
argv, packet semantics, RF windows, scratch paths, and restoration behavior.

The owner is deliberately *not* a harness framework: it owns exactly the
resources a peer child process holds — the OS process and its captured stdio —
and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path
from threading import Event
from types import TracebackType
from typing import TextIO, TypeVar

_POLL_WAIT = Event()
_PeerT = TypeVar("_PeerT")


async def _join_worker(task: asyncio.Task[_PeerT]) -> bool:
    """Wait for a bounded worker without letting repeated cancellation orphan it."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


async def async_peer_call(call: Callable[[], _PeerT]) -> _PeerT:
    """Run a bounded synchronous operation and join it before cancellation returns."""
    worker = asyncio.create_task(asyncio.to_thread(call))
    cancelled = await _join_worker(worker)
    result = worker.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _exit_peer(
    listener: AbstractContextManager[_PeerT],
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    traceback: TracebackType | None,
) -> bool:
    return bool(
        await async_peer_call(lambda: listener.__exit__(exc_type, exc, traceback))
    )


@asynccontextmanager
async def async_peer_context(
    listener: AbstractContextManager[_PeerT],
) -> AsyncIterator[_PeerT]:
    """Keep bounded peer entry/exit off the runtime loop, even on cancellation.

    A failed synchronous entry owns its own cleanup. A successful entry is
    exited exactly once, and repeated cancellation cannot orphan its worker.
    """
    startup = asyncio.create_task(asyncio.to_thread(listener.__enter__))
    try:
        peer = await asyncio.shield(startup)
    except asyncio.CancelledError:
        await _join_worker(startup)
        if not startup.cancelled():
            try:
                startup.result()
            except Exception:
                # A failed __enter__ owns cleanup; preserve cancellation.
                pass
            else:
                await _exit_peer(listener, None, None, None)
        raise
    try:
        yield peer
    except BaseException:
        if not await _exit_peer(listener, *sys.exc_info()):
            raise
    else:
        await _exit_peer(listener, None, None, None)


async def async_packets_until(
    snapshot: Callable[[Callable[[list[dict]], bool], float], list[dict]],
    predicate: Callable[[list[dict]], bool],
    timeout: float,
    interval: float = 0.5,
) -> list[dict]:
    """Poll a listener off-loop without leaving a long-lived poll on cancellation.

    Each worker performs one zero-time read (including transport-specific
    checks); only the async caller owns the wait between reads.
    """
    _validate_poll_window(timeout, interval)
    deadline = time.monotonic() + timeout
    while True:
        packets = await async_peer_call(lambda: snapshot(lambda _: False, 0.0))
        if predicate(packets):
            return packets
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return packets
        await asyncio.sleep(min(interval, remaining))


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL scratch file into dicts; missing file means no data."""
    try:
        data = path.read_text()
    except FileNotFoundError:
        return []
    # A producer may be between writes. Only newline-terminated records are
    # committed to this scratch stream; malformed complete lines still fail.
    packets: list[dict] = []
    for line in data.split("\n")[:-1]:
        line = line.strip()
        if line:
            packets.append(json.loads(line))
    return packets


def _validate_poll_window(timeout: float, interval: float) -> None:
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("peer observation timeout must be finite and nonnegative")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("peer observation interval must be finite and positive")


def _reject_running_loop(message: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(message)


def poll_packets_until(
    read_packets: Callable[[], list[dict]],
    predicate: Callable[[list[dict]], bool],
    timeout: float,
    interval: float = 0.5,
) -> list[dict]:
    """Poll collected packets until ``predicate`` holds or *timeout* ends.

    Returns the last-read packet list either way, so absence evidence is
    expressed with the same shape as positive evidence. The wait is bounded by
    the remaining deadline rather than an unconditional fixed sleep.
    """
    _validate_poll_window(timeout, interval)
    if timeout > 0:
        _reject_running_loop(
            "Blocking peer observation on the event loop; "
            "use await async_packets_until(listener.packets_until, ...)"
        )
    deadline = time.monotonic() + timeout
    packets: list[dict] = []
    while True:
        packets = read_packets()
        if predicate(packets):
            return packets
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return packets
        _POLL_WAIT.wait(min(interval, remaining))


class PeerProcess:
    """Own one spawned peer child: readiness handshake and teardown.

    Readiness is a file handshake written by the child only after its
    collection is armed (subscription/pubsub registered, firmware buffer
    drained — per-transport detail). ``start`` returns once the file exists
    and raises if the child exits first or the deadline passes.

    Child stdout/stderr are captured in temporary files rather than pipes.
    Long hardware windows can generate enough SDK logging to fill an OS pipe;
    file-backed capture prevents the child from blocking before it can append
    its JSONL evidence.

    Teardown is idempotent and also safe after a failed ``start`` (Python does
    not call a context manager's ``__exit__`` when ``__enter__`` raises).
    """

    def __init__(self, ready_path: Path, ready_timeout: float) -> None:
        if not math.isfinite(ready_timeout) or ready_timeout <= 0:
            raise ValueError("peer readiness timeout must be finite and positive")
        self._ready_path = ready_path
        self._ready_timeout = ready_timeout
        self._proc: subprocess.Popen[str] | None = None
        self._stdout: TextIO | None = None
        self._stderr: TextIO | None = None

    @property
    def proc(self) -> subprocess.Popen[str] | None:
        """Return the owned process, if one has been spawned."""
        return self._proc

    @property
    def stdio_closed(self) -> bool:
        """Whether all owned stdio capture handles have been closed."""
        return all(
            stream is None or stream.closed for stream in (self._stdout, self._stderr)
        )

    def start(self, argv: list[str]) -> None:
        """Spawn the child and block until readiness is signalled."""
        _reject_running_loop("Blocking peer startup; use async_peer_context")
        if self._proc is not None and self._proc.poll() is None:
            raise RuntimeError("peer process already running")
        self._close_streams()
        self._ready_path.unlink(missing_ok=True)
        try:
            self._stdout = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
            self._stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
            self._proc = subprocess.Popen(
                argv,
                stdout=self._stdout,
                stderr=self._stderr,
                text=True,
            )
        except BaseException:
            self._close_streams()
            raise

        try:
            deadline = time.monotonic() + self._ready_timeout
            while True:
                if self._proc.poll() is not None:
                    _, err = self.communicate(timeout=10)
                    raise AssertionError(
                        f"peer listener died before readiness: {err[-400:]}"
                    )
                if self._ready_path.exists():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("peer listener never signalled ready")
                _POLL_WAIT.wait(min(0.2, remaining))
        except BaseException:
            self.terminate()
            raise

    def poll(self) -> int | None:
        """Return the child return code, or ``None`` while it is running."""
        if self._proc is None:
            return None
        return self._proc.poll()

    def raise_if_failed(self) -> None:
        """A failed observer cannot supply a valid absence window."""
        code = self.poll()
        if code not in (None, 0):
            raise AssertionError(f"native peer listener exited early ({code})")

    def communicate(self, timeout: float) -> tuple[str, str]:
        """Wait for child completion and return captured stdout/stderr."""
        _reject_running_loop("Blocking peer drain; use async_peer_call")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("peer drain timeout must be finite and nonnegative")
        assert self._proc is not None
        self._proc.wait(timeout=timeout)
        return self._read_stream(self._stdout), self._read_stream(self._stderr)

    def terminate(self) -> None:
        """Kill a live child, reap it, and close captured stdio (idempotent)."""
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()
                self._proc.wait(timeout=10)
        finally:
            self._close_streams()

    @staticmethod
    def _read_stream(stream: TextIO | None) -> str:
        if stream is None or stream.closed:
            return ""
        stream.flush()
        stream.seek(0)
        return stream.read()

    def _close_streams(self) -> None:
        for stream in (self._stdout, self._stderr):
            if stream is not None and not stream.closed:
                stream.close()
