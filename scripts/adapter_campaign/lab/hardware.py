"""Read-only hardware discovery; inaccessible devices retain explicit outcomes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from ..meshtastic_connection import open_interface, role_connection
from .sources import write_json


async def inspect_hardware(root: Path, *, meshcore_repaired: bool = False) -> dict:
    """Probe independent devices once; retain a MeshCore pause until repair is acknowledged."""
    requested = json.loads((root / "hardware.json").read_text())
    pause = root / "results/meshcore-paused.json"
    if meshcore_repaired:
        if "meshcore" not in requested:
            raise ValueError(
                "MeshCore repair acknowledgement requires configured hardware"
            )
        pause.unlink(missing_ok=True)
    report = {}
    if "meshtastic" in requested:
        from google.protobuf.json_format import MessageToDict

        connections = {
            role: role_connection(requested["meshtastic"], role)
            for role in ("gateway", "peer")
        }
        results = {}
        for role, connection in connections.items():

            def probe(connection=connection, role=role):
                interface = open_interface(connection, timeout=20)
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
        results = {}
        for role in ("gateway", "peer"):
            if pause.exists():
                results[role] = {
                    "status": "not_attempted",
                    "reason": "re_pair_required",
                }
                continue
            try:
                from ..meshcore_ble import open_meshcore_ble

                async with asyncio.timeout(45):
                    async with open_meshcore_ble(
                        requested["meshcore"][role + "_ble_address"]
                    ) as connection:
                        info = connection.self_info
                        if not isinstance(info, dict) or not info.get("name"):
                            raise ConnectionError("MeshCore self-info unavailable")
                        clock = await connection.commands.get_time()
                        results[role] = {
                            "status": "available",
                            "name": info["name"],
                            "clock": clock.payload,
                        }
            except Exception as exc:
                (root / "logs" / f"meshcore-{role}-failure.log").write_text(repr(exc))
                results[role] = {
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                }
                write_json(pause, {"role": role, "error_type": type(exc).__name__})
                print(
                    "MeshCore hardware paused: re-pair the boards before acknowledging repair."
                )
        report["meshcore"] = results
    write_json(root / "results/hardware.json", report)
    return report
