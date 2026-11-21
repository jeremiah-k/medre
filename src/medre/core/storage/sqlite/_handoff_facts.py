"""Content-free closed evidence facts surviving hard-ceiling data erasure.

These rows support exact-attempt acknowledgments and evidence classification.
They never contain wire payloads, grants or transport submission authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping


def observation_scope(metadata: object) -> str | None:
    """Retain only packet correlation; malformed claims never become messages."""
    if not isinstance(metadata, Mapping):
        return "null"
    if "radio_packet" not in metadata:
        return None
    packet = metadata["radio_packet"]
    if not isinstance(packet, Mapping):
        return '{"radio_packet":null}'
    ordinal = packet.get("ordinal")
    attempt = packet.get("origin_attempt_number")
    outbox = packet.get("origin_outbox_id")
    if (
        type(ordinal) is not int
        or not 1 <= ordinal <= 128
        or type(attempt) is not int
        or attempt < 1
        or not isinstance(outbox, str)
        or not outbox
    ):
        return '{"radio_packet":null}'
    return json.dumps(
        {
            "radio_packet": {
                "ordinal": ordinal,
                "origin_attempt_number": attempt,
                "origin_outbox_id": outbox,
            }
        },
        separators=(",", ":"),
    )
