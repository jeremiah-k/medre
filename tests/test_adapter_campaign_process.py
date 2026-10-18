"""Peer subprocess ownership, bounded evidence, and failure propagation."""

import json
import sys

import pytest

from scripts.adapter_campaign.process import Peer

CHILD = """
import json, sys
settings = json.loads(sys.stdin.readline())
print('SDK diagnostic, not evidence', flush=True)
print('MRPEER ' + json.dumps({'kind': 'ready', 'identity': 'peer'}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    if command['op'] == 'stop':
        break
    print('MRPEER ' + json.dumps({'kind': 'sent', 'command': command['id'], 'native_id': '42'}), flush=True)
"""


async def test_peer_drives_native_commands_and_captures_only_protocol_records(tmp_path):
    peer = Peer("radio", tmp_path, 3)
    try:
        await peer.start(
            {"password": "private-fixture"}, argv=[sys.executable, "-c", CHILD]
        )
        assert (await peer.send("MR-test café ✓"))["native_id"] == "42"
        await peer.synchronize()
    finally:
        await peer.stop()
    records = [
        json.loads(line) for line in (tmp_path / "radio.jsonl").read_text().splitlines()
    ]
    assert [record["kind"] for record in records] == ["ready", "sent", "sent"]
    assert "private-fixture" not in (tmp_path / "radio.jsonl").read_text()
    assert peer.process.returncode == 0
    assert "SDK diagnostic" in (tmp_path / "radio.stdout").read_text()


async def test_unresponsive_stop_reaps_the_peer_and_fails_qualification(
    tmp_path, monkeypatch
):
    from scripts.adapter_campaign import process

    monkeypatch.setattr(process, "_STOP_TIMEOUT", 0.1)
    peer = Peer("stuck", tmp_path, 3)
    await peer.start(
        {},
        argv=[sys.executable, "-c", CHILD.replace("        break", "        continue")],
    )
    with pytest.raises(RuntimeError, match="cleanup"):
        await peer.stop()
    assert peer.process.returncode is not None
    assert peer.log.closed and peer.errors.closed and peer.output.closed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group ownership")
async def test_forced_cleanup_terminates_native_descendants(tmp_path, monkeypatch):
    import os

    from scripts.adapter_campaign import process
    from tests.helpers.async_utils import wait_until

    monkeypatch.setattr(process, "_STOP_TIMEOUT", 0.1)
    ready, terminated = tmp_path / "native-ready", tmp_path / "native-stopped"
    native = (
        "import signal, sys; from pathlib import Path; "
        f"signal.signal(signal.SIGTERM, lambda *_: (Path({str(terminated)!r}).touch(), sys.exit(0))); "
        f"Path({str(ready)!r}).write_text(str(__import__('os').getpid())); "
        "signal.pause()"
    )
    setup = (
        "import subprocess, signal\n"
        f"native = subprocess.Popen([sys.executable, '-c', {native!r}], stdin=subprocess.DEVNULL)\n"
        "def terminate(*_):\n"
        "    native.wait()\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, terminate)\n"
    )
    child = CHILD.replace("for line in sys.stdin:", setup + "for line in sys.stdin:")
    child = child.replace("        break", "        continue")
    peer = Peer("group", tmp_path, 3)
    try:
        await peer.start({}, argv=[sys.executable, "-c", child])
        await wait_until(ready.exists, timeout=3)
        assert os.getpgid(int(ready.read_text())) == peer.process.pid
    finally:
        with pytest.raises(RuntimeError, match="cleanup"):
            await peer.stop()
    assert terminated.exists()


async def test_peer_startup_crash_is_not_a_readiness_timeout(tmp_path):
    peer = Peer("mail", tmp_path, 3)
    with pytest.raises(RuntimeError, match="exited"):
        try:
            await peer.start({}, argv=[sys.executable, "-c", "raise SystemExit(3)"])
        finally:
            with pytest.raises(RuntimeError, match="cleanup"):
                await peer.stop()
    assert peer.process.returncode == 3


async def test_peer_reports_native_failure_even_after_readiness(tmp_path):
    child = CHILD.replace(
        "for line in sys.stdin:",
        'print(\'MRPEER {"kind": "error"}\', flush=True)\nfor line in sys.stdin:',
    )
    peer = Peer("xmpp", tmp_path, 3)
    with pytest.raises(RuntimeError, match="native peer reported failure"):
        try:
            await peer.start({}, argv=[sys.executable, "-c", child])
            await peer.send("nonce")
        finally:
            with pytest.raises(RuntimeError, match="cleanup"):
                await peer.stop()


async def test_malformed_protocol_output_fails_the_campaign(tmp_path):
    peer = Peer("broken", tmp_path, 3)
    with pytest.raises(RuntimeError, match="evidence stream"):
        try:
            await peer.start(
                {}, argv=[sys.executable, "-c", "print('MRPEER not-json', flush=True)"]
            )
        finally:
            with pytest.raises(RuntimeError, match="cleanup"):
                await peer.stop()
