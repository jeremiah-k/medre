"""Matrix relation extraction and construction helpers.

Matrix events carry relations (replies, reactions, edits) in the
``content["m.relates_to"]`` subtree.  This module provides pure
functions for extracting and building those structures without
coupling to nio event objects.
"""
from __future__ import annotations

from typing import Optional


def extract_reply_target(source: dict) -> str | None:
    """Extract the reply-target event ID from a Matrix event source dict.

    Looks for ``content["m.relates_to"]["m.in_reply_to"]["event_id"]``.

    Parameters
    ----------
    source:
        The raw Matrix event source dictionary.

    Returns
    -------
    str | None
        The event ID being replied to, or ``None`` if not a reply.
    """
    try:
        return source["content"]["m.relates_to"]["m.in_reply_to"]["event_id"]
    except (KeyError, TypeError):
        return None


def extract_reaction(source: dict) -> tuple[str, str] | None:
    """Extract a reaction annotation from a Matrix event source dict.

    Looks for ``content["m.relates_to"]`` with ``rel_type ==
    "m.annotation"`` and returns ``(event_id, key)``.

    Parameters
    ----------
    source:
        The raw Matrix event source dictionary.

    Returns
    -------
    tuple[str, str] | None
        ``(target_event_id, emoji_key)`` or ``None`` if not a reaction.
    """
    try:
        relates_to = source["content"]["m.relates_to"]
    except (KeyError, TypeError):
        return None

    if relates_to.get("rel_type") != "m.annotation":
        return None

    event_id = relates_to.get("event_id")
    key = relates_to.get("key")
    if event_id is None or key is None:
        return None

    return (event_id, key)


def build_reply_body(body: str, sender: str, original_text: str) -> str:
    """Build a Matrix reply body with quoted original message.

    Prepends a ``> <sender> original_text`` fallback header followed
    by the new body text, conforming to the Matrix reply format.

    Parameters
    ----------
    body:
        The new reply body text.
    sender:
        The sender of the original message being replied to.
    original_text:
        The text of the original message.

    Returns
    -------
    str
        The formatted reply body.
    """
    return f"> <{sender}> {original_text}\n\n{body}"


class MatrixRelationHandler:
    """Convenience wrapper that groups Matrix relation operations.

    Delegates to the module-level helper functions so callers that
    prefer a single object can use it as::

        handler = MatrixRelationHandler()
        reply_to = handler.extract_reply_target(source)
    """

    __slots__ = ()

    extract_reply_target = staticmethod(extract_reply_target)
    extract_reaction = staticmethod(extract_reaction)
    build_reply_body = staticmethod(build_reply_body)
