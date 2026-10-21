"""Run each application's real OAuth CLI and browser in its own SDK environment.

Executed as a script by either environment; imports deliberately stay independent
of MEDRE so MMRelay cannot inherit MEDRE's SDK pins through a shared interpreter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from browser import ApprovalBrowser
from oauth_checks import concurrent_refresh, published_identity

ROOT = Path("/lab")


async def cli(owner: str, browser: ApprovalBrowser, *extra: str) -> str:
    profile = ROOT / "profiles" / owner
    profile.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        XDG_CONFIG_HOME=str(profile / "config"),
        XDG_STATE_HOME=str(profile / "state"),
        XDG_DATA_HOME=str(profile / "data"),
        MMRELAY_HOME=str(profile),
        PYTHONUNBUFFERED="1",
    )
    executable = (
        "/opt/mmrelay/bin/mmrelay" if owner == "mmrelay" else "/opt/medre/bin/medre"
    )
    command = [
        executable,
        *(["adapter", "matrix"] if owner == "medre" else []),
        "auth",
        "login",
        "--oauth",
        "--homeserver",
        "https://matrix.lab.test",
        "--user" if owner == "medre" else "--username",
        f"@{owner}:matrix.lab.test",
    ]
    if owner == "medre":
        command += ["--adapter-id", "matrix"]
    command += extra
    process = await asyncio.create_subprocess_exec(
        *command,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    transcript = []
    url = None
    try:
        async with asyncio.timeout(360):
            while line := await process.stdout.readline():
                text = line.decode(errors="replace").strip()
                transcript.append(text)
                if text.startswith("https://auth.lab.test/"):
                    url = text
                    # UIA paths carry an operation-specific token; device-login
                    # paths require the separate code printed on the next line.
                    if any(
                        word in text for word in ("reauth", "cross-signing", "action")
                    ):
                        await browser.approve(url)
                        url = None
                if text.startswith("Enter code:") and url:
                    await browser.approve(url, text.split(":", 1)[1].strip())
                    url = None
            status = await process.wait()
        if status:
            raise RuntimeError(f"{owner} OAuth login exited {status}")
        return "\n".join(transcript)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        (
            ROOT
            / "logs"
            / f"{owner}-oauth-cli-{len(list((ROOT / 'logs').glob(owner + '-oauth-cli-*')))}.log"
        ).write_text("\n".join(transcript))


async def main(owner: str) -> None:
    settings = json.loads((ROOT / "config/accounts.json").read_text())
    profile = ROOT / "profiles" / owner
    profile.mkdir(parents=True, exist_ok=True)
    if owner == "mmrelay":
        import yaml

        (profile / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "matrix": {
                        "homeserver": settings["homeserver"],
                        "bot_user_id": "@mmrelay:matrix.lab.test",
                        "e2ee": {"enabled": True},
                    },
                    "meshtastic": {"connection_type": "serial"},
                    "matrix_rooms": [],
                }
            )
        )
    async with ApprovalBrowser(owner, settings["password"]) as browser:
        # Explicit reset is authorized only for a fresh disposable identity.
        # Repeating a stage must preserve the keys established by earlier runs.
        existing = list(profile.rglob("credentials.json"))
        reset = owner == "mmrelay" and not existing
        first = await cli(owner, browser, *(["--reset-cross-signing"] if reset else []))
        credentials = list(profile.rglob("credentials.json")) + list(
            profile.rglob("matrix.json")
        )
        records = [(path, json.loads(path.read_text())) for path in credentials]
        path, before = next(
            (path, row) for path, row in records if row.get("auth_type") == "oauth"
        )
        master_before = await published_identity(before)
        repeated = await cli(owner, browser)
        after = json.loads(path.read_text())
        assert (
            after["device_id"] == before["device_id"]
        ), "Routine login allocated another device"
        assert after["user_id"] == before["user_id"]
        assert (
            "verified" in (first + repeated).lower()
            or "cross-sign" in (first + repeated).lower()
        )
        master_after = await published_identity(after)
        assert (
            master_before == master_after
        ), "Routine login replaced the account master key"
        await concurrent_refresh(owner, path)
        result = {
            "owner": owner,
            "status": "passed",
            "device_id": after["device_id"],
            "user_id": after["user_id"],
            "master_key_digest": hashlib.sha256(
                json.dumps(master_after, sort_keys=True).encode()
            ).hexdigest(),
            "checks": [
                "native_oauth_login_or_resume",
                "native_sdk_device_signing",
                "routine_login_reuses_device_and_master_key",
                "cryptographic_server_signature_chain",
                "cross_process_refresh_with_forced_local_deadline",
            ],
        }
        (ROOT / "results" / f"{owner}-oauth.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Qualification requires enabled assertions")
    asyncio.run(main(sys.argv[1]))
