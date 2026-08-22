"""Conversation identity authorities for ingress evidence and current ancestry.

Two deliberately separate concepts live here:

* :class:`ConversationGraphAuthority` assigns ``root_event_id`` and
  ``conversation_id`` once during ingress, before the canonical event is
  persisted.  Those fields are immutable historical snapshots of what was
  resolvable at admission time.
* :class:`ConversationProjectionService` derives current conversation
  membership from immutable canonical events, relation rows, and native-ref
  mappings.  Its projection is mutable, idempotent, and rebuildable so late
  parents and native targets converge without rewriting evidence.

For now ``conversation_id`` equals ``root_event_id`` in both representations.
Cross-transport grouping or merged-conversation semantics remain intentionally
out of scope until this ancestry projection is stable.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import msgspec

from medre.core.events.canonical import CanonicalEvent, NativeMessageRef, NativeRef
from medre.core.storage.backend import (
    ConversationMembership,
    ConversationProjectionState,
)

_logger = logging.getLogger(__name__)

# Maximum depth for ancestor walk to prevent runaway recursion.
_MAX_WALK_DEPTH = 64
_CONVERSATION_PROJECTION_REVISION = 1
_REBUILD_PAGE_SIZE = 256
_REBUILD_MEMBERSHIP_CACHE_SIZE = 256

_EventGetFn = Callable[[str], Awaitable[CanonicalEvent | None]]


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

        .. note::

           ``conversation_id`` is always set equal to ``root_event_id``.
           Ancestor ``conversation_id`` values are not independently
           propagated — the ancestor walk reads only ``root_event_id``,
           never the ancestor's own ``conversation_id``.  Future divergence
           (merged threads, cross-transport grouping) would require a
           separate authority rule; do not implement divergence here.

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
            The original event when it already has ``root_event_id`` set
            (and ``conversation_id`` matches), or a new event with
            ``root_event_id`` preserved and ``conversation_id`` filled.
            When ``root_event_id`` is already set, relation-walking is
            skipped entirely so the existing root is never overwritten.
        """
        # Fast path: root_event_id already set (e.g. replay or derived event).
        # Do NOT fall through to relation-walking, which could overwrite an
        # existing root from relation targets.  Instead, ensure conversation_id
        # is consistent with the preserved root_event_id.
        if event.root_event_id is not None:
            return self._assign_identity(event, event.root_event_id)

        get_fn = cached_get_fn or getattr(self._storage, "get", None)

        # Iterate through all resolved relations to find a target that
        # exists in storage.  Only self-root when every relation target
        # is missing.
        #
        # Root selection rule: the *first* relation with a resolved
        # ``target_event_id`` that is present in storage wins.  If an
        # event carries multiple relations (e.g. reply + reaction), only
        # the first stored target is walked.  This is intentional — it
        # keeps root selection deterministic and avoids ambiguity when
        # different relation targets point to different conversation
        # roots.
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
        """Fetch an event by ID.

        Returns ``None`` when *get_fn* is unavailable or the event is not
        found in storage.  Storage errors (transient failures, corruption,
        etc.) are **not** caught here — they propagate to the caller so
        that retry / circuit-breaker logic can act on them.  Swallowing
        exceptions would silently turn every storage error into a
        "missing event" self-root, which is incorrect.
        """
        if get_fn is None or not callable(get_fn):
            return None
        return await get_fn(event_id)

@dataclass(frozen=True)
class ConversationRepairResult:
    """Result of reconciling one event against current relation facts."""

    membership: ConversationMembership
    changed_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class ConversationRebuildSummary:
    """Deterministic summary of a storage-wide projection rebuild."""

    scanned_events: int
    changed_events: int
    skipped_current: bool


class ConversationProjectionStorage(Protocol):
    """Narrow storage surface required by the conversation projection."""

    async def get(self, event_id: str) -> CanonicalEvent | None: ...

    async def list_event_ids_page(
        self, *, after_event_id: str | None, limit: int
    ) -> list[str]: ...

    async def resolve_native_ref(
        self,
        adapter: str,
        native_channel_id: str | None,
        native_message_id: str,
    ) -> str | None: ...

    async def list_native_refs_for_event(
        self, event_id: str
    ) -> list[NativeMessageRef]: ...

    async def list_relation_sources(self, target_event_id: str) -> list[str]: ...

    async def list_relation_sources_for_native_ref(
        self,
        adapter: str,
        native_channel_id: str | None,
        native_message_id: str,
    ) -> list[str]: ...

    async def put_conversation_membership(
        self, membership: ConversationMembership
    ) -> bool: ...

    async def get_conversation_membership(
        self, event_id: str
    ) -> ConversationMembership | None: ...

    async def get_conversation_projection_state(
        self,
    ) -> ConversationProjectionState | None: ...

    async def put_conversation_projection_state(
        self, state: ConversationProjectionState
    ) -> None: ...


class ConversationProjectionService:
    """Own the rebuildable current-state view of conversation ancestry.

    Canonical events and their inline relations remain append-only ingress
    evidence.  This service resolves those facts against the *current* native
    reference map and writes only :class:`ConversationMembership` projection
    rows.  Late parents therefore repair derived membership without rewriting
    historical canonical events.

    The selected conversational parent follows the same deterministic rule as
    ingress identity assignment: the first relation whose target can currently
    be resolved to a stored canonical event wins.  A relation with an explicit
    ``target_event_id`` never falls back to its native reference; the explicit
    canonical target is authoritative and remains unresolved until that event
    exists.
    """

    def __init__(
        self,
        storage: ConversationProjectionStorage,
        logger: logging.Logger | None = None,
    ) -> None:
        self._storage = storage
        self._log = logger or _logger
        # Projection repair is a process-local authority. Canonical/native fact
        # writes may proceed concurrently, but derived graph recomputation must
        # serialize so an older calculation cannot overwrite a newer one after
        # all corresponding repair calls have completed.
        self._repair_lock = asyncio.Lock()

    async def rebuild_all(self) -> ConversationRebuildSummary:
        """Recompute the complete projection from immutable stored evidence.

        A clean marker for the current projection revision skips redundant
        startup work.  Dirty, interrupted, or older-revision state triggers a
        complete rebuild.  Event IDs are paged, canonical-event caches are
        released after each ancestry chain, and the small membership cache is
        pruned so the working set does not grow with the full store.
        """
        async with self._repair_lock:
            prior = await self._storage.get_conversation_projection_state()
            if (
                prior is not None
                and prior.projection_revision == _CONVERSATION_PROJECTION_REVISION
                and prior.status == "clean"
            ):
                await self._storage.put_conversation_projection_state(
                    ConversationProjectionState(
                        projection_revision=_CONVERSATION_PROJECTION_REVISION,
                        status="dirty",
                    )
                )
                return ConversationRebuildSummary(
                    scanned_events=0,
                    changed_events=0,
                    skipped_current=True,
                )

            cursor = (
                prior.last_event_id
                if prior is not None
                and prior.projection_revision == _CONVERSATION_PROJECTION_REVISION
                and prior.status == "rebuilding"
                else None
            )
            await self._storage.put_conversation_projection_state(
                ConversationProjectionState(
                    projection_revision=_CONVERSATION_PROJECTION_REVISION,
                    status="rebuilding",
                    last_event_id=cursor,
                )
            )

            scanned = 0
            changed = 0
            membership_cache: dict[str, ConversationMembership] = {}
            while True:
                event_ids = await self._storage.list_event_ids_page(
                    after_event_id=cursor,
                    limit=_REBUILD_PAGE_SIZE,
                )
                if not event_ids:
                    break

                for event_id in event_ids:
                    cache = dict(membership_cache)
                    previously_cached = set(cache)
                    event_cache: dict[str, CanonicalEvent | None] = {}
                    await self._resolve_membership(
                        event_id,
                        cache=cache,
                        event_cache=event_cache,
                    )
                    for resolved_id in sorted(cache.keys() - previously_cached):
                        membership = cache[resolved_id]
                        if await self._storage.put_conversation_membership(membership):
                            changed += 1
                        membership_cache[resolved_id] = membership

                    while len(membership_cache) > _REBUILD_MEMBERSHIP_CACHE_SIZE:
                        oldest = next(iter(membership_cache))
                        membership_cache.pop(oldest)
                    scanned += 1

                cursor = event_ids[-1]
                await self._storage.put_conversation_projection_state(
                    ConversationProjectionState(
                        projection_revision=_CONVERSATION_PROJECTION_REVISION,
                        status="rebuilding",
                        last_event_id=cursor,
                    )
                )

            await self._storage.put_conversation_projection_state(
                ConversationProjectionState(
                    projection_revision=_CONVERSATION_PROJECTION_REVISION,
                    status="dirty",
                )
            )
            return ConversationRebuildSummary(
                scanned_events=scanned,
                changed_events=changed,
                skipped_current=False,
            )

    async def mark_clean(self) -> None:
        """Record that an orderly shutdown left the projection current."""
        async with self._repair_lock:
            await self._storage.put_conversation_projection_state(
                ConversationProjectionState(
                    projection_revision=_CONVERSATION_PROJECTION_REVISION,
                    status="clean",
                )
            )

    async def reconcile_event(
        self, event_id: str, *, get_fn: _EventGetFn | None = None
    ) -> ConversationRepairResult:
        """Recompute *event_id* and any ancestors/cycle peers needed by it."""
        async with self._repair_lock:
            return await self._reconcile_event_unlocked(event_id, get_fn=get_fn)

    async def _reconcile_event_unlocked(
        self,
        event_id: str,
        *,
        get_fn: _EventGetFn | None = None,
        membership_cache: dict[str, ConversationMembership] | None = None,
        event_cache: dict[str, CanonicalEvent | None] | None = None,
    ) -> ConversationRepairResult:
        """Reconcile one event while the caller owns ``_repair_lock``."""
        cache = membership_cache if membership_cache is not None else {}
        events = event_cache if event_cache is not None else {}
        previously_cached = set(cache)
        membership = await self._resolve_membership(
            event_id, cache=cache, event_cache=events, get_fn=get_fn
        )
        changed: list[str] = []
        for resolved_id in sorted(cache.keys() - previously_cached):
            if await self._storage.put_conversation_membership(cache[resolved_id]):
                changed.append(resolved_id)
        return ConversationRepairResult(
            membership=membership,
            changed_event_ids=tuple(changed),
        )

    async def repair_after_event_available(
        self, event_id: str, *, get_fn: _EventGetFn | None = None
    ) -> ConversationRepairResult:
        """Repair *event_id* and all relation dependents reachable from it.

        Reverse canonical-target traversal repairs children that already knew a
        canonical target ID.  Reverse native-target traversal repairs children
        whose relation was admitted before the target native identity existed.
        Each source is reconciled at most once per repair run; recursive
        ancestry resolution means it observes the final current facts when it
        is processed.
        """
        async with self._repair_lock:
            pending: deque[str] = deque([event_id])
            queued: set[str] = {event_id}
            processed: set[str] = set()
            changed: set[str] = set()
            requested_membership: ConversationMembership | None = None
            membership_cache: dict[str, ConversationMembership] = {}
            event_cache: dict[str, CanonicalEvent | None] = {}

            while pending:
                current = pending.popleft()
                processed.add(current)
                result = await self._reconcile_event_unlocked(
                    current,
                    get_fn=get_fn,
                    membership_cache=membership_cache,
                    event_cache=event_cache,
                )
                if current == event_id:
                    requested_membership = result.membership
                changed.update(result.changed_event_ids)

                # The newly available event itself is always an anchor, even when
                # its own projection row was already correct: its newly stored
                # canonical/native identity may resolve pre-existing child edges.
                anchors = set(result.changed_event_ids)
                anchors.add(current)
                for anchor in sorted(anchors):
                    for source_id in await self._dependent_sources(anchor):
                        if source_id in processed or source_id in queued:
                            continue
                        pending.append(source_id)
                        queued.add(source_id)

            if requested_membership is None:
                raise RuntimeError(
                    f"conversation repair produced no membership for {event_id!r}"
                )
            return ConversationRepairResult(
                membership=requested_membership,
                changed_event_ids=tuple(sorted(changed)),
            )

    async def repair_after_native_ref_available(self, event_id: str) -> tuple[str, ...]:
        """Repair only dependents unblocked by a new native identity.

        Persisting another native reference for *event_id* cannot change that
        event's own ancestry; it can only make pre-existing native-target
        relations from other events resolvable.  Starting from those native
        dependents avoids re-reading and rewriting the anchor event after every
        successful outbound send while still propagating any resulting root
        changes through canonical or native descendants.
        """
        async with self._repair_lock:
            initial = await self._native_dependent_sources(event_id)
            return await self._repair_dependents_unlocked(initial)

    async def _repair_dependents_unlocked(
        self, initial_event_ids: list[str]
    ) -> tuple[str, ...]:
        """Reconcile a dependent frontier while ``_repair_lock`` is held."""
        pending: deque[str] = deque(initial_event_ids)
        queued: set[str] = set(initial_event_ids)
        processed: set[str] = set()
        changed: set[str] = set()
        membership_cache: dict[str, ConversationMembership] = {}
        event_cache: dict[str, CanonicalEvent | None] = {}

        while pending:
            current = pending.popleft()
            processed.add(current)
            result = await self._reconcile_event_unlocked(
                current,
                membership_cache=membership_cache,
                event_cache=event_cache,
            )
            changed.update(result.changed_event_ids)

            anchors = set(result.changed_event_ids)
            anchors.add(current)
            for anchor in sorted(anchors):
                for source_id in await self._dependent_sources(anchor):
                    if source_id in processed or source_id in queued:
                        continue
                    pending.append(source_id)
                    queued.add(source_id)

        return tuple(sorted(changed))

    async def project_event(self, event: CanonicalEvent) -> CanonicalEvent:
        """Overlay persisted current membership on an in-memory event copy.

        Callers that have just admitted a new relation fact must reconcile the
        projection first.  This method deliberately does not repeat a graph
        walk when a membership row already exists; it is the cheap projection
        boundary used immediately before routing/rendering.  A missing row is
        repaired defensively so direct callers cannot consume an unprojected
        event.
        """
        membership = await self._storage.get_conversation_membership(event.event_id)
        if membership is None:
            membership = (await self.reconcile_event(event.event_id)).membership
        if (
            event.root_event_id == membership.root_event_id
            and event.conversation_id == membership.conversation_id
        ):
            return event
        return msgspec.structs.replace(
            event,
            root_event_id=membership.root_event_id,
            conversation_id=membership.conversation_id,
        )

    async def _resolve_membership(
        self,
        event_id: str,
        *,
        cache: dict[str, ConversationMembership],
        event_cache: dict[str, CanonicalEvent | None],
        get_fn: _EventGetFn | None = None,
    ) -> ConversationMembership:
        """Resolve one functional ancestry chain without Python recursion.

        Each event selects at most one currently resolvable parent.  Walking
        iteratively avoids recursion-depth failures on long conversations and
        makes cycle detection explicit.  ``cache`` may be shared by a full
        rebuild so every event is solved at most once in that pass.
        """
        if event_id in cache:
            return cache[event_id]

        path: list[str] = []
        positions: dict[str, int] = {}
        edges: dict[str, tuple[str | None, str | None, bool]] = {}
        current = event_id

        while current not in cache:
            cycle_at = positions.get(current)
            if cycle_at is not None:
                cycle = path[cycle_at:]
                cycle_root = min(cycle)
                for cycle_id in cycle:
                    parent_id, relation_type, _ = edges[cycle_id]
                    cache[cycle_id] = ConversationMembership(
                        event_id=cycle_id,
                        root_event_id=cycle_root,
                        conversation_id=cycle_root,
                        resolved_target_event_id=parent_id,
                        relation_type=relation_type,
                        depth=0,
                        resolution_state="cycle",
                    )
                break

            event = await self._get_event(
                current, event_cache=event_cache, get_fn=get_fn
            )
            if event is None:
                raise ValueError(
                    f"cannot project missing canonical event: {current!r}"
                )

            parent_id, relation_type, has_dependency = await self._select_parent(
                event, event_cache=event_cache, get_fn=get_fn
            )
            positions[current] = len(path)
            path.append(current)
            edges[current] = (parent_id, relation_type, has_dependency)

            if parent_id is None:
                cache[current] = ConversationMembership(
                    event_id=current,
                    root_event_id=current,
                    conversation_id=current,
                    resolved_target_event_id=None,
                    relation_type=relation_type,
                    depth=0,
                    resolution_state="unresolved" if has_dependency else "root",
                )
                break
            current = parent_id

        for node_id in reversed(path):
            if node_id in cache:
                continue
            parent_id, relation_type, _ = edges[node_id]
            if parent_id is None:
                raise RuntimeError(
                    f"conversation projection lost terminal edge for {node_id!r}"
                )
            parent = cache.get(parent_id)
            if parent is None:
                raise RuntimeError(
                    "conversation projection could not resolve parent "
                    f"{parent_id!r} for {node_id!r}"
                )
            state = (
                "unresolved"
                if parent.resolution_state == "unresolved"
                else "resolved"
            )
            cache[node_id] = ConversationMembership(
                event_id=node_id,
                root_event_id=parent.root_event_id,
                conversation_id=parent.conversation_id,
                resolved_target_event_id=parent_id,
                relation_type=relation_type,
                depth=parent.depth + 1,
                resolution_state=state,
            )

        membership = cache.get(event_id)
        if membership is None:
            raise RuntimeError(
                f"conversation projection produced no membership for {event_id!r}"
            )
        return membership

    async def _select_parent(
        self,
        event: CanonicalEvent,
        *,
        event_cache: dict[str, CanonicalEvent | None],
        get_fn: _EventGetFn | None,
    ) -> tuple[str | None, str | None, bool]:
        """Return the first currently resolvable conversational parent."""
        has_dependency = False
        first_dependency_type: str | None = None
        for relation in event.relations:
            target_id: str | None = None
            if relation.target_event_id is not None:
                has_dependency = True
                first_dependency_type = first_dependency_type or relation.relation_type
                if (
                    await self._get_event(
                        relation.target_event_id,
                        event_cache=event_cache,
                        get_fn=get_fn,
                    )
                    is not None
                ):
                    target_id = relation.target_event_id
            elif relation.target_native_ref is not None:
                has_dependency = True
                first_dependency_type = first_dependency_type or relation.relation_type
                ref: NativeRef = relation.target_native_ref
                resolved = await self._storage.resolve_native_ref(
                    ref.adapter,
                    ref.native_channel_id,
                    ref.native_message_id,
                )
                if resolved is not None and (
                    await self._get_event(
                        resolved, event_cache=event_cache, get_fn=get_fn
                    )
                    is not None
                ):
                    target_id = resolved

            if target_id is not None:
                return target_id, relation.relation_type, True
        return None, first_dependency_type, has_dependency

    async def _get_event(
        self,
        event_id: str,
        *,
        event_cache: dict[str, CanonicalEvent | None],
        get_fn: _EventGetFn | None,
    ) -> CanonicalEvent | None:
        """Fetch one canonical event once per projection calculation."""
        if event_id in event_cache:
            return event_cache[event_id]
        fetch = get_fn or self._storage.get
        event = await fetch(event_id)
        event_cache[event_id] = event
        return event

    async def _native_dependent_sources(self, event_id: str) -> list[str]:
        """Return sources whose native relation may resolve to *event_id*."""
        ordered: list[str] = []
        seen: set[str] = set()
        for ref in await self._storage.list_native_refs_for_event(event_id):
            sources = await self._storage.list_relation_sources_for_native_ref(
                ref.adapter,
                ref.native_channel_id,
                ref.native_message_id,
            )
            for source_id in sources:
                if source_id not in seen:
                    ordered.append(source_id)
                    seen.add(source_id)
        return ordered

    async def _dependent_sources(self, event_id: str) -> list[str]:
        """Return deterministic reverse relation sources for one event."""
        ordered: list[str] = []
        seen: set[str] = set()

        for source_id in await self._storage.list_relation_sources(event_id):
            if source_id not in seen:
                ordered.append(source_id)
                seen.add(source_id)

        for source_id in await self._native_dependent_sources(event_id):
            if source_id not in seen:
                ordered.append(source_id)
                seen.add(source_id)
        return ordered
