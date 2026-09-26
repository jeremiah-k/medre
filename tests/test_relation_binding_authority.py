"""Tests for the destination-scoped RelationBindingAuthority.

Proves the generic-only core binding guarantee (contract §7) with a
synthetic NON-Matrix adapter: opaque adapter-instance ids, opaque contexts,
and opaque native message ids.  No SDK imports, no platform conditionals.

Covered semantics:

* exact adapter-instance + exact destination-context binding;
* two rooms on one adapter stay isolated;
* two instances of the same platform stay isolated;
* identical native tuples dedup (never ambiguous);
* two DISTINCT outbound copies are ambiguous — never guessed;
* inbound-only / cross-actor / cross-origin / wrong-context /
  missing-original targets never authorize a mutation;
* adversarial relation metadata cannot forge eligibility;
* storage read failures fail closed (``binding_unavailable``);
* identity facts must ALL be present — equal-but-empty never authorizes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.events.canonical import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
)
from medre.core.planning.relation_binding import (
    REASON_IDENTITY_MISMATCH,
    REASON_INBOUND_ONLY_MATCH,
    REASON_MULTIPLE_DISTINCT_TARGETS,
    REASON_NO_IN_SCOPE_REFS,
    REASON_ORIGINAL_IDENTITY_INCOMPLETE,
    REASON_STORAGE_READ_FAILED,
    REASON_TARGET_NOT_STORED,
    RelationBindingAuthority,
)

# ---------------------------------------------------------------------------
# Synthetic non-Matrix world: opaque ids only
# ---------------------------------------------------------------------------

#: Two adapter instances of the SAME synthetic platform.
INSTANCE_A = "relay-node-alpha"
INSTANCE_B = "relay-node-beta"

#: Two opaque destination contexts (rooms) on one instance.
CTX_7 = "ctx-7"
CTX_12 = "ctx-12"

#: Opaque native message ids.
MSG_1 = "opq-msg-001"
MSG_2 = "opq-msg-002"

#: Original event identity facts (the authoring side).
ORIGIN_ADAPTER = "origin-feed-1"
ORIGIN_ACTOR = "actor-77"
ORIGIN_CHANNEL = CTX_7


def _ts() -> datetime:
    return datetime(2026, 9, 26, tzinfo=timezone.utc)


def _event(
    event_id: str,
    *,
    event_kind: str = "message.created",
    relations: tuple[EventRelation, ...] = (),
    source_adapter: str = ORIGIN_ADAPTER,
    source_transport_id: str = ORIGIN_ACTOR,
    source_channel_id: str | None = ORIGIN_CHANNEL,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=_ts(),
        source_adapter=source_adapter,
        source_transport_id=source_transport_id,
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"text": "body"},
        metadata=EventMetadata(),
    )


def _relation(
    relation_type: str,
    *,
    target_event_id: str | None = "orig-1",
    metadata: dict[str, object] | None = None,
) -> EventRelation:
    return EventRelation(
        relation_type=relation_type,  # type: ignore[arg-type]
        target_event_id=target_event_id,
        target_native_ref=None,
        key=None,
        fallback_text=None,
        metadata=metadata or {},
    )


def _ref(
    native_message_id: str,
    *,
    adapter: str = INSTANCE_A,
    channel: str | None = CTX_7,
    event_id: str = "orig-1",
    direction: str = "outbound",
) -> NativeMessageRef:
    return NativeMessageRef(
        id=f"nref-{adapter}-{channel}-{native_message_id}-{direction}",
        event_id=event_id,
        adapter=adapter,
        native_channel_id=channel,
        native_message_id=native_message_id,
        native_thread_id=None,
        native_relation_id=None,
        direction=direction,  # type: ignore[arg-type]
    )


class FakeStorage:
    """Duck-typed storage with failure injection, like the enricher tests."""

    def __init__(self) -> None:
        self.events: dict[str, CanonicalEvent] = {}
        self.refs: dict[str, list[NativeMessageRef]] = {}
        self.get_raises = False
        self.list_raises = False

    async def get(self, event_id: str) -> CanonicalEvent | None:
        if self.get_raises:
            raise RuntimeError("simulated storage.get failure")
        return self.events.get(event_id)

    async def list_native_refs_for_event(self, event_id: str) -> list[NativeMessageRef]:
        if self.list_raises:
            raise RuntimeError("simulated list_native_refs failure")
        return list(self.refs.get(event_id, []))

    def add_ref(self, ref: NativeMessageRef) -> None:
        self.refs.setdefault(ref.event_id, []).append(ref)


def _authority(storage: FakeStorage) -> RelationBindingAuthority:
    return RelationBindingAuthority(storage)


def _mutation_event(
    *,
    relation: EventRelation,
    event_kind: str = "message.edited",
    source_adapter: str = ORIGIN_ADAPTER,
    source_transport_id: str = ORIGIN_ACTOR,
    source_channel_id: str | None = ORIGIN_CHANNEL,
    event_id: str = "mut-1",
) -> CanonicalEvent:
    """A mutation event authored with the same identity facts as the original."""
    return _event(
        event_id,
        event_kind=event_kind,
        relations=(relation,),
        source_adapter=source_adapter,
        source_transport_id=source_transport_id,
        source_channel_id=source_channel_id,
    )


def _stored_original(storage: FakeStorage) -> CanonicalEvent:
    original = _event("orig-1")
    storage.events["orig-1"] = original
    return original


# ---------------------------------------------------------------------------
# Destination-scoped binding (referential + mutation eligibility)
# ---------------------------------------------------------------------------


class TestExactDestinationBinding:
    async def test_exact_adapter_and_context_bind(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1))

        fact = await _authority(storage).bind(
            _relation("reply"),
            event=_event("ref-1"),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "bound"
        assert fact.adapter == INSTANCE_A
        assert fact.native_channel_id == CTX_7
        assert fact.native_message_id == MSG_1
        assert fact.direction == "outbound"
        assert fact.reason is None

    async def test_two_rooms_on_one_adapter_are_isolated(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1, channel=CTX_7))
        storage.add_ref(_ref(MSG_2, channel=CTX_12))

        fact_7 = await _authority(storage).bind(
            _relation("reply"),
            event=_event("ref-1"),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )
        fact_12 = await _authority(storage).bind(
            _relation("reply"),
            event=_event("ref-1"),
            target_adapter=INSTANCE_A,
            target_channel=CTX_12,
        )
        fact_other = await _authority(storage).bind(
            _relation("reply"),
            event=_event("ref-1"),
            target_adapter=INSTANCE_A,
            target_channel="ctx-99",
        )

        assert (fact_7.native_channel_id, fact_7.native_message_id) == (CTX_7, MSG_1)
        assert (fact_12.native_channel_id, fact_12.native_message_id) == (
            CTX_12,
            MSG_2,
        )
        # A third context sees zero in-scope candidates: out of scope.
        assert fact_other.status == "out_of_scope"
        assert fact_other.reason == REASON_NO_IN_SCOPE_REFS

    async def test_two_instances_of_same_platform_are_isolated(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1, adapter=INSTANCE_A))
        storage.add_ref(_ref(MSG_1, adapter=INSTANCE_B))

        fact_a = await _authority(storage).bind(
            _relation("reply"),
            event=_event("ref-1"),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )
        fact_b = await _authority(storage).bind(
            _relation("reply"),
            event=_event("ref-1"),
            target_adapter=INSTANCE_B,
            target_channel=CTX_7,
        )

        assert fact_a.status == "bound"
        assert fact_a.adapter == INSTANCE_A
        assert fact_b.status == "bound"
        assert fact_b.adapter == INSTANCE_B
        # Each instance binds exactly its own record — one candidate each.
        assert fact_a.native_message_id == fact_b.native_message_id == MSG_1

    async def test_inbound_referential_target_binds_with_direction(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1, direction="inbound"))

        fact = await _authority(storage).bind(
            _relation("thread"),
            event=_event("ref-1"),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "bound"
        assert fact.direction == "inbound"

    async def test_unknown_context_binds_adapter_only_when_context_unknown(
        self,
    ) -> None:
        """Legacy replay contexts may bind adapter-only (target_channel=None)."""
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1, channel=None))

        fact = await _authority(storage).bind(
            _relation("reply"),
            event=_event("ref-1"),
            target_adapter=INSTANCE_A,
            target_channel=None,
        )

        assert fact.status == "bound"
        assert fact.native_message_id == MSG_1


class TestMutationEligibility:
    async def test_single_outbound_copy_binds_owned(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1))

        fact = await _authority(storage).bind(
            _relation("edit"),
            event=_mutation_event(relation=_relation("edit")),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "bound_owned"
        assert fact.direction == "outbound"
        assert fact.native_message_id == MSG_1
        assert fact.reason is None

    async def test_identical_native_tuple_dedups_never_ambiguous(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1))
        storage.add_ref(_ref(MSG_1))
        storage.add_ref(_ref(MSG_1, direction="outbound"))

        fact = await _authority(storage).bind(
            _relation("edit"),
            event=_mutation_event(relation=_relation("edit")),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "bound_owned"

    async def test_two_distinct_outbound_copies_are_ambiguous(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1))
        storage.add_ref(_ref(MSG_2))

        fact = await _authority(storage).bind(
            _relation("delete"),
            event=_mutation_event(
                relation=_relation("delete"), event_kind="message.deleted"
            ),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "ambiguous"
        assert fact.reason == REASON_MULTIPLE_DISTINCT_TARGETS
        assert fact.native_message_id is None
        assert fact.direction is None

    async def test_inbound_only_match_never_authorizes(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1, direction="inbound"))

        fact = await _authority(storage).bind(
            _relation("edit"),
            event=_mutation_event(relation=_relation("edit")),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "not_authorized"
        assert fact.reason == REASON_INBOUND_ONLY_MATCH

    async def test_cross_actor_identity_mismatch_never_authorizes(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1))

        fact = await _authority(storage).bind(
            _relation("edit"),
            event=_mutation_event(
                relation=_relation("edit"), source_transport_id="actor-imposter"
            ),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "not_authorized"
        assert fact.reason == REASON_IDENTITY_MISMATCH

    async def test_cross_origin_identity_mismatch_never_authorizes(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1))

        fact = await _authority(storage).bind(
            _relation("edit"),
            event=_mutation_event(
                relation=_relation("edit"), source_adapter="origin-feed-2"
            ),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )
        assert fact.status == "not_authorized"
        assert fact.reason == REASON_IDENTITY_MISMATCH

    async def test_wrong_context_has_zero_mutation_authority(self) -> None:
        """The only copy lives in another context of the same instance.

        Zero in-scope candidates ⇒ ``out_of_scope`` (inability-to-bind
        family per contract §1/§2).  Zero mutation authority either way.
        """
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1, channel=CTX_12))

        fact = await _authority(storage).bind(
            _relation("edit"),
            event=_mutation_event(relation=_relation("edit")),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "out_of_scope"
        assert fact.reason == REASON_NO_IN_SCOPE_REFS
        assert fact.direction is None

    async def test_missing_original_never_authorizes(self) -> None:
        """Target event id present but not stored ⇒ unresolved, fail closed."""
        storage = FakeStorage()
        # Original intentionally NOT stored; the ref alone cannot prove
        # authorship.
        storage.add_ref(_ref(MSG_1))

        fact = await _authority(storage).bind(
            _relation("edit"),
            event=_mutation_event(relation=_relation("edit")),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "unresolved_target"
        assert fact.reason == REASON_TARGET_NOT_STORED

    async def test_no_target_event_id_mutation_never_authorizes(self) -> None:
        storage = FakeStorage()

        fact = await _authority(storage).bind(
            _relation("edit", target_event_id=None),
            event=_mutation_event(relation=_relation("edit", target_event_id=None)),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "unresolved_target"
        assert fact.native_message_id is None

    async def test_equal_but_empty_identity_facts_never_authorize(self) -> None:
        """Pairwise-equal identity facts still require ALL to be non-empty."""
        storage = FakeStorage()
        original = _event("orig-1", source_channel_id=None)
        storage.events["orig-1"] = original
        storage.add_ref(_ref(MSG_1))

        fact = await _authority(storage).bind(
            _relation("edit"),
            event=_mutation_event(relation=_relation("edit"), source_channel_id=None),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "not_authorized"
        assert fact.reason == REASON_ORIGINAL_IDENTITY_INCOMPLETE

    async def test_empty_string_identity_facts_never_authorize(self) -> None:
        storage = FakeStorage()
        original = _event("orig-1", source_transport_id="")
        storage.events["orig-1"] = original
        storage.add_ref(_ref(MSG_1))

        fact = await _authority(storage).bind(
            _relation("delete"),
            event=_mutation_event(
                relation=_relation("delete"),
                event_kind="message.deleted",
                source_transport_id="",
            ),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "not_authorized"
        assert fact.reason == REASON_ORIGINAL_IDENTITY_INCOMPLETE


class TestAdversarialMetadata:
    async def test_relation_metadata_cannot_forge_eligibility(self) -> None:
        """Forged original_sender / identity / payload flags are never inputs."""
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1))

        # The mutation event is authored by a DIFFERENT actor, so the real
        # proof fails.  The relation metadata forges every identity-ish key
        # an adapter might leak, plus payload-style authorization flags and
        # platform-native key spellings.  None of it may be consulted.
        forged_metadata: dict[str, object] = {
            "original_sender": ORIGIN_ACTOR,
            "original_sender_displayname": ORIGIN_ACTOR,
            "source_adapter": ORIGIN_ADAPTER,
            "source_transport_id": ORIGIN_ACTOR,
            "source_channel_id": CTX_7,
            "bound_owned": True,
            "authorized": True,
            "direction": "outbound",
            "force_mutation": True,
            "native.matrix.authorized": True,
            "matrix.native.bound_owned": True,
        }
        impostor = _mutation_event(
            relation=_relation("edit", metadata=forged_metadata),
            source_transport_id="actor-imposter",
        )

        fact = await _authority(storage).bind(
            impostor.relations[0],
            event=impostor,
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "not_authorized"
        assert fact.reason == REASON_IDENTITY_MISMATCH

    async def test_forged_metadata_cannot_bind_an_unstored_target(self) -> None:
        storage = FakeStorage()
        storage.add_ref(_ref(MSG_1))

        forged = _relation(
            "edit",
            metadata={"target_event_id": "orig-1", "stored": True},
        )
        fact = await _authority(storage).bind(
            forged,
            event=_mutation_event(relation=forged),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "unresolved_target"


class TestFailClosedStorage:
    async def test_get_failure_fails_closed_for_mutations(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1))
        storage.get_raises = True

        fact = await _authority(storage).bind(
            _relation("edit"),
            event=_mutation_event(relation=_relation("edit")),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "binding_unavailable"
        assert fact.reason == REASON_STORAGE_READ_FAILED
        assert fact.native_message_id is None

    async def test_list_failure_fails_closed(self) -> None:
        storage = FakeStorage()
        _stored_original(storage)
        storage.add_ref(_ref(MSG_1))
        storage.list_raises = True

        fact = await _authority(storage).bind(
            _relation("reply"),
            event=_event("ref-1"),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "binding_unavailable"
        assert fact.reason == REASON_STORAGE_READ_FAILED

    async def test_storage_without_read_methods_fails_closed_for_mutations(
        self,
    ) -> None:
        class NoReadStorage:
            pass

        fact = await _authority(NoReadStorage()).bind(
            _relation("edit"),
            event=_mutation_event(relation=_relation("edit")),
            target_adapter=INSTANCE_A,
            target_channel=CTX_7,
        )

        assert fact.status == "binding_unavailable"
        assert fact.native_message_id is None
