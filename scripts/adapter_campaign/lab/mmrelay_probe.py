"""Sequential native MMRelay bridge qualification using independent SDK peers."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import yaml

from ..process import Peer
from ..run import native_receipt
from .sources import write_json


async def run(root: Path) -> None:
    import aiohttp

    from medre.adapters.matrix.oauth import OAuthSession
    from medre.adapters.matrix.oauth_session import OAuthSessionManager

    fixture = json.loads((root / "results/provision.json").read_text())["matrix"][
        "endpoint"
    ]
    hardware = json.loads((root / "hardware.json").read_text())["meshtastic"]
    health = json.loads((root / "results/hardware.json").read_text())["meshtastic"]
    if any(health[role]["status"] != "available" for role in ("gateway", "peer")):
        raise ConnectionError(
            "MMRelay requires an independently readable Meshtastic pair"
        )
    room = fixture["channel"]
    credentials = root / "profiles/medre/config/medre/credentials/matrix.json"
    record = json.loads(credentials.read_text())
    token = await OAuthSessionManager(
        OAuthSession.parse(record), credentials
    ).access_token()
    async with aiohttp.ClientSession(
        headers={"Authorization": "Bearer " + token}
    ) as http:
        async with http.post(
            "https://matrix.lab.test/_matrix/client/v3/rooms/"
            + quote(room, safe="")
            + "/invite",
            json={"user_id": "@mmrelay:matrix.lab.test"},
        ) as response:
            # A rerun can invite a member already joined to the same room.
            if response.status != 200:
                async with http.get(
                    "https://matrix.lab.test/_matrix/client/v3/rooms/"
                    + quote(room, safe="")
                    + "/joined_members"
                ) as members:
                    members.raise_for_status()
                    assert (
                        "@mmrelay:matrix.lab.test" in (await members.json())["joined"]
                    )
    profile = root / "profiles/mmrelay"
    config = yaml.safe_load((profile / "config.yaml").read_text())
    config["matrix_rooms"] = [
        {"id": room, "meshtastic_channel": int(hardware.get("channel", 0))}
    ]
    config["meshtastic"] = {
        "connection_type": "serial",
        "serial_port": hardware["gateway_serial_port"],
        "meshnet_name": "Radio lab",
    }
    config["logging"] = {"level": "info", "color_enabled": False, "log_to_file": False}
    config["plugins"] = {}
    path = profile / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    directory = root / "results" / ("mmrelay-native-" + str(time.time_ns()))
    directory.mkdir()
    marker = "MRLAB-" + uuid4().hex[:12]
    radio_endpoint = {
        "transport": "meshtastic",
        "channel": str(hardware.get("channel", 0)),
        "expected_sender": health["gateway"]["node_id"],
        "expected_prefixes": {"matrix": "Peer[M]: "},
        "peer": {"serial_port": hardware["peer_serial_port"]},
    }
    matrix_endpoint = {
        **fixture,
        "expected_sender": "@mmrelay:matrix.lab.test",
        "expected_prefixes": {
            "meshtastic": "[" + health["peer"]["long_name"] + "/Radio lab]: "
        },
    }
    peers = {}
    process = None
    report = {"status": "failed", "checks": []}
    log = (directory / "runtime.log").open("wb")
    try:
        for name, endpoint in (
            ("matrix", matrix_endpoint),
            ("meshtastic", radio_endpoint),
        ):
            peer = Peer(name, directory, 90)
            peers[name] = peer
            await peer.start({**endpoint, "marker_prefix": marker})
        ready = directory / "ready"
        process = await asyncio.create_subprocess_exec(
            "/opt/mmrelay/bin/mmrelay",
            "--home",
            str(profile),
            "--config",
            str(path),
            env={
                **os.environ,
                "MMRELAY_HOME": str(profile),
                "MMRELAY_READY_FILE": str(ready),
                "PYTHONUNBUFFERED": "1",
            },
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        async with asyncio.timeout(120):
            while not ready.exists():
                if process.returncode is not None:
                    raise RuntimeError("MMRelay exited before native readiness")
                await asyncio.sleep(0.2)
        report["checks"].append("native_runtime_readiness")
        text = marker + "-radio café ✓"
        await peers["meshtastic"].send(text)
        await peers["matrix"].wait(
            lambda row: row["kind"] == "received"
            and text in row["text"]
            and row["sender"] == matrix_endpoint["expected_sender"]
        )
        native_receipt(
            peers["matrix"], matrix_endpoint, text, source="meshtastic", message=text
        )
        report["checks"].append("radio_to_matrix_prefix_plain_html_parity")
        # Pace physical radio legs and test the documented UTF-8 byte boundary.
        await asyncio.sleep(2.5)
        text = marker + "-matrix café ✓ " + "é" * 250
        await peers["matrix"].send(text)
        await peers["meshtastic"].wait(
            lambda row: row["kind"] == "received"
            and marker + "-matrix" in row["text"]
            and row["sender"] == radio_endpoint["expected_sender"]
        )
        native_receipt(
            peers["meshtastic"],
            radio_endpoint,
            marker + "-matrix",
            source="matrix",
            message=text,
            max_text_bytes=227,
        )
        report["checks"].append("matrix_to_radio_prefix_utf8_truncation")
        # Detect delayed duplicates before stopping either independent witness.
        await asyncio.sleep(5)
        native_receipt(
            peers["matrix"],
            matrix_endpoint,
            marker + "-radio",
            source="meshtastic",
            message=marker + "-radio café ✓",
        )
        native_receipt(
            peers["meshtastic"],
            radio_endpoint,
            marker + "-matrix",
            source="matrix",
            message=text,
            max_text_bytes=227,
        )
        report["status"] = "passed"
    finally:
        cleanup_errors = []
        if process is not None and process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 30)
            except TimeoutError:
                cleanup_errors.append("mmrelay_forced_stop")
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        for name, peer in reversed(list(peers.items())):
            try:
                await peer.stop()
            except Exception:
                cleanup_errors.append(name)
        if process is not None:
            report["runtime_exit_code"] = process.returncode
            if process.returncode != 0:
                cleanup_errors.append("mmrelay_exit_status")
        log.close()
        report["cleanup_errors"] = cleanup_errors
        if cleanup_errors:
            report["status"] = "failed"
        write_json(directory / "results.json", report)
        if cleanup_errors:
            raise RuntimeError("MMRelay native campaign cleanup failed")
