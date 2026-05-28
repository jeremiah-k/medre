"""Shared text extraction and truncation helpers for renderers.

Provides :func:`extract_relation_text` and :func:`truncate_text` as
public utilities so that adapters (e.g. MatrixRenderer) do not need to
reach into the private methods of :class:`~medre.core.rendering.text.TextRenderer`.

Both functions are pure and do not mutate the original event.
"""

from __future__ import annotations

from medre.core.events import CanonicalEvent, EventKind, EventRelation

# Maximum characters for rendered text before truncation.
_DEFAULT_MAX_TEXT_LENGTH: int = 500


# ---------------------------------------------------------------------------
# Internal resolution helpers (shared with TextRenderer)
# ---------------------------------------------------------------------------


def _resolve_target_display(rel: EventRelation) -> str:
    """Resolve a human-readable display string for the relation target."""
    if rel.fallback_text:
        return rel.fallback_text
    if rel.target_event_id:
        eid = rel.target_event_id
        return f"{eid[:8]}…" if len(eid) > 8 else eid
    if rel.target_native_ref is not None and rel.target_native_ref.native_message_id:
        return rel.target_native_ref.native_message_id
    return "unknown message"


def _resolve_actor(event: CanonicalEvent) -> str:
    """Resolve the best available actor display name."""
    return str(
        event.payload.get("displayname")
        or event.payload.get("user")
        or event.source_adapter
    )


def _resolve_reaction_key(rel: EventRelation, event: CanonicalEvent) -> str | None:
    """Resolve the reaction key (emoji or label)."""
    if rel.key:
        return rel.key
    key = event.payload.get("key")
    if key:
        return str(key)
    emoji = event.payload.get("emoji")
    if emoji:
        return str(emoji)
    body = event.payload.get("body")
    if body:
        return str(body)
    return None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def extract_relation_text(event: CanonicalEvent) -> str:
    """Extract the raw (pre-truncation) text from *event*.

    When the event carries relations the text is augmented with
    fallback formatting before kind-based logic is applied.

    Both ``payload["text"]`` and ``payload["body"]`` are checked —
    adapters use either key depending on their native format.

    **Relation handling** — When ``event.relations`` is non-empty
    only the **first** relation is processed.  Each branch produces
    deterministic, meaningful degraded text even when relation
    metadata is partially or fully missing — the function never
    emits empty or ambiguous fallback when relation payload data
    exists.

    Parameters
    ----------
    event:
        The canonical event to extract text from.

    Returns
    -------
    str
        The extracted text, possibly including relation prefixes.
    """
    kind = event.event_kind

    if event.relations:
        rel = event.relations[0]

        if rel.relation_type == "reply":
            payload_text = str(event.payload.get("text", event.payload.get("body", "")))
            target = _resolve_target_display(rel)
            sender_display = (
                rel.metadata.get("sender_displayname")
                or rel.metadata.get("displayname")
                or rel.metadata.get("sender")
            )
            prefix = f"[replying to: {target}"
            if sender_display:
                prefix += f" by {sender_display}"
            prefix += "]"
            if payload_text:
                return f"{prefix} {payload_text}"
            return prefix

        if rel.relation_type == "reaction":
            actor = _resolve_actor(event)
            key = _resolve_reaction_key(rel, event)
            if key:
                return f"{actor} reacted with {key}"
            return f"{actor} reacted"

        if rel.relation_type == "edit":
            payload_text = str(event.payload.get("text", event.payload.get("body", "")))
            if payload_text:
                return f"[edited] {payload_text}"
            return "[edited]"

        if rel.relation_type == "delete":
            target = _resolve_target_display(rel)
            if target != "unknown message":
                return f"[deleted: {target}]"
            return "[deleted]"

        if rel.relation_type == "thread":
            payload_text = str(event.payload.get("text", event.payload.get("body", "")))
            target = _resolve_target_display(rel)
            if payload_text:
                return f"[thread: {target}] {payload_text}"
            return f"[thread: {target}]"

    # Kind-based rendering
    if kind in (EventKind.MESSAGE_TEXT, EventKind.MESSAGE_CREATED):
        return str(event.payload.get("text", event.payload.get("body", "")))

    if kind == EventKind.MESSAGE_EDITED:
        return "[edited] " + str(
            event.payload.get("text", event.payload.get("body", ""))
        )

    if kind == EventKind.MESSAGE_DELETED:
        return "[deleted]"

    if kind == EventKind.MESSAGE_REACTED:
        return str(event.payload.get("text", event.payload.get("body", "")))

    if kind == EventKind.PRESENCE_CHANGED:
        user = str(event.payload.get("user", "unknown"))
        status = str(event.payload.get("status", "unknown"))
        return f"{user} is now {status}"

    if kind == EventKind.PLUGIN_CUSTOM:
        return str(event.payload.get("text", event.payload.get("body", "")))

    return str(event.payload.get("text", event.payload.get("body", "")))


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
