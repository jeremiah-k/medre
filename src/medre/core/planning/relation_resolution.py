"""Relation resolution for cross-adapter event linking.

The :class:`RelationResolver` resolves :class:`EventRelation` objects
whose ``target_native_ref`` is known but whose ``target_event_id`` has
not yet been resolved to a canonical ID.  It also produces new
canonical events that represent a relation to a native-space target.

This module depends on the core event types only – it accepts a storage
protocol object (anything with a ``resolve_native_ref`` method) but does
not import from the storage package.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
    NativeRef,
)
from medre.core.events.metadata import EventMetadata


# ---------------------------------------------------------------------------
# Relation resolver
# ---------------------------------------------------------------------------


class RelationResolver:
    """Resolve and create event relations across adapter boundaries.

    Parameters
    ----------
    storage:
        An object providing a ``resolve_native_ref`` async method that
        maps a :class:`NativeRef` to a :class:`CanonicalEvent`.  The
        storage layer is not imported directly to keep this module
        decoupled from the storage implementation.

    Example
    -------
    >>> resolver = RelationResolver(storage=my_store)
    >>> resolved = await resolver.resolve_relation(unresolved_relation)
    """

    def __init__(self, storage: object) -> None:
        self._storage = storage

    # -- Async API --------------------------------------------------------

    async def resolve_relation(
        self,
        relation: EventRelation,
    ) -> EventRelation:
        """Resolve a relation's native reference to a canonical event ID.

        If the relation already has a ``target_event_id``, it is returned
        unchanged.  Otherwise, ``resolve_native_ref`` is called on the
        storage backend to look up the canonical event, and a new
        :class:`EventRelation` with the resolved ID is returned.

        Parameters
        ----------
        relation:
            The relation to resolve.

        Returns
        -------
        EventRelation
            A new relation with ``target_event_id`` populated, or the
            original relation if it was already resolved.

        Raises
        ------
        ValueError
            If the relation has no ``target_native_ref`` and no
            ``target_event_id``.
        """
        if relation.target_event_id is not None:
            return relation

        if relation.target_native_ref is None:
            raise ValueError(
                "Relation must have either target_event_id or "
                "target_native_ref to be resolved"
            )

        resolve_fn = getattr(self._storage, "resolve_native_ref", None)
        if resolve_fn is None:
            raise AttributeError(
                "Storage object must provide a 'resolve_native_ref' method"
            )

        resolved_event: CanonicalEvent | None = await resolve_fn(
            relation.target_native_ref,
        )

        if resolved_event is None:
            return relation

        return EventRelation(
            relation_type=relation.relation_type,
            target_event_id=resolved_event.event_id,
            target_native_ref=relation.target_native_ref,
            key=relation.key,
            fallback_text=relation.fallback_text,
            metadata=relation.metadata,
        )

    async def create_relation_event(
        self,
        source_event: CanonicalEvent,
        relation_type: str,
        target_native_ref: NativeRef,
        key: str | None = None,
    ) -> CanonicalEvent:
        """Create a new canonical event representing a relation.

        This is used when an adapter reports a relation (e.g. a reaction)
        that needs to be recorded as a first-class canonical event.

        Parameters
        ----------
        source_event:
            The event that the new relation refers *from*.
        relation_type:
            The kind of relation (``"reply"``, ``"reaction"``, etc.).
        target_native_ref:
            Native-space reference to the target of the relation.
        key:
            Optional discriminator (e.g. the emoji for a reaction).

        Returns
        -------
        CanonicalEvent
            A new event with the relation embedded and lineage
            metadata pointing to *source_event*.
        """
        relation = EventRelation(
            relation_type=relation_type,  # type: ignore[arg-type]
            target_event_id=None,
            target_native_ref=target_native_ref,
            key=key,
            fallback_text=None,
        )

        now = datetime.now(tz=timezone.utc)
        new_event_id = str(uuid.uuid4())

        return CanonicalEvent(
            event_id=new_event_id,
            event_kind=source_event.event_kind,
            schema_version=source_event.schema_version,
            timestamp=now,
            source_adapter=source_event.source_adapter,
            source_transport_id=source_event.source_transport_id,
            source_channel_id=source_event.source_channel_id,
            parent_event_id=source_event.event_id,
            lineage=(*source_event.lineage, source_event.event_id),
            relations=(relation,),
            payload=source_event.payload,
            metadata=EventMetadata(),
            depth=source_event.depth + 1,
            trace_id=source_event.trace_id,
        )
