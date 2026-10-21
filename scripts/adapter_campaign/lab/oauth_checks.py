"""Native OAuth and signature checks independent of application orchestration."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path


def claim_initial_signing_reset(profile: Path, marker: Path) -> bool:
    """Authorize at most one reset in a fresh disposable lab identity.

    The marker is written *before* the potentially destructive request, so a
    crash or timeout cannot inadvertently authorize the reset again.
    """
    if marker.exists() or any(
        "signing" in file.name.lower()
        or file.suffix in {".db", ".sqlite", ".sqlite3", ".pickle"}
        for file in profile.rglob("*")
        if file.is_file()
    ):
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=False)
    return True


async def published_identity(owner: str, path: Path) -> dict:
    record = json.loads(path.read_text())
    if owner == "medre":
        from medre.adapters.matrix.oauth import OAuthSession
        from medre.adapters.matrix.oauth_session import OAuthSessionManager

        manager = OAuthSessionManager(OAuthSession.parse(record), path)
    else:
        from mmrelay.matrix.oauth import OAuthSession
        from mmrelay.matrix.oauth_session import OAuthSessionManager
        from mmrelay.matrix.oauth_store import OAuthStore

        store = OAuthStore(path)
        manager = OAuthSessionManager(OAuthSession.parse(await store.load()), store)
    access_token = await manager.access_token()
    import aiohttp
    from nio import Api
    from vodozemac import Ed25519PublicKey, Ed25519Signature

    async with aiohttp.ClientSession(
        headers={"Authorization": "Bearer " + access_token}
    ) as http:
        async with http.post(
            record["homeserver"] + "/_matrix/client/v3/keys/query",
            json={"device_keys": {record["user_id"]: [record["device_id"]]}},
        ) as response:
            response.raise_for_status()
            keys = await response.json()
    user = record["user_id"]
    master = keys["master_keys"][user]
    signing = keys["self_signing_keys"][user]
    device = keys["device_keys"][user][record["device_id"]]

    def verify(parent, child):
        key_id, public = next(iter(parent["keys"].items()))
        signature = child["signatures"][user][key_id]
        message = {
            key: value
            for key, value in child.items()
            if key not in {"signatures", "unsigned"}
        }
        Ed25519PublicKey.from_base64(public).verify_signature(
            Api.to_canonical_json(message).encode("utf-8"),
            Ed25519Signature.from_base64(signature),
        )

    assert master["user_id"] == signing["user_id"] == device["user_id"] == user
    assert master["usage"] == ["master"] and signing["usage"] == ["self_signing"]
    assert device["device_id"] == record["device_id"]
    verify(master, signing)
    verify(signing, device)
    return master["keys"]


async def concurrent_refresh(owner: str, path: Path) -> None:
    before = json.loads(path.read_text())
    before["oauth"]["refresh_at"] = min(
        time.time() - 1, before["oauth"]["expires_at"] - 1
    )
    temporary = path.with_suffix(".lab-refresh")
    temporary.write_text(json.dumps(before))
    temporary.chmod(0o600)
    temporary.replace(path)
    workers = []
    try:
        for _ in range(2):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("token_probe.py")),
                owner,
                str(path),
                stdin=-1,
                stdout=-1,
                stderr=-1,
            )
            workers.append(process)
        async with asyncio.timeout(90):
            for process in workers:
                assert await process.stdout.readline() == b"READY\n"
            for process in workers:
                process.stdin.write(b"rotate\n")
                await process.stdin.drain()
            outcomes = []
            for process in workers:
                stdout, stderr = await process.communicate()
                if process.returncode:
                    Path("/lab/logs", owner + "-refresh-error.log").write_bytes(stderr)
                    raise RuntimeError("Native concurrent refresh failed")
                outcomes.append(json.loads(stdout))
        after = json.loads(path.read_text())
        assert outcomes[0] == outcomes[1], "Refresh contenders did not converge"
        assert before["oauth"]["refresh_token"] != after["oauth"]["refresh_token"]
        assert after["device_id"] == before["device_id"]
    finally:
        for process in workers:
            if process.returncode is None:
                process.kill()
                await process.wait()
            error = await process.stderr.read()
            if error:
                with Path("/lab/logs", owner + "-refresh-error.log").open("ab") as log:
                    log.write(error)
