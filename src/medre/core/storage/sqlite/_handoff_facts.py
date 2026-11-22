"""Content-free closed evidence facts surviving hard-ceiling data erasure.

These rows support exact-attempt acknowledgments and evidence classification.
They never contain wire payloads, grants or transport submission authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from medre.core.rendering.radio_text import MAX_RADIO_TEXT_PARTS


def observation_scope(
    metadata: object, *, outbox_id: str, attempt_number: int
) -> str | None:
    """Retain only packet correlation; malformed claims never become messages.

    The packet ordinal bound is the plan's own shared limit: retention and
    resolution must accept the same ordinals, or a raised plan limit would
    let retention replace a valid packet scope with an unreadable marker.
    """
    if not isinstance(metadata, Mapping):
        return '{"radio_packet":null}'
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
        or not 1 <= ordinal <= MAX_RADIO_TEXT_PARTS
        or type(attempt) is not int
        or attempt != attempt_number
        or not isinstance(outbox, str)
        or outbox != outbox_id
    ):
        return '{"radio_packet":null}'
    return json.dumps(
        {
            "radio_packet": {
                "ordinal": ordinal,
                "origin_attempt_number": attempt_number,
                "origin_outbox_id": outbox_id,
            }
        },
        separators=(",", ":"),
    )
