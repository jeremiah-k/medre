"""Shared transport constants used across CLI command modules."""
from __future__ import annotations

# Radio transports that use fire-and-forget delivery.
RADIO_TRANSPORTS = frozenset({"meshtastic", "meshcore", "lxmf"})
