"""Transport adapter registry and SDK availability probing."""

from __future__ import annotations

import importlib

# Transport adapter types that medre supports.
# Each entry: (transport_key, dist_name, import_module_names).
TRANSPORTS: list[tuple[str, str, tuple[str, ...]]] = [
    ("matrix", "mindroom-nio", ("mindroom_nio", "nio")),
    ("meshtastic", "mtjk", ("mtjk", "meshtastic")),
    ("meshcore", "meshcore", ("meshcore",)),
    ("lxmf", "lxmf", ("lxmf", "RNS")),
]


def is_transport_installed(transport: str) -> bool:
    """Check whether a transport SDK is available via dynamic import."""
    for t_key, _dist, import_names in TRANSPORTS:
        if t_key == transport:
            for mod_name in import_names:
                try:
                    importlib.import_module(mod_name)
                    return True
                except ImportError:
                    pass
            return False
    return False
