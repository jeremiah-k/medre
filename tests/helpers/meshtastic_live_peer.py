"""Native Meshtastic peer harness shared by live hardware tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.helpers.live_peer_common import PeerProcess, poll_packets_until, read_jsonl

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
            with open("/tmp/meshtastic_pair_mt.json", "a") as fh:
                fh.write(json.dumps(rec) + "\n")
pub.subscribe(on_packet, "meshtastic.receive")
if mode == "listen":
    # Ready handshake: subscription armed before any MEDRE-side traffic.
    with open("/tmp/meshtastic_pair_mt.ready", "w") as fh:
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
    then fail honestly.  Process/readiness/teardown ownership lives in
    :class:`~tests.helpers.live_peer_common.PeerProcess`.
    """

    _JSON_PATH = Path("/tmp/meshtastic_pair_mt.json")
    _READY_PATH = Path("/tmp/meshtastic_pair_mt.ready")
    _READY_TIMEOUT = 40.0

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._owner = PeerProcess(self._READY_PATH, self._READY_TIMEOUT)

    def __enter__(self) -> "MeshtasticPeerListener":
        for attempt in range(2):
            self._JSON_PATH.unlink(missing_ok=True)
            self._READY_PATH.unlink(missing_ok=True)
            try:
                # Ready handshake: pubsub armed (serial connect included).
                self._owner.start(
                    [
                        sys.executable,
                        "-c",
                        _PEER_SCRIPT,
                        str(Path(__file__).resolve().parents[2]),
                        "listen",
                        _MT_PEER,
                        str(self._seconds),
                    ]
                )
                return self
            except AssertionError as exc:
                if attempt == 0 and "lock" in str(exc).lower():
                    time.sleep(5.0)  # previous holder releasing the flock
                    continue
                raise
        raise AssertionError("MT listener never signalled ready")

    def _read_packets(self) -> list[dict]:
        return read_jsonl(self._JSON_PATH)

    def packets_until(self, predicate, timeout: float) -> list[dict]:
        """Poll collected packets until ``predicate`` holds or timeout."""
        return poll_packets_until(self._read_packets, predicate, timeout)

    def packets(self, timeout: float | None = None) -> list[dict]:
        """Drain the listener's full window (absence/negative evidence)."""
        out, err = self._owner.communicate(timeout=timeout or self._seconds + 30)
        assert self._owner.proc is not None
        if self._owner.proc.returncode != 0:
            raise AssertionError(f"MT listener failed: {err[-600:]}")
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else []

    def __exit__(self, *exc: object) -> None:
        self._owner.terminate()
