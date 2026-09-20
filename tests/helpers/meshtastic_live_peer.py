"""Native Meshtastic peer harness shared by live hardware tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.live, pytest.mark.hardware]

_MT_PEER = os.environ.get("MESHTASTIC_PEER_SERIAL_PORT", "")

_PEER_SCRIPT = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
mode, port = sys.argv[2], sys.argv[3]
from meshtastic.serial_interface import SerialInterface
from pubsub import pub
iface = SerialInterface(devPath=port, noProto=False)
my_info = getattr(iface, "myInfo", None)
my_num = getattr(my_info, "myNodeNum", None)
own_id = f"!{int(my_num):08x}" if isinstance(my_num, int) and my_num >= 0 else None
got = []
def on_packet(packet, interface=None):
    d = packet.get("decoded", {}) or {}
    if d.get("portnum") == "TEXT_MESSAGE_APP":
        rec = {
            "ts": time.time(),
            "id": packet.get("id"),
            "from": packet.get("fromId"),
            "_from": packet.get("fromId"),
            "to": packet.get("toId"),
            "channel": packet.get("channel"),
            "text": d.get("text"),
            "rx_snr": packet.get("rxSnr"),
        }
        got.append(rec)
        if mode == "listen":
            with open("/tmp/meshcore_pair_mt.json", "a") as fh:
                fh.write(json.dumps(rec) + "\n")
pub.subscribe(on_packet, "meshtastic.receive")
if mode == "listen":
    # Ready handshake: subscription armed before any MEDRE-side traffic.
    with open("/tmp/meshcore_pair_mt.ready", "w") as fh:
        fh.write("1")
    deadline = time.time() + float(sys.argv[4])
    while time.time() < deadline:
        time.sleep(0.2)
else:
    for text in json.loads(sys.argv[4]):
        p = iface.sendText(text, channelIndex=0, wantAck=False)
        got.append({"sent_text": text, "sent_id": p.id if p else None, "sender_id": own_id})
        time.sleep(2.5)
iface.close()
print(json.dumps(got))
"""


def run_meshtastic_peer(args: list[str], timeout: float) -> list[dict]:
    repo = str(Path(__file__).resolve().parents[2])
    proc = subprocess.run(
        [sys.executable, "-c", _PEER_SCRIPT, repo, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"MT peer failed ({proc.returncode}): {proc.stderr[-600:]}"
        )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else []

class MeshtasticPeerListener:
    """Bounded MT-B listener (serial SDK, channel 0) — incremental.

    Received packets land in a JSONL scratch file as they arrive
    (``packets_until`` for positive cases; ``packets`` drains the full
    window for absence evidence).  Readiness is a file handshake written
    once the pubsub subscription is armed.  A previous test's pyserial
    process may still be releasing the port's exclusive flock when the
    next listener spawns; one bounded settle-retry on that condition,
    then fail honestly.
    """

    _JSON_PATH = Path("/tmp/meshcore_pair_mt.json")
    _READY_PATH = Path("/tmp/meshcore_pair_mt.ready")

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._proc: subprocess.Popen[str] | None = None

    def _spawn(self) -> None:
        repo = str(Path(__file__).resolve().parents[2])
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _PEER_SCRIPT,
                repo,
                "listen",
                _MT_PEER,
                str(self._seconds),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def __enter__(self) -> "MeshtasticPeerListener":
        for attempt in range(2):
            self._JSON_PATH.unlink(missing_ok=True)
            self._READY_PATH.unlink(missing_ok=True)
            self._spawn()
            # Ready handshake: pubsub armed (serial connect included).
            deadline = time.monotonic() + 40.0
            while time.monotonic() < deadline:
                if self._READY_PATH.exists():
                    return self
                if self._proc.poll() is not None:
                    break
                time.sleep(0.2)
            if self._READY_PATH.exists():
                return self
            assert self._proc is not None
            if self._proc.poll() is None:
                # __exit__ is not called when __enter__ raises.  A listener
                # that stays alive without signalling readiness must be killed
                # here or it keeps the peer serial port exclusively open.
                self.__exit__()
                raise AssertionError("MT listener never signalled ready")
            _, err = self._proc.communicate(timeout=10)
            died = f"listener exited during settle: {err[-300:]}"
            self._close_streams()
            if attempt == 0 and "lock" in (err or "").lower():
                time.sleep(5.0)  # previous holder releasing the flock
                continue
            raise AssertionError(died)
        raise AssertionError("MT listener never signalled ready")

    def _read_packets(self) -> list[dict]:
        if not self._JSON_PATH.exists():
            return []
        packets: list[dict] = []
        for line in self._JSON_PATH.read_text().splitlines():
            line = line.strip()
            if line:
                packets.append(json.loads(line))
        return packets

    def packets_until(self, predicate, timeout: float) -> list[dict]:
        """Poll collected packets until ``predicate`` holds or timeout."""
        deadline = time.monotonic() + timeout
        packets: list[dict] = []
        while time.monotonic() < deadline:
            packets = self._read_packets()
            if predicate(packets):
                return packets
            time.sleep(0.5)
        return self._read_packets()

    def packets(self, timeout: float | None = None) -> list[dict]:
        """Drain the listener's full window (absence/negative evidence)."""
        out, err = self._proc.communicate(timeout=timeout or self._seconds + 30)
        if self._proc.returncode != 0:
            raise AssertionError(f"MT listener failed: {err[-600:]}")
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else []

    def _close_streams(self) -> None:
        if self._proc is None:
            return
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def __exit__(self, *exc: object) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=10)
        # Close child pipe handles even when the test body fails before
        # packets() drains them; warnings are errors in this suite.
        self._close_streams()
