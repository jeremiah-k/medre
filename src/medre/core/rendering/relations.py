"""Shared relation-degradation utilities for inline text rendering.

Provides :func:`degrade_relations_inline` which appends human-readable
relation descriptions to existing text so that relation information is
preserved in the content body when native relation handling is unavailable.

This is the canonical implementation used by LXMF and MeshCore renderers
when ``delivery_strategy == "fallback_text"``.
"""

from __future__ import annotations

from medre.core.events import CanonicalEvent


def degrade_relations_inline(event: CanonicalEvent, text: str) -> str:
    """Degrade relation semantics into inline text.

    Appends human-readable relation descriptions to *text* so that
    relation information is preserved in the content body when
    native relation handling is unavailable.

    Ensures the result is non-empty when relation data exists,
    preventing false sent-receipt appearance from empty content.

    Parameters
    ----------
    event:
        The canonical event whose relations to degrade.
    text:
        The existing content text.

    Returns
    -------
    str
        Content text with inline relation descriptions appended.
    """
    if not event.relations:
        return text

    parts: list[str] = []
    for rel in event.relations:
        target = (
            rel.fallback_text
            or rel.target_event_id
            or (
                rel.target_native_ref.native_message_id
                if rel.target_native_ref is not None
                else None
            )
            or "?"
        )
        if rel.relation_type == "reply":
            parts.append(f"[reply to: {target}]")
        elif rel.relation_type == "reaction":
            emoji = (
                rel.key
                or event.payload.get("key")
                or event.payload.get("emoji")
                or "∟"
            )
            parts.append(f"[reaction {emoji} to: {target}]")
        elif rel.relation_type == "edit":
            parts.append(f"[edit of: {target}]")
        elif rel.relation_type == "delete":
            parts.append(f"[delete of: {target}]")
        elif rel.relation_type == "thread":
            parts.append(f"[thread on: {target}]")
        else:
            parts.append(f"[{rel.relation_type}: {target}]")

    inline = " ".join(parts)
    if text:
        return f"{text} {inline}"
    return inline
