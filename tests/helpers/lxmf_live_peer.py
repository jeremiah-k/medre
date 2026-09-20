"""Native LXMF peer harness shared by live hardware tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.live, pytest.mark.hardware]

_PEER_RNS = os.environ.get("LXMF_PEER_RNS_CONFIG", "")
_PEER_IDENTITY = os.environ.get("LXMF_PEER_IDENTITY", "")
_PEER_READY_TIMEOUT = 40.0
_SCRATCH_JSONL = Path("/tmp/medre_lxmf_pair_peer.json")
_READY_PATH = Path("/tmp/medre_lxmf_pair_peer.ready")

def delivery_dest_hash(identity_path: str) -> str:
    """LXMF delivery destination hash for an identity file (32 hex)."""
    import RNS

    identity = RNS.Identity.from_file(identity_path)
    assert identity is not None, f"cannot load identity {identity_path!r}"
    return RNS.Destination.hash_from_name_and_identity("lxmf.delivery", identity).hex()

_PEER_SCRIPT = r'''
import json, sys, time

MODE = sys.argv[1]
RNS_DIR = sys.argv[2]
IDENTITY = sys.argv[3]
STORAGE = sys.argv[4]
READY = sys.argv[5]
JSONL = sys.argv[6]

import RNS, LXMF

reticulum = RNS.Reticulum(configdir=RNS_DIR, loglevel=0)
identity = RNS.Identity.from_file(IDENTITY)
router = LXMF.LXMRouter(identity=identity, storagepath=STORAGE, autopeer=False)
delivery_dest = router.register_delivery_identity(identity, display_name="lx-b-peer")

import threading

def announce_loop():
    while True:
        try:
            router.announce(delivery_dest.hash)
        except Exception:
            pass
        time.sleep(8.0)

def wait_terminal(lxm, timeout=90.0):
    """Wait for the router to reach a terminal LXMF state before this
    process exits — exiting early would kill the delivery jobs."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = lxm.state
        if last in (LXMF.LXMessage.DELIVERED, LXMF.LXMessage.FAILED):
            return last
        time.sleep(1.0)
    return last

def recall_or_fail(dest_hex, timeout=45.0):
    end = time.time() + timeout
    dest_identity = None
    while time.time() < end and dest_identity is None:
        dest_identity = RNS.Identity.recall(bytes.fromhex(dest_hex))
        if dest_identity is None:
            time.sleep(1.0)
    if dest_identity is None:
        print(json.dumps({"error": "no identity recall for " + dest_hex}))
        sys.exit(4)
    return dest_identity

if MODE == "listen":
    got = []
    def on_delivery(message):
        mh = getattr(message, "hash", None)
        sh = getattr(message, "source_hash", None)
        content = getattr(message, "content", None)
        if isinstance(content, (bytes, bytearray)):
            content = content.decode("utf-8", "replace")
        fields = getattr(message, "fields", None) or {}
        raw = fields.get(0xFD) if isinstance(fields, dict) else None
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = json.loads(raw.decode("utf-8"))
            except Exception:
                raw = None
        envelope = raw if isinstance(raw, dict) else None
        rec = {
            "hash": mh.hex() if mh is not None else None,
            "source": sh.hex() if sh is not None else None,
            "content": content,
            "envelope": envelope,
        }
        # default=str: never let a non-serialisable payload attribute kill
        # the delivery callback — the evidence line must land.
        with open(JSONL, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    router.register_delivery_callback(on_delivery)
    threading.Thread(target=announce_loop, daemon=True).start()
    with open(READY, "w") as fh:
        fh.write("1")
    time.sleep(float(sys.argv[7]))
    print(json.dumps({"received_count": len(got)}))
elif MODE == "send":
    dest_hex = sys.argv[7]
    texts = json.loads(sys.argv[8])
    dest_identity = recall_or_fail(dest_hex)
    threading.Thread(target=announce_loop, daemon=True).start()
    out = []
    for text in texts:
        dest = RNS.Destination(
            dest_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
            "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(
            dest, delivery_dest, text,
            desired_method=LXMF.LXMessage.DIRECT,
        )
        router.handle_outbound(lxm)
        state = wait_terminal(lxm)
        out.append({"text": text, "hash": lxm.hash.hex(), "state": int(state)})
        time.sleep(2.5)
    print(json.dumps({"sent": out}))
elif MODE == "sendenv":
    dest_hex = sys.argv[7]
    text = sys.argv[8]
    envelope = json.loads(sys.argv[9])
    dest_identity = recall_or_fail(dest_hex)
    threading.Thread(target=announce_loop, daemon=True).start()
    dest = RNS.Destination(
        dest_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
        "lxmf", "delivery",
    )
    lxm = LXMF.LXMessage(
        dest, delivery_dest, text,
        fields={0xFD: envelope},
        desired_method=LXMF.LXMessage.DIRECT,
    )
    router.handle_outbound(lxm)
    state = wait_terminal(lxm)
    print(json.dumps({"sent": [{"text": text, "hash": lxm.hash.hex(),
                                "state": int(state)}]}))
else:
    print(json.dumps({"error": "unknown mode"}))
    sys.exit(2)
'''


def run_lxmf_peer(args: list[str], timeout: float) -> dict:
    """Run the native peer script once and return its JSON payload.

    Each invocation gets a FRESH LXMF router storage dir so a pending
    outbound from a previous phase cannot leak into this one.
    """
    tmp = Path("/tmp/medre_lxmf_peer_tmp")
    tmp.mkdir(exist_ok=True)
    storage = tempfile.mkdtemp(prefix="medre_lxmf_peer_storage_")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                _PEER_SCRIPT,
                args[0],
                _PEER_RNS,
                _PEER_IDENTITY,
                storage,
                "unused",
                "unused",
                *args[1:],
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        shutil.rmtree(storage, ignore_errors=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"native peer failed ({proc.returncode}): {proc.stderr[-800:]}"
        )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise AssertionError(f"native peer produced no JSON: {proc.stderr[-400:]}")
    return json.loads(lines[-1])


class LxmfPeerListener:
    """Background native-peer LXMF listener with incremental collection.

    Readiness is a file handshake armed after the router and delivery
    callback are registered — before any MEDRE-side traffic.  Received
    messages append to a JSONL scratch file as they arrive so positive
    cases finish as soon as correlated evidence lands.
    """

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "LxmfPeerListener":
        _SCRATCH_JSONL.unlink(missing_ok=True)
        _READY_PATH.unlink(missing_ok=True)
        self._storage = tempfile.mkdtemp(prefix="medre_lxmf_peer_storage_")
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _PEER_SCRIPT,
                "listen",
                _PEER_RNS,
                _PEER_IDENTITY,
                self._storage,
                str(_READY_PATH),
                str(_SCRATCH_JSONL),
                str(self._seconds),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + _PEER_READY_TIMEOUT
            while time.monotonic() < deadline:
                if _READY_PATH.exists():
                    return self
                if self._proc.poll() is not None:
                    _, err = self._proc.communicate(timeout=10)
                    raise AssertionError(f"native peer listener died: {err[-400:]}")
                time.sleep(0.2)
            raise AssertionError("native peer listener never signalled ready")
        except BaseException:
            # __exit__ is not called when __enter__ raises.  Tear down the
            # child explicitly so a failed readiness handshake cannot leave
            # an RNS process holding the serial interface.
            self.__exit__()
            raise

    def _read_packets(self) -> list[dict]:
        if not _SCRATCH_JSONL.exists():
            return []
        packets: list[dict] = []
        for line in _SCRATCH_JSONL.read_text().splitlines():
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

    def __exit__(self, *exc: object) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=10)
        # Close the child pipe handles — under filterwarnings=error an
        # unclosed-pipe ResourceWarning during interpreter GC (e.g. inside
        # RNS's gc.collect()) surfaces as an unraisable-exception error.
        if self._proc is not None:
            for stream in (self._proc.stdout, self._proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        shutil.rmtree(getattr(self, "_storage", ""), ignore_errors=True)
