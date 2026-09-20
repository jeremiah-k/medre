"""Native MeshCore peer harness shared by live hardware tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.live_peer_common import PeerProcess, poll_packets_until, read_jsonl

pytestmark = [pytest.mark.live, pytest.mark.hardware]

_PEER_BLE = os.environ.get("MESHCORE_PEER_BLE_ADDRESS", "")
_PEER_READY_TIMEOUT: float = 60.0

_PEER_SCRIPT = r'''
import asyncio, json, subprocess, sys, time

ADDRESS = sys.argv[2]
MODE = sys.argv[1]


async def _connect():
    from meshcore import EventType
    from meshcore import MeshCore
    last = None
    for attempt in range(3):
        if attempt:  # remedy only after a failed attempt, not proactively
            subprocess.run(["bluetoothctl", "disconnect", ADDRESS], capture_output=True)
            await asyncio.sleep(1.5)
        try:
            mc = await asyncio.wait_for(
                MeshCore.create_ble(address=ADDRESS, default_timeout=10),
                timeout=25,
            )
            if mc is not None:
                return mc
            last = "None"
        except Exception as exc:
            last = type(exc).__name__
        print(f"peer connect attempt {attempt}: {last}", file=sys.stderr, flush=True)
    raise RuntimeError(f"peer connect failed: {last}")


async def _own_pubkey_prefix(mc):
    """First 12 hex of this board's public key via self contact export."""
    import re as _re
    try:
        ec = await asyncio.wait_for(mc.commands.export_contact(), timeout=10)
        blob = json.dumps(ec.payload, default=str) + json.dumps(
            ec.attributes, default=str
        )
        m = _re.search(r"[0-9a-fA-F]{64}", blob)
        if m:
            return m.group(0)[:12]
    except Exception:
        pass
    from meshcore import EventType
    got = {}

    def on_info(event):
        p = event.payload or {}
        pk = p.get("public_key") or ""
        if pk and "prefix" not in got:
            got["prefix"] = pk[:12]

    sub = mc.subscribe(EventType.SELF_INFO, on_info)
    try:
        await asyncio.sleep(2.5)
    finally:
        try:
            mc.unsubscribe(sub)
        except Exception:
            pass
    return got.get("prefix")


async def main():
    from meshcore import EventType
    out = {"mode": MODE, "sent": [], "received": [], "own_pubkey_prefix": None}

    if MODE == "listen":
        seconds = float(sys.argv[3])
        mc = await _connect()
        try:
            out["own_pubkey_prefix"] = await _own_pubkey_prefix(mc)
            # The firmware replays buffered group messages to a newly
            # connected app; drain that queue BEFORE arming collection so
            # a previous run's traffic cannot satisfy this window.  Bounded
            # and non-fatal: a misbehaving queue must not hang the listener.
            for _ in range(10):
                try:
                    ev = await asyncio.wait_for(
                        mc.commands.get_msg(), timeout=4
                    )
                except Exception:
                    break
                if ev.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                    break
            got = []
            raw = []

            def on_any(event):
                raw.append(f"{event.type.value}:{str(event.payload)[:60]}")

            raw_sub = mc.subscribe(None, on_any)

            def on_msg(event):
                p = event.payload or {}
                got.append({
                    "text": p.get("text") or p.get("message", ""),
                    "sender": (p.get("pubkey_prefix") or "")[:12],
                    "channel": p.get("channel_idx"),
                    "snr": p.get("SNR", p.get("snr")),
                    "sender_timestamp": p.get("sender_timestamp"),
                })
                with open("/tmp/meshcore_pair_peer.json", "a") as fh:
                    fh.write(json.dumps(got[-1]) + "\n")

            sub = mc.subscribe(EventType.CHANNEL_MSG_RECV, on_msg)
            await mc.start_auto_message_fetching()
            # Ready handshake: collection is armed (drained, subscribed).
            with open("/tmp/meshcore_pair_peer.ready", "w") as fh:
                fh.write("1")
            await asyncio.sleep(seconds)
            mc.unsubscribe(sub)
            try:
                mc.unsubscribe(raw_sub)
            except Exception:
                pass
            out["received"] = got
            out["raw_events"] = raw
        finally:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=8)
            except Exception:
                pass
    elif MODE in ("sendn", "sendts"):
        mc = await _connect()
        try:
            out["own_pubkey_prefix"] = await _own_pubkey_prefix(mc)
            if MODE == "sendn":
                texts = json.loads(sys.argv[3])
                for text in texts:
                    res = await asyncio.wait_for(
                        mc.commands.send_chan_msg(1, text), timeout=15)
                    out["sent"].append({
                        "text": text,
                        "type": res.type.value,
                        "error": res.is_error(),
                    })
                    await asyncio.sleep(3.0)
            else:
                # Controlled sender-set wire timestamps (supported SDK arg):
                # same text, explicit one-second-resolution timestamps.
                text = sys.argv[3]
                ts_a, ts_b = int(sys.argv[4]), int(sys.argv[5])
                for ts in (ts_a, ts_b):
                    res = await asyncio.wait_for(
                        mc.commands.send_chan_msg(1, text, timestamp=ts), timeout=15)
                    out["sent"].append({
                        "text": text, "timestamp": ts,
                        "type": res.type.value, "error": res.is_error(),
                    })
                    await asyncio.sleep(3.0)
        finally:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=8)
            except Exception:
                pass
    elif MODE == "probe":
        # Preflight: prove the board is connectable and time-synced.
        mc = await _connect()
        try:
            out["own_pubkey_prefix"] = await _own_pubkey_prefix(mc)
            t = await asyncio.wait_for(mc.commands.get_time(), timeout=10)
            dev = (t.payload or {}).get("unix_time") or (t.payload or {}).get("time")
            out["drift_s"] = (dev - int(time.time())) if dev else None
        finally:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=8)
            except Exception:
                pass
    elif MODE == "n6probe":
        # Wrong-channel negative control: ch2 carries a secret the MEDRE
        # board does not share.  Restore ch2 to empty afterwards and prove
        # a positive ch1 delivery still works.
        import os
        mc = await _connect()
        try:
            out["own_pubkey_prefix"] = await _own_pubkey_prefix(mc)
            wrong_key = os.urandom(16)
            r = await asyncio.wait_for(
                mc.commands.set_channel(2, "MEDRE-WRONG", wrong_key), timeout=10)
            out["set_wrong"] = not r.is_error()
            res = await asyncio.wait_for(
                mc.commands.send_chan_msg(2, sys.argv[3]), timeout=15)
            out["sent"].append({"text": sys.argv[3], "channel": 2,
                                "type": res.type.value, "error": res.is_error()})
            await asyncio.sleep(2.0)
            r = await asyncio.wait_for(
                mc.commands.set_channel(2, "", b"\x00" * 16), timeout=10)
            out["restore_ok"] = not r.is_error()
            ch = await asyncio.wait_for(mc.commands.get_channel(2), timeout=10)
            cp = ch.payload or {}
            sec = cp.get("channel_secret", b"") or b""
            if isinstance(sec, str):
                try:
                    sec = bytes.fromhex(sec)
                except ValueError:
                    sec = sec.encode()
            out["ch2_after"] = {
                "name": cp.get("channel_name"),
                "secret_len": len(sec),
                # The firmware keeps a fixed 16-byte secret slot: the
                # empty-channel default reads back as 16 zero bytes.
                "secret_zeroed": sec == b"\x00" * 16,
                "secret_is_wrong": sec == wrong_key,
            }
            pos = sys.argv[4]
            res = await asyncio.wait_for(
                mc.commands.send_chan_msg(1, pos), timeout=15)
            out["sent"].append({"text": pos, "channel": 1,
                                "type": res.type.value, "error": res.is_error()})
        finally:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=8)
            except Exception:
                pass

    print(json.dumps(out))


asyncio.run(main())
'''


def run_meshcore_peer(args: list[str], timeout: float) -> dict:
    """Run the native peer script once and return its JSON payload."""
    proc = subprocess.run(
        [sys.executable, "-c", _PEER_SCRIPT, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"native peer failed ({proc.returncode}): {proc.stderr[-800:]}"
        )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise AssertionError(f"native peer produced no JSON: {proc.stderr[-400:]}")
    return json.loads(lines[-1])


class MeshCorePeerListener:
    """Background native-peer BLE listener with incremental collection.

    The peer appends each received packet to a JSONL scratch file as it
    arrives, so positive cases finish as soon as the expected evidence
    lands (``packets_until``) while absence cases still drain a full
    window (``packets``).  Readiness is a file handshake: arming happens
    after the firmware buffer drain + subscribe, not after a fixed sleep.
    Process/readiness/teardown ownership lives in
    :class:`~tests.helpers.live_peer_common.PeerProcess`.
    """

    _JSON_PATH = Path("/tmp/meshcore_pair_peer.json")
    _READY_PATH = Path("/tmp/meshcore_pair_peer.ready")
    _READY_TIMEOUT = _PEER_READY_TIMEOUT

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._owner = PeerProcess(self._READY_PATH, self._READY_TIMEOUT)

    def __enter__(self) -> "MeshCorePeerListener":
        self._JSON_PATH.unlink(missing_ok=True)
        self._READY_PATH.unlink(missing_ok=True)
        # Ready handshake: drain + subscribe + auto-fetch armed before any
        # MEDRE-side TX (bounded; no fixed startup sleep).
        self._owner.start(
            [
                sys.executable,
                "-c",
                _PEER_SCRIPT,
                "listen",
                _PEER_BLE,
                str(self._seconds),
            ]
        )
        return self

    def _read_packets(self) -> list[dict]:
        return read_jsonl(self._JSON_PATH)

    def packets_until(self, predicate, timeout: float) -> list[dict]:
        """Poll collected packets until ``predicate`` holds or timeout."""
        return poll_packets_until(self._read_packets, predicate, timeout)

    def packets(self, timeout: float | None = None) -> list[dict]:
        """Drain the listener's full window (absence/negative evidence)."""
        out, err = self._owner.communicate(timeout=timeout or self._seconds + 40)
        assert self._owner.proc is not None
        if self._owner.proc.returncode != 0:
            raise AssertionError(f"native peer listener failed: {err[-800:]}")
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        return json.loads(lines[-1])["received"] if lines else []

    def __exit__(self, *exc: object) -> None:
        self._owner.terminate()
