"""Create disposable native conversations, independent peers, and campaign inputs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import yaml

from ..meshtastic_connection import connection_config, role_connection
from .sources import write_json


async def matrix(root: Path, accounts: dict) -> tuple[dict, dict]:
    import aiohttp

    server = accounts["homeserver"]
    async with aiohttp.ClientSession() as http:

        async def request(method, path, token=None, **kwargs):
            headers = {"Authorization": "Bearer " + token} if token else {}
            async with http.request(
                method, server + "/_matrix/client/v3/" + path, headers=headers, **kwargs
            ) as response:
                response.raise_for_status()
                return await response.json()

        peer = await request(
            "POST",
            "login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "peer"},
                "password": accounts["password"],
                "initial_device_display_name": "Independent campaign peer",
            },
        )
        credentials = list((root / "profiles/medre").rglob("matrix.json")) + list(
            (root / "profiles/medre").rglob("credentials.json")
        )
        bot = next(
            (
                json.loads(path.read_text())
                for path in credentials
                if json.loads(path.read_text()).get("auth_type") == "oauth"
            ),
            None,
        )
        if bot is None:
            raise ValueError(
                "Qualify MEDRE OAuth before provisioning its campaign room"
            )
        # Refresh through the production manager, not by constructing credentials.
        from medre.adapters.matrix.oauth import OAuthSession
        from medre.adapters.matrix.oauth_session import OAuthSessionManager

        path = next(
            path
            for path in credentials
            if json.loads(path.read_text()).get("auth_type") == "oauth"
        )
        manager = OAuthSessionManager(OAuthSession.parse(bot), path)
        token = await manager.access_token()
        for record, name in ((peer, "Peer"), (bot, "MEDRE")):
            await request(
                "PUT",
                "profile/" + quote(record["user_id"], safe="") + "/displayname",
                peer["access_token"] if name == "Peer" else token,
                json={"displayname": name},
            )
        room = (
            await request(
                "POST",
                "createRoom",
                token,
                json={
                    "name": "MEDRE native campaign",
                    "preset": "private_chat",
                    "invite": [peer["user_id"]],
                },
            )
        )["room_id"]
        await request(
            "POST", "join/" + quote(room, safe=""), peer["access_token"], json={}
        )
    return (
        {
            "homeserver": server,
            "user_id": "",
            "access_token": "",
            "room_allowlist": [room],
            "encryption_mode": "plaintext",
        },
        {
            "transport": "matrix",
            "channel": room,
            "expected_sender": bot["user_id"],
            "peer": {
                "homeserver": server,
                "user_id": peer["user_id"],
                "access_token": peer["access_token"],
            },
        },
    )


async def xmpp(root: Path, accounts: dict) -> tuple[dict, dict]:
    import ssl

    import slixmpp

    room = "campaign@rooms.xmpp.lab.test"
    client = slixmpp.ClientXMPP("medre@xmpp.lab.test/provision", accounts["password"])
    client.ssl_context = ssl.create_default_context()
    for plugin in ("xep_0004", "xep_0030", "xep_0045"):
        client.register_plugin(plugin)
    ready = asyncio.get_running_loop().create_future()

    async def connected(_):
        try:
            client.send_presence()
            await client["xep_0045"].join_muc_wait(
                room, "medre", maxstanzas=0, timeout=20
            )
            form = await client["xep_0045"].get_room_config(room)
            form["type"] = "submit"
            form["values"] = {
                **form["values"],
                "muc#roomconfig_persistentroom": True,
                "muc#roomconfig_membersonly": False,
            }
            await client["xep_0045"].set_room_config(room, form)
            ready.set_result(None)
        except Exception as exc:
            ready.set_exception(exc)

    client.add_event_handler("session_start", connected)
    connection = client.connect("xmpp.lab.test", 5222)
    try:
        await asyncio.wait_for(ready, 35)
    finally:
        await client.disconnect(wait=2)
        if not connection.done():
            connection.cancel()
        await asyncio.gather(connection, return_exceptions=True)
    return (
        {
            "jid": "medre@xmpp.lab.test/bridge",
            "host": "xmpp.lab.test",
            "port": 5222,
            "password": accounts["password"],
            "nickname": "medre",
            "rooms": [room],
        },
        {
            "transport": "xmpp",
            "channel": room,
            "expected_sender": room + "/medre",
            "peer": {
                "jid": "peer@xmpp.lab.test/campaign",
                "password": accounts["password"],
                "nickname": "peer",
                "host": "xmpp.lab.test",
            },
        },
    )


def chatmail(root: Path, accounts: dict) -> tuple[dict, dict]:
    from deltachat_rpc_client import DeltaChat, Rpc

    class DrainRpc(Rpc):
        def events_loop(self):
            with contextlib.suppress(Exception):
                while self.get_next_event_batch() and not self.closing:
                    pass

    clients = []
    try:
        profiles = []
        for role, name in (("bot", "MailBot"), ("peer", "MailPeer")):
            directory = root / "profiles" / ("mail-" + role + "-" + str(time.time_ns()))
            rpc = DrainRpc(
                str(directory),
                rpc_server_path=shutil.which("deltachat-rpc-server"),
                stderr=subprocess.DEVNULL,
                process_group=None,
            )
            rpc.start()
            clients.append(rpc)
            account = DeltaChat(rpc).add_account()
            account.set_config("displayname", name)
            account.add_or_update_transport(
                {
                    "addr": role + "@chatmail.test",
                    "password": accounts["password"],
                    "imapServer": "mail",
                    "imapPort": 3143,
                    "imapSecurity": "plain",
                    "imapUser": role,
                    "smtpServer": "mail",
                    "smtpPort": 3025,
                    "smtpSecurity": "plain",
                    "smtpUser": role,
                }
            )
            account.start_io()
            profiles.append((account, directory))
        (bot, bot_dir), (peer, peer_dir) = profiles
        group = bot.create_group("Native campaign")
        peer.secure_join(group.get_qr_code())
        deadline = time.monotonic() + 90
        while True:
            groups = [
                chat
                for chat in peer.get_chatlist()
                if chat.get_basic_snapshot().get("name") == "Native campaign"
            ]
            if groups and groups[0].can_send():
                peer_group = groups[0]
                peer_group.accept()
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Chatmail group invitation was not received")
            time.sleep(0.25)
        remote_members = [
            contact.id for contact in peer_group.get_contacts() if contact.id != 1
        ]
        if len(remote_members) != 1:
            raise ValueError(
                "Chatmail fixture must contain exactly one independent remote member"
            )
        return (
            {
                "accounts_dir": str(bot_dir),
                "account_id": bot.id,
                "chats": [group.id],
                "state_dir": str(root / "profiles/mail-journal"),
            },
            {
                "transport": "chatmail",
                "channel": str(group.id),
                "expected_sender": str(remote_members[0]),
                "peer": {
                    "accounts_dir": str(peer_dir),
                    "account_id": peer.id,
                    "channel": str(peer_group.id),
                },
            },
        )
    finally:
        for rpc in reversed(clients):
            rpc.close()
            rpc.process.wait(timeout=10)


class SimplexPeer:
    def __init__(self, root: Path, role: str, smp: str):
        self.root, self.role, self.smp = root, role, smp

    async def start(self):
        directory = (
            self.root
            / "profiles"
            / ("simplex-" + self.role + "-" + str(time.time_ns()))
        )
        directory.mkdir()
        self.prefix = str(directory / "profile")
        self.log = (self.root / "logs" / ("simplex-setup-" + self.role + ".log")).open(
            "wb"
        )
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scripts.adapter_campaign.lab.simplex_setup",
            stdin=-1,
            stdout=-1,
            stderr=self.log,
        )
        await self.write(
            {
                "prefix": self.prefix,
                "name": "SimpleXPeer" if self.role == "peer" else "SimpleXBot",
                "smp": self.smp,
            }
        )
        self.user = (await self.read())["ready"]

    async def write(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self):
        async with asyncio.timeout(60):
            while line := await self.process.stdout.readline():
                self.log.write(line)
                self.log.flush()
                if line.startswith(b"MRSETUP "):
                    return json.loads(line[8:])
            raise RuntimeError("SimpleX setup worker exited")

    async def call(self, operation, *args):
        await self.write({"op": operation, "args": args})
        response = await self.read()
        if "error" in response:
            raise RuntimeError("SimpleX setup command failed: " + operation)
        return response["result"]

    async def close(self):
        try:
            if self.process.returncode is None:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await self.write({"op": "stop"})
                try:
                    await asyncio.wait_for(self.process.wait(), 15)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        finally:
            self.log.close()


async def simplex(root: Path) -> tuple[dict, dict]:
    # Recorded by the host controller from the relay's generated TLS identity.
    fingerprint = (root / "config/smp-fingerprint").read_text().strip()
    smp = "smp://" + fingerprint + "@smp.lab.test:5223"
    bot, peer = SimplexPeer(root, "bot", smp), SimplexPeer(root, "peer", smp)
    started = []
    try:
        for endpoint in (bot, peer):
            started.append(endpoint)
            await endpoint.start()
        group = await bot.call(
            "api_new_group",
            bot.user,
            {"displayName": "Native campaign", "fullName": ""},
        )
        link = await bot.call("api_create_group_link", group["groupId"], "member")
        await peer.call("api_connect_active_user", link)
        deadline = time.monotonic() + 120
        while True:
            groups = await peer.call("api_list_groups", peer.user)
            selected = next(
                (
                    entry
                    for entry in groups
                    if entry["groupProfile"]["displayName"] == "Native campaign"
                ),
                None,
            )
            if selected and selected["membership"]["memberStatus"] in {
                "connected",
                "complete",
            }:
                break
            if selected and selected["membership"]["memberStatus"] == "invited":
                await peer.call("api_join_group", selected["groupId"])
            if time.monotonic() >= deadline:
                raise TimeoutError("SimpleX native group join timed out")
            await asyncio.sleep(0.25)
        members = await peer.call("api_list_members", selected["groupId"])
        sender = next(
            member
            for member in members
            if member["memberProfile"]["displayName"] == "SimpleXBot"
        )
        return (
            {
                "database_prefix": bot.prefix,
                "native_cache_dir": "/opt/native-cache",
                "user_id": bot.user,
                "chats": ["group:" + str(group["groupId"])],
            },
            {
                "transport": "simplexchat",
                "channel": "group:" + str(group["groupId"]),
                "expected_sender": str(sender["groupMemberId"]),
                "peer": {
                    "database_prefix": peer.prefix,
                    "user_id": peer.user,
                    "channel": "group:" + str(selected["groupId"]),
                },
            },
        )
    finally:
        for endpoint in reversed(started):
            if hasattr(endpoint, "process"):
                await endpoint.close()


async def provision(root: Path) -> None:
    accounts = json.loads((root / "config/accounts.json").read_text())
    result_path = root / "results/provision.json"
    results = json.loads(result_path.read_text()) if result_path.exists() else {}
    failures = []
    if results.get("lxmf", {}).get("status") != "ready":
        try:
            runtime, endpoint = lxmf(root)
            results["lxmf"] = {
                "runtime": runtime,
                "endpoint": endpoint,
                "status": "ready",
                "substrate": "docker_tcp",
            }
        except Exception as exc:
            import traceback

            traceback.print_exc()
            results["lxmf"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            failures.append("lxmf")
        write_json(root / "results/provision.json", results)
    # Preserve independent provisioning outcomes; one unavailable service must
    # not hide the readiness of every other native transport.
    for name, function in (("matrix", matrix), ("xmpp", xmpp), ("chatmail", chatmail)):
        if results.get(name, {}).get("status") == "ready":
            continue
        try:
            result = (
                await asyncio.to_thread(function, root, accounts)
                if name == "chatmail"
                else await function(root, accounts)
            )
            results[name] = {
                "runtime": result[0],
                "endpoint": result[1],
                "status": "ready",
            }
        except Exception as exc:
            import traceback

            traceback.print_exc()
            results[name] = {"status": "failed", "error_type": type(exc).__name__}
            failures.append(name)
        write_json(root / "results/provision.json", results)
    if results.get("simplexchat", {}).get("status") != "ready":
        try:
            runtime, endpoint = await simplex(root)
            results["simplexchat"] = {
                "runtime": runtime,
                "endpoint": endpoint,
                "status": "ready",
            }
        except Exception as exc:
            import traceback

            traceback.print_exc()
            results["simplexchat"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            failures.append("simplexchat")
    write_json(root / "results/provision.json", results)
    if failures:
        raise RuntimeError("Native provisioning failed: " + ", ".join(failures))


async def campaign(root: Path) -> None:
    from ..plan import TRANSPORTS
    from .control import command

    provisions = json.loads((root / "results/provision.json").read_text())
    hardware = json.loads((root / "hardware.json").read_text())
    health_file = root / "results/hardware.json"
    health = json.loads(health_file.read_text()) if health_file.exists() else {}
    # Device identities and names come from the independent provisioning read,
    # never from the outbox or bodies under examination.
    for name in ("meshtastic", "meshcore"):
        if name == "meshcore" and (root / "results/meshcore-paused.json").exists():
            continue
        pair = health.get(name, {})
        if not pair or any(
            pair.get(role, {}).get("status") != "available"
            for role in ("gateway", "peer")
        ):
            continue
        settings = hardware[name]
        channel = int(settings.get("channel", 0 if name == "meshtastic" else 1))
        if name == "meshtastic":
            config = {
                **connection_config(role_connection(settings, "gateway")),
                "default_channel": channel,
                "message_delay_seconds": 2.5,
            }
            endpoint = {
                "transport": name,
                "channel": str(channel),
                "expected_sender": pair["gateway"]["node_id"],
                "peer": role_connection(settings, "peer"),
            }
        else:
            config = {
                "connection_type": "ble",
                "ble_address": settings["gateway_ble_address"],
                "default_channel": channel,
                "identity": pair["gateway"]["name"],
                "message_delay_seconds": 2.5,
                "meshcore_relay_prefix": "[{origin_label}] {sender}: ",
            }
            endpoint = {
                "transport": name,
                "channel": str(channel),
                "expected_sender": pair["gateway"]["name"],
                "peer": {"ble_address": settings["peer_ble_address"]},
            }
        provisions[name] = {"runtime": config, "endpoint": endpoint, "status": "ready"}
    selected = {
        name: value for name, value in provisions.items() if value["status"] == "ready"
    }
    required = TRANSPORTS - {"meshtastic", "meshcore"}
    if not required <= selected.keys():
        raise ValueError(
            "Every Docker messaging fixture must be ready before native fanout"
        )
    if len(selected) < 2:
        raise ValueError("At least two qualified native fixtures are required")
    names = {
        "matrix": "Peer",
        "xmpp": "peer",
        "chatmail": "MailPeer",
        "simplexchat": "SimpleXPeer",
        "lxmf": "MEDRE campaign peer",
    }
    short_names = {**names, "lxmf": "MEDREcampaignpeer"}
    if "meshtastic" in selected:
        names["meshtastic"] = health["meshtastic"]["peer"]["long_name"]
    if "meshcore" in selected:
        names["meshcore"] = health["meshcore"]["peer"]["name"] + " (unverified)"
    labels = {name: name.capitalize() + " lab" for name in selected}
    labels["meshtastic"] = "Radio lab"
    runtime = {"adapters": {}}
    endpoints = {}
    for name, fixture in selected.items():
        config = {
            "enabled": True,
            "adapter_kind": "real",
            "origin_label": labels[name],
            **fixture["runtime"],
        }
        runtime["adapters"][name] = {name: config}
        endpoint = fixture["endpoint"]
        prefixes = {}
        for source in selected:
            if source == name:
                continue
            sender = names[source]
            if name == "matrix":
                prefix = f"[{sender}/{labels[source]}]: "
            elif name == "meshtastic":
                prefix = (
                    short_names.get(source, sender)
                    + ("[M]" if source == "matrix" else "")
                    + ": "
                )
            else:
                prefix = f"[{labels[source]}] {sender}: "
            prefixes[source] = prefix
        endpoint["expected_prefixes"] = prefixes
        endpoints[name] = endpoint
    (root / "runtime.yaml").write_text(yaml.safe_dump(runtime, sort_keys=False))
    (root / "campaign.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "runtime_config": "runtime.yaml",
                "timeout_seconds": 90,
                "pacing_seconds": 2.5,
                "settle_seconds": 5,
                "endpoints": endpoints,
            },
            sort_keys=False,
        )
    )
    write_json(
        root / "results/coverage.json",
        {
            "requested": sorted(TRANSPORTS),
            "selected": sorted(selected),
            "excluded": {
                name: health.get(
                    name, provisions.get(name, {"status": "not_configured"})
                )
                for name in TRANSPORTS - selected.keys()
            },
            "coverage": "full" if set(selected) == TRANSPORTS else "partial",
        },
    )
    # Both runtime and its child peer processes must see the OAuth sidecar and
    # E2EE identity prepared by the real CLI under this profile.
    profile = root / "profiles/medre"
    os.environ.update(
        XDG_CONFIG_HOME=str(profile / "config"),
        XDG_STATE_HOME=str(profile / "state"),
        XDG_DATA_HOME=str(profile / "data"),
    )
    output = root / "results" / ("native-" + str(time.time_ns()))
    argv = [
        sys.executable,
        "-m",
        "scripts.adapter_campaign.lab.campaign_runner",
        "--config",
        str(root / "campaign.yaml"),
        "--run",
        "--output",
        str(output),
    ]
    if set(selected) != TRANSPORTS:
        argv += ["--only", ",".join(sorted(selected))]
    failure = None
    try:
        await asyncio.to_thread(command, root, "native-campaign", argv, timeout=1200)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        failure = exc
    if (output / "results.json").exists():
        try:
            await asyncio.to_thread(
                command,
                root,
                "evidence-analysis",
                [sys.executable, "-m", "scripts.analyze_adapter_campaign", str(output)],
            )
        except (RuntimeError, subprocess.TimeoutExpired):
            if failure is None:
                raise
    if failure is not None:
        raise failure


def lxmf(root: Path) -> tuple[dict, dict]:
    import RNS

    configs = []
    for role in ("bot", "peer"):
        directory = root / "profiles" / ("lxmf-" + role + "-" + str(time.time_ns()))
        directory.mkdir()
        identity = RNS.Identity()
        identity_path = directory / "identity"
        identity.to_file(str(identity_path))
        config = directory / "reticulum"
        config.mkdir()
        interface = (
            "type = TCPServerInterface\n  listen_ip = 0.0.0.0\n  listen_port = 55249"
            if role == "peer"
            else "type = TCPClientInterface\n  target_host = 127.0.0.1\n  target_port = 55249"
        )
        (config / "config").write_text(
            "[reticulum]\n  enable_transport = No\n  share_instance = No\n[interfaces]\n [[Campaign TCP]]\n  enabled = Yes\n  "
            + interface
            + "\n"
        )
        configs.append(
            {
                "identity_path": str(identity_path),
                "reticulum_config_dir": str(config),
                "storage_path": str(directory / "storage"),
                "destination": RNS.Destination.hash_from_name_and_identity(
                    "lxmf.delivery", identity
                ).hex(),
            }
        )
    bot, peer = configs
    return (
        {
            "connection_type": "reticulum",
            **{key: value for key, value in bot.items() if key != "destination"},
            "default_delivery_method": "direct",
            "announce_interval_seconds": 8,
            "stamp_cost": 0,
            "lxmf_relay_prefix": "[{origin_label}] {sender}: ",
        },
        {
            "transport": "lxmf",
            "channel": peer["destination"],
            "expected_sender": bot["destination"],
            "peer": {key: value for key, value in peer.items() if key != "destination"},
        },
    )
