"""Matrix relation extraction and construction helpers.

Matrix events carry replies, reactions, edits, and threads in the
``content["m.relates_to"]`` subtree.  Redactions carry their target in the
``redacts`` field of an ``m.room.redaction`` event.  This module provides pure
helpers for classifying those structures without coupling to nio event types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MatrixRelationKind = Literal["reply", "reaction", "edit", "thread", "redaction"]


@dataclass(frozen=True)
class MatrixRelationDescriptor:
    """Normalized Matrix relation semantics before canonical conversion."""

    kind: MatrixRelationKind
    target_event_id: str
    rel_type: str | None = None
    key: str | None = None
    reply_to_event_id: str | None = None
    is_falling_back: bool | None = None

    def to_native_metadata(self) -> dict[str, object]:
        """Return the stable relation fragment stored in Matrix native metadata."""
        result: dict[str, object] = {
            "kind": self.kind,
            "target_event_id": self.target_event_id,
        }
        if self.rel_type is not None:
            result["rel_type"] = self.rel_type
        if self.key is not None:
            result["key"] = self.key
        if self.reply_to_event_id is not None:
            result["reply_to_event_id"] = self.reply_to_event_id
        if self.is_falling_back is not None:
            result["is_falling_back"] = self.is_falling_back
        return result


def _content(source: dict[str, Any]) -> dict[str, Any]:
    content = source.get("content")
    return content if isinstance(content, dict) else {}


def _relates_to(source: dict[str, Any]) -> dict[str, Any]:
    relates_to = _content(source).get("m.relates_to")
    return relates_to if isinstance(relates_to, dict) else {}


def _event_id(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def extract_reply_target(source: dict[str, Any]) -> str | None:
    """Extract ``m.in_reply_to.event_id`` from a Matrix event."""
    reply = _relates_to(source).get("m.in_reply_to")
    if not isinstance(reply, dict):
        return None
    return _event_id(reply.get("event_id"))


def extract_reaction(source: dict[str, Any]) -> tuple[str, str] | None:
    """Extract an ``m.annotation`` reaction as ``(event_id, key)``."""
    relates_to = _relates_to(source)
    if relates_to.get("rel_type") != "m.annotation":
        return None
    event_id = _event_id(relates_to.get("event_id"))
    key = relates_to.get("key")
    if event_id is None or not isinstance(key, str) or not key:
        return None
    return (event_id, key)


def extract_matrix_relation(
    source: dict[str, Any],
    event_type: str | None = None,
) -> MatrixRelationDescriptor | None:
    """Return the primary relation represented by a Matrix event.

    Relation precedence is semantic rather than structural.  A thread event can
    also contain ``m.in_reply_to`` as fallback context, so thread classification
    must win over ordinary reply classification.

    ``event_type`` is the codec-resolved event type (top-level first, then
    ``source["type"]``).  Passing it keeps classification in one place so a
    producer that supplies only the top-level key is not misread.
    """
    resolved_type = event_type or source.get("type")
    if resolved_type == "m.room.redaction":
        target = _event_id(source.get("redacts"))
        if target is None:
            target = _event_id(_content(source).get("redacts"))
        if target is not None:
            return MatrixRelationDescriptor(kind="redaction", target_event_id=target)
        return None

    relates_to = _relates_to(source)
    rel_type = relates_to.get("rel_type")
    target = _event_id(relates_to.get("event_id"))

    if rel_type == "m.annotation" and target is not None:
        key = relates_to.get("key")
        if isinstance(key, str) and key:
            return MatrixRelationDescriptor(
                kind="reaction",
                target_event_id=target,
                rel_type="m.annotation",
                key=key,
            )

    if rel_type == "m.replace" and target is not None:
        return MatrixRelationDescriptor(
            kind="edit",
            target_event_id=target,
            rel_type="m.replace",
        )

    if rel_type == "m.thread" and target is not None:
        reply_target = extract_reply_target(source)
        falling_back = relates_to.get("is_falling_back")
        return MatrixRelationDescriptor(
            kind="thread",
            target_event_id=target,
            rel_type="m.thread",
            reply_to_event_id=reply_target,
            is_falling_back=(falling_back if isinstance(falling_back, bool) else None),
        )

    reply_target = extract_reply_target(source)
    if reply_target is not None:
        return MatrixRelationDescriptor(kind="reply", target_event_id=reply_target)
    return None


def strip_reply_fallback_body(body: str) -> str:
    """Strip the Matrix reply fallback prefix from a message body."""
    normalised = body.replace("\r\n", "\n")
    if not normalised.startswith("> "):
        return body

    lines = normalised.split("\n")
    idx = 0
    while idx < len(lines) and lines[idx].startswith("> "):
        idx += 1
    if idx < len(lines) and lines[idx] == "":
        idx += 1

    remainder = "\n".join(lines[idx:])
    if "\r\n" in body:
        return remainder.replace("\n", "\r\n")
    return remainder


def build_reply_body(body: str, sender: str, original_text: str) -> str:
    """Build a Matrix reply body with quoted original-message fallback."""
    return f"> <{sender}> {original_text}\n\n{body}"


class MatrixRelationHandler:
    """Convenience wrapper grouping Matrix relation operations."""

    __slots__ = ()

    extract_reply_target = staticmethod(extract_reply_target)
    extract_reaction = staticmethod(extract_reaction)
    extract_matrix_relation = staticmethod(extract_matrix_relation)
    strip_reply_fallback_body = staticmethod(strip_reply_fallback_body)
    build_reply_body = staticmethod(build_reply_body)
