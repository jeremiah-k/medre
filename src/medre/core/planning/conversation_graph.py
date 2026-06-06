"""Conversation graph authority for assigning stable conversation identity.

This module provides :class:`ConversationGraphAuthority`, an internal helper
that computes ``root_event_id`` and ``conversation_id`` for inbound events
after relation resolution has populated ``target_event_id`` on event
relations, but before the event is persisted to storage.

Algorithm
---------
1. **No resolved relation target**: the event is a conversation root.
   ``root_event_id = conversation_id = event.event_id``.

2. **Relation target has ``root_event_id``/``conversation_id`` already**:
   inherit them directly (fast path for previously-stored ancestors).

3. **Relation target lacks identity fields**: recursively walk the target
   event's own relations via ``storage.get()`` to find an ancestor that
   carries ``root_event_id``.  A ``visited`` set bounds the walk to
   prevent infinite loops on cyclic relation graphs.

4. **Target event not found in storage** (or walk exhausts without a root):
   degrade safely — the current event becomes its own root.

For now ``conversation_id`` always equals ``root_event_id``.

This class is **not** part of the public API.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

import msgspec

from medre.core.events.canonical import CanonicalEvent

_logger = logging.getLogger(__name__)

# Maximum depth for ancestor walk to prevent runaway recursion.
_MAX_WALK_DEPTH = 64


class ConversationGraphAuthority:
    """Assign ``root_event_id`` and ``conversation_id`` on ingress.

    Parameters
    ----------
    storage:
        Duck-typed storage backend.  Must support ``get(event_id)`` when
        available (used for ancestor lookups).
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

    async def resolve_conversation_identity(
        self,
        event: CanonicalEvent,
        *,
        cached_get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
    ) -> CanonicalEvent:
        """Compute and assign ``root_event_id`` and ``conversation_id``.

        Called after :meth:`RelationResolver.resolve_event_relations` has
        populated ``target_event_id`` on the event's relations, and before
        the event is stored.

        Parameters
        ----------
        event:
            The inbound canonical event whose identity is being resolved.
        cached_get_fn:
            Optional memoized ``storage.get`` callable.  When provided,
            used instead of ``getattr(storage, "get")`` so callers can
            share lookups across a single ingress pass.

        Returns
        -------
        CanonicalEvent
            The original event when it already has ``root_event_id`` set,
            or a new event with ``root_event_id`` and ``conversation_id``
            populated.
        """
        # Fast path: already assigned (e.g. replay or derived event).
        if event.root_event_id is not None and event.conversation_id is not None:
            return event

        get_fn = cached_get_fn or getattr(self._storage, "get", None)

        # Iterate through all resolved relations to find a target that
        # exists in storage.  Only self-root when every relation target
        # is missing.
        if event.relations:
            for rel in event.relations:
                if rel.target_event_id is None:
                    continue
                target_event = await self._safe_get(rel.target_event_id, get_fn=get_fn)
                if target_event is not None:
                    # Walk from the resolved target to find the root.
                    root_id = await self._resolve_root_from(
                        target_event,
                        get_fn=get_fn,
                        visited=set(),
                        depth=0,
                    )
                    return self._assign_identity(event, root_id)

        # No resolved relation target, or all targets missing → this event
        # is its own root.
        return self._assign_identity(event, event.event_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assign_identity(self, event: CanonicalEvent, root_id: str) -> CanonicalEvent:
        """Return a new event with root_event_id and conversation_id set."""
        if event.root_event_id == root_id and event.conversation_id == root_id:
            return event
        return msgspec.structs.replace(
            event,
            root_event_id=root_id,
            conversation_id=root_id,
        )

    async def _resolve_root_from(
        self,
        event: CanonicalEvent,
        *,
        get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None,
        visited: set[str],
        depth: int,
    ) -> str:
        """Walk ancestors from an already-fetched event to find the root.

        Returns the resolved root event ID, falling back to *event.event_id*
        when no root can be found.
        """
        # Cycle / depth guard.
        if event.event_id in visited or depth >= _MAX_WALK_DEPTH:
            self._log.debug(
                "conversation walk: cycle or depth limit hit at %s "
                "(depth=%d, visited=%d)",
                event.event_id,
                depth,
                len(visited),
            )
            return event.event_id

        visited.add(event.event_id)

        # Fast path: target already has root_event_id.
        if event.root_event_id is not None:
            return event.root_event_id

        # Target has no root — walk its relations.  Try each relation
        # and continue to the next if the parent is missing, rather than
        # self-rooting on the first missing parent.
        if event.relations:
            for rel in event.relations:
                if rel.target_event_id is None:
                    continue
                parent = await self._safe_get(rel.target_event_id, get_fn=get_fn)
                if parent is not None:
                    return await self._resolve_root_from(
                        parent,
                        get_fn=get_fn,
                        visited=visited,
                        depth=depth + 1,
                    )
                # Parent not found for this relation — try the next one.

        # Target has no relations and no root — it is the root.
        return event.event_id

    async def _safe_get(
        self,
        event_id: str,
        *,
        get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None,
    ) -> CanonicalEvent | None:
        """Fetch an event, returning None on any failure."""
        if get_fn is None or not callable(get_fn):
            return None
        try:
            return await get_fn(event_id)
        except Exception:
            self._log.debug(
                "conversation walk: failed to fetch event %s",
                event_id,
                exc_info=True,
            )
            return None
