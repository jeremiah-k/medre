"""Transport-neutral descriptions of canonical relations.

Renderers supply already-authorized target context. No storage access, native
identity interpretation, or storage-backed target binding is performed here.
"""

from __future__ import annotations

from medre.core.events import CanonicalEvent, EventKind, EventRelation, NativeRef
from medre.core.events.canonical import RelationTargetFact

__all__ = [
    "compose_relation_text",
    "reaction_key",
    "reaction_target_preview",
    "describe_reaction",
    "relation_target_display",
    "destination_native_message_id",
]


def reaction_key(rel: EventRelation) -> str:
    """Require the explicit canonical key, preserving every code point."""
    if not isinstance(rel.key, str) or not rel.key.strip():
        raise ValueError("Canonical reaction requires an explicit nonempty key")
    return rel.key


def reaction_target_preview(rel: EventRelation) -> str:
    """Select authorized target text, flatten whitespace, and bound the preview.

    Neither a reaction's authored payload nor an opaque identifier is target
    content. Original text takes precedence over a supplied fallback quote.
    """
    for candidate in (rel.metadata.get("original_text"), rel.fallback_text):
        if isinstance(candidate, str):
            text = " ".join(candidate.split())
            if text:
                return text[:40] + ("..." if len(text) > 40 else "")
    return ""


def describe_reaction(rel: EventRelation) -> str:
    """Describe a reaction without inventing target content or sender identity."""
    key = reaction_key(rel)
    preview = reaction_target_preview(rel)
    target = f'"{preview}"' if preview else "an earlier message"
    return f"reacted {key} to {target}"


def relation_target_display(rel: EventRelation) -> str:
    """Display the supplied quote, otherwise a correlation marker, never guessed text."""
    if rel.fallback_text:
        return rel.fallback_text
    if rel.target_event_id:
        eid = rel.target_event_id
        return f"{eid[:8]}…" if len(eid) > 8 else eid
    if rel.target_native_ref is not None and rel.target_native_ref.native_message_id:
        return rel.target_native_ref.native_message_id
    return "unknown message"


def compose_relation_text(
    event: CanonicalEvent,
    text: str,
    *,
    represented: tuple[EventRelation, ...] = (),
) -> str:
    """Describe each unrepresented relation in canonical tuple order before body text.

    Preserve authored message text once. A reaction event's body is not
    additional message content; the exact canonical relation key represents
    it. A delete event has no authored replacement text. Native renderers can
    exclude the exact relations they have already represented on the wire.
    If the event kind has no matching mutation/reaction relation, a neutral
    kind marker preserves that fact without inventing a target or reaction key.
    """
    parts: list[str] = []
    # A kind-level marker is needed when the canonical event lacks its
    # corresponding relation. An explicitly represented mutation still counts
    # as a matching relation and must not gain a duplicate marker.
    mutation = {
        EventKind.MESSAGE_EDITED: ("edit", "[edited]"),
        EventKind.MESSAGE_DELETED: ("delete", "[deleted]"),
        EventKind.MESSAGE_REACTED: ("reaction", "[reacted]"),
    }.get(event.event_kind)
    if mutation is not None and not any(
        rel.relation_type == mutation[0] for rel in event.relations
    ):
        parts.append(mutation[1])
    for rel in event.relations:
        if rel in represented:
            continue
        target = relation_target_display(rel)
        if rel.relation_type == "reply":
            for name in (
                "original_sender_displayname",
                "sender_displayname",
                "displayname",
                "sender",
            ):
                sender = rel.metadata.get(name)
                if isinstance(sender, str) and sender.strip() and sender.isprintable():
                    target += f" by {sender}"
                    break
            parts.append(f"[replying to: {target}]")
        elif rel.relation_type == "reaction":
            parts.append(describe_reaction(rel))
        elif rel.relation_type == "edit":
            parts.append(f"[edit of: {target}]")
        elif rel.relation_type == "delete":
            parts.append(f"[delete of: {target}]")
        elif rel.relation_type == "thread":
            parts.append(f"[thread on: {target}]")
        else:
            parts.append(f"[{rel.relation_type}: {target}]")
    if event.event_kind in (EventKind.MESSAGE_REACTED, EventKind.MESSAGE_DELETED):
        text = ""
    if not parts:
        return text
    return " ".join(part for part in (*parts, text) if part)


def destination_native_message_id(
    rel: EventRelation,
    target_adapter: str,
    target_channel: str | None,
    *,
    require_fact: bool = False,
) -> str | None:
    """Resolve the destination-scoped native ID, honoring any core binding fact.

    Native references remain usable by direct embedding consumers that do not
    construct core planning facts. A present negative or mismatched fact must
    never be bypassed. Providers requiring durable source binding set require_fact.
    """
    ref: RelationTargetFact | NativeRef | None
    fact = rel.target_fact
    if fact is not None:
        if fact.status not in ("bound", "bound_owned"):
            return None
        ref = fact
    elif require_fact:
        return None
    else:
        ref = rel.target_native_ref
    if ref is None or ref.adapter != target_adapter:
        return None
    if target_channel is not None and ref.native_channel_id != target_channel:
        return None
    mid = ref.native_message_id
    return mid if isinstance(mid, str) and mid.strip() else None
