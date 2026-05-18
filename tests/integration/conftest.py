"""Shared fixtures for Docker-based integration tests.

Manages the lifecycle of Synapse and meshtasticd containers used by
``tests/integration/`` test modules.  Every fixture is session-scoped so
containers are started once and reused across the whole integration run.

Skip strategy
-------------
All fixtures in this module check for Docker availability and the
``MEDRE_SKIP_DOCKER`` environment variable.  If either Docker is not
installed or the variable is set, **every** docker-marked test is
skipped with a clear reason string.  This keeps ``pytest`` fast and
green on machines without Docker.

Container naming
----------------
Container names are derived from a unique session prefix based on PID
to avoid collisions when multiple test sessions run concurrently.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Generator

import pytest

# ---------------------------------------------------------------------------
# Module-level gate — skip the entire package when Docker is unavailable.
# ---------------------------------------------------------------------------

_DOCKER_AVAILABLE: bool = shutil.which("docker") is not None
_SKIP_DOCKER: bool = os.environ.get("MEDRE_SKIP_DOCKER", "").strip() in (
    "1",
    "true",
    "yes",
)

pytestmark = pytest.mark.docker

if _SKIP_DOCKER or not _DOCKER_AVAILABLE:
    _SKIP_REASON = (
        "Docker not available" if not _DOCKER_AVAILABLE else "MEDRE_SKIP_DOCKER is set"
    )
    # Every test in this package gets an additional skip-if marker so that
    # they are *collected* (visible in ``-v`` output) but *skipped* unless
    # Docker is actually available and not explicitly disabled.
    pytestmark = [
        pytest.mark.docker,
        pytest.mark.skipif(
            _SKIP_DOCKER or not _DOCKER_AVAILABLE,
            reason=_SKIP_REASON,
        ),
    ]

# ---------------------------------------------------------------------------
# Defaults — configurable via environment variables.
# ---------------------------------------------------------------------------

_SYNAPSE_IMAGE = os.environ.get("MEDRE_SYNAPSE_IMAGE", "matrixdotorg/synapse:v1.149.0")
_MESHTASTICD_IMAGE = os.environ.get(
    "MEDRE_MESHTASTICD_IMAGE", "meshtastic/meshtasticd:2.7.15"
)
_SYNAPSE_PORT = int(os.environ.get("MEDRE_SYNAPSE_PORT", "8008"))
_MESHTASTICD_PORT = int(os.environ.get("MEDRE_MESHTASTICD_PORT", "4403"))
_MESHTASTICD_HWID = os.environ.get("MEDRE_MESHTASTICD_HWID", "11")
_SYNAPSE_SERVER_NAME = os.environ.get("MEDRE_SYNAPSE_SERVER_NAME", "localhost")
_READY_TIMEOUT = int(os.environ.get("MEDRE_DOCKER_READY_TIMEOUT", "120"))

_SESSION_PREFIX = f"medre-ci-{os.getpid()}"

_ARTIFACT_DIR = Path(
    os.environ.get(
        "MEDRE_CI_ARTIFACT_DIR",
        str(
            Path(__file__).resolve().parent.parent.parent
            / ".ci-artifacts"
            / "docker-integration"
        ),
    )
)

# Artifact run directory — when set, integration tests persist structured
# metadata, container logs, and config snapshots here.  Default Docker test
# behaviour is unchanged when this is unset.
_RUN_ARTIFACT_DIR: Path | None = (
    Path(p) if (p := os.environ.get("MEDRE_DOCKER_ARTIFACT_RUN_DIR", "")) else None
)

logger = logging.getLogger("medre.tests.integration")


# ---------------------------------------------------------------------------
# Artifact helpers (conditional on MEDRE_DOCKER_ARTIFACT_RUN_DIR)
# ---------------------------------------------------------------------------

# Keys whose values should be redacted in config/metadata snapshots.
_SECRET_PATTERNS = re.compile(
    r"(token|secret|password|access_token|registration_shared_secret)",
    re.IGNORECASE,
)


def _get_run_artifact_dir() -> Path | None:
    """Return the artifact run directory, creating it if needed.

    Returns ``None`` when ``MEDRE_DOCKER_ARTIFACT_RUN_DIR`` is not set so that
    all artifact writing is a silent no-op during normal test runs.
    """
    if _RUN_ARTIFACT_DIR is None:
        return None
    _RUN_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    return _RUN_ARTIFACT_DIR


def _redact_secrets(data: Any) -> Any:
    """Recursively redact values whose keys look like secrets."""
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, str) and _SECRET_PATTERNS.search(k):
                redacted[k] = "***REDACTED***"
            else:
                redacted[k] = _redact_secrets(v)
        return redacted
    if isinstance(data, list):
        return [_redact_secrets(item) for item in data]
    return data


def _capture_container_logs(container_name: str, log_name: str) -> None:
    """Capture ``docker logs`` to *log_name* inside the artifact dir."""
    artifact_dir = _get_run_artifact_dir()
    if artifact_dir is None:
        return
    try:
        result = subprocess.run(
            ["docker", "logs", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        log_path = artifact_dir / log_name
        with open(log_path, "w") as fh:
            fh.write(
                f"=== stdout ===\n{result.stdout}\n=== stderr ===\n{result.stderr}\n"
            )
        logger.info("Captured container logs: %s -> %s", container_name, log_path)
    except Exception as exc:
        logger.warning("Failed to capture logs for %s: %s", container_name, exc)


def _write_artifact_json(filename: str, data: dict[str, Any]) -> None:
    """Write a JSON file to the artifact run directory (no-op if unset)."""
    artifact_dir = _get_run_artifact_dir()
    if artifact_dir is None:
        return
    path = artifact_dir / filename
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, default=str)
    logger.info("Wrote artifact: %s", path)


def _merge_run_metadata(
    existing: dict[str, Any],
    new: dict[str, Any],
) -> dict[str, Any]:
    """Deep-merge *new* into *existing*, returning the merged dict.

    Container/image/port dicts are merged by key (new values win).
    Lists (e.g. ``events``) are concatenated with deduplication.
    Scalar values from *new* overwrite *existing*.
    """
    merged = dict(existing)
    for key, new_val in new.items():
        if key in merged:
            old_val = merged[key]
            if isinstance(old_val, dict) and isinstance(new_val, dict):
                merged[key] = _merge_run_metadata(old_val, new_val)
            elif isinstance(old_val, list) and isinstance(new_val, list):
                # Concatenate, preserving order, deduplicating by identity.
                seen: set[int] = set()
                combined: list[Any] = []
                for item in old_val:
                    hid = id(item)
                    if hid not in seen:
                        seen.add(hid)
                        combined.append(item)
                for item in new_val:
                    hid = id(item)
                    if hid not in seen:
                        seen.add(hid)
                        combined.append(item)
                merged[key] = combined
            else:
                merged[key] = new_val
        else:
            merged[key] = new_val
    return merged


def _write_run_metadata(
    *,
    scenario: str,
    containers: dict[str, str],
    storage_path: str | None = None,
    extras: dict[str, Any] | None = None,
) -> None:
    """Merge-aware write of ``run-metadata.json``.

    Reads the existing file (if any), merges the new data, and writes
    the result.  This prevents successive teardown calls from
    overwriting prior metadata.
    """
    artifact_dir = _get_run_artifact_dir()
    if artifact_dir is None:
        return

    new: dict[str, Any] = {
        "scenario": scenario,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pid": os.getpid(),
        "session_prefix": _SESSION_PREFIX,
        "containers": containers,
        "images": {
            "synapse": _SYNAPSE_IMAGE,
            "meshtasticd": _MESHTASTICD_IMAGE,
        },
        "ports": {
            "synapse": _SYNAPSE_PORT,
            "meshtasticd": _MESHTASTICD_PORT,
        },
    }
    if storage_path:
        new["storage_path"] = storage_path
    if extras:
        new.update(extras)

    meta_path = artifact_dir / "run-metadata.json"
    existing: dict[str, Any] = {}
    if meta_path.exists():
        try:
            with open(meta_path) as fh:
                existing = json.load(fh)
        except (json.JSONDecodeError, OSError):
            existing = {}

    merged = _merge_run_metadata(existing, new)
    with open(meta_path, "w") as fh:
        json.dump(merged, fh, indent=2, default=str)
    logger.info("Wrote artifact (merged): %s", meta_path)


def _write_config_snapshot(
    config_path: Path | None = None,
    raw_data: dict[str, Any] | None = None,
    filename: str = "config-snapshot.json",
) -> None:
    """Write a redacted config snapshot to the artifact dir."""
    artifact_dir = _get_run_artifact_dir()
    if artifact_dir is None:
        return
    if raw_data is not None:
        redacted = _redact_secrets(raw_data)
        _write_artifact_json(filename, redacted)
        return
    if config_path is not None and config_path.exists():
        try:
            # Try reading as key=value YAML-ish lines for homeserver.yaml.
            lines = config_path.read_text().splitlines()
            snapshot: dict[str, str] = {}
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if ":" in stripped:
                    key, _, value = stripped.partition(":")
                    key = key.strip()
                    value = value.strip()
                    if _SECRET_PATTERNS.search(key):
                        value = "***REDACTED***"
                    snapshot[key] = value
            _write_artifact_json(filename, snapshot)
        except Exception as exc:
            logger.warning(
                "Failed to read config snapshot from %s: %s", config_path, exc
            )


def _persist_storage_db(source_db_path: str, dest_filename: str) -> None:
    """Copy the SQLite DB file into the artifact dir for post-run collection."""
    artifact_dir = _get_run_artifact_dir()
    if artifact_dir is None:
        return
    src = Path(source_db_path)
    if not src.exists():
        return
    dest = artifact_dir / dest_filename
    try:
        shutil.copy2(src, dest)
        logger.info("Persisted storage DB: %s -> %s", src, dest)
    except Exception as exc:
        logger.warning("Failed to persist storage DB: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docker_is_running() -> bool:
    """Return True if the Docker daemon is responsive."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _container_exists(name: str) -> bool:
    try:
        subprocess.run(
            ["docker", "inspect", name],
            capture_output=True,
            check=True,
            timeout=5,
        )
        return True
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return False


def _container_running(name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip().lower() == "true"
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return False


def _docker_run(
    args: list[str], timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    """Run a docker CLI command, raising on failure."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def _wait_for_tcp(host: str, port: int, timeout: int = _READY_TIMEOUT) -> bool:
    """Block until a TCP connection succeeds or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


def _wait_for_http_200(url: str, timeout: int = _READY_TIMEOUT) -> bool:
    """Block until an HTTP GET returns 200 or timeout elapses.

    Uses subprocess + curl to avoid adding ``requests`` as a dependency.
    Falls back to urllib if curl is unavailable.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            import urllib.request

            req = urllib.request.Request(url)
            with urllib.request.urlopen(
                req, timeout=5
            ) as resp:  # nosec: localhost test container only
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _ensure_image(image: str) -> None:
    """Pull a Docker image if not already present locally."""
    try:
        subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=True,
            timeout=5,
        )
        logger.info("Using cached image: %s", image)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.info("Pulling image: %s", image)
        _docker_run(["pull", image], timeout=300)


# ---------------------------------------------------------------------------
# Synapse fixtures
# ---------------------------------------------------------------------------


class SynapseEnvironment:
    """Holds all connection details for a running Synapse container."""

    container_name: str
    base_url: str
    port: int
    bot_user_id: str
    bot_access_token: str
    test_room_id: str
    data_dir: Path
    test_user_id: str
    test_access_token: str

    def __init__(
        self,
        container_name: str,
        base_url: str,
        port: int,
        bot_user_id: str,
        bot_access_token: str,
        test_room_id: str,
        data_dir: Path,
        test_user_id: str = "",
        test_access_token: str = "",
    ) -> None:
        self.container_name = container_name
        self.base_url = base_url
        self.port = port
        self.bot_user_id = bot_user_id
        self.bot_access_token = bot_access_token
        self.test_room_id = test_room_id
        self.data_dir = data_dir
        self.test_user_id = test_user_id
        self.test_access_token = test_access_token


@pytest.fixture(scope="session")
def synapse_env() -> Generator[SynapseEnvironment, None, None]:
    """Start a Synapse homeserver and yield connection details.

    Creates a single bot user and a test room.  The container is stopped
    and removed on teardown.
    """
    if not _DOCKER_AVAILABLE or _SKIP_DOCKER:
        pytest.skip("Docker not available or MEDRE_SKIP_DOCKER is set")
        return  # unreachable, satisfies type checker

    if not _docker_is_running():
        pytest.skip("Docker daemon is not running")
        return

    container = f"{_SESSION_PREFIX}-synapse"
    data_dir = _ARTIFACT_DIR / "synapse"

    bot_localpart = "medre-bot"
    bot_password = "medre-bot-ci-password"
    user_localpart = "medre-test-user"
    user_password = "medre-test-user-ci-password"

    base_url = f"http://localhost:{_SYNAPSE_PORT}"

    # Cleanup any previous container with same name.
    if _container_exists(container):
        _docker_run(["rm", "-f", container], timeout=30)

    # Pull image early — needed for permission fixup below when stale
    # artifacts are owned by the Synapse container UID (991).
    _ensure_image(_SYNAPSE_IMAGE)

    # Wipe stale artifacts from a previous run.  The Synapse container
    # creates files as UID 991, so the host user may be unable to remove
    # them directly.  Fall back to a Docker-based removal when that happens.
    if data_dir.exists():
        try:
            shutil.rmtree(data_dir)
        except PermissionError:
            _docker_run(
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

    # Generate Synapse config.
    _docker_run(
        [
            "run",
            "--rm",
            "-e",
            f"SYNAPSE_SERVER_NAME={_SYNAPSE_SERVER_NAME}",
            "-e",
            "SYNAPSE_REPORT_STATS=no",
            "-v",
            f"{data_dir}:/data",
            _SYNAPSE_IMAGE,
            "generate",
        ],
        timeout=60,
    )

    # Fix permissions on generated files so the host process (which runs the
    # test suite) can read *and write* them.  The Synapse container creates
    # files as UID 991; ``chmod a+rw`` is applied via the same image running
    # as root so it works regardless of host UID.
    _docker_run(
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

    # Append CI-friendly config.
    homeserver_yaml = data_dir / "homeserver.yaml"
    if homeserver_yaml.exists():
        with open(homeserver_yaml, "a") as fh:
            fh.write("\n# CI integration overrides\n")
            fh.write("enable_registration: true\n")
            fh.write("enable_registration_without_verification: true\n")
            fh.write("registration_shared_secret: medre-ci-shared-secret\n")
            fh.write("rc_message:\n  per_second: 25\n  burst_count: 100\n")
            fh.write("rc_login:\n  account:\n    per_second: 5\n    burst_count: 30\n")

    # Start Synapse.
    _docker_run(
        [
            "run",
            "-d",
            "--name",
            container,
            "-e",
            f"SYNAPSE_SERVER_NAME={_SYNAPSE_SERVER_NAME}",
            "-e",
            "SYNAPSE_REPORT_STATS=no",
            "-p",
            f"{_SYNAPSE_PORT}:8008",
            "-v",
            f"{data_dir}:/data",
            _SYNAPSE_IMAGE,
        ],
        timeout=60,
    )

    logger.info("Waiting for Synapse to become ready on %s ...", base_url)
    if not _wait_for_http_200(
        f"{base_url}/_matrix/client/versions", timeout=_READY_TIMEOUT
    ):
        _docker_run(["rm", "-f", container], timeout=30)
        pytest.fail(f"Synapse did not become ready within {_READY_TIMEOUT}s")

    # Register bot user.
    _docker_run(
        [
            "exec",
            container,
            "register_new_matrix_user",
            "-u",
            bot_localpart,
            "-p",
            bot_password,
            "-a",
            "-c",
            "/data/homeserver.yaml",
            "http://localhost:8008",
        ],
        timeout=30,
    )

    # Register test user.
    _docker_run(
        [
            "exec",
            container,
            "register_new_matrix_user",
            "-u",
            user_localpart,
            "-p",
            user_password,
            "--no-admin",
            "-c",
            "/data/homeserver.yaml",
            "http://localhost:8008",
        ],
        timeout=30,
    )

    # Get bot access token via login API.
    import urllib.error
    import urllib.request

    login_payload = json.dumps(
        {
            "type": "m.login.password",
            "user": bot_localpart,
            "password": bot_password,
        }
    ).encode()

    req = urllib.request.Request(
        f"{base_url}/_matrix/client/v3/login",
        data=login_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(
        req, timeout=10
    ) as resp:  # nosec: localhost test container
        login_body = json.loads(resp.read())

    bot_access_token = login_body["access_token"]
    bot_user_id = login_body["user_id"]

    # Create a test room.
    room_payload = json.dumps(
        {
            "room_alias_name": "medre-ci-test",
            "name": "MEDRE CI Test Room",
            "preset": "public_chat",
        }
    ).encode()
    room_req = urllib.request.Request(
        f"{base_url}/_matrix/client/v3/createRoom",
        data=room_payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bot_access_token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(
        room_req, timeout=10
    ) as resp:  # nosec: localhost test container
        room_body = json.loads(resp.read())

    test_room_id = room_body["room_id"]

    # Get test user access token via login API (for inbound message tests).
    test_login_payload = json.dumps(
        {
            "type": "m.login.password",
            "user": user_localpart,
            "password": user_password,
        }
    ).encode()
    test_login_req = urllib.request.Request(
        f"{base_url}/_matrix/client/v3/login",
        data=test_login_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(
        test_login_req, timeout=10
    ) as resp:  # nosec: localhost test container
        test_login_body = json.loads(resp.read())

    test_user_id = test_login_body["user_id"]
    test_access_token = test_login_body["access_token"]

    # Join the test user to the bot-created room so it can send messages.
    # Without this, the test user gets 403 on /send because it has not
    # joined the room (even public rooms require an explicit join).
    join_req = urllib.request.Request(
        f"{base_url}/_matrix/client/v3/join/{test_room_id}",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {test_access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            join_req, timeout=10
        ) as resp:  # nosec: localhost test container
            json.loads(resp.read())  # consume response body
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Test user {test_user_id} failed to join room "
            f"{test_room_id}: HTTP {exc.code} {exc.read().decode()}"
        ) from exc

    env = SynapseEnvironment(
        container_name=container,
        base_url=base_url,
        port=_SYNAPSE_PORT,
        bot_user_id=bot_user_id,
        bot_access_token=bot_access_token,
        test_room_id=test_room_id,
        data_dir=data_dir,
        test_user_id=test_user_id,
        test_access_token=test_access_token,
    )

    logger.info(
        "Synapse ready: bot=%s room=%s",
        bot_user_id,
        test_room_id,
    )

    yield env

    # Teardown — capture logs and metadata before removing container when
    # MEDRE_DOCKER_ARTIFACT_RUN_DIR is set.
    if _RUN_ARTIFACT_DIR is not None:
        _capture_container_logs(container, "synapse.log")
        _write_config_snapshot(
            config_path=data_dir / "homeserver.yaml",
            filename="synapse-config-snapshot.json",
        )
        _write_run_metadata(
            scenario="synapse_run_session",
            containers={"synapse": container},
            extras={
                "bot_user_id": env.bot_user_id,
                "test_room_id": env.test_room_id,
            },
        )
    logger.info("Stopping Synapse container %s", container)
    _docker_run(["rm", "-f", container], timeout=30)


# ---------------------------------------------------------------------------
# Meshtasticd fixtures
# ---------------------------------------------------------------------------


class MeshtasticdEnvironment:
    """Holds connection details for a running meshtasticd container."""

    def __init__(
        self,
        container_name: str,
        host: str,
        port: int,
    ) -> None:
        self.container_name = container_name
        self.host = host
        self.port = port


@pytest.fixture(scope="session")
def meshtasticd_env() -> Generator[MeshtasticdEnvironment, None, None]:
    """Start a meshtasticd container and yield connection details.

    The container is stopped and removed on teardown.
    """
    if not _DOCKER_AVAILABLE or _SKIP_DOCKER:
        pytest.skip("Docker not available or MEDRE_SKIP_DOCKER is set")
        return

    if not _docker_is_running():
        pytest.skip("Docker daemon is not running")
        return

    container = f"{_SESSION_PREFIX}-meshtasticd"
    host = "localhost"

    # Cleanup any previous container.
    if _container_exists(container):
        _docker_run(["rm", "-f", container], timeout=30)

    _ensure_image(_MESHTASTICD_IMAGE)

    _docker_run(
        [
            "run",
            "-d",
            "--name",
            container,
            "--network",
            "host",
            _MESHTASTICD_IMAGE,
            "meshtasticd",
            "-s",
            "--fsdir=/var/lib/meshtasticd-medre-ci",
            "-p",
            str(_MESHTASTICD_PORT),
            "-h",
            _MESHTASTICD_HWID,
        ],
        timeout=60,
    )

    logger.info(
        "Waiting for meshtasticd on %s:%s ...",
        host,
        _MESHTASTICD_PORT,
    )
    if not _wait_for_tcp(host, _MESHTASTICD_PORT, timeout=_READY_TIMEOUT):
        _docker_run(["rm", "-f", container], timeout=30)
        pytest.fail(f"meshtasticd did not become ready within {_READY_TIMEOUT}s")

    env = MeshtasticdEnvironment(
        container_name=container,
        host=host,
        port=_MESHTASTICD_PORT,
    )
    logger.info("meshtasticd ready on %s:%s", host, _MESHTASTICD_PORT)

    yield env

    # Capture logs before teardown when artifact collection is enabled.
    if _RUN_ARTIFACT_DIR is not None:
        _capture_container_logs(container, "meshtasticd.log")
        _write_run_metadata(
            scenario="meshtasticd_sdk_bridge",
            containers={"meshtasticd": container},
            extras={
                "meshtasticd_hwid": _MESHTASTICD_HWID,
            },
        )
    logger.info("Stopping meshtasticd container %s", container)
    _docker_run(["rm", "-f", container], timeout=30)
