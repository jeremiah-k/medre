"""Shared ownership primitives for the native live-peer listeners.

One small owner for the process/readiness/teardown skeleton that the
Meshtastic, MeshCore, and LXMF peer listeners previously re-implemented
(with drift in stream-close guards and scratch-path naming).  Everything
transport-specific stays in the per-transport modules: the child script,
argv, packet semantics, RF windows, scratch paths, and restoration
behavior.

The owner is deliberately *not* a harness framework: it owns exactly the
resources a peer child process holds — the OS process and its stdio
pipes — and nothing else.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


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
    read_packets, predicate, timeout: float, interval: float = 0.5
) -> list[dict]:
    """Poll collected packets until ``predicate`` holds or *timeout* ends.

    Returns the last-read packet list either way, so absence evidence is
    expressed with the same shape as positive evidence.
    """
    deadline = time.monotonic() + timeout
    packets: list[dict] = []
    while time.monotonic() < deadline:
        packets = read_packets()
        if predicate(packets):
            return packets
        time.sleep(interval)
    return read_packets()


class PeerProcess:
    """Own one spawned peer child: readiness handshake and teardown.

    Readiness is a file handshake written by the child only after its
    collection is armed (subscription/pubsub registered, firmware buffer
    drained — per-transport detail).  ``start`` returns once the file
    exists and raises if the child exits first or the deadline passes.

    Teardown semantics (all paths):

    * a live child is killed and reaped,
    * stdout/stderr pipe handles are closed even when the test body
      failed before draining them — under ``filterwarnings=error`` an
      unclosed-pipe ``ResourceWarning`` during interpreter GC surfaces as
      an unraisable-exception test failure,
    * teardown is idempotent and safe to call from a failed ``__enter__``
      (Python does not call ``__exit__`` when ``__enter__`` raises).
    """

    def __init__(self, ready_path: Path, ready_timeout: float) -> None:
        self._ready_path = ready_path
        self._ready_timeout = ready_timeout
        self._proc: subprocess.Popen[str] | None = None

    @property
    def proc(self) -> subprocess.Popen[str] | None:
        return self._proc

    def start(self, argv: list[str]) -> None:
        """Spawn the child and block until readiness is signalled."""
        self._proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + self._ready_timeout
            while time.monotonic() < deadline:
                if self._ready_path.exists():
                    return
                if self._proc.poll() is not None:
                    _, err = self._proc.communicate(timeout=10)
                    self._close_streams()
                    raise AssertionError(
                        f"peer listener died before readiness: {err[-400:]}"
                    )
                time.sleep(0.2)
            raise AssertionError("peer listener never signalled ready")
        except BaseException:
            # __exit__ is not called when __enter__ raises; a failed
            # readiness handshake must not leak a child holding the
            # transport exclusively open.
            self.terminate()
            raise

    def poll(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.poll()

    def communicate(self, timeout: float) -> tuple[str, str]:
        assert self._proc is not None
        return self._proc.communicate(timeout=timeout)

    def terminate(self) -> None:
        """Kill a live child and close its stdio pipes (idempotent)."""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=10)
        self._close_streams()

    def _close_streams(self) -> None:
        if self._proc is None:
            return
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()
