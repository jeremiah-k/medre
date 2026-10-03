"""Standalone Synapse-in-Docker starter for live test harnesses.

Encapsulates the config generation, registration, and readiness waiting
that ``tests/integration/conftest.py`` does, without pytest fixture
machinery, so opt-in live harnesses (radio matrix) can add a Matrix
homeserver to a multi-transport runtime.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_SYNAPSE_IMAGE = (
    "matrixdotorg/synapse:v1.162.0"
    "@sha256:6b84a7bbac36f080b2d2e51e0289cf1b08b349598ea44a558df38d558f2c2311"
)
_CONTAINER = "medre-matrix-synapse"
_PORT = 18008

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SynapseInstance:
    """Connection details for a running Synapse."""

    base_url: str
    bot_user_id: str
    bot_access_token: str
    bot_device_id: str
    test_user_id: str
    test_user_password: str
    test_access_token: str
    room_id: str
    container: str


def _docker(
    args: list[str], *, timeout: int = 60, check: bool = True
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed with {proc.returncode}: "
            f"{proc.stderr.strip()}"
        )
    return proc


def _fresh_credentials() -> tuple[str, str, str]:
    """Return per-run registration secret and account passwords.

    The values exist only for the lifetime of one container and are
    never written to the repository; the port is loopback-bound and
    the container is torn down after each run, but other processes
    on the bench host can reach the loopback, so fixed committed
    credentials would hand them an admin account on the harness
    homeserver.
    """
    return (
        secrets.token_urlsafe(24),
        secrets.token_urlsafe(16),
        secrets.token_urlsafe(16),
    )


def start_synapse(data_dir: Path) -> SynapseInstance:
    """Start a Synapse homeserver with bot and test users; return details.

    Idempotent: removes any previous container with the same name.  The
    caller owns stopping the container (``stop_synapse``).  The
    registration secret and account passwords are generated per run.
    """
    # Removing a non-existent container exits nonzero; that is expected.
    _docker(["rm", "-f", _CONTAINER], timeout=30, check=False)
    registration_secret, bot_password, peer_password = _fresh_credentials()

    if data_dir.exists():
        try:
            shutil.rmtree(data_dir)
        except PermissionError:
            _docker(
                [
                    "run",
                    "--rm",
                    "--user",
                    "root",
                    "--entrypoint",
                    "",
                    "-v",
                    f"{data_dir}:/data",
                    _SYNAPSE_IMAGE,
                    "find",
                    "/data",
                    "-mindepth",
                    "1",
                    "-delete",
                ],
                timeout=30,
            )
    data_dir.mkdir(parents=True, exist_ok=True)

    _docker(
        [
            "run",
            "--rm",
            "-e",
            "SYNAPSE_SERVER_NAME=matrix.localhost",
            "-e",
            "SYNAPSE_REPORT_STATS=no",
            "-v",
            f"{data_dir}:/data",
            _SYNAPSE_IMAGE,
            "generate",
        ],
        timeout=60,
    )
    _docker(
        [
            "run",
            "--rm",
            "--user",
            "root",
            "--entrypoint",
            "",
            "-v",
            f"{data_dir}:/data",
            _SYNAPSE_IMAGE,
            "chmod",
            "-R",
            "a+rw",
            "/data",
        ],
        timeout=30,
    )
    homeserver = data_dir / "homeserver.yaml"
    if homeserver.exists():
        # Open registration must stay off (Synapse refuses to start when
        # enable_registration has no verification path); the shared secret
        # below is what authorizes register_new_matrix_user.
        with open(homeserver, "a") as fh:
            fh.write("\n# Live harness overrides\n")
            fh.write(f"registration_shared_secret: {registration_secret}\n")
            fh.write("rc_message:\n  per_second: 25\n  burst_count: 100\n")

    _docker(
        [
            "run",
            "-d",
            "--name",
            _CONTAINER,
            "-e",
            "SYNAPSE_SERVER_NAME=matrix.localhost",
            "-e",
            "SYNAPSE_REPORT_STATS=no",
            "-p",
            f"127.0.0.1:{_PORT}:8008",
            "-v",
            f"{data_dir}:/data",
            _SYNAPSE_IMAGE,
        ],
        timeout=60,
    )

    base_url = f"http://localhost:{_PORT}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{base_url}/_matrix/client/versions", timeout=3
            ):  # nosec B310 - local Synapse test harness on a fixed loopback port
                pass
            break
        except Exception:
            time.sleep(2)
    else:
        stop_synapse(suppress_errors=True)
        raise RuntimeError("Synapse did not become ready within 60s")

    # Registration requires the admin flag to be stated explicitly on
    # non-admin accounts too: without --no-admin the pinned script prompts
    # on stdin, which docker exec never provides.
    def _register(localpart: str, password: str, *, admin: bool) -> None:
        _docker(
            [
                "exec",
                _CONTAINER,
                "register_new_matrix_user",
                "-u",
                localpart,
                "-p",
                password,
                "-c",
                "/data/homeserver.yaml",
                "-a" if admin else "--no-admin",
            ],
            timeout=30,
        )

    def _login(user: str, password: str) -> dict:
        req = urllib.request.Request(
            f"{base_url}/_matrix/client/v3/login",
            data=json.dumps(
                {"type": "m.login.password", "user": user, "password": password}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(
            req, timeout=10
        ) as resp:  # nosec B310 - local Synapse test harness on a fixed loopback port
            return json.loads(resp.read())

    def _set_display_name(session: dict, name: str) -> None:
        # Display names are what cross-transport attribution renders for
        # Matrix-originated messages; harnesses assert against them.
        req = urllib.request.Request(
            f"{base_url}/_matrix/client/v3/profile/"
            f"{session['user_id']}/displayname",
            data=json.dumps({"displayname": name}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {session['access_token']}",
            },
            method="PUT",
        )
        urllib.request.urlopen(
            req, timeout=10
        )  # nosec B310 - local Synapse test harness on a fixed loopback port

    try:
        _register("medre-bot", bot_password, admin=True)
        _register("medre-peer", peer_password, admin=False)
        bot = _login("medre-bot", bot_password)
        peer = _login("medre-peer", peer_password)
        _set_display_name(bot, "MEDRE-MX-BRIDGE")
        _set_display_name(peer, "MEDRE-MX-PEER")

        room_req = urllib.request.Request(
            f"{base_url}/_matrix/client/v3/createRoom",
            data=json.dumps(
                {"name": "MEDRE matrix harness", "preset": "public_chat"}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {bot['access_token']}",
            },
            method="POST",
        )
        with urllib.request.urlopen(
            room_req, timeout=10
        ) as resp:  # nosec B310 - local Synapse test harness on a fixed loopback port
            room_id = json.loads(resp.read())["room_id"]

        # Invite and join the peer user so both sides can observe the room.
        invite_req = urllib.request.Request(
            f"{base_url}/_matrix/client/v3/rooms/{room_id}/invite",
            data=json.dumps({"user_id": peer["user_id"]}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {bot['access_token']}",
            },
            method="POST",
        )
        urllib.request.urlopen(
            invite_req, timeout=10
        )  # nosec B310 - local Synapse test harness on a fixed loopback port
        join_req = urllib.request.Request(
            f"{base_url}/_matrix/client/v3/rooms/{room_id}/join",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {peer['access_token']}",
            },
            method="POST",
        )
        urllib.request.urlopen(
            join_req, timeout=10
        )  # nosec B310 - local Synapse test harness on a fixed loopback port
    except Exception:
        # Provisioning failed after the container started; do not leave
        # the named container running with no instance handle to stop.
        # Cleanup failures are suppressed so the provisioning error is
        # the one that surfaces.
        stop_synapse(suppress_errors=True)
        raise

    return SynapseInstance(
        base_url=base_url,
        bot_user_id=bot["user_id"],
        bot_access_token=bot["access_token"],
        bot_device_id=bot.get("device_id", ""),
        test_user_id=peer["user_id"],
        test_user_password=peer_password,
        test_access_token=peer["access_token"],
        room_id=room_id,
        container=_CONTAINER,
    )


def stop_synapse(*, suppress_errors: bool = False) -> None:
    """Remove the named Synapse container.

    On cleanup paths (*suppress_errors*) a docker failure — nonzero
    result, timeout, or launch error — is logged instead of raised, so
    it cannot replace the failure that triggered the cleanup.
    """
    try:
        _docker(["rm", "-f", _CONTAINER], timeout=30)
    except Exception:
        if not suppress_errors:
            raise
        _LOGGER.exception("Failed to remove Synapse container %s", _CONTAINER)
