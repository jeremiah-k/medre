"""Sample configuration generator for MEDRE.

Provides :func:`generate_sample_config` which returns a complete, documented
TOML configuration string.  This is the output shown by ``medre config sample``.
"""
from __future__ import annotations


def generate_sample_config() -> str:
    """Return a complete TOML sample config with all sections documented."""

    return """\
# MEDRE Configuration — TOML format
# Copy this file to ~/.config/medre/config.toml or $MEDRE_HOME/config.toml
# and adjust values for your deployment.

[runtime]
name = "medre"
shutdown_timeout_seconds = 10

[logging]
level = "INFO"
format = "text"

[storage]
backend = "sqlite"
# {state} expands to XDG state dir or MEDRE_HOME/state
path = "{state}/medre.sqlite"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.example.com"
user_id = "@bot:example.com"
# Prefer using MEDRE_MATRIX_ACCESS_TOKEN env var over embedding tokens
access_token = ""
room_allowlist = ["!room:example.com"]
# device_id and store_path are derived internally (whoami + state dir).
# They should NOT be set by operators. Reserved for test harnesses.
# device_id = "MEDREBOT"                    # internal/test only
# store_path = "{state}/matrix/main/store"  # internal/test only
encryption_mode = "plaintext"
# When using E2EE:
# encryption_mode = "e2ee_required"

[adapters.meshtastic.radio]
enabled = false
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "MyMesh"

[adapters.meshcore.radio]
enabled = false
connection_type = "serial"
serial_port = "/dev/ttyUSB0"

[adapters.lxmf.local]
enabled = false
connection_type = "reticulum"
display_name = "MEDRE"
identity_path = "{state}/lxmf/identity"
"""
