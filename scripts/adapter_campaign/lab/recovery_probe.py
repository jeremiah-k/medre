"""Native logout, revoked-refresh, and login with retained encryption identity."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from browser import ApprovalBrowser
from oauth_checks import concurrent_refresh, published_identity
from oauth_probe import cli

ROOT = Path("/lab")


async def main(owner: str) -> None:
    import aiohttp

    profile = ROOT / "profiles" / owner
    candidates = list(profile.rglob("credentials.json")) + list(
        profile.rglob("matrix.json")
    )
    path = next(
        path
        for path in candidates
        if json.loads(path.read_text()).get("auth_type") == "oauth"
    )
    await concurrent_refresh(owner, path)
    before = json.loads(path.read_text())
    master = await published_identity(before)
    signing = {
        str(file.relative_to(profile)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in profile.rglob("*.json")
        if "signing" in file.name
    }
    assert signing, "No persistent signing identity was found"
    databases = [file for file in profile.rglob("*.db")]
    assert databases, "No persistent encryption store was found"
    executable = (
        "/opt/mmrelay/bin/mmrelay" if owner == "mmrelay" else "/opt/medre/bin/medre"
    )
    argv = [
        executable,
        *(["adapter", "matrix"] if owner == "medre" else []),
        "auth",
        "logout",
    ]
    if owner == "mmrelay":
        argv.append("--yes")
    env = dict(
        os.environ,
        XDG_CONFIG_HOME=str(profile / "config"),
        XDG_STATE_HOME=str(profile / "state"),
        XDG_DATA_HOME=str(profile / "data"),
        MMRELAY_HOME=str(profile),
        PYTHONUNBUFFERED="1",
    )
    log = (ROOT / "logs" / (owner + "-native-logout.log")).open("wb")
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(60):
            assert await process.wait() == 0, "Native logout failed"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        log.close()
    assert not path.exists(), "Logout retained authentication credentials"
    assert all(file.is_file() for file in databases), "Logout removed the crypto store"
    for name, digest in signing.items():
        assert hashlib.sha256((profile / name).read_bytes()).hexdigest() == digest
    # Verify an actual invalid_grant, not a timeout or an access token's natural
    # expiry (short-lived Matrix access tokens can remain briefly cached).
    async with aiohttp.ClientSession() as http:
        async with http.post(
            before["oauth"]["token_endpoint"],
            data={
                "grant_type": "refresh_token",
                "client_id": before["oauth"]["client_id"],
                "refresh_token": before["oauth"]["refresh_token"],
            },
        ) as response:
            assert response.status in {400, 401}, "Revoked session could still refresh"
            assert (await response.json())["error"] == "invalid_grant"
    accounts = json.loads((ROOT / "config/accounts.json").read_text())
    async with ApprovalBrowser(owner, accounts["password"]) as browser:
        await cli(owner, browser)  # Deliberately no reset on either application.
    after = json.loads(path.read_text())
    assert (
        after["device_id"] != before["device_id"]
    ), "Fresh OAuth login reused a revoked device"
    assert (
        await published_identity(after) == master
    ), "Relogin replaced the account identity"
    result = {
        "status": "passed",
        "owner": owner,
        "checks": [
            "native_logout_removes_credentials",
            "logout_preserves_crypto_store_and_signing_keys",
            "revoked_refresh_returns_invalid_grant",
            "new_device_signs_without_reset",
            "account_master_key_survives_relogin",
        ],
    }
    (ROOT / "results" / (owner + "-recovery.json")).write_text(
        json.dumps(result, indent=2) + "\n"
    )


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Qualification requires enabled assertions")
    asyncio.run(main(sys.argv[1]))
