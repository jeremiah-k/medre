"""Common live-peer process ownership: readiness, release, failure paths.

Proves the shared ``PeerProcess`` owner with plain child processes — no
radios, no SDK imports.  Assertions observe release and responsiveness
(process reaped, pipes closed, bounded waits) rather than implementation
details.  The per-transport listeners keep their own semantics; these
tests cover only the ownership skeleton they all consume.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from tests.helpers.live_peer_common import (
    PeerProcess,
    poll_packets_until,
    read_jsonl,
)

_READY_TIMEOUT = 5.0


def _write_ready_child(ready_path: Path, scratch_path: Path, line: str) -> list[str]:
    """A child that arms its 'collection' (writes one JSONL line), signals
    readiness, and idles until killed."""
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
    """A child that exits non-zero without ever signalling readiness."""
    return [sys.executable, "-c", "import sys; sys.exit(3)"]


def _silent_child() -> list[str]:
    """A child that stays alive but never signals readiness."""
    return [sys.executable, "-c", "import time; time.sleep(60)"]


class TestPeerProcessReadiness:
    def test_start_returns_once_ready_file_exists(self, tmp_path: Path) -> None:
        ready = tmp_path / "peer.ready"
        owner = PeerProcess(ready, _READY_TIMEOUT)
        argv = _write_ready_child(ready, tmp_path / "peer.jsonl", '{"id": 1}')
        try:
            owner.start(argv)
            assert owner.poll() is None  # child alive, held by the owner
        finally:
            owner.terminate()

    def test_early_child_exit_raises_and_releases(self, tmp_path: Path) -> None:
        ready = tmp_path / "peer.ready"
        owner = PeerProcess(ready, _READY_TIMEOUT)
        with pytest.raises(AssertionError, match="died before readiness"):
            owner.start(_dying_child())
        # Released: reaped and pipes closed despite the failed start.
        assert owner.proc is not None
        assert owner.proc.poll() is not None
        assert owner.proc.stdout is not None and owner.proc.stdout.closed
        assert owner.proc.stderr is not None and owner.proc.stderr.closed

    def test_silent_child_raises_within_deadline_and_is_killed(
        self, tmp_path: Path
    ) -> None:
        ready = tmp_path / "peer.ready"
        owner = PeerProcess(ready, 1.0)
        started = time.monotonic()
        with pytest.raises(AssertionError, match="never signalled ready"):
            owner.start(_silent_child())
        assert time.monotonic() - started < 5.0  # bounded, not hung
        assert owner.proc is not None
        assert owner.proc.poll() is not None  # killed and reaped
        assert owner.proc.stdout is not None and owner.proc.stdout.closed


class TestPeerProcessTeardown:
    def test_exceptional_body_exit_releases_child(self, tmp_path: Path) -> None:
        """The with-body raising still reaps the child and closes pipes."""
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
        assert proc.stdout is not None and proc.stdout.closed
        assert proc.stderr is not None and proc.stderr.closed

    def test_terminate_is_idempotent(self, tmp_path: Path) -> None:
        ready = tmp_path / "peer.ready"
        owner = PeerProcess(ready, _READY_TIMEOUT)
        owner.start(_write_ready_child(ready, tmp_path / "peer.jsonl", "{}"))
        owner.terminate()
        owner.terminate()  # must not raise on an already-reaped child


class TestJsonlCollection:
    def test_read_jsonl_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert read_jsonl(tmp_path / "absent.jsonl") == []

    def test_poll_packets_until_returns_on_predicate(self, tmp_path: Path) -> None:
        scratch = tmp_path / "peer.jsonl"
        scratch.write_text('{"id": 1}\n{"id": 2}\n')
        packets = poll_packets_until(
            lambda: read_jsonl(scratch),
            lambda pkts: len(pkts) >= 2,
            timeout=2.0,
            interval=0.05,
        )
        assert [p["id"] for p in packets] == [1, 2]

    def test_poll_packets_until_times_out_with_last_read(self, tmp_path: Path) -> None:
        scratch = tmp_path / "peer.jsonl"
        scratch.write_text('{"id": 1}\n')
        packets = poll_packets_until(
            lambda: read_jsonl(scratch),
            lambda pkts: len(pkts) >= 5,
            timeout=0.3,
            interval=0.05,
        )
        assert [p["id"] for p in packets] == [1]  # absence evidence, same shape


class TestEndToEndOwnerRoundTrip:
    def test_full_lifecycle_with_incremental_collection(self, tmp_path: Path) -> None:
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
