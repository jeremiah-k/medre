"""Shared text extraction and truncation helpers for renderers.

Provides :func:`extract_message_body`, :func:`extract_event_text` and
:func:`truncate_text` as public utilities so adapters do not need to
reach into the private methods of :class:`~medre.core.rendering.text.TextRenderer`.

These functions are pure and do not mutate the original event.
"""

from __future__ import annotations

from medre.core.events import CanonicalEvent, EventKind
from medre.core.events.text import select_message_body
from medre.core.rendering.relations import compose_relation_text

__all__ = [
    "extract_message_body",
    "extract_event_text",
    "truncate_text",
    "truncate_text_bytes",
]

# Maximum characters for rendered text before truncation.
_DEFAULT_MAX_TEXT_LENGTH: int = 500


def extract_message_body(event: CanonicalEvent) -> str:
    """Use the authoritative body for styled events, the ``text`` field otherwise.

    A stale ``text`` value must never override the body whose offsets are
    described by the optional presentation metadata. Unstyled messages select
    ``text`` first, then ``body``.
    """
    return select_message_body(event.payload, strict=False)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def extract_event_text(event: CanonicalEvent) -> str:
    """Extract kind-specific text, then describe unrepresented relations."""
    if event.event_kind == EventKind.PRESENCE_CHANGED:
        user = str(event.payload.get("user", "unknown"))
        status = str(event.payload.get("status", "unknown"))
        text = f"{user} is now {status}"
    else:
        text = extract_message_body(event)
    return compose_relation_text(event, text)


def truncate_text(
    text: str,
    *,
    max_text_chars: int | None = None,
) -> tuple[str, bool]:
    """Cap *text* at the configured character limit.

    Parameters
    ----------
    text:
        The text to potentially truncate.
    max_text_chars:
        Maximum characters to allow.  When ``None``, falls back to
        the module-level default (500).

    Returns
    -------
    tuple[str, bool]
        The (possibly truncated) text and whether truncation occurred.
    """
    limit = max(
        0, max_text_chars if max_text_chars is not None else _DEFAULT_MAX_TEXT_LENGTH
    )
    if limit == 0 and text:
        return "", True
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def truncate_text_bytes(
    text: str,
    max_text_bytes: int | None,
) -> tuple[str, bool, int, int]:
    """Cap *text* at a UTF-8 byte limit, splitting safely on character
    boundaries.

    Parameters
    ----------
    text:
        The text to potentially truncate.
    max_text_bytes:
        Maximum UTF-8 bytes to allow.  When ``None`` no truncation is
        performed and the function returns ``(text, False, …)``.

    Returns
    -------
    tuple[str, bool, int, int]
        ``(truncated_text, was_truncated, original_byte_count,
        rendered_byte_count)``.
    """
    encoded = text.encode("utf-8")
    original_bytes = len(encoded)
    if max_text_bytes is None:
        return text, False, original_bytes, original_bytes
    limit = max(0, max_text_bytes)
    if original_bytes <= limit:
        return text, False, original_bytes, original_bytes
    # Valid input encodes once. Only an incomplete final code point can
    # be discarded by decoding this prefix of an otherwise valid encoding.
    truncated = encoded[:limit].decode("utf-8", errors="ignore")
    return truncated, True, original_bytes, len(truncated.encode("utf-8"))
