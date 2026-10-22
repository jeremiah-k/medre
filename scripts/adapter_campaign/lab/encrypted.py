"""Provision encrypted rooms and run the independently witnessed crypto tier."""

from __future__ import annotations

import json
from pathlib import Path
import time
from urllib.parse import quote

import yaml

from ..encrypted_evidence import PREFIX
from ..encrypted_peer import seed
from .sources import write_json


async def provision(root: Path, directory: Path) -> dict:
    """Create disposable devices and encrypted rooms without rotating identities.

    Password sessions deliberately isolate crypto/attachment qualification from
    OAuth and cross-signing, which have their own native lab stage. Each device
    owns one exclusive store; the witness belongs to neither relay identity.
    """
    import aiohttp

    accounts = json.loads((root / "config/accounts.json").read_text())
    server = accounts["homeserver"]
    devices = {}
    topology = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:

        async def request(method: str, path: str, token: str | None = None, **kwargs):
            """Use the real server API and retain failures only in private logs."""
            headers = {"Authorization": "Bearer " + token} if token else {}
            async with http.request(
                method, server + "/_matrix/client/v3/" + path, headers=headers, **kwargs
            ) as response:
                response.raise_for_status()
                return await response.json()

        for name, user in (
            ("matrix", "medre"),
            ("matrix_target", "mmrelay"),
            ("peer", "peer"),
        ):
            login = await request(
                "POST",
                "login",
                json={
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": user},
                    "password": accounts["password"],
                    "initial_device_display_name": "Encrypted campaign " + name,
                },
            )
            settings = {
                key: login[key] for key in ("user_id", "device_id", "access_token")
            }
            settings.update(
                homeserver=server, store_path=str(directory / (name + "-crypto"))
            )
            keys = await seed(settings)
            devices[name] = settings
            topology[name] = dict(
                user_id=settings["user_id"], device_id=settings["device_id"], keys=keys
            )
        for name in ("matrix", "matrix_target"):
            room = (
                await request(
                    "POST",
                    "createRoom",
                    devices[name]["access_token"],
                    json={
                        "preset": "private_chat",
                        "name": "Encrypted campaign " + name,
                        "invite": [devices["peer"]["user_id"]],
                        "initial_state": [
                            {
                                "type": "m.room.encryption",
                                "state_key": "",
                                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                            }
                        ],
                    },
                )
            )["room_id"]
            await request(
                "POST",
                "join/" + quote(room, safe=""),
                devices["peer"]["access_token"],
                json={},
            )
            topology[name]["room"] = room
    runtime = {
        "adapters": {
            "matrix": {
                name: {
                    **devices[name],
                    "adapter_kind": "real",
                    "enabled": True,
                    "room_allowlist": [topology[name]["room"]],
                    "encryption_mode": "e2ee_required",
                    "require_encrypted_rooms": True,
                    "relay_prefix": PREFIX,
                }
                for name in ("matrix", "matrix_target")
            }
        },
        "storage": {"backend": "sqlite", "path": str(directory / "medre.sqlite3")},
        "logging": {"level": "INFO"},
        "retry": {"enabled": False},
        "attachments": {
            "enabled": True,
            "max_attachment_bytes": 65536,
            "max_retained_bytes": 1048576,
            "transfer_timeout_seconds": 30,
        },
        "routes": {
            f"{source}_to_{target}": {
                "source_adapters": [source],
                "dest_adapters": [target],
                "source_channel": topology[source]["room"],
                "dest_channel": topology[target]["room"],
                "policy": {
                    "allowed_event_types": [
                        "message.created",
                        "message.text",
                        "message.file",
                    ]
                },
            }
            for source, target in (
                ("matrix", "matrix_target"),
                ("matrix_target", "matrix"),
            )
        },
    }
    runtime_file = directory / "runtime.yaml"
    runtime_file.write_text(yaml.safe_dump(runtime))
    fixture = dict(
        runtime_file=str(runtime_file),
        expectations=topology,
        peer={
            **devices["peer"],
            "rooms": [topology[name]["room"] for name in ("matrix", "matrix_target")],
        },
    )
    write_json(directory / "fixture.json", fixture)
    return fixture


async def run(root: Path) -> None:
    """Qualify real encrypted traffic, restart persistence, and offline evidence."""
    from ..encrypted import run as campaign
    from scripts.analyze_encrypted_campaign import analyze

    directory = root / "results" / ("encrypted-" + str(time.time_ns()))
    directory.mkdir(mode=0o700)
    fixture = await provision(root, directory)
    await campaign(fixture, directory)
    write_json(directory / "analysis.json", await analyze(directory))
