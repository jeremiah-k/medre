"""Prepare disposable native Matrix/XMPP identities for typing qualification."""

from __future__ import annotations

import json
from pathlib import Path
import time
from urllib.parse import quote

import yaml

from ..plan import load_plan
from ..typing_campaign import run as campaign
from .sources import write_json


async def provision(root: Path, directory: Path) -> Path:
    """Use isolated password accounts; OAuth and crypto retain separate tiers."""
    import aiohttp
    from .provision import xmpp

    accounts = json.loads((root / "config/accounts.json").read_text())
    server = accounts["homeserver"]
    identities = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:

        async def request(method, path, token=None, **kwargs):
            """Call the test server directly, retaining failures in private logs."""
            headers = {"Authorization": "Bearer " + token} if token else {}
            async with http.request(
                method, server + "/_matrix/client/v3/" + path, headers=headers, **kwargs
            ) as response:
                response.raise_for_status()
                return await response.json()

        for user in ("medre", "peer"):
            identities[user] = await request(
                "POST",
                "login",
                json={
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": user},
                    "password": accounts["password"],
                    "initial_device_display_name": "Typing qualification",
                },
            )
        bot, peer = identities["medre"], identities["peer"]
        room = (
            await request(
                "POST",
                "createRoom",
                bot["access_token"],
                json={
                    "preset": "private_chat",
                    "name": "Typing qualification",
                    "invite": [peer["user_id"]],
                },
            )
        )["room_id"]
        await request(
            "POST", "join/" + quote(room, safe=""), peer["access_token"], json={}
        )
    xmpp_config, xmpp_endpoint = await xmpp(root, accounts)
    runtime = {
        "adapters": {
            "matrix": {
                "matrix": {
                    "adapter_kind": "real",
                    "homeserver": server,
                    "user_id": bot["user_id"],
                    "access_token": bot["access_token"],
                    "device_id": bot["device_id"],
                    "encryption_mode": "plaintext",
                    "room_allowlist": [room],
                }
            },
            "xmpp": {"xmpp": {"adapter_kind": "real", **xmpp_config}},
        }
    }
    runtime_file = directory / "runtime.yaml"
    runtime_file.write_text(yaml.safe_dump(runtime))
    endpoints = {
        "matrix": {
            "transport": "matrix",
            "channel": room,
            "expected_sender": bot["user_id"],
            "expected_prefixes": {"xmpp": ""},
            "peer": {
                "homeserver": server,
                "user_id": peer["user_id"],
                "access_token": peer["access_token"],
            },
        },
        "xmpp": {**xmpp_endpoint, "expected_prefixes": {"matrix": ""}},
    }
    fixture = directory / "campaign.yaml"
    fixture.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "runtime_config": "runtime.yaml",
                "timeout_seconds": 60,
                "settle_seconds": 3,
                "endpoints": endpoints,
            }
        )
    )
    return fixture


async def run(root: Path) -> None:
    """Qualify transient activity with native witnesses and durable row inspection."""
    directory = root / "results" / ("typing-" + str(time.time_ns()))
    directory.mkdir(mode=0o700)
    write_json(
        directory / "sources.json", json.loads((root / "sources.json").read_text())
    )
    fixture = await provision(root, directory)
    # The campaign owns its own child evidence directory beside the fixture.
    report = await campaign(
        load_plan(fixture, only={"matrix", "xmpp"}), directory / "evidence"
    )
    write_json(directory / "qualification.json", report)
