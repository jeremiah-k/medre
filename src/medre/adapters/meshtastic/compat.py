"""Optional dependency guard and authoritative PortNum helper.

``mtjk`` (distribution name ``mtjk``, version 2.7.8.post2+) is a fork of the
upstream Meshtastic Python library maintained at ``github.com/jeremiah-k/mtjk``.
It is imported as ``meshtastic``.

When available, the protobuf ``PortNum`` enum can be used for authoritative
numeric portnum values.  Tests that depend on the real enum should skip
when the dependency is absent.
"""
from __future__ import annotations

import typing as _t

HAS_MESHTASTIC: bool
_PORTNUM_ENUM: type | None = None

try:
    import meshtastic  # noqa: F401
    from meshtastic.protobuf import portnums_pb2  # noqa: F401

    HAS_MESHTASTIC = True
except ImportError:
    HAS_MESHTASTIC = False

if HAS_MESHTASTIC:
    try:
        from meshtastic.protobuf.portnums_pb2 import PortNum as _PortNum

        _PORTNUM_ENUM = _PortNum
    except ImportError:
        _PORTNUM_ENUM = None


def get_portnum_table() -> dict[int, str] | None:
    """Return the authoritative real ``{int: name}`` PortNum map if the
    optional ``meshtastic`` package is installed, or ``None`` otherwise.

    The returned dict uses lowercase MEDRE-normalised names for keys that
    match tranche-1 categories (``"text_message"``, ``"telemetry"``,
    ``"position"``, ``"nodeinfo"``, ``"admin"``, ``"routing"``) and keeps
    the original ``PortNum.Name()`` string for all other values.

    This is intended for use in **optional** test helpers and diagnostics.
    Core classifier logic must not depend on it — always use the scaffold
    map for default code paths so that tests pass without the dependency.
    """
    if _PORTNUM_ENUM is None:
        return None

    enum = _t.cast(type, _PORTNUM_ENUM)
    desc = getattr(enum, "DESCRIPTOR", None)
    if desc is None:
        return None

    result: dict[int, str] = {}
    for v in desc.values:
        # Map protobuf names to MEDRE normalised form for known categories
        raw_name: str = v.name  # e.g. "TEXT_MESSAGE_APP"
        lower_name = raw_name.lower()
        if lower_name == "unknown_app":
            result[v.number] = "unknown"
        elif lower_name == "text_message_app":
            result[v.number] = "text_message"
        elif lower_name == "telemetry_app":
            result[v.number] = "telemetry"
        elif lower_name == "position_app":
            result[v.number] = "position"
        elif lower_name == "nodeinfo_app":
            result[v.number] = "nodeinfo"
        elif lower_name == "admin_app":
            result[v.number] = "admin"
        elif lower_name == "routing_app":
            result[v.number] = "routing"
        else:
            result[v.number] = raw_name.lower()
    return result
