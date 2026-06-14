"""Projected sender-metadata enrichment tests for :class:`RelationEnricher`.

These tests lock the layering contract that core relation enrichment
sources sender labels exclusively from generic projected fields
(``source_sender_label``, ``source_sender_short_label``,
``source_sender_id``, ``source_sender_handle``) provided by an injected
:class:`SenderProjectionFn`.  Native transport identity keys
(``displayname``, ``meshtastic.longname``, bare ``longname``) are never
read by core; per-transport projection lives in adapter attribution
modules.

The tests use the real adapter projection helpers via
:func:`medre.adapters._attribution_dispatch.project_source_fields`, which
is the same dispatch wired into the runtime by
:func:`medre.runtime.builder._build_project_sender_metadata_fn`.  This
keeps tests aligned with production wiring without importing adapter
SDKs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from medre.adapters._attribution_dispatch import project_source_fields
from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
)
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.planning.relation_enricher import RelationEnricher

# ---------------------------------------------------------------------------
# Fakes & helpers
# ---------------------------------------------------------------------------


class FakeStorage:
    """Minimal storage stub returning canned events by id."""

    def __init__(self, events: dict[str, CanonicalEvent]) -> None:
        self._events = events

    async def get(self, event_id: str) -> CanonicalEvent | None:
        return self._events.get(event_id)

    async def list_native_refs_for_event(
        self, event_id: str
    ) -> list[Any]:  # pragma: no cover - unused in this file
        return []


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_target_event(
    *,
    event_id: str,
    source_adapter: str,
    source_transport_id: str,
    native_data: dict[str, Any],
    payload_body: str = "target body",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=_ts(),
        source_adapter=source_adapter,
        source_transport_id=source_transport_id,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": payload_body, "text": payload_body},
        metadata=EventMetadata(native=NativeMetadata(data=native_data)),
    )


def _make_reply_event(target_event_id: str) -> CanonicalEvent:
    rel = EventRelation(
        relation_type="reply",
        target_event_id=target_event_id,
        target_native_ref=None,
        key=None,
        fallback_text=None,
        metadata={},
    )
    return CanonicalEvent(
        event_id="src-reply-001",
        event_kind="message.created",
        schema_version=1,
        timestamp=_ts(),
        source_adapter="src",
        source_transport_id="src-node-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": "a reply"},
        metadata=EventMetadata(),
    )


def _real_projection_fn(
    source_adapter: str = "",
) -> "Any":
    """Build a projection fn that calls the real adapter dispatch.

    Mirrors :func:`medre.runtime.builder._build_project_sender_metadata_fn`
    but without the source_attribution registry, so platform detection
    falls back to adapter-ID heuristic and native-key shape.
    """

    def _project(event: CanonicalEvent) -> Mapping[str, str | None]:
        native_data: dict[str, Any] = {}
        meta = getattr(event, "metadata", None)
        native = getattr(meta, "native", None) if meta is not None else None
        data = getattr(native, "data", None) if native is not None else None
        if isinstance(data, dict):
            native_data = dict(data)
        return project_source_fields(
            native_data,
            source_adapter=getattr(event, "source_adapter", "") or source_adapter,
            source_transport_id=getattr(event, "source_transport_id", None),
        )

    return _project


# ---------------------------------------------------------------------------
# Matrix projected sender label
# ---------------------------------------------------------------------------


async def test_matrix_projected_display_label_populates_original_sender_displayname() -> (
    None
):
    """Matrix reply fallback uses the Matrix projected display label.

    The target event carries Matrix native ``sender`` and ``displayname``
    keys.  Core relation enrichment must NOT read those keys directly;
    instead the projection callback (wired by the runtime) projects them
    into ``source_sender_label`` and ``source_sender_id``, which core
    consumes to populate ``original_sender_displayname`` and
    ``original_sender``.
    """
    target = _make_target_event(
        event_id="target-matrix-001",
        source_adapter="matrix-bot",
        source_transport_id="@alice:example.org",
        native_data={"sender": "@alice:example.org", "displayname": "Alice Liddell"},
    )
    storage = FakeStorage({"target-matrix-001": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-matrix-001")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="meshtastic-radio",
        target_channel="0",
        project_sender_fn=_real_projection_fn(source_adapter="matrix-bot"),
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") == "Alice Liddell"
    assert enriched.metadata.get("original_sender") == "@alice:example.org"
    # Reply fallback text remains stable.
    assert enriched.fallback_text == "target body"
    assert enriched.metadata.get("original_text") == "target body"


async def test_matrix_no_display_name_uses_projected_short_label() -> None:
    """Matrix target without ``displayname`` falls back to MXID localpart.

    The dispatch ``project_matrix_attribution`` is display-name-only for
    ``source_sender_label`` and applies no MXID fallback there.  However,
    ``source_sender_short_label`` is the MXID localpart (extracted via
    ``extract_mxid_localpart``).  The enricher's
    ``source_sender_label → source_sender_short_label`` priority chain
    honours this projected output, so the localpart becomes the
    ``original_sender_displayname`` when no display name is captured.
    Core never reads the native keys directly — it consumes only the
    projected generic fields.
    """
    target = _make_target_event(
        event_id="target-matrix-noDN",
        source_adapter="matrix-bot",
        source_transport_id="@bob:matrix.org",
        native_data={"sender": "@bob:matrix.org"},
    )
    storage = FakeStorage({"target-matrix-noDN": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-matrix-noDN")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="meshtastic-radio",
        target_channel="0",
        project_sender_fn=_real_projection_fn(source_adapter="matrix-bot"),
    )

    enriched = result.relations[0]
    # MXID localpart is the projected short label; it is honoured as the
    # display label when the display name is absent.
    assert enriched.metadata.get("original_sender_displayname") == "bob"
    assert enriched.metadata.get("original_sender") == "@bob:matrix.org"


# ---------------------------------------------------------------------------
# Meshtastic projected sender label
# ---------------------------------------------------------------------------


async def test_meshtastic_projected_sender_label_from_adapter_projection() -> None:
    """Meshtastic reply fallback uses the projected sender label.

    The target event carries Meshtastic namespaced identity keys
    (``meshtastic.longname`` / ``meshtastic.shortname``).  The Meshtastic
    adapter projection helper converts these to ``source_sender_label`` /
    ``source_sender_short_label``, which core consumes.  Core itself
    never reads the namespaced keys.
    """
    target = _make_target_event(
        event_id="target-mesh-001",
        source_adapter="meshtastic-radio",
        source_transport_id="!1234abcd",
        native_data={
            "meshtastic.from_id": "!1234abcd",
            "meshtastic.longname": "Alpha Node",
            "meshtastic.shortname": "Alpha",
        },
    )
    storage = FakeStorage({"target-mesh-001": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-mesh-001")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="matrix-bot",
        target_channel="!room:matrix.org",
        project_sender_fn=_real_projection_fn(source_adapter="meshtastic-radio"),
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") == "Alpha Node"
    assert enriched.metadata.get("original_sender") == "!1234abcd"


async def test_meshtastic_no_longname_uses_short_label_as_display_fallback() -> None:
    """When ``source_sender_label`` is absent, the short label is used.

    The Meshtastic projection falls back to ``meshtastic.shortname`` for
    ``source_sender_label`` only when ``meshtastic.longname`` is missing.
    This test exercises the enricher's label→short-label priority by
    providing only the shortname at the projection boundary.
    """
    target = _make_target_event(
        event_id="target-mesh-short",
        source_adapter="meshtastic-radio",
        source_transport_id="!abcd1234",
        native_data={
            "meshtastic.from_id": "!abcd1234",
            "meshtastic.shortname": "Bravo",
        },
    )
    storage = FakeStorage({"target-mesh-short": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-mesh-short")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="matrix-bot",
        target_channel=None,
        project_sender_fn=_real_projection_fn(source_adapter="meshtastic-radio"),
    )

    enriched = result.relations[0]
    # Only shortname present → projection produces both label and short
    # label equal to "Bravo" (with compact() fallback).  Core reads
    # source_sender_label first.
    assert enriched.metadata.get("original_sender_displayname") == "Bravo"
    assert enriched.metadata.get("original_sender") == "!abcd1234"


# ---------------------------------------------------------------------------
# LXMF: source_hash never becomes display label
# ---------------------------------------------------------------------------


async def test_lxmf_source_hash_does_not_become_display_label() -> None:
    """LXMF source_hash must not populate ``original_sender_displayname``.

    Without a real ``lxmf.display_name`` captured at ingress, the LXMF
    projection returns ``None`` for both label fields.  Core must honour
    that and leave ``original_sender_displayname`` unset rather than
    coercing the opaque hash into a display label.
    """
    source_hash_hex = "ab" * 16
    target = _make_target_event(
        event_id="target-lxmf-001",
        source_adapter="lxmf-node",
        source_transport_id=source_hash_hex,
        native_data={"source_hash": source_hash_hex},
    )
    storage = FakeStorage({"target-lxmf-001": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-lxmf-001")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="matrix-bot",
        target_channel="!room:matrix.org",
        project_sender_fn=_real_projection_fn(source_adapter="lxmf-node"),
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") is None
    # source_sender_id IS the normalised hash — that is the correct
    # generic field for the opaque identifier.
    assert enriched.metadata.get("original_sender") == source_hash_hex


async def test_lxmf_display_name_projects_into_label_when_present() -> None:
    """When ``lxmf.display_name`` exists, it populates the label.

    The LXMF projection helper accepts ``lxmf.display_name`` only when
    non-empty; otherwise both label fields stay ``None`` so the opaque
    hash does not leak into ``{sender}``.
    """
    source_hash_hex = "cd" * 16
    target = _make_target_event(
        event_id="target-lxmf-dn",
        source_adapter="lxmf-node",
        source_transport_id=source_hash_hex,
        native_data={
            "source_hash": source_hash_hex,
            "lxmf.display_name": "Café Operator",
        },
    )
    storage = FakeStorage({"target-lxmf-dn": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-lxmf-dn")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="matrix-bot",
        target_channel="!room:matrix.org",
        project_sender_fn=_real_projection_fn(source_adapter="lxmf-node"),
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") == "Café Operator"
    assert enriched.metadata.get("original_sender") == source_hash_hex


# ---------------------------------------------------------------------------
# MeshCore: pubkey prefix never becomes display label
# ---------------------------------------------------------------------------


async def test_meshcore_pubkey_prefix_does_not_become_display_label() -> None:
    """MeshCore pubkey prefix must not populate ``original_sender_displayname``.

    Without a contact-label resolution at ingress, the MeshCore projection
    returns ``None`` for both label fields.  Core must honour that and
    leave ``original_sender_displayname`` unset rather than coercing the
    opaque pubkey prefix into a display label.
    """
    pubkey_prefix = "a1b2c3d4e5f6"
    target = _make_target_event(
        event_id="target-meshcore-001",
        source_adapter="meshcore-node",
        source_transport_id=pubkey_prefix,
        native_data={
            "meshcore.pubkey_prefix": pubkey_prefix,
            "meshcore.channel": "0",
            "meshcore.packet_id": "1700000000",
        },
    )
    storage = FakeStorage({"target-meshcore-001": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-meshcore-001")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="matrix-bot",
        target_channel="!room:matrix.org",
        project_sender_fn=_real_projection_fn(source_adapter="meshcore-node"),
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") is None
    assert enriched.metadata.get("original_sender") == pubkey_prefix


async def test_meshcore_contact_label_projects_into_label_when_present() -> None:
    """When ``meshcore.contact_label`` exists, it populates the label.

    The MeshCore projection helper accepts ``meshcore.contact_label`` as
    a human label.  The opaque pubkey prefix never populates the label.
    """
    pubkey_prefix = "a1b2c3d4e5f6"
    target = _make_target_event(
        event_id="target-meshcore-contact",
        source_adapter="meshcore-node",
        source_transport_id=pubkey_prefix,
        native_data={
            "meshcore.pubkey_prefix": pubkey_prefix,
            "meshcore.channel": "0",
            "meshcore.packet_id": "1700000001",
            "meshcore.contact_label": "KG6XYZ",
            "meshcore.contact_short_label": "KG6",
        },
    )
    storage = FakeStorage({"target-meshcore-contact": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-meshcore-contact")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="matrix-bot",
        target_channel="!room:matrix.org",
        project_sender_fn=_real_projection_fn(source_adapter="meshcore-node"),
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") == "KG6XYZ"
    assert enriched.metadata.get("original_sender") == pubkey_prefix


# ---------------------------------------------------------------------------
# Reply / reaction fallback text stability
# ---------------------------------------------------------------------------


async def test_reply_fallback_text_remains_stable_with_projection() -> None:
    """Wiring a projection fn does not disturb fallback_text / original_text."""
    target = _make_target_event(
        event_id="target-stable-text",
        source_adapter="matrix-bot",
        source_transport_id="@alice:example.org",
        native_data={"sender": "@alice:example.org", "displayname": "Alice"},
        payload_body="original message body",
    )
    storage = FakeStorage({"target-stable-text": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-stable-text")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="meshtastic-radio",
        target_channel="0",
        project_sender_fn=_real_projection_fn(source_adapter="matrix-bot"),
    )

    enriched = result.relations[0]
    assert enriched.fallback_text == "original message body"
    assert enriched.metadata.get("original_text") == "original message body"
    # And sender enrichment still ran.
    assert enriched.metadata.get("original_sender_displayname") == "Alice"


async def test_reaction_relation_text_unchanged_by_projection_fn() -> None:
    """Reaction relations don't carry sender metadata; projection is harmless."""
    target = _make_target_event(
        event_id="target-reaction-src",
        source_adapter="matrix-bot",
        source_transport_id="@alice:example.org",
        native_data={"sender": "@alice:example.org", "displayname": "Alice"},
    )
    storage = FakeStorage({"target-reaction-src": target})
    enricher = RelationEnricher(storage=storage)
    rel = EventRelation(
        relation_type="reaction",
        target_event_id="target-reaction-src",
        target_native_ref=None,
        key="👍",
        fallback_text=None,
        metadata={},
    )
    event = CanonicalEvent(
        event_id="src-reaction-001",
        event_kind="message.reacted",
        schema_version=1,
        timestamp=_ts(),
        source_adapter="src",
        source_transport_id="src-node-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": "thumbs up"},
        metadata=EventMetadata(),
    )

    result = await enricher.enrich_for_target(
        event,
        target_adapter="meshtastic-radio",
        target_channel="0",
        project_sender_fn=_real_projection_fn(source_adapter="matrix-bot"),
    )

    enriched = result.relations[0]
    # Reaction relation's fallback_text is populated from target body
    # (text-enrichment phase); sender metadata also runs since fields
    # were empty.
    assert enriched.fallback_text == "target body"
    assert enriched.metadata.get("original_sender_displayname") == "Alice"
    # Reaction key is untouched.
    assert enriched.key == "👍"


# ---------------------------------------------------------------------------
# Projection-fn failure resilience
# ---------------------------------------------------------------------------


async def test_projection_fn_failure_falls_back_to_source_transport_id() -> None:
    """A raising projection fn degrades gracefully.

    The enricher logs the failure and treats the projected dict as empty,
    so ``original_sender`` falls back to the generic
    ``source_transport_id`` and ``original_sender_displayname`` stays
    unset.
    """

    def _broken_projection(event: CanonicalEvent) -> Mapping[str, str | None]:
        raise RuntimeError("simulated projection failure")

    target = _make_target_event(
        event_id="target-broken-proj",
        source_adapter="matrix-bot",
        source_transport_id="@alice:example.org",
        native_data={"sender": "@alice:example.org", "displayname": "Alice"},
    )
    storage = FakeStorage({"target-broken-proj": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-broken-proj")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="meshtastic-radio",
        target_channel="0",
        project_sender_fn=_broken_projection,
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") is None
    # Generic terminal fallback still applies.
    assert enriched.metadata.get("original_sender") == "@alice:example.org"


async def test_projection_fn_returning_empty_dict_uses_generic_fallback() -> None:
    """An empty projected dict yields the generic terminal fallback only."""

    def _empty_projection(event: CanonicalEvent) -> Mapping[str, str | None]:
        return {}

    target = _make_target_event(
        event_id="target-empty-proj",
        source_adapter="src",
        source_transport_id="generic-id-7",
        native_data={},
    )
    storage = FakeStorage({"target-empty-proj": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-empty-proj")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="meshtastic-radio",
        target_channel="0",
        project_sender_fn=_empty_projection,
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") is None
    assert enriched.metadata.get("original_sender") == "generic-id-7"


# ---------------------------------------------------------------------------
# Field priority within projected dict
# ---------------------------------------------------------------------------


async def test_source_sender_label_preferred_over_short_label() -> None:
    """``source_sender_label`` wins over ``source_sender_short_label``."""
    projected_static = {
        "source_sender_label": "Full Label",
        "source_sender_short_label": "Short",
        "source_sender_id": "id-1",
    }

    def _projection(event: CanonicalEvent) -> Mapping[str, str | None]:
        return projected_static

    target = _make_target_event(
        event_id="target-priority",
        source_adapter="src",
        source_transport_id="fallback-id",
        native_data={},
    )
    storage = FakeStorage({"target-priority": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-priority")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="mesh",
        target_channel="0",
        project_sender_fn=_projection,
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") == "Full Label"
    assert enriched.metadata.get("original_sender") == "id-1"


async def test_short_label_used_when_main_label_absent() -> None:
    """Empty main label falls through to the short label."""
    projected_static = {
        "source_sender_label": None,
        "source_sender_short_label": "OnlyShort",
        "source_sender_id": "id-2",
    }

    def _projection(event: CanonicalEvent) -> Mapping[str, str | None]:
        return projected_static

    target = _make_target_event(
        event_id="target-short-only",
        source_adapter="src",
        source_transport_id="fallback-id",
        native_data={},
    )
    storage = FakeStorage({"target-short-only": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-short-only")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="mesh",
        target_channel="0",
        project_sender_fn=_projection,
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") == "OnlyShort"


async def test_source_sender_id_preferred_over_handle() -> None:
    """``source_sender_id`` wins over ``source_sender_handle``."""
    projected_static = {
        "source_sender_label": None,
        "source_sender_id": "primary-id",
        "source_sender_handle": "@handle:form",
    }

    def _projection(event: CanonicalEvent) -> Mapping[str, str | None]:
        return projected_static

    target = _make_target_event(
        event_id="target-id-prio",
        source_adapter="src",
        source_transport_id="generic-fallback",
        native_data={},
    )
    storage = FakeStorage({"target-id-prio": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-id-prio")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="mesh",
        target_channel="0",
        project_sender_fn=_projection,
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender") == "primary-id"


async def test_handle_used_when_id_absent() -> None:
    """Empty ``source_sender_id`` falls through to ``source_sender_handle``."""
    projected_static = {
        "source_sender_label": None,
        "source_sender_id": None,
        "source_sender_handle": "@handle:form",
    }

    def _projection(event: CanonicalEvent) -> Mapping[str, str | None]:
        return projected_static

    target = _make_target_event(
        event_id="target-handle-only",
        source_adapter="src",
        source_transport_id="generic-fallback",
        native_data={},
    )
    storage = FakeStorage({"target-handle-only": target})
    enricher = RelationEnricher(storage=storage)
    event = _make_reply_event("target-handle-only")

    result = await enricher.enrich_for_target(
        event,
        target_adapter="mesh",
        target_channel="0",
        project_sender_fn=_projection,
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender") == "@handle:form"


# ---------------------------------------------------------------------------
# Existing values not overwritten
# ---------------------------------------------------------------------------


async def test_existing_original_sender_displayname_not_overwritten_by_projection() -> (
    None
):
    """Projection output never replaces a pre-existing displayname."""
    projected_static = {
        "source_sender_label": "Projected Label",
        "source_sender_id": "projected-id",
    }

    def _projection(event: CanonicalEvent) -> Mapping[str, str | None]:
        return projected_static

    target = _make_target_event(
        event_id="target-no-ow-dn",
        source_adapter="src",
        source_transport_id="fallback-id",
        native_data={},
    )
    storage = FakeStorage({"target-no-ow-dn": target})
    enricher = RelationEnricher(storage=storage)
    rel = EventRelation(
        relation_type="reply",
        target_event_id="target-no-ow-dn",
        target_native_ref=None,
        key=None,
        fallback_text=None,
        metadata={"original_sender_displayname": "PreExisting"},
    )
    event = CanonicalEvent(
        event_id="src-no-ow-dn",
        event_kind="message.created",
        schema_version=1,
        timestamp=_ts(),
        source_adapter="src",
        source_transport_id="src-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": "x"},
        metadata=EventMetadata(),
    )

    result = await enricher.enrich_for_target(
        event,
        target_adapter="mesh",
        target_channel="0",
        project_sender_fn=_projection,
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender_displayname") == "PreExisting"
    # original_sender was missing → populated from projection.
    assert enriched.metadata.get("original_sender") == "projected-id"


async def test_existing_original_sender_not_overwritten_by_projection() -> None:
    """Projection output never replaces a pre-existing sender id."""
    projected_static = {
        "source_sender_label": "Projected Label",
        "source_sender_id": "projected-id",
    }

    def _projection(event: CanonicalEvent) -> Mapping[str, str | None]:
        return projected_static

    target = _make_target_event(
        event_id="target-no-ow-snd",
        source_adapter="src",
        source_transport_id="fallback-id",
        native_data={},
    )
    storage = FakeStorage({"target-no-ow-snd": target})
    enricher = RelationEnricher(storage=storage)
    rel = EventRelation(
        relation_type="reply",
        target_event_id="target-no-ow-snd",
        target_native_ref=None,
        key=None,
        fallback_text=None,
        metadata={"original_sender": "preexisting-id"},
    )
    event = CanonicalEvent(
        event_id="src-no-ow-snd",
        event_kind="message.created",
        schema_version=1,
        timestamp=_ts(),
        source_adapter="src",
        source_transport_id="src-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": "x"},
        metadata=EventMetadata(),
    )

    result = await enricher.enrich_for_target(
        event,
        target_adapter="mesh",
        target_channel="0",
        project_sender_fn=_projection,
    )

    enriched = result.relations[0]
    assert enriched.metadata.get("original_sender") == "preexisting-id"
    # original_sender_displayname was missing → populated from projection.
    assert enriched.metadata.get("original_sender_displayname") == "Projected Label"
