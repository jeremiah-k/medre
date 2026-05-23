"""Sample configuration generator for MEDRE.

Provides :func:`generate_sample_config` which returns a complete, documented
TOML configuration string.  This is the output shown by ``medre config sample``.

The generated sample uses ``adapter_kind = "fake"`` for all adapters, making it
loadable and buildable without any optional SDKs or network access.  Operators
can switch adapters to real by changing ``adapter_kind`` and filling in
transport-specific credentials.

Example configs for more advanced scenarios (Docker bridges, retry workers,
mixed transports) live in ``examples/configs/`` in the source repository.
They are reference documentation, not shipped as package data.  The generated
sample config is the installed-package config access path.
"""

from __future__ import annotations


def generate_sample_config() -> str:
    """Return a complete TOML sample config with all sections documented."""

    return """\
# MEDRE Configuration — TOML format
# Copy this file to ~/.config/medre/config.toml or $MEDRE_HOME/config.toml
# and adjust values for your deployment.
#
# This sample uses adapter_kind = "fake" for all adapters so it works
# without any optional SDKs or network.  To use real adapters, change
# adapter_kind to "real" and fill in transport-specific credentials.
# For more example configs (Docker bridges, retry workers, mixed transports),
# see examples/configs/ in the source repository.

[runtime]
name = "medre"
shutdown_timeout_seconds = 10

# Resource limits for the runtime engine.
# These control concurrency and drain behaviour during shutdown.
[runtime.limits]
# Maximum number of deliveries that may be in-flight concurrently.
max_inflight_deliveries = 100
# Maximum number of replay events that may be processed concurrently.
max_inflight_replay_events = 100
# Maximum time (seconds) to wait for in-flight work to drain during shutdown.
shutdown_drain_timeout_seconds = 10
# Timeout (seconds) for acquiring a delivery slot when the in-flight limit is reached.
delivery_acquire_timeout_seconds = 1.0

[logging]
level = "INFO"
format = "text"

[storage]
backend = "sqlite"
# {state} expands to XDG state dir or MEDRE_HOME/state
path = "{state}/medre.sqlite"

# --- Matrix adapter (fake by default) ---
# To use a real Matrix homeserver, change adapter_kind to "real" and set:
#   homeserver = "https://your-homeserver.org"
#   user_id = "@bot:your-homeserver.org"
#   access_token = ""  (or set MEDRE_ADAPTER__MAIN__ACCESS_TOKEN)
[adapters.matrix.main]
enabled = true
adapter_kind = "fake"
# adapter_id = "main"    # Defaults to the TOML section key.  Only set when
#                         # you need an ID that differs from the section name.
homeserver = "https://matrix.example.com"
user_id = "@bot:example.com"
# Prefer MEDRE_ADAPTER__MAIN__ACCESS_TOKEN over embedding tokens
access_token = "fake_sample_token"
room_allowlist = ["!room:example.com"]
# device_id and store_path are derived internally (whoami + state dir).
# They should NOT be set by operators. Reserved for test harnesses.
# device_id = "MEDREBOT"                                      # internal/test only
# store_path = "{state}/adapters/{adapter_id}/matrix/store"   # internal/test only
encryption_mode = "plaintext"
# When using E2EE:
# encryption_mode = "e2ee_required"

# --- Meshtastic adapter (fake by default) ---
# To use a real Meshtastic radio, change adapter_kind to "real" and set:
#   connection_type = "serial"
#   serial_port = "/dev/ttyACM0"
[adapters.meshtastic.radio]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "MyMesh"
# max_text_bytes = 227  # UTF-8 byte budget for final radio text (default 227)

# --- MeshCore adapter (fake, disabled by default) ---
# To use a real MeshCore radio, change adapter_kind to "real", enabled to true,
# and set connection_type and serial_port.
[adapters.meshcore.mc_node]
enabled = false
adapter_kind = "fake"
connection_type = "fake"

# --- LXMF adapter (fake, disabled by default) ---
# To use a real Reticulum/LXMF node, change adapter_kind to "real", enabled
# to true, and set identity_path and storage_path.
[adapters.lxmf.lxmf_node]
enabled = false
adapter_kind = "fake"
connection_type = "fake"
display_name = "MEDRE"

# ---------------------------------------------------------------------------
# Routes — named bridge routes between adapters
# ---------------------------------------------------------------------------
# Each [routes.<id>] section defines a static route from one or more source
# adapters to one or more destination adapters.  Routes are evaluated in the
# order they appear in this file.
#
# Required fields:
#   source_adapters  — list of adapter IDs that originate messages
#   dest_adapters    — list of adapter IDs that receive messages
#
# Optional fields:
#   directionality   — "source_to_dest" (default), "dest_to_source", or
#                       "bidirectional"
#   enabled          — true (default) or false
#   source_room      — Matrix room ID on the source side (alias for
#                       source_channel)
#   source_channel   — channel/conversation ID on the source side
#   dest_room        — Matrix room ID on the dest side (alias for
#                       dest_channel)
#   dest_channel     — channel/conversation ID on the dest side
#
# Policy ([routes.<id>.policy]):
#   allowed_event_types — list of event kinds to permit (e.g. ["message"])
#   NOTE: Other policy fields (sender_allowlist, room_allowlist,
#   channel_allowlist, allowed_source_adapters, allowed_dest_adapters) are
#   reserved and not yet enforced. Do not set them.

# --- Active route: Matrix -> Meshtastic bridge ---
# Sends messages from the Matrix room to Meshtastic radio channel 1.
# For bidirectional (two-way) bridging, change directionality to "bidirectional".
[routes.matrix_radio_bridge]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
enabled = true
source_room = "!room:example.com"
dest_channel = "1"

# Only bridge "message" events (not reactions, edits, etc.)
[routes.matrix_radio_bridge.policy]
allowed_event_types = ["message"]

# --- Disabled route example ---
# This route is defined but will not be activated at startup.
# Set enabled = true to activate it.
# [routes.radio_to_matrix]
# source_adapters = ["radio"]
# dest_adapters = ["main"]
# directionality = "source_to_dest"
# enabled = false
# source_channel = "1"
# dest_room = "!room:example.com"

# --- Hub fan-out example ---
# One Matrix room fanning out to two Meshtastic radios.
# Uncomment and adjust adapter IDs to match your setup.
# [adapters.meshtastic.radio2]
# enabled = true
# connection_type = "tcp"
# host = "192.168.1.50"
# port = 4403
# meshnet_name = "MyMesh"

# [routes.matrix_fanout]
# source_adapters = ["main"]
# dest_adapters = ["radio", "radio2"]
# directionality = "source_to_dest"
# enabled = true
# source_room = "!room:example.com"

# --- Route with channel/room targeting ---
# Demonstrates targeting specific channels and rooms.
# Uncomment and adjust to match your setup.
# [routes.targeted_bridge]
# source_adapters = ["main"]
# dest_adapters = ["radio"]
# directionality = "source_to_dest"
# enabled = true
# source_room = "!specific:example.com"
# dest_channel = "2"
#
# [routes.targeted_bridge.policy]
# allowed_event_types = ["message"]
"""
