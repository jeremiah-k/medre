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

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.example.com"
user_id = "@bot:example.com"
# Prefer using MEDRE_MATRIX_ACCESS_TOKEN env var over embedding tokens
access_token = ""
room_allowlist = ["!room:example.com"]
# device_id and store_path are derived internally (whoami + state dir).
# They should NOT be set by operators. Reserved for test harnesses.
# device_id = "MEDREBOT"                                      # internal/test only
# store_path = "{state}/adapters/{adapter_id}/matrix/store"   # internal/test only
encryption_mode = "plaintext"
# When using E2EE:
# encryption_mode = "e2ee_required"

[adapters.meshtastic.radio]
enabled = true
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

# --- Active route: Matrix <-> Meshtastic bridge ---
# Bridges the Matrix room !room:example.com to Meshtastic radio channel 1.
# Messages flow in both directions.
[routes.matrix_radio_bridge]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "bidirectional"
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
