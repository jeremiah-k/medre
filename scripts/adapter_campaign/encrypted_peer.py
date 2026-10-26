"""Independent SDK peer: decrypt native events without using MEDRE's adapter."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import sys
import traceback
from pathlib import Path
from urllib.parse import quote

from .worker import emit


def digest(data: bytes) -> str:
    """Return the SHA-256 witness used for plaintext and ciphertext bytes."""
    return hashlib.sha256(data).hexdigest()


async def close_client(client) -> None:
    """Drain SDK tasks and close its separately owned SQLite crypto store."""
    try:
        await client.close()
    finally:
        database = getattr(getattr(client, "store", None), "database", None)
        if database is not None:
            from playhouse.sqliteq import SqliteQueueDatabase

            if isinstance(database, SqliteQueueDatabase) and not database.is_stopped():
                database.stop()
            if not database.is_closed():
                database.close()


def client_for(settings: dict):
    """Restore one explicitly provisioned device into its exclusive store."""
    from nio import AsyncClient, AsyncClientConfig

    Path(settings["store_path"]).mkdir(parents=True, exist_ok=True)
    client = AsyncClient(
        settings["homeserver"],
        settings["user_id"],
        device_id=settings["device_id"],
        store_path=settings["store_path"],
        config=AsyncClientConfig(encryption_enabled=True),
    )
    client.restore_login(
        settings["user_id"], settings["device_id"], settings["access_token"]
    )
    return client


def checked(response, expected):
    """Reject native errors without writing credentials or response bodies."""
    if not isinstance(response, expected):
        raise RuntimeError(f"Native Matrix operation failed: {type(response).__name__}")
    return response


async def seed(settings: dict) -> dict:
    """Publish a device's crypto keys, returning only its public identity keys."""
    from nio import KeysUploadResponse

    client = client_for(settings)
    try:
        checked(await client.keys_upload(), KeysUploadResponse)
        return dict(client.olm.account.identity_keys)
    finally:
        await close_client(client)


async def run(settings: dict) -> None:
    """Own a sync loop and command channel; every observed nonce must decrypt.

    Evidence contains hashes and public identities, never media keys, IVs,
    access tokens, or ciphertext. Unknown devices receive keys under the same
    permissive peer-trust policy as the adapter; this is not trust verification.
    """
    import aiohttp
    import nio
    from nio.crypto.attachments import decrypt_attachment

    client = client_for(settings)
    lock = asyncio.Lock()
    task = None
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + settings["access_token"]},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as http:

            async def received(room, event):
                """Independently witness ciphertext, then decrypt file bytes."""
                content = event.source.get("content", {})
                body = content.get("body", "")
                if not isinstance(body, str) or settings["prefix"] not in body:
                    return
                if room.room_id not in settings["rooms"] or not event.decrypted:
                    raise RuntimeError(
                        "Campaign event was not decrypted in a declared room"
                    )
                url = (
                    settings["homeserver"]
                    + "/_matrix/client/v3/rooms/"
                    + quote(room.room_id, safe="")
                    + "/event/"
                    + quote(event.event_id, safe="")
                )
                async with http.get(url) as response:
                    response.raise_for_status()
                    wire = await response.json()
                encrypted = wire.get("content", {})
                if (
                    wire.get("type") != "m.room.encrypted"
                    or encrypted.get("algorithm") != "m.megolm.v1.aes-sha2"
                    or not isinstance(encrypted.get("ciphertext"), str)
                    or not encrypted["ciphertext"]
                    or any(key in encrypted for key in ("body", "file", "url"))
                ):
                    raise RuntimeError("Native event lacks encrypted wire evidence")
                witness = dict(
                    room=room.room_id,
                    native_id=event.event_id,
                    sender=event.sender,
                    text=body,
                    decrypted=True,
                    wire_type=wire["type"],
                    algorithm=encrypted["algorithm"],
                    device_id=encrypted["device_id"],
                    wire_sha256=digest(encrypted["ciphertext"].encode()),
                )
                if content.get("msgtype") == "m.file":
                    file = content["file"]
                    download = checked(
                        await client.download(file["url"]), nio.DownloadResponse
                    )
                    ciphertext = download.body
                    plaintext = decrypt_attachment(
                        ciphertext,
                        file["key"]["k"],
                        file["hashes"]["sha256"],
                        file["iv"],
                    )
                    if ciphertext == plaintext or "url" in content:
                        raise RuntimeError("Media was uploaded without encryption")
                    witness["attachment"] = dict(
                        sha256=digest(plaintext),
                        size_bytes=len(plaintext),
                        ciphertext_sha256=digest(ciphertext),
                        filename=content["filename"],
                        mime_type=content["info"]["mimetype"],
                    )
                emit("received", **witness)

            client.add_event_callback(received, nio.RoomMessage)

            async def synchronize():
                """Serialize crypto mutation and surface every failed SDK operation."""
                while True:
                    async with lock:
                        checked(await client.sync(timeout=1000), nio.SyncResponse)
                        if client.should_upload_keys:
                            checked(await client.keys_upload(), nio.KeysUploadResponse)
                        if client.should_query_keys:
                            checked(await client.keys_query(), nio.KeysQueryResponse)
                        if client.should_claim_keys:
                            users = client.get_users_for_key_claiming()
                            if users:
                                checked(
                                    await client.keys_claim(users),
                                    nio.KeysClaimResponse,
                                )
                        for response in await client.send_to_device_messages():
                            checked(response, nio.ToDeviceResponse)
                    await asyncio.sleep(0)

            # Learn room encryption state before announcing readiness.
            checked(await client.sync(full_state=True), nio.SyncResponse)
            if any(
                room not in client.rooms or not client.rooms[room].encrypted
                for room in settings["rooms"]
            ):
                raise RuntimeError("Every declared campaign room must be encrypted")
            task = asyncio.create_task(synchronize())
            emit(
                "ready",
                identity=settings["user_id"],
                device_id=settings["device_id"],
                keys=dict(client.olm.account.identity_keys),
            )
            while True:
                line_task = asyncio.create_task(asyncio.to_thread(sys.stdin.readline))
                done, _ = await asyncio.wait(
                    {task, line_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if task in done:
                    line_task.cancel()
                    await asyncio.gather(line_task, return_exceptions=True)
                    await task
                    raise RuntimeError("Encrypted peer sync loop exited")
                line = line_task.result()
                if not line:
                    raise RuntimeError("Encrypted peer supervisor closed without stop")
                command = json.loads(line)
                if command["op"] == "stop":
                    return
                if command["op"] == "identities":
                    async with http.post(
                        settings["homeserver"] + "/_matrix/client/v3/keys/query",
                        json={
                            "device_keys": {
                                entry["user_id"]: [entry["device_id"]]
                                for entry in settings["identities"].values()
                            }
                        },
                    ) as response:
                        response.raise_for_status()
                        result = await response.json()
                    identities = {}
                    for name, entry in settings["identities"].items():
                        device = result["device_keys"][entry["user_id"]][
                            entry["device_id"]
                        ]
                        identities[name] = {
                            key.split(":", 1)[0]: value
                            for key, value in device["keys"].items()
                        }
                    emit("sent", command=command["id"], identities=identities)
                    continue
                if (
                    command["op"] not in {"send", "file"}
                    or command["room"] not in settings["rooms"]
                ):
                    raise ValueError("Unknown encrypted peer command or room")
                async with lock:
                    content = {"msgtype": "m.text", "body": command["text"]}
                    if command["op"] == "file":
                        data = bytes.fromhex(command["bytes_hex"])
                        upload, keys = await client.upload(
                            io.BytesIO(data),
                            content_type="application/octet-stream",
                            encrypt=True,
                            filesize=len(data),
                        )
                        checked(upload, nio.UploadResponse)
                        if not keys:
                            raise RuntimeError("SDK omitted encrypted media keys")
                        content = dict(
                            msgtype="m.file",
                            body=command["text"],
                            filename=command["filename"],
                            info={
                                "mimetype": "application/octet-stream",
                                "size": len(data),
                            },
                            file={**keys, "url": upload.content_uri},
                        )
                    response = checked(
                        await client.room_send(
                            command["room"],
                            "m.room.message",
                            content,
                            ignore_unverified_devices=True,
                        ),
                        nio.RoomSendResponse,
                    )
                    emit("sent", command=command["id"], native_id=response.event_id)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await close_client(client)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    try:
        asyncio.run(run(json.loads(sys.stdin.readline())))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        emit("error")
        raise SystemExit(1) from None
