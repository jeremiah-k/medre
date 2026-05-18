"""Meshtastic packet snapshot helpers for safe serialisation.

These utilities convert raw Meshtastic packet dicts into JSON-safe
structures suitable for logging, diagnostics, and ``msgspec`` /
``json`` serialisation.

* :func:`json_safe` converts arbitrary values to JSON-serialisable form.
* :func:`snapshot_decoded` snapshots the ``decoded`` sub-dict of a packet.
* :func:`snapshot_packet` snapshots a full packet dict.
"""

from __future__ import annotations

import base64
from typing import Any


def json_safe(value: object) -> Any:
    """Convert *value* into a structure safe for ``msgspec`` and ``json``.

    Conversion rules:

    * ``bytes`` / ``bytearray`` → ``{"encoding": "base64", "data": "..."}``
    * ``dict`` → recursive conversion of both keys (to ``str``) and values.
    * ``list`` / ``tuple`` → recursive conversion of items (as ``list``).
    * ``str``, ``int``, ``float``, ``bool``, ``None`` → passed through.
    * Everything else → ``repr(value)``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return repr(value)


def snapshot_decoded(decoded: Any) -> Any:
    """Return a JSON-safe snapshot of a Meshtastic ``decoded`` value.

    Parameters
    ----------
    decoded:
        The ``decoded`` value from a raw Meshtastic packet.
        May be a dict, ``None``, or any other type.
    """
    return json_safe(decoded)


def snapshot_packet(packet: Any) -> Any:
    """Return a JSON-safe snapshot of a full Meshtastic packet.

    Parameters
    ----------
    packet:
        Raw Meshtastic packet.  May be a dict or any other type.
    """
    return json_safe(packet)
