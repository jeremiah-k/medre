"""Read-only hardware discovery; inaccessible devices retain explicit outcomes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from .sources import write_json


async def inspect_hardware(root: Path) -> dict:
    requested = json.loads((root / "hardware.json").read_text())
    report = {}
    if "meshtastic" in requested:
        from google.protobuf.json_format import MessageToDict
        from meshtastic.serial_interface import SerialInterface

        results = {}
        for role in ("gateway", "peer"):
            port = requested["meshtastic"][role + "_serial_port"]

            def probe(port=port, role=role):
                interface = SerialInterface(port, timeout=20)
                try:
                    node = interface.getMyNodeInfo()
                    channels = [
                        MessageToDict(channel)
                        for channel in interface.localNode.channels
                    ]
                    write_json(
                        root / "profiles" / f"meshtastic-{role}-snapshot.json",
                        {
                            "node": node,
                            "channels": channels,
                            "config": MessageToDict(interface.localNode.localConfig),
                        },
                    )
                    # Do not publish channel PSKs in the coverage summary.
                    fingerprints = [
                        hashlib.sha256(bytes(channel.settings.psk)).hexdigest()
                        for channel in interface.localNode.channels
                    ]
                    return {
                        "status": "available",
                        "node_id": str(interface.myInfo.my_node_num),
                        "long_name": node["user"].get("longName", ""),
                        "short_name": node["user"].get("shortName", ""),
                        "channel_fingerprints": fingerprints,
                    }
                finally:
                    interface.close()

            try:
                results[role] = await asyncio.to_thread(probe)
            except Exception as exc:
                (root / "logs" / f"meshtastic-{role}-failure.log").write_text(repr(exc))
                results[role] = {
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                }
        report["meshtastic"] = results
    if "meshcore" in requested:
        from meshcore import EventType, MeshCore

        results = {}
        for role in ("gateway", "peer"):
            connection = None
            try:
                async with asyncio.timeout(35):
                    connection = await MeshCore.create_ble(
                        address=requested["meshcore"][role + "_ble_address"],
                        default_timeout=10,
                    )
                    if connection is None:
                        raise ConnectionError("MeshCore BLE unavailable")
                    info = await connection.commands.send_appstart()
                    if info.type != EventType.SELF_INFO:
                        raise ConnectionError("MeshCore self-info unavailable")
                    clock = await connection.commands.get_time()
                    results[role] = {
                        "status": "available",
                        "name": info.payload["name"],
                        "clock": clock.payload,
                    }
            except Exception as exc:
                (root / "logs" / f"meshcore-{role}-failure.log").write_text(repr(exc))
                results[role] = {
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                }
            finally:
                if connection is not None:
                    try:
                        async with asyncio.timeout(10):
                            await connection.disconnect()
                    except Exception:
                        results[role]["cleanup"] = "failed"
        report["meshcore"] = results
    write_json(root / "results/hardware.json", report)
    return report
