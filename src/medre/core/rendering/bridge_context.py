"""Bounded platform-neutral relay hints; never identity or delivery authority."""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

import msgspec

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent
    from medre.core.rendering.attribution import RelayAttribution

_MAX_STRING = 1024
_MAX_RELATIONS = 32
_MAX_FACTS = 12
_MAX_FACT_VALUE = 256
_FACT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
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


def _utf8(value: str) -> bool:
    """Reject lone surrogate code points that wire encoders cannot serialize."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _text(value: object) -> bool:
    """Accept bounded, encodable strings or absence; IDs are never truncated."""
    return value is None or (
        isinstance(value, str) and len(value) <= _MAX_STRING and _utf8(value)
    )


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


def _native_facts(value: object) -> dict[str, object] | None:
    """Validate one explicitly public, source-owned fact projection.

    This is an *untrusted claim*, not native identity, signature evidence or
    authorization. Only scalar, bounded data may cross the transport boundary.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"namespace", "fields"}:
        raise ValueError("invalid native-facts envelope")
    namespace, fields = value["namespace"], value["fields"]
    if not isinstance(namespace, str) or not _FACT_NAME.fullmatch(namespace):
        raise ValueError("invalid native-facts namespace")
    if not isinstance(fields, dict) or not 1 <= len(fields) <= _MAX_FACTS:
        raise ValueError("invalid native-facts inventory")
    clean: dict[str, object] = {}
    for key, item in fields.items():
        if not isinstance(key, str) or not _FACT_NAME.fullmatch(key):
            raise ValueError("invalid native-facts field")
        if type(item) is str and len(item) <= _MAX_FACT_VALUE and _utf8(item):
            clean[key] = item
        elif type(item) is bool:
            clean[key] = item
        elif type(item) is int and -(2**31) <= item < 2**31:
            clean[key] = item
        elif (
            type(item) is float
            and math.isfinite(item)
            and item.is_integer()
            and -(2**31) <= item < 2**31
        ):
            # JSON Schema integers are mathematical values, including 1.0/1e0.
            clean[key] = int(item)
        else:
            raise ValueError("invalid native-facts value")
    return {"namespace": namespace, "fields": clean}


def _public_source_facts(event: CanonicalEvent) -> dict[str, object] | None:
    """Select only an adapter's explicit public_facts, never arbitrary native data."""
    native = event.metadata.native if event.metadata else None
    if native is None:
        return None
    candidates = [
        {"namespace": namespace, "fields": data["public_facts"]}
        for namespace, data in native.data.items()
        if isinstance(namespace, str)
        and isinstance(data, dict)
        and type(data.get("schema_version")) is int
        and data["schema_version"] == 1
        and "public_facts" in data
    ]
    # Ambiguous native sources are not transport-neutral evidence.
    return candidates[0] if len(candidates) == 1 else None


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
        if "native_facts" in source:
            facts = _native_facts(source["native_facts"])
            if facts is None:
                raise ValueError("null native facts are not a valid projection")
            clean_source["native_facts"] = facts
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
    native_facts = _public_source_facts(event)
    if native_facts is not None:
        source["native_facts"] = native_facts
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
