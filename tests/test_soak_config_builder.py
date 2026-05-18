"""Unit tests for soak config builder infrastructure.

These tests verify the **actual** functions in ``tests.helpers.soak`` —
``_validate_meshtastic_soak_env`` and ``_make_meshtastic_config`` —
by patching ``os.environ`` and the module-level
``_MESHTASTIC_CONNECTION_TYPE`` attribute so no real hardware or env
is needed.  They run in the default pytest suite (no ``live`` mark).

Import strategy
1. ``tests.helpers.soak`` sets ``_MESHTASTIC_CONNECTION_TYPE`` and
   module-level state at import time from ``os.environ``.  We import the
   module once and then, per test, monkeypatch both ``os.environ``
   *and* the module attribute so the functions under test see consistent
   values without ``importlib.reload``.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import tests.helpers.soak as _soak_mod

# Convenience aliases to the *real* functions under test.
_validate = _soak_mod._validate_meshtastic_soak_env
_make_config = _soak_mod._make_meshtastic_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_env(ct: str, **extra: str) -> dict[str, str]:
    """Build a minimal env dict with the given connection type + extras."""
    env: dict[str, str] = {"MESHTASTIC_CONNECTION_TYPE": ct}
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Env validation tests
# ---------------------------------------------------------------------------


class TestSoakEnvValidation:
    """Verify that soak env gating accepts/rejects correctly.

    Each test patches ``os.environ`` **and** the module-level
    ``_MESHTASTIC_CONNECTION_TYPE`` so ``_validate_meshtastic_soak_env``
    operates on controlled values.
    """

    def test_no_connection_type_rejected(self) -> None:
        env = {"MESHTASTIC_CONNECTION_TYPE": ""}
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", ""
        ):
            ok, reason = _validate()
        assert not ok
        assert "MESHTASTIC_CONNECTION_TYPE" in reason

    def test_tcp_with_host_accepted(self) -> None:
        env = _patch_env("tcp", MESHTASTIC_HOST="meshtastic.local")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "tcp"
        ):
            ok, _ = _validate()
        assert ok

    def test_tcp_without_host_rejected(self) -> None:
        env = _patch_env("tcp")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "tcp"
        ):
            ok, reason = _validate()
        assert not ok
        assert "MESHTASTIC_HOST" in reason

    def test_serial_with_port_accepted(self) -> None:
        env = _patch_env("serial", MESHTASTIC_SERIAL_PORT="/dev/ttyUSB0")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "serial"
        ):
            ok, _ = _validate()
        assert ok

    def test_serial_without_port_rejected(self) -> None:
        env = _patch_env("serial")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "serial"
        ):
            ok, reason = _validate()
        assert not ok
        assert "MESHTASTIC_SERIAL_PORT" in reason

    def test_ble_with_address_accepted(self) -> None:
        env = _patch_env("ble", MESHTASTIC_BLE_ADDRESS="AA:BB:CC:DD:EE:FF")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "ble"
        ):
            ok, _ = _validate()
        assert ok

    def test_ble_without_address_rejected(self) -> None:
        env = _patch_env("ble")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "ble"
        ):
            ok, reason = _validate()
        assert not ok
        assert "MESHTASTIC_BLE_ADDRESS" in reason

    def test_unknown_type_rejected(self) -> None:
        env = _patch_env("mqtt")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "mqtt"
        ):
            ok, reason = _validate()
        assert not ok
        assert "Unknown" in reason

    def test_serial_does_not_require_host(self) -> None:
        """Serial soak must NOT require MESHTASTIC_HOST."""
        env = _patch_env("serial", MESHTASTIC_SERIAL_PORT="/dev/ttyACM0")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "serial"
        ):
            ok, _ = _validate()
        assert ok

    def test_tcp_does_not_require_serial_port(self) -> None:
        """TCP soak must NOT require MESHTASTIC_SERIAL_PORT."""
        env = _patch_env("tcp", MESHTASTIC_HOST="192.168.1.100")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "tcp"
        ):
            ok, _ = _validate()
        assert ok


# ---------------------------------------------------------------------------
# Config builder tests
# ---------------------------------------------------------------------------


class TestSoakConfigBuilder:
    """Verify that ``_make_meshtastic_config`` produces valid configs.

    The function reads the module-level ``_MESHTASTIC_CONNECTION_TYPE``
    *and* ``os.environ`` for transport-specific vars, so we patch both.
    """

    def test_tcp_config(self) -> None:
        env = _patch_env(
            "tcp", MESHTASTIC_HOST="meshtastic.local", MESHTASTIC_PORT="4403"
        )
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "tcp"
        ):
            config = _make_config()
        assert config.connection_type == "tcp"
        assert config.host == "meshtastic.local"
        assert config.port == 4403
        assert config.serial_port is None
        config.validate()

    def test_tcp_default_port(self) -> None:
        env = _patch_env("tcp", MESHTASTIC_HOST="meshtastic.local")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "tcp"
        ):
            config = _make_config()
        assert config.port == 4403

    def test_serial_config(self) -> None:
        env = _patch_env("serial", MESHTASTIC_SERIAL_PORT="/dev/ttyUSB0")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "serial"
        ):
            config = _make_config()
        assert config.connection_type == "serial"
        assert config.serial_port == "/dev/ttyUSB0"
        assert config.host is None
        config.validate()

    def test_serial_config_custom_channel(self) -> None:
        env = _patch_env(
            "serial",
            MESHTASTIC_SERIAL_PORT="/dev/ttyACM0",
            MESHTASTIC_CHANNEL_INDEX="2",
        )
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "serial"
        ):
            config = _make_config()
        assert config.default_channel == 2

    def test_ble_config(self) -> None:
        env = _patch_env("ble", MESHTASTIC_BLE_ADDRESS="AA:BB:CC:DD:EE:FF")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "ble"
        ):
            config = _make_config()
        assert config.connection_type == "ble"
        assert config.ble_address == "AA:BB:CC:DD:EE:FF"
        config.validate()

    def test_adapter_id_is_soak(self) -> None:
        """All soak configs must use the 'meshtastic-soak' adapter_id."""
        cases: list[tuple[str, dict[str, str]]] = [
            ("tcp", _patch_env("tcp", MESHTASTIC_HOST="h")),
            ("serial", _patch_env("serial", MESHTASTIC_SERIAL_PORT="/dev/ttyUSB0")),
            ("ble", _patch_env("ble", MESHTASTIC_BLE_ADDRESS="AA:BB:CC:DD:EE:FF")),
        ]
        for ct, env in cases:
            with patch.dict(os.environ, env, clear=True), patch.object(
                _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", ct
            ):
                config = _make_config()
            assert config.adapter_id == "meshtastic-soak", f"failed for {ct}"


# ---------------------------------------------------------------------------
# Parity with live smoke config builder
# ---------------------------------------------------------------------------


class TestSoakLiveSmokeParity:
    """Verify soak config builder parity with live smoke test patterns.

    The soak config builder must produce configs that are structurally
    equivalent to the live smoke test builder for the same env vars.
    Key difference: adapter_id is "meshtastic-soak" vs "meshtastic-live-smoke".
    """

    def test_serial_config_parity(self) -> None:
        """Serial soak config must set the same fields as live smoke serial."""
        env = _patch_env(
            "serial",
            MESHTASTIC_SERIAL_PORT="/dev/ttyUSB0",
            MESHTASTIC_CHANNEL_INDEX="1",
        )
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "serial"
        ):
            config = _make_config()
        assert config.connection_type == "serial"
        assert config.serial_port == "/dev/ttyUSB0"
        assert config.default_channel == 1
        assert config.host is None
        assert config.port is None

    def test_tcp_config_parity(self) -> None:
        """TCP soak config must set the same fields as live smoke TCP."""
        env = _patch_env(
            "tcp",
            MESHTASTIC_HOST="192.168.1.100",
            MESHTASTIC_PORT="4403",
        )
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "tcp"
        ):
            config = _make_config()
        assert config.connection_type == "tcp"
        assert config.host == "192.168.1.100"
        assert config.port == 4403
        assert config.serial_port is None

    def test_ble_config_parity(self) -> None:
        """BLE soak config must set the same fields as live smoke BLE."""
        env = _patch_env("ble", MESHTASTIC_BLE_ADDRESS="AA:BB:CC:DD:EE:FF")
        with patch.dict(os.environ, env, clear=True), patch.object(
            _soak_mod, "_MESHTASTIC_CONNECTION_TYPE", "ble"
        ):
            config = _make_config()
        assert config.connection_type == "ble"
        assert config.ble_address == "AA:BB:CC:DD:EE:FF"
        assert config.host is None
        assert config.serial_port is None
