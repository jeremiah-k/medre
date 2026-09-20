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

import json
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import TextIO

_POLL_WAIT = Event()


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL scratch file into dicts; missing file means no data."""
    if not path.exists():
        return []
    packets: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            packets.append(json.loads(line))
    return packets


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
        self._close_streams()
        self._stdout = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        self._stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        try:
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
                if self._ready_path.exists():
                    return
                if self._proc.poll() is not None:
                    _, err = self.communicate(timeout=10)
                    raise AssertionError(
                        f"peer listener died before readiness: {err[-400:]}"
                    )
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

    def communicate(self, timeout: float) -> tuple[str, str]:
        """Wait for child completion and return captured stdout/stderr."""
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
