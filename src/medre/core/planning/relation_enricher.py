"""Internal relation/provenance enrichment for per-target delivery.

This module provides :class:`RelationEnricher`, an internal helper that
enriches event relations with target-adapter native refs, fallback text,
and sender metadata so that the rendering pipeline and downstream adapters
receive native IDs for structured replies and reactions.

This class is **not** part of the public API.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, cast

import msgspec

from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)

_logger = logging.getLogger(__name__)


class RelationEnricher:
    """Enriches event relations with target-adapter native refs and metadata.

    For each relation that has a ``target_event_id`` but whose
    ``target_native_ref`` is either missing or not for the target adapter,
    this class looks up stored native refs for the target event and attaches
    the best matching one.  When *target_channel* is provided, an exact
    channel match is preferred over a bare adapter-only match.  This enables
    structured replies / reactions in target-adapter native ID space.

    Additionally extracts original text, sender display name, and sender
    identity from the target event to populate ``fallback_text`` and relation
    metadata so renderers can produce meaningful fallback content.

    Parameters
    ----------
    storage:
        The storage backend (duck-typed).  Must support
        ``list_native_refs_for_event(event_id)`` and ``get(event_id)`` when
        available.
    logger:
        Optional logger override; defaults to the module logger.
    """

    def __init__(
        self,
        storage: object,
        logger: logging.Logger | None = None,
    ) -> None:
        self._storage = storage
        self._log: logging.Logger = logger or _logger

    async def enrich_for_target(
        self,
        event: CanonicalEvent,
        *,
        target_adapter: str,
        target_channel: str | None = None,
        cached_get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
        cached_list_fn: (
            Callable[[str], Awaitable[list[NativeMessageRef]]] | None
        ) = None,
    ) -> CanonicalEvent:
        """Enrich relations with target-adapter native refs for rendering.

        For each relation that has a ``target_event_id`` but whose
        ``target_native_ref`` is either missing or not for *target_adapter*,
        look up stored native refs for the target event and attach the
        best matching one.  When *target_channel* is provided, an exact
        channel match is preferred over a bare adapter-only match.  This
        enables structured replies / reactions in target-adapter native ID
        space.

        Args:
            event: The canonical event whose relations may be enriched.
            target_adapter: Adapter ID to match native refs against.
            target_channel: Optional native channel ID — when given, prefer
                refs whose ``native_channel_id`` equals this value.
            cached_get_fn: Optional pre-wired/cached ``storage.get`` callable.
                When provided, used instead of ``getattr(storage, "get")`` so
                callers (e.g. :class:`PipelineRunner`) can memoize lookups
                across multiple enrichment calls within a single ingress.
                Must have the same signature as ``storage.get(event_id)``.
            cached_list_fn: Optional pre-wired/cached
                ``storage.list_native_refs_for_event`` callable.  When
                provided, used instead of ``getattr(storage,
                "list_native_refs_for_event")`` for the same memoization
                purpose.

        Returns a new event when any relation is enriched; returns the
        original event unchanged otherwise.  **Never mutates** the stored
        original event.
        """
        if not event.relations:
            return event

        storage = self._storage
        list_fn = cached_list_fn or getattr(storage, "list_native_refs_for_event", None)
        get_fn = cached_get_fn or getattr(storage, "get", None)

        changed = False
        new_relations: list[EventRelation] = []

        for rel in event.relations:
            if not rel.target_event_id:
                new_relations.append(rel)
                continue

            current_rel = rel
            target_event_id = rel.target_event_id

            # -- Phase 1: Native-ref enrichment --------------------------------
            # Only when list_fn is available; otherwise skip native-ref lookup.
            native_ref_changed = False
            if callable(list_fn):
                # Check if already has a native ref for the target adapter.
                skip_native = False
                if (
                    current_rel.target_native_ref is not None
                    and current_rel.target_native_ref.adapter == target_adapter
                ):
                    existing_channel = current_rel.target_native_ref.native_channel_id
                    if target_channel is None:
                        # No target channel specified — adapter match + any
                        # channel (including None) is fine.
                        skip_native = True
                    elif existing_channel == target_channel:
                        # Exact channel match — compatible.
                        skip_native = True
                    # else: existing_channel is None or differs from
                    # target_channel — fall through to lookup.

                if not skip_native:
                    # Look up stored native refs for the target event.
                    try:
                        list_native_refs = cast(
                            Callable[[str], Awaitable[list[NativeMessageRef]]],
                            list_fn,
                        )
                        refs = await list_native_refs(target_event_id)
                    except Exception:
                        self._log.debug(
                            "Failed to enrich relation native ref for "
                            "target_event_id=%s target_adapter=%s "
                            "relation_type=%s",
                            current_rel.target_event_id,
                            target_adapter,
                            current_rel.relation_type,
                            exc_info=True,
                        )
                        refs = []

                    # Find ref matching target adapter.
                    if target_channel is not None:
                        # When target_channel is specified, only accept
                        # exact channel match — no adapter-only fallback.
                        matching = None
                        for nref in refs:
                            if (
                                nref.adapter == target_adapter
                                and nref.native_channel_id == target_channel
                            ):
                                matching = nref
                                break
                    else:
                        # Without target_channel, fall back to adapter-only.
                        matching = None
                        for nref in refs:
                            if nref.adapter == target_adapter:
                                matching = nref
                                break

                    if matching is not None:
                        enriched_native_ref = NativeRef(
                            adapter=matching.adapter,
                            native_channel_id=matching.native_channel_id,
                            native_message_id=matching.native_message_id,
                            native_thread_id=matching.native_thread_id,
                        )
                        current_rel = EventRelation(
                            relation_type=current_rel.relation_type,
                            target_event_id=target_event_id,
                            target_native_ref=enriched_native_ref,
                            key=current_rel.key,
                            fallback_text=current_rel.fallback_text,
                            metadata=(
                                dict(current_rel.metadata)
                                if current_rel.metadata
                                else {}
                            ),
                        )
                        native_ref_changed = True

                    # No exact match found — strip incompatible ref if it
                    # belongs to the right adapter but wrong/unknown channel.
                    if matching is None and target_channel is not None:
                        existing_ref = current_rel.target_native_ref
                        if (
                            existing_ref is not None
                            and existing_ref.adapter == target_adapter
                            and existing_ref.native_channel_id != target_channel
                        ):
                            current_rel = EventRelation(
                                relation_type=current_rel.relation_type,
                                target_event_id=target_event_id,
                                target_native_ref=None,
                                key=current_rel.key,
                                fallback_text=current_rel.fallback_text,
                                metadata=(
                                    dict(current_rel.metadata)
                                    if current_rel.metadata
                                    else {}
                                ),
                            )
                            native_ref_changed = True

            # -- Phase 2: Text enrichment --------------------------------------
            # Extract original text from the target event to populate
            # fallback_text and metadata["original_text"] when missing.
            # This runs regardless of whether native-ref enrichment succeeded.
            text_changed = False
            _cur_meta = current_rel.metadata or {}
            if callable(get_fn) and (
                not current_rel.fallback_text
                or not _cur_meta.get("original_text")
                or not _cur_meta.get("original_sender_displayname")
                or not _cur_meta.get("original_sender")
            ):
                try:
                    target_event = await cast(
                        Callable[[str], Awaitable[object]],
                        get_fn,
                    )(target_event_id)
                    if target_event is not None:
                        target_payload = getattr(target_event, "payload", None)
                        extracted_text: str | None = None
                        if isinstance(target_payload, dict):
                            raw = target_payload.get("body", target_payload.get("text"))
                            if raw is not None:
                                extracted_text = (
                                    str(raw) if not isinstance(raw, str) else raw
                                )
                        if extracted_text:
                            new_fallback = (
                                current_rel.fallback_text
                                if current_rel.fallback_text
                                else extracted_text
                            )
                            new_meta = (
                                dict(current_rel.metadata)
                                if current_rel.metadata
                                else {}
                            )
                            if "original_text" not in new_meta:
                                new_meta["original_text"] = extracted_text
                            current_rel = EventRelation(
                                relation_type=current_rel.relation_type,
                                target_event_id=target_event_id,
                                target_native_ref=current_rel.target_native_ref,
                                key=current_rel.key,
                                fallback_text=new_fallback,
                                metadata=new_meta,
                            )
                            text_changed = True

                        # -- Sender info enrichment ---------------------------------
                        sender_meta = (
                            dict(current_rel.metadata) if current_rel.metadata else {}
                        )
                        sender_changed = False

                        # Compute target native metadata once for both fields.
                        target_data = None
                        _tn = getattr(target_event, "metadata", None)
                        if _tn is not None:
                            _tn = getattr(_tn, "native", None)
                        if _tn is not None:
                            _td = getattr(_tn, "data", None)
                            if isinstance(_td, dict):
                                target_data = _td

                        if "original_sender_displayname" not in sender_meta:
                            _dn = None
                            if target_data is not None:
                                _dn = target_data.get("displayname") or target_data.get(
                                    "longname"
                                )
                            if _dn:
                                sender_meta["original_sender_displayname"] = str(_dn)
                                sender_changed = True

                        if "original_sender" not in sender_meta:
                            _snd = None
                            if target_data is not None:
                                _snd = target_data.get("sender")
                            if not _snd:
                                _snd = getattr(
                                    target_event,
                                    "source_transport_id",
                                    None,
                                )
                            if _snd:
                                sender_meta["original_sender"] = str(_snd)
                                sender_changed = True

                        if sender_changed:
                            current_rel = EventRelation(
                                relation_type=current_rel.relation_type,
                                target_event_id=target_event_id,
                                target_native_ref=current_rel.target_native_ref,
                                key=current_rel.key,
                                fallback_text=current_rel.fallback_text,
                                metadata=sender_meta,
                            )
                            text_changed = True
                except Exception:
                    self._log.debug(
                        "Failed to enrich relation text for "
                        "target_event_id=%s target_adapter=%s "
                        "relation_type=%s",
                        current_rel.target_event_id,
                        target_adapter,
                        current_rel.relation_type,
                        exc_info=True,
                    )

            new_relations.append(current_rel)
            if native_ref_changed or text_changed:
                changed = True

        if not changed:
            return event

        return msgspec.structs.replace(event, relations=tuple(new_relations))
