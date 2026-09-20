"""Live Matrix room observer: a second nio client with its OWN crypto store.

Purpose: independent observation of messages the MEDRE runtime publishes into
the encrypted room — proves real Megolm encryption plus far-side decryption.
The observer is the SAME bot account on a second device (contract-disclosed:
this is NOT an independent-sender ingress test).

Mirrors the subprocess peer-listener conventions used by the Meshtastic,
MeshCore, and LXMF live peers (JSON final line + optional JSONL evidence
file).  Opt-in only; the access token is passed to the subprocess via
environment (never argv).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_OBSERVER_SCRIPT = r"""
import asyncio
import json
import os
import sys
import time


async def main() -> None:
    mode = sys.argv[1]
    homeserver = sys.argv[2]
    user_id = sys.argv[3]
    token = os.environ["MEDRE_OBSERVER_TOKEN"]
    device_id = sys.argv[4]
    store = sys.argv[5]

    from nio import (
        AsyncClient,
        MegolmEvent,
        RoomMessageEmote,
        RoomMessageNotice,
        RoomMessageText,
    )

    if mode == "devices":
        client = AsyncClient(homeserver, user_id)
        client.restore_login(user_id, device_id, token)
        try:
            resp = await client.devices()
            ids = sorted(d.id for d in resp.devices) if hasattr(resp, "devices") else []
            print(json.dumps({"devices": ids}))
        finally:
            await client.close()
        return

    room_id = sys.argv[6]
    seconds = float(sys.argv[7])
    substr = sys.argv[8] if len(sys.argv) > 8 else ""
    jsonl_path = sys.argv[9] if len(sys.argv) > 9 else ""

    client = AsyncClient(
        homeserver, user_id, device_id=device_id, store_path=store,
        encryption_enabled=True,
    )
    client.restore_login(user_id, device_id, token)

    events: list[dict] = []
    matched: dict | None = None

    def _record(room, event) -> None:
        nonlocal matched
        rec = {
            "type": type(event).__name__,
            "sender": getattr(event, "sender", None),
            "event_id": getattr(event, "event_id", None),
            "body": getattr(event, "body", None),
            "server_ts": getattr(event, "server_timestamp", None),
        }
        events.append(rec)
        if jsonl_path:
            with open(jsonl_path, "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        if substr and substr in (rec["body"] or "") and matched is None:
            matched = rec

    client.add_event_callback(
        _record, (RoomMessageText, RoomMessageNotice, RoomMessageEmote)
    )
    # MegolmEvent reaches callbacks only when nio could NOT auto-decrypt.
    client.add_event_callback(_record, (MegolmEvent,))

    deadline = time.monotonic() + seconds
    first = True
    try:
        while time.monotonic() < deadline and matched is None:
            remaining_ms = int(max(1000.0, (deadline - time.monotonic()) * 1000))
            await client.sync(
                timeout=min(8000, remaining_ms), full_state=first
            )
            first = False
    finally:
        result = {
            "found": matched is not None,
            "matched": matched,
            "event_count": len(events),
            "undecryptable": sum(1 for e in events if e["type"] == "MegolmEvent"),
        }
        print(json.dumps(result))
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
"""


def _observer_env(token: str) -> dict[str, str]:
    return {**os.environ, "MEDRE_OBSERVER_TOKEN": token}


def _parse_last_json(text: str) -> dict:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        raise AssertionError(f"observer produced no JSON: {text[-400:]}")
    return json.loads(lines[-1])


def run_matrix_observer(args: list[str], timeout: float) -> dict:
    """Run the observer script once and return its final JSON payload."""
    homeserver = os.environ["MATRIX_HOMESERVER"]
    user_id = os.environ["MATRIX_USER_ID"]
    token = os.environ["MATRIX_OBSERVER_TOKEN"]
    device_id = os.environ["MATRIX_OBSERVER_DEVICE_ID"]
    store = os.environ["MATRIX_OBSERVER_STORE_PATH"]
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _OBSERVER_SCRIPT,
            args[0],
            homeserver,
            user_id,
            device_id,
            store,
            *args[1:],
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_observer_env(token),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"matrix observer failed ({proc.returncode}): {proc.stderr[-800:]}"
        )
    return _parse_last_json(proc.stdout)


def observer_account_devices(timeout: float = 45.0) -> list[str]:
    """Return the sorted device IDs visible on the account (continuity)."""
    result = run_matrix_observer(["devices"], timeout)
    return result.get("devices", [])


class MatrixRoomObserver:
    """Context-managed watch session recording decrypted room events.

    Events stream into ``jsonl_path`` as they arrive; ``wait()`` blocks for
    the process to finish (substr matched or window elapsed) and returns the
    final JSON payload.
    """

    def __init__(self, seconds: float, substr: str, jsonl_path: str) -> None:
        self._seconds = seconds
        self._substr = substr
        self._jsonl = jsonl_path
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "MatrixRoomObserver":
        if os.path.exists(self._jsonl):
            os.unlink(self._jsonl)
        homeserver = os.environ["MATRIX_HOMESERVER"]
        user_id = os.environ["MATRIX_USER_ID"]
        token = os.environ["MATRIX_OBSERVER_TOKEN"]
        device_id = os.environ["MATRIX_OBSERVER_DEVICE_ID"]
        store = os.environ["MATRIX_OBSERVER_STORE_PATH"]
        room_id = os.environ["MATRIX_ROOM_ID"]
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _OBSERVER_SCRIPT,
                "watch",
                homeserver,
                user_id,
                device_id,
                store,
                room_id,
                str(self._seconds),
                self._substr,
                self._jsonl,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_observer_env(token),
        )
        return self

    def events(self) -> list[dict]:
        if not os.path.exists(self._jsonl):
            return []
        out = []
        with open(self._jsonl) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def wait(self, timeout: float | None = None) -> dict:
        assert self._proc is not None, "observer not started"
        stdout, stderr = self._proc.communicate(timeout=timeout)
        if self._proc.returncode != 0:
            raise AssertionError(
                f"matrix observer failed ({self._proc.returncode}): " f"{stderr[-800:]}"
            )
        return _parse_last_json(stdout)

    def __exit__(self, *exc: object) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
            self._proc.communicate()
