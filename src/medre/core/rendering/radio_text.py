"""Pure UTF-8 radio text planning, independent of queues and native SDKs.

A plan describes exact wire payloads. It does not submit them, assign delivery
authority, or permit a parent attempt to finish after only one accepted part.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

MAX_RADIO_TEXT_PARTS = 128
MAX_RADIO_SOURCE_BYTES = 1_048_576


@dataclass(frozen=True)
class RadioTextPart:
    """One numbered wire payload with the original body fragment and its digest.

    ``wire_text`` includes repeated attribution and, for multipart text, a
    numbered marker. ``body`` excludes that overhead so fragments reconstruct
    the authored text exactly, including whitespace and newlines.
    """

    ordinal: int
    body: str
    wire_text: str
    wire_bytes: int
    wire_sha256: str


@dataclass(frozen=True)
class RadioTextPlan:
    """Immutable wire plan for later binding to one durable delivery attempt.

    The fingerprint identifies the source, effective budget, prefix and exact
    wire parts. It is a content digest, not a native message ID or evidence of
    acceptance. The budget must already exclude transport/firmware overhead.
    """

    max_text_bytes: int
    prefix: str
    source_sha256: str
    parts: tuple[RadioTextPart, ...]
    fingerprint: str
    schema_version: int = 1


def _utf8(text: str) -> bytes:
    """Reject unencodable text without including its content in the error."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("radio text must be valid UTF-8") from None


def _fragment_bodies(
    source: bytes, prefix: str, budget: int, total: int, max_parts: int
) -> list[str]:
    """Greedily fit whole words where possible without dropping any source bytes."""
    bodies: list[str] = []
    cursor = 0
    while cursor < len(source):
        if len(bodies) == max_parts:
            raise ValueError("radio text exceeds the configured part limit")
        marker = f"({len(bodies) + 1}/{total}) "
        capacity = budget - len(_utf8(prefix + marker))
        if capacity <= 0:
            raise ValueError("radio budget cannot fit attribution and a part marker")
        # The source is valid UTF-8 and cursor is a code-point boundary. Ignoring
        # errors here can remove only an incomplete final code point in the slice.
        candidate = source[cursor : cursor + capacity].decode("utf-8", "ignore")
        if not candidate:
            raise ValueError("radio budget cannot fit the next Unicode code point")
        if cursor + len(_utf8(candidate)) < len(source):
            first_content = len(candidate) - len(candidate.lstrip())
            for index in range(len(candidate) - 1, first_content, -1):
                if candidate[index].isspace():
                    candidate = candidate[: index + 1]
                    break
        bodies.append(candidate)
        cursor += len(_utf8(candidate))
    return bodies


def plan_radio_text(
    text: str,
    *,
    max_text_bytes: int,
    prefix: str = "",
    max_parts: int = 16,
) -> RadioTextPlan:
    """Plan bounded plain-text fragments with repeated attribution and numbering.

    Preserve short messages as one unnumbered payload. Longer messages repeat
    ``prefix`` followed by ``(i/n) `` on every part. Replan when the denominator
    grows so marker digit widths and multibyte names count against every budget.
    Whitespace stays in its original order; oversized words split at Unicode
    code-point boundaries. Native reactions and reply IDs are outside this API.

    Raise ValueError before producing a plan if the text is empty, not valid
    UTF-8, exceeds one MiB, cannot fit within the part limit, or leaves too little
    room for a code point after attribution/numbering. No partial plan escapes.
    Raise TypeError for non-string text or prefixes. Nothing is sent or paced.
    """
    if not isinstance(text, str) or not isinstance(prefix, str):
        raise TypeError("radio text and prefix must be strings")
    if type(max_text_bytes) is not int or max_text_bytes <= 0:
        raise ValueError("max_text_bytes must be a positive integer")
    if type(max_parts) is not int or not 1 <= max_parts <= MAX_RADIO_TEXT_PARTS:
        raise ValueError(f"max_parts must be between 1 and {MAX_RADIO_TEXT_PARTS}")
    if not text or len(text) > MAX_RADIO_SOURCE_BYTES:
        raise ValueError("radio text must contain between one byte and one MiB")
    if len(prefix) > min(max_text_bytes, MAX_RADIO_SOURCE_BYTES):
        raise ValueError("radio prefix exceeds the effective byte budget")
    source, prefix_bytes = _utf8(text), _utf8(prefix)
    if len(prefix_bytes) > MAX_RADIO_SOURCE_BYTES:
        raise ValueError("radio prefix exceeds one MiB")
    if len(source) > MAX_RADIO_SOURCE_BYTES:
        raise ValueError("radio source text exceeds one MiB")
    if len(source) + len(prefix_bytes) <= max_text_bytes:
        bodies = [text]
    else:
        if max_parts == 1:
            raise ValueError("radio text exceeds the configured part limit")
        total = 2
        while True:
            bodies = _fragment_bodies(source, prefix, max_text_bytes, total, max_parts)
            if len(bodies) == total:
                break
            # Increasing denominator width cannot create more room. Thus the
            # number of parts is monotonic, bounded by max_parts, and converges.
            total = len(bodies)

    parts = []
    for ordinal, body in enumerate(bodies, 1):
        marker = f"({ordinal}/{len(bodies)}) " if len(bodies) > 1 else ""
        wire = prefix + marker + body
        encoded = _utf8(wire)
        parts.append(
            RadioTextPart(
                ordinal, body, wire, len(encoded), hashlib.sha256(encoded).hexdigest()
            )
        )
    source_digest = hashlib.sha256(source).hexdigest()
    fingerprint_input = json.dumps(
        [1, max_text_bytes, prefix, source_digest, [part.wire_text for part in parts]],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return RadioTextPlan(
        max_text_bytes=max_text_bytes,
        prefix=prefix,
        source_sha256=source_digest,
        parts=tuple(parts),
        fingerprint=hashlib.sha256(fingerprint_input).hexdigest(),
    )
