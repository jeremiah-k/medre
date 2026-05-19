"""Matrix relation extraction and construction helpers.

Matrix events carry relations (replies, reactions, edits) in the
``content["m.relates_to"]`` subtree.  This module provides pure
functions for extracting and building those structures without
coupling to nio event objects.
"""

from __future__ import annotations


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


def strip_reply_fallback_body(body: str) -> str:
    """Strip the Matrix reply fallback prefix from a message body.

    Matrix clients embed a quoted fallback when replying.  The wire format
    is::

        > <@sender:server> original line 1
        > <@sender:server> original line 2

        User-authored reply text

    Lines starting with ``"> "`` form the quoted block, a single blank line
    separates it from the user's reply, and everything after that separator
    is the actual reply text.

    Parameters
    ----------
    body:
        The raw message body that may contain a reply fallback prefix.

    Returns
    -------
    str
        The body with the fallback prefix removed.  If *body* does not
        start with ``"> "``, it is returned unchanged — this preserves
        ordinary messages that happen to contain ``"> "`` quotes later
        in the text.
    """
    # Normalise line endings so we only deal with \n internally.
    normalised = body.replace("\r\n", "\n")
    if not normalised.startswith("> "):
        return body

    lines = normalised.split("\n")
    idx = 0
    # Skip consecutive "> " lines (the quoted fallback block).
    while idx < len(lines) and lines[idx].startswith("> "):
        idx += 1

    # Skip the blank separator line immediately after the quoted block.
    if idx < len(lines) and lines[idx] == "":
        idx += 1

    remainder = "\n".join(lines[idx:])
    # Preserve the original line-ending style if possible.
    if "\r\n" in body:
        return remainder.replace("\n", "\r\n")
    return remainder


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
    strip_reply_fallback_body = staticmethod(strip_reply_fallback_body)
    build_reply_body = staticmethod(build_reply_body)
