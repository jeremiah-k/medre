"""Bounded platform-neutral relay hints; never identity or delivery authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import msgspec

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent
    from medre.core.rendering.attribution import RelayAttribution

_MAX_STRING = 1024
_MAX_RELATIONS = 32
_SOURCE_FIELDS = (
    "adapter",
    "platform",
    "canonical_event_id",
    "channel",
    "sender_id",
    "sender_name",
    "sender_short_name",
    "sender_verification",
    "origin_label",
)
_RELATION_FIELDS = ("relation_type", "target_event_id", "key")


def _text(value: object) -> bool:
    """Accept bounded strings or explicit absence; IDs are never truncated."""
    return value is None or (isinstance(value, str) and len(value) <= _MAX_STRING)


def _native_ref(value: object) -> dict[str, object] | None:
    """Copy only a scoped native-reference hint, with no transport-private data."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid reference")
    result = {
        name: value.get(name)
        for name in (
            "adapter",
            "native_channel_id",
            "native_message_id",
            "native_thread_id",
        )
    }
    if not all(_text(item) for item in result.values()):
        raise ValueError("invalid reference fields")
    if not result["adapter"] or not result["native_message_id"]:
        raise ValueError("unscoped reference")
    return result


def normalize_bridge_context(value: object) -> dict[str, Any] | None:
    """Validate known v1 fields and discard unknown data without trusting its claims."""
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        return None
    source, relations = value.get("source"), value.get("relations")
    if (
        not isinstance(source, dict)
        or not isinstance(relations, list)
        or len(relations) > _MAX_RELATIONS
    ):
        return None
    clean_source = {name: source.get(name) for name in _SOURCE_FIELDS}
    if not all(_text(item) for item in clean_source.values()):
        return None
    if not clean_source["adapter"] or not clean_source["canonical_event_id"]:
        return None
    try:
        clean_source["native_ref"] = _native_ref(source.get("native_ref"))
        clean_relations = []
        for relation in relations:
            if not isinstance(relation, dict):
                return None
            clean = {name: relation.get(name) for name in _RELATION_FIELDS}
            if (
                not all(_text(item) for item in clean.values())
                or not clean["relation_type"]
            ):
                return None
            clean["target_native_ref"] = _native_ref(relation.get("target_native_ref"))
            clean_relations.append(clean)
    except ValueError:
        return None
    return {"schema_version": 1, "source": clean_source, "relations": clean_relations}


def build_bridge_context(
    event: CanonicalEvent,
    attribution: RelayAttribution | None = None,
    *,
    include_relations: bool = True,
) -> dict[str, Any] | None:
    """Export bounded source claims and relation hints without native SDK data.

    Set ``include_relations=False`` when a destination degrades relations into
    inline text. The source event and attribution snapshot are never mutated.
    Embedded claims never grant identity, relation binding or delivery authority.
    """
    source: dict[str, object] = {
        "adapter": event.source_adapter,
        "canonical_event_id": event.event_id,
        "channel": event.source_channel_id,
        "sender_id": event.source_transport_id,
        "native_ref": msgspec.to_builtins(event.source_native_ref),
        "sender_verification": "unknown",
    }
    if attribution is not None:
        for output, raw, fallback in (
            ("sender_name", "source_sender_label_raw", "source_sender_label"),
            (
                "sender_short_name",
                "source_sender_short_label_raw",
                "source_sender_short_label",
            ),
        ):
            name = getattr(attribution, raw, None)
            source[output] = (
                name if name is not None else getattr(attribution, fallback, None)
            )
        source.update(
            platform=attribution.source_platform,
            origin_label=attribution.source_origin_label,
            sender_verification=getattr(attribution, "source_sender_verification", None)
            or "unknown",
        )
    relations = [
        {
            "relation_type": relation.relation_type,
            "target_event_id": relation.target_event_id,
            "target_native_ref": msgspec.to_builtins(relation.target_native_ref),
            "key": relation.key,
        }
        for relation in (event.relations[:_MAX_RELATIONS] if include_relations else ())
    ]
    return normalize_bridge_context(
        {"schema_version": 1, "source": source, "relations": relations}
    )
