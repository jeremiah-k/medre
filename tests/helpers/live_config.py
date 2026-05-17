"""Build RuntimeConfig or temp TOML from env vars for live bridge tests.

This helper module centralises the environment-variable gates and config
construction shared by the live Matrix↔Meshtastic bridge smoke tests.
It uses the **same** env var names as ``test_matrix_live.py`` and
``test_meshtastic_live.py`` so operators can reuse a single environment
for all live test suites.

Environment variables
---------------------

**Matrix (all required for Matrix tests):**

``MATRIX_HOMESERVER``
    Full URL of the Matrix homeserver.
``MATRIX_USER_ID``
    Fully-qualified Matrix user ID (e.g. ``@bot:localhost``).
``MATRIX_ACCESS_TOKEN``
    Access token for the bot account.
``MATRIX_ROOM_ID``
    Room ID to send test messages to.

**Meshtastic (connection-type dependent):**

``MESHTASTIC_CONNECTION_TYPE``
    Connection mode: ``tcp``, ``serial``, or ``ble``.
``MESHTASTIC_HOST``
    Hostname or IP for TCP connections.
``MESHTASTIC_PORT``
    Port for TCP (default ``4403``).
``MESHTASTIC_SERIAL_PORT``
    Serial device path for serial connections.
``MESHTASTIC_BLE_ADDRESS``
    BLE MAC address for BLE connections.
``MESHTASTIC_CHANNEL_INDEX``
    Channel index for outbound test messages (default ``0``).
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "all_live_env_set",
    "matrix_env_set",
    "meshtastic_env_set",
    "build_live_bridge_runtime_config",
    "write_live_bridge_toml",
]


# ---------------------------------------------------------------------------
# Env var accessors
# ---------------------------------------------------------------------------

def _get_matrix_env() -> tuple[str, str, str, str]:
    """Return (homeserver, user_id, access_token, room_id) from env."""
    return (
        os.environ.get("MATRIX_HOMESERVER", ""),
        os.environ.get("MATRIX_USER_ID", ""),
        os.environ.get("MATRIX_ACCESS_TOKEN", ""),
        os.environ.get("MATRIX_ROOM_ID", ""),
    )


def _get_meshtastic_env() -> tuple[str, str, str, str, str, str]:
    """Return (connection_type, host, port, serial_port, ble_address, channel_index)."""
    return (
        os.environ.get("MESHTASTIC_CONNECTION_TYPE", "").lower(),
        os.environ.get("MESHTASTIC_HOST", ""),
        os.environ.get("MESHTASTIC_PORT", "4403"),
        os.environ.get("MESHTASTIC_SERIAL_PORT", ""),
        os.environ.get("MESHTASTIC_BLE_ADDRESS", ""),
        os.environ.get("MESHTASTIC_CHANNEL_INDEX", "0"),
    )


# ---------------------------------------------------------------------------
# Env gate predicates
# ---------------------------------------------------------------------------

def all_live_env_set() -> bool:
    """Return ``True`` only when **all** required Matrix and Meshtastic env vars are set."""
    return matrix_env_set() and meshtastic_env_set()


def matrix_env_set() -> bool:
    """Return ``True`` when all ``MATRIX_*`` vars are populated."""
    homeserver, user_id, access_token, room_id = _get_matrix_env()
    return bool(homeserver and user_id and access_token and room_id)


def meshtastic_env_set() -> bool:
    """Return ``True`` when ``MESHTASTIC_CONNECTION_TYPE`` and type-specific vars are set."""
    ct, host, _port, serial_port, ble_address, _ch = _get_meshtastic_env()
    if not ct:
        return False
    if ct == "tcp":
        return bool(host)
    if ct == "serial":
        return bool(serial_port)
    if ct == "ble":
        return bool(ble_address)
    return False


# ---------------------------------------------------------------------------
# RuntimeConfig builder
# ---------------------------------------------------------------------------

def build_live_bridge_runtime_config(tmp_path: Path) -> "RuntimeConfig":
    """Construct a :class:`RuntimeConfig` from live environment variables.

    Parameters
    ----------
    tmp_path:
        Temporary directory for the SQLite database.

    Returns
    -------
    RuntimeConfig
        Fully-populated config with a real Matrix adapter (``matrix``),
        a real Meshtastic adapter (``radio``), and two bridge routes.

    Raises
    ------
    RuntimeError
        If required environment variables are missing.
    """
    from medre.adapters.matrix.config import MatrixConfig
    from medre.adapters.meshtastic.config import MeshtasticConfig
    from medre.config.model import (
        AdapterConfigSet,
        LoggingConfig,
        MatrixRuntimeConfig,
        MeshtasticRuntimeConfig,
        RuntimeConfig,
        RuntimeOptions,
        StorageConfig,
    )
    from medre.runtime.routes import RouteConfig, RouteConfigSet, RouteDirectionality

    # --- Matrix env vars ---
    homeserver, user_id, access_token, room_id = _get_matrix_env()
    if not (homeserver and user_id and access_token):
        raise RuntimeError(
            "Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, "
            "and MATRIX_ROOM_ID env vars to build live bridge config"
        )

    # --- Meshtastic env vars ---
    ct, host, port, serial_port, ble_address, channel_index = _get_meshtastic_env()
    if not ct:
        raise RuntimeError(
            "Set MESHTASTIC_CONNECTION_TYPE (tcp/serial/ble) to build "
            "live bridge config"
        )

    # Build Meshtastic config based on connection type.
    meshtastic_kwargs: dict = {
        "adapter_id": "radio",
    }
    if ct == "tcp":
        if not host:
            raise RuntimeError(
                "MESHTASTIC_HOST is required for TCP connection type"
            )
        meshtastic_kwargs["connection_type"] = "tcp"
        meshtastic_kwargs["host"] = host
        meshtastic_kwargs["port"] = int(port) if port else 4403
    elif ct == "serial":
        if not serial_port:
            raise RuntimeError(
                "MESHTASTIC_SERIAL_PORT is required for serial connection type"
            )
        meshtastic_kwargs["connection_type"] = "serial"
        meshtastic_kwargs["serial_port"] = serial_port
    elif ct == "ble":
        if not ble_address:
            raise RuntimeError(
                "MESHTASTIC_BLE_ADDRESS is required for BLE connection type"
            )
        meshtastic_kwargs["connection_type"] = "ble"
        meshtastic_kwargs["ble_address"] = ble_address
    else:
        raise RuntimeError(
            f"Unknown MESHTASTIC_CONNECTION_TYPE {ct!r}; "
            "use tcp, serial, or ble"
        )

    matrix_config = MatrixConfig(
        adapter_id="matrix",
        homeserver=homeserver,
        user_id=user_id,
        access_token=access_token,
        room_allowlist={room_id},
    )
    meshtastic_config = MeshtasticConfig(**meshtastic_kwargs)

    # --- Routes ---
    route_matrix_to_radio = RouteConfig(
        route_id="matrix_to_radio",
        source_adapters=("matrix",),
        dest_adapters=("radio",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        source_room=room_id,
        dest_channel=channel_index,
    )
    route_radio_to_matrix = RouteConfig(
        route_id="radio_to_matrix",
        source_adapters=("radio",),
        dest_adapters=("matrix",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        source_channel=channel_index,
        dest_room=room_id,
    )

    return RuntimeConfig(
        runtime=RuntimeOptions(name="live-bridge-test"),
        logging=LoggingConfig(level="DEBUG", format="text"),
        storage=StorageConfig(backend="sqlite", path=str(tmp_path / "test.sqlite")),
        adapters=AdapterConfigSet(
            matrix={
                "matrix": MatrixRuntimeConfig(
                    adapter_id="matrix",
                    enabled=True,
                    adapter_kind="real",
                    config=matrix_config,
                ),
            },
            meshtastic={
                "radio": MeshtasticRuntimeConfig(
                    adapter_id="radio",
                    enabled=True,
                    adapter_kind="real",
                    config=meshtastic_config,
                ),
            },
        ),
        routes=RouteConfigSet(
            routes=(route_matrix_to_radio, route_radio_to_matrix),
        ),
    )


# ---------------------------------------------------------------------------
# TOML writer
# ---------------------------------------------------------------------------

def _escape_toml_string(value: str) -> str:
    """Escape *value* for safe embedding in a TOML double-quoted string.

    Handles backslashes, double quotes, newlines, tabs, and other control
    characters.  No external dependencies required.
    """
    out: list[str] = []
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def write_live_bridge_toml(tmp_path: Path) -> Path:
    """Write a TOML config file from live environment variables.

    Uses string formatting (f-string template), not ``tomli_w``.

    Parameters
    ----------
    tmp_path:
        Directory in which to write the TOML file.

    Returns
    -------
    Path
        Path to the written TOML file.

    Raises
    ------
    RuntimeError
        If required environment variables are missing.
    """
    homeserver, user_id, access_token, room_id = _get_matrix_env()
    ct, host, port, serial_port, ble_address, channel_index = _get_meshtastic_env()

    if not (homeserver and user_id and access_token):
        raise RuntimeError(
            "Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, "
            "and MATRIX_ROOM_ID env vars to write live bridge TOML"
        )
    if not ct:
        raise RuntimeError(
            "Set MESHTASTIC_CONNECTION_TYPE (tcp/serial/ble) to write "
            "live bridge TOML"
        )

    # Build meshtastic connection fields based on type.
    if ct == "tcp":
        meshtastic_connection_block = (
            f'connection_type = "tcp"\n'
            f'host = "{_escape_toml_string(host)}"\n'
            f'tcp_port = {int(port) if port else 4403}'
        )
    elif ct == "serial":
        meshtastic_connection_block = (
            f'connection_type = "serial"\n'
            f'serial_port = "{_escape_toml_string(serial_port)}"'
        )
    elif ct == "ble":
        meshtastic_connection_block = (
            f'connection_type = "ble"\n'
            f'ble_address = "{_escape_toml_string(ble_address)}"'
        )
    else:
        meshtastic_connection_block = (
            f'connection_type = "{_escape_toml_string(ct)}"'
        )

    toml_content = f"""\
[runtime]
name = "live-bridge-test"

[logging]
level = "DEBUG"
format = "text"

[storage]
backend = "sqlite"
path = "{_escape_toml_string(str(tmp_path / "test.sqlite"))}"

[adapters.matrix.matrix]
adapter_kind = "real"
enabled = true
homeserver = "{_escape_toml_string(homeserver)}"
user_id = "{_escape_toml_string(user_id)}"
access_token = "{_escape_toml_string(access_token)}"
room_allowlist = ["{_escape_toml_string(room_id)}"]

[adapters.meshtastic.radio]
adapter_kind = "real"
enabled = true
{meshtastic_connection_block}

[routes.matrix_to_radio]
source_adapters = ["matrix"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
source_room = "{_escape_toml_string(room_id)}"
dest_channel = "{_escape_toml_string(channel_index)}"
enabled = true

[routes.radio_to_matrix]
source_adapters = ["radio"]
dest_adapters = ["matrix"]
directionality = "source_to_dest"
source_channel = "{_escape_toml_string(channel_index)}"
dest_room = "{_escape_toml_string(room_id)}"
enabled = true
"""

    toml_path = tmp_path / "live-bridge-test.toml"
    toml_path.write_text(toml_content, encoding="utf-8")
    return toml_path
