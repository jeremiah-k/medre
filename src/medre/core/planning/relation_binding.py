"""Destination-scoped relation binding authority.

This module owns :class:`RelationBindingAuthority`, the single authority that
resolves an :class:`~medre.core.events.canonical.EventRelation` against a
specific delivery destination (exact adapter instance + exact destination
native context) and — for mutation relations — decides mutation eligibility.

Binding is deliberately conservative and destination-scoped:

* Candidates are STORED ``NativeMessageRef`` records only.  Relation
  ``metadata`` and any other wire/user-supplied data are never inputs.
* When the destination context is known, candidates must match the exact
  ``(adapter, native_channel_id)`` pair; adapter-only matching is allowed
  only when no destination context is known (legacy replay contexts).
* Identical ``(native_channel_id, native_message_id)`` tuples are deduped;
  more than one DISTINCT tuple is ``ambiguous`` — core never guesses.
* Mutation relations (``edit`` / ``delete``) additionally require the
  ``bound_owned`` proof: the target canonical event is stored, the original
  identity facts (``source_adapter``, ``source_transport_id``,
  ``source_channel_id``) are present/non-empty on the original AND the
  mutation event and pairwise equal, exactly one distinct OUTBOUND candidate
  exists, and no storage read failed anywhere in the decision.
* Any storage read failure is ``binding_unavailable`` — fail closed, never
  authorizing.

Results are :class:`~medre.core.events.canonical.RelationTargetFact` values
carrying stable snake_case reason codes that distinguish inability-to-bind
(``unresolved_target`` / ``out_of_scope`` / ``ambiguous`` /
``binding_unavailable``) from authorization failure (``not_authorized``).

This module is **not** part of the public API and contains no
transport-specific branches or identifiers.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, cast

from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
    NativeMessageRef,
    RelationTargetFact,
)
from medre.core.events.kinds import EventKind

_logger = logging.getLogger(__name__)

#: Relation types whose delivery is a mutation of an existing native message.
MUTATION_RELATION_TYPES: frozenset[str] = frozenset({"edit", "delete"})

#: Event kinds whose PRIMARY semantic is a mutation of an existing message.
MUTATION_EVENT_KINDS: frozenset[str] = frozenset(
    {EventKind.MESSAGE_EDITED, EventKind.MESSAGE_DELETED}
)

# ---------------------------------------------------------------------------
# Stable reason codes (snake_case, never localized, safe for machines)
# ---------------------------------------------------------------------------

REASON_NO_TARGET_EVENT_ID = "unresolved_target:no_target_event_id"
REASON_TARGET_NOT_STORED = "unresolved_target:target_event_not_stored"
REASON_NO_IN_SCOPE_REFS = "out_of_scope:no_native_refs_in_destination"
REASON_MULTIPLE_DISTINCT_TARGETS = "ambiguous:multiple_distinct_native_targets"
REASON_STORAGE_READ_FAILED = "binding_unavailable:storage_read_failed"
REASON_REF_READ_UNAVAILABLE = "binding_unavailable:ref_read_unavailable"
REASON_TARGET_READ_UNAVAILABLE = "binding_unavailable:target_read_unavailable"
REASON_INBOUND_ONLY_MATCH = "not_authorized:inbound_only_match"
REASON_DIRECTION_CONFLICT = "not_authorized:direction_conflict"
REASON_ORIGINAL_IDENTITY_INCOMPLETE = "not_authorized:original_identity_incomplete"
REASON_MUTATION_IDENTITY_INCOMPLETE = "not_authorized:mutation_identity_incomplete"
REASON_IDENTITY_MISMATCH = "not_authorized:identity_mismatch"

#: Error prefix used by the delivery gate for dynamically unbindable
#: mutation targets.  Distinct from the static ``capability_suppressed:``
#: prefix so operators can tell "capability unsupported" from "cannot bind".
RELATION_TARGET_NOT_BINDABLE_PREFIX = "relation_target_not_bindable"

_GetEventFn = Callable[[str], Awaitable[CanonicalEvent | None]]
_ListNativeRefsFn = Callable[[str], Awaitable[list[NativeMessageRef]]]


def _identity_facts(event: CanonicalEvent) -> tuple[str, str, str | None]:
    """Return the ``(source_adapter, source_transport_id, source_channel_id)`` identity triple."""
    return (
        event.source_adapter,
        event.source_transport_id,
        event.source_channel_id,
    )


def _identity_complete(facts: tuple[str, str, str | None]) -> bool:
    """All three identity facts present and non-empty."""
    adapter, transport_id, channel = facts
    return bool(adapter) and bool(transport_id) and bool(channel)


def _usable_relation_native_ref(
    relation: EventRelation,
    *,
    target_adapter: str,
    target_channel: str | None,
):
    """Return the relation's own native ref when it matches the destination scope.

    A ref is usable when its adapter equals *target_adapter* and — when
    *target_channel* is known — its channel equals *target_channel*.
    """
    ref = relation.target_native_ref
    if ref is None or ref.adapter != target_adapter:
        return None
    if target_channel is not None and ref.native_channel_id != target_channel:
        return None
    return ref


def _group_by_tuple(
    refs: list[NativeMessageRef],
) -> list[list[NativeMessageRef]]:
    """Group refs by identical ``(native_channel_id, native_message_id)`` tuple.

    First-seen order is preserved so fact fields are deterministic.  Records
    sharing one tuple stay grouped so direction conflicts remain detectable.
    """
    seen: dict[tuple[str | None, str], list[NativeMessageRef]] = {}
    for ref in refs:
        key = (ref.native_channel_id, ref.native_message_id)
        seen.setdefault(key, []).append(ref)
    return list(seen.values())


def is_mutation_delivery(event: CanonicalEvent) -> bool:
    """Return ``True`` when *event*'s primary semantic is a native mutation.

    A mutation delivery is a ``message.edited`` / ``message.deleted`` event
    carrying at least one ``edit`` / ``delete`` relation.
    """
    if event.event_kind not in MUTATION_EVENT_KINDS:
        return False
    return any(rel.relation_type in MUTATION_RELATION_TYPES for rel in event.relations)


def mutation_binding_suppression_error(event: CanonicalEvent) -> str | None:
    """Return the suppression error string for an unbound mutation delivery.

    Inspects the in-flight (enriched) *event*'s mutation relations and returns
    ``None`` when every mutation relation carries a ``bound_owned`` fact —
    i.e. the delivery may proceed.  Otherwise returns a stable error string
    ``relation_target_not_bindable:<status>:<reason>`` naming why the target
    could not be bound or authorized in this destination.

    A missing fact is itself fail-closed: an un-enriched mutation relation
    has no authorization evidence, so it suppresses.
    """
    if not is_mutation_delivery(event):
        return None
    for rel in event.relations:
        if rel.relation_type not in MUTATION_RELATION_TYPES:
            continue
        fact = rel.target_fact
        if fact is None:
            return (
                f"{RELATION_TARGET_NOT_BINDABLE_PREFIX}:"
                "binding_unavailable:no_binding_fact"
            )
        if fact.status != "bound_owned":
            reason = fact.reason or fact.status
            return f"{RELATION_TARGET_NOT_BINDABLE_PREFIX}:{reason}"
    return None


class RelationBindingAuthority:
    """Binds relation targets to native destinations and proves ownership.

    Parameters
    ----------
    storage:
        The storage backend (duck-typed).  Uses ``get(event_id)`` and
        ``list_native_refs_for_event(event_id)`` when available.
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

    async def bind(
        self,
        relation: EventRelation,
        *,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None,
        cached_get_fn: _GetEventFn | None = None,
        cached_list_fn: _ListNativeRefsFn | None = None,
    ) -> RelationTargetFact:
        """Compute the destination-scoped :class:`RelationTargetFact`.

        Args:
            relation: The relation whose target is being bound.
            event: The referencing/mutating event; supplies the identity
                facts for the mutation ``bound_owned`` proof.
            target_adapter: EXACT destination adapter instance id.
            target_channel: EXACT destination native context (e.g. room id).
                ``None`` allows adapter-only matching (legacy replay
                contexts).
            cached_get_fn: Optional memoized ``storage.get`` callable.
            cached_list_fn: Optional memoized
                ``storage.list_native_refs_for_event`` callable.

        Returns an immutable fact.  Never mutates stored events or refs.
        """
        get_fn = cached_get_fn or getattr(self._storage, "get", None)
        list_fn = cached_list_fn or getattr(
            self._storage, "list_native_refs_for_event", None
        )

        is_mutation = relation.relation_type in MUTATION_RELATION_TYPES

        # -- No canonical target identity ---------------------------------
        if not relation.target_event_id:
            # The target canonical event is unknown.  A relation-native ref
            # matching this destination can still support REFERENTIAL
            # rendering, but never authorizes a mutation: bound_owned
            # requires the stored original.
            usable_ref = _usable_relation_native_ref(
                relation,
                target_adapter=target_adapter,
                target_channel=target_channel,
            )
            if usable_ref is not None and not is_mutation:
                return RelationTargetFact(
                    status="bound",
                    adapter=target_adapter,
                    native_channel_id=usable_ref.native_channel_id,
                    native_message_id=usable_ref.native_message_id,
                    native_thread_id=usable_ref.native_thread_id,
                )
            return self._fact(
                status="unresolved_target",
                target_adapter=target_adapter,
                reason=REASON_NO_TARGET_EVENT_ID,
            )

        target_event_id = relation.target_event_id

        # -- Storage reads (any failure ⇒ binding_unavailable, fail closed)
        target_event: CanonicalEvent | None = None
        refs: list[NativeMessageRef] | None = None

        if callable(get_fn):
            try:
                target_event = await cast(_GetEventFn, get_fn)(target_event_id)
            except Exception:
                self._log.debug(
                    "Relation binding storage.get failed: "
                    "target_event_id=%s target_adapter=%s relation_type=%s",
                    target_event_id,
                    target_adapter,
                    relation.relation_type,
                    exc_info=True,
                )
                return self._fact(
                    status="binding_unavailable",
                    target_adapter=target_adapter,
                    reason=REASON_STORAGE_READ_FAILED,
                )
        elif is_mutation:
            # bound_owned requires proof the target canonical event is
            # stored; without a read authority that proof cannot be made.
            return self._fact(
                status="binding_unavailable",
                target_adapter=target_adapter,
                reason=REASON_TARGET_READ_UNAVAILABLE,
            )

        if callable(list_fn):
            try:
                refs = await cast(_ListNativeRefsFn, list_fn)(target_event_id)
            except Exception:
                self._log.debug(
                    "Relation binding list_native_refs_for_event failed: "
                    "target_event_id=%s target_adapter=%s relation_type=%s",
                    target_event_id,
                    target_adapter,
                    relation.relation_type,
                    exc_info=True,
                )
                return self._fact(
                    status="binding_unavailable",
                    target_adapter=target_adapter,
                    reason=REASON_STORAGE_READ_FAILED,
                )

        if target_event is None and callable(get_fn):
            # get authority was available and reported the target missing.
            return self._fact(
                status="unresolved_target",
                target_adapter=target_adapter,
                reason=REASON_TARGET_NOT_STORED,
            )

        if refs is None:
            # No ref-read authority: storage enumeration is impossible.
            # Referential binding may still fall back to the relation's own
            # stored-ref-derived native ref when it matches this
            # destination.  Mutations cannot: bound_owned requires proof
            # that exactly one distinct OUTBOUND copy exists, which needs
            # enumeration, so they fail closed here as well.
            usable_ref = _usable_relation_native_ref(
                relation,
                target_adapter=target_adapter,
                target_channel=target_channel,
            )
            if usable_ref is not None and not is_mutation:
                return RelationTargetFact(
                    status="bound",
                    adapter=target_adapter,
                    native_channel_id=usable_ref.native_channel_id,
                    native_message_id=usable_ref.native_message_id,
                    native_thread_id=usable_ref.native_thread_id,
                )
            return self._fact(
                status="binding_unavailable",
                target_adapter=target_adapter,
                reason=REASON_REF_READ_UNAVAILABLE,
            )

        # -- Destination-scoped candidate selection ------------------------
        # Group ALL in-scope records by native tuple so identical copies are
        # deduped while direction conflicts inside one tuple stay detectable.
        in_scope = [
            ref
            for ref in refs
            if ref.adapter == target_adapter
            and (target_channel is None or ref.native_channel_id == target_channel)
        ]
        groups = _group_by_tuple(in_scope)

        if len(groups) > 1:
            return self._fact(
                status="ambiguous",
                target_adapter=target_adapter,
                reason=REASON_MULTIPLE_DISTINCT_TARGETS,
            )
        if not groups:
            return self._fact(
                status="out_of_scope",
                target_adapter=target_adapter,
                reason=REASON_NO_IN_SCOPE_REFS,
            )

        tuple_records = groups[0]
        candidate = tuple_records[0]

        if not is_mutation:
            return RelationTargetFact(
                status="bound",
                adapter=target_adapter,
                native_channel_id=candidate.native_channel_id,
                native_message_id=candidate.native_message_id,
                native_thread_id=candidate.native_thread_id,
                direction=candidate.direction,
            )

        # -- Mutation bound_owned proof ------------------------------------
        directions = {record.direction for record in tuple_records}
        if directions == {"inbound"}:
            return self._fact(
                status="not_authorized",
                target_adapter=target_adapter,
                reason=REASON_INBOUND_ONLY_MATCH,
                native_channel_id=candidate.native_channel_id,
                native_message_id=candidate.native_message_id,
            )
        if len(directions) > 1:
            return self._fact(
                status="not_authorized",
                target_adapter=target_adapter,
                reason=REASON_DIRECTION_CONFLICT,
                native_channel_id=candidate.native_channel_id,
                native_message_id=candidate.native_message_id,
            )

        original_facts = _identity_facts(target_event)
        mutation_facts = _identity_facts(event)
        if not _identity_complete(original_facts):
            return self._fact(
                status="not_authorized",
                target_adapter=target_adapter,
                reason=REASON_ORIGINAL_IDENTITY_INCOMPLETE,
                native_channel_id=candidate.native_channel_id,
                native_message_id=candidate.native_message_id,
            )
        if not _identity_complete(mutation_facts):
            return self._fact(
                status="not_authorized",
                target_adapter=target_adapter,
                reason=REASON_MUTATION_IDENTITY_INCOMPLETE,
                native_channel_id=candidate.native_channel_id,
                native_message_id=candidate.native_message_id,
            )
        if original_facts != mutation_facts:
            return self._fact(
                status="not_authorized",
                target_adapter=target_adapter,
                reason=REASON_IDENTITY_MISMATCH,
                native_channel_id=candidate.native_channel_id,
                native_message_id=candidate.native_message_id,
            )

        return RelationTargetFact(
            status="bound_owned",
            adapter=target_adapter,
            native_channel_id=candidate.native_channel_id,
            native_message_id=candidate.native_message_id,
            native_thread_id=candidate.native_thread_id,
            direction="outbound",
        )

    # -- Internal helpers --------------------------------------------------

    @staticmethod
    def _fact(
        *,
        status: str,
        target_adapter: str,
        reason: str | None,
        native_channel_id: str | None = None,
        native_message_id: str | None = None,
    ) -> RelationTargetFact:
        """Build a fact; unbound statuses carry no native tuple evidence."""
        return RelationTargetFact(
            status=status,  # type: ignore[arg-type]
            adapter=target_adapter,
            native_channel_id=native_channel_id,
            native_message_id=native_message_id,
            reason=reason,
        )
