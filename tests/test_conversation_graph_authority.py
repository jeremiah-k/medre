"""Tests for ConversationGraphAuthority — conversation identity assignment.

Validates that ``root_event_id`` and ``conversation_id`` are correctly
computed after relation resolution and before storage, covering:
- single-node (no relations) → self as root
- reply inherits root from target that has root_event_id
- multi-hop chain walks to the ultimate root
- missing target degrades safely to self
- cycle guard breaks loops and degrades to first visited cycle node
- target without root_event_id but with its own relations → walks further
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.events.canonical import CanonicalEvent, EventRelation
from medre.core.events.metadata import EventMetadata
from medre.core.planning.conversation_graph import ConversationGraphAuthority

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_event(
    event_id: str = "evt-001",
    relations: tuple[EventRelation, ...] = (),
    root_event_id: str | None = None,
    conversation_id: str | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=_ts(),
        source_adapter="adapter-a",
        source_transport_id="node-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"text": "hello"},
        metadata=EventMetadata(),
        root_event_id=root_event_id,
        conversation_id=conversation_id,
    )


class FakeStorage:
    """Duck-typed fake storage providing ``get(event_id)``."""

    def __init__(self, events: dict[str, CanonicalEvent] | None = None) -> None:
        self._events: dict[str, CanonicalEvent] = events or {}

    async def get(self, event_id: str) -> CanonicalEvent | None:
        return self._events.get(event_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSingleEventNoRelations:
    """Single event with no relations → root and conversation are self."""

    @pytest.mark.asyncio
    async def test_no_relations_assigns_self_as_root(self) -> None:
        storage = FakeStorage()
        authority = ConversationGraphAuthority(storage=storage)
        event = _make_event(event_id="evt-1", relations=())

        result = await authority.resolve_conversation_identity(event)

        assert result.root_event_id == "evt-1"
        assert result.conversation_id == "evt-1"

    @pytest.mark.asyncio
    async def test_no_resolved_target_assigns_self_as_root(self) -> None:
        """Relations exist but target_event_id is None → self as root."""
        storage = FakeStorage()
        authority = ConversationGraphAuthority(storage=storage)
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-1", relations=(rel,))

        result = await authority.resolve_conversation_identity(event)

        assert result.root_event_id == "evt-1"
        assert result.conversation_id == "evt-1"


class TestReplyInheritsRoot:
    """Reply event inherits root_event_id from target that has one."""

    @pytest.mark.asyncio
    async def test_target_has_root_inherits_directly(self) -> None:
        root = _make_event(
            event_id="root-1", root_event_id="root-1", conversation_id="root-1"
        )
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id="root-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        reply = _make_event(event_id="reply-1", relations=(reply_rel,))

        storage = FakeStorage(events={"root-1": root})
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(reply)

        assert result.root_event_id == "root-1"
        assert result.conversation_id == "root-1"

    @pytest.mark.asyncio
    async def test_target_is_root_node_no_root_field(self) -> None:
        """Target event has no root_event_id and no relations → target is root."""
        root = _make_event(event_id="root-1")  # no root_event_id
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id="root-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        reply = _make_event(event_id="reply-1", relations=(reply_rel,))

        storage = FakeStorage(events={"root-1": root})
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(reply)

        assert result.root_event_id == "root-1"
        assert result.conversation_id == "root-1"


class TestMultiHopChain:
    """Multi-hop chain: C→B→A. C should inherit A's root."""

    @pytest.mark.asyncio
    async def test_three_hop_chain(self) -> None:
        # A is the root (no relations, no root_event_id).
        a = _make_event(event_id="evt-a")

        # B replies to A (no root_event_id yet, but has relation to A).
        b_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-a",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        b = _make_event(event_id="evt-b", relations=(b_rel,))

        # C replies to B.
        c_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-b",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        c = _make_event(event_id="evt-c", relations=(c_rel,))

        storage = FakeStorage(events={"evt-a": a, "evt-b": b})
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(c)

        # C → B → A. A has no root and no relations → A is root.
        assert result.root_event_id == "evt-a"
        assert result.conversation_id == "evt-a"

    @pytest.mark.asyncio
    async def test_chain_with_cached_root(self) -> None:
        """When B already has root_event_id, C inherits it without further walk."""
        a = _make_event(
            event_id="evt-a", root_event_id="evt-a", conversation_id="evt-a"
        )
        b = _make_event(
            event_id="evt-b", root_event_id="evt-a", conversation_id="evt-a"
        )
        c_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-b",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        c = _make_event(event_id="evt-c", relations=(c_rel,))

        storage = FakeStorage(events={"evt-b": b, "evt-a": a})
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(c)

        assert result.root_event_id == "evt-a"
        assert result.conversation_id == "evt-a"


class TestMissingTarget:
    """Target event not in storage → current event becomes root."""

    @pytest.mark.asyncio
    async def test_target_not_found_degrades_to_self(self) -> None:
        rel = EventRelation(
            relation_type="reply",
            target_event_id="missing-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-1", relations=(rel,))

        storage = FakeStorage()  # empty — no events
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(event)

        # Target missing → degrade to self.
        assert result.root_event_id == "evt-1"
        assert result.conversation_id == "evt-1"


class TestCycleGuard:
    """Cyclic relation graph → visited set breaks cycle safely."""

    @pytest.mark.asyncio
    async def test_direct_cycle(self) -> None:
        """A → B → A cycle: walk should break and return a stable root."""
        a_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-b",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        a = _make_event(event_id="evt-a", relations=(a_rel,))

        b_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-a",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        b = _make_event(event_id="evt-b", relations=(b_rel,))

        storage = FakeStorage(events={"evt-a": a, "evt-b": b})
        authority = ConversationGraphAuthority(storage=storage)

        # Event C targets B, which starts the cycle A↔B.
        c_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-b",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        c = _make_event(event_id="evt-c", relations=(c_rel,))

        result = await authority.resolve_conversation_identity(c)

        # Should degrade safely — the exact root depends on which node
        # the walk stops at, but it must be one of the cycle nodes.
        assert result.root_event_id in ("evt-a", "evt-b")
        assert result.conversation_id == result.root_event_id

    @pytest.mark.asyncio
    async def test_self_referencing_event(self) -> None:
        """Event whose relation targets itself → visited set catches it."""
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-1", relations=(rel,))

        storage = FakeStorage(events={"evt-1": event})
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(event)

        # Self-reference → visited set detects cycle → self is root.
        assert result.root_event_id == "evt-1"
        assert result.conversation_id == "evt-1"


class TestAlreadyAssigned:
    """Event that already has root_event_id is returned unchanged."""

    @pytest.mark.asyncio
    async def test_already_assigned_no_mutation(self) -> None:
        event = _make_event(
            event_id="evt-1",
            root_event_id="root-x",
            conversation_id="root-x",
        )
        storage = FakeStorage()
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(event)

        assert result is event  # same object, no new copy
        assert result.root_event_id == "root-x"
        assert result.conversation_id == "root-x"


class TestCachedGetUsage:
    """Authority uses cached_get_fn when provided."""

    @pytest.mark.asyncio
    async def test_cached_fn_called_not_storage(self) -> None:
        """When cached_get_fn is given, storage.get should not be called."""
        call_log: list[str] = []

        async def _cached_get(event_id: str) -> CanonicalEvent | None:
            call_log.append(f"cached:{event_id}")
            if event_id == "target-1":
                return _make_event(
                    event_id="target-1",
                    root_event_id="target-1",
                    conversation_id="target-1",
                )
            return None

        rel = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-1", relations=(rel,))

        # Storage has events, but cached_get_fn should be used instead.
        storage = FakeStorage(events={"target-1": _make_event(event_id="target-1")})
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(
            event, cached_get_fn=_cached_get
        )

        assert result.root_event_id == "target-1"
        assert call_log == ["cached:target-1"]


class TestReactionRelation:
    """Reaction relations also resolve conversation identity."""

    @pytest.mark.asyncio
    async def test_reaction_inherits_root(self) -> None:
        root = _make_event(
            event_id="root-1", root_event_id="root-1", conversation_id="root-1"
        )
        reaction_rel = EventRelation(
            relation_type="reaction",
            target_event_id="root-1",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        reaction = _make_event(event_id="react-1", relations=(reaction_rel,))

        storage = FakeStorage(events={"root-1": root})
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(reaction)

        assert result.root_event_id == "root-1"
        assert result.conversation_id == "root-1"


class TestStorageGetFailure:
    """Storage.get raising an exception degrades safely."""

    @pytest.mark.asyncio
    async def test_get_exception_degrades_to_self(self) -> None:
        class FailingStorage(FakeStorage):
            async def get(self, event_id: str) -> CanonicalEvent | None:
                raise RuntimeError("storage unavailable")

        rel = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-1", relations=(rel,))

        storage = FailingStorage()
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(event)

        # Degrades to self.
        assert result.root_event_id == "evt-1"
        assert result.conversation_id == "evt-1"


class TestNoGetOnStorage:
    """Storage without a ``get`` method degrades safely."""

    @pytest.mark.asyncio
    async def test_storage_without_get(self) -> None:
        class MinimalStorage:
            pass

        rel = EventRelation(
            relation_type="reply",
            target_event_id="target-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="evt-1", relations=(rel,))

        authority = ConversationGraphAuthority(storage=MinimalStorage())

        result = await authority.resolve_conversation_identity(event)

        # No get method → target lookup fails → degrade to self.
        assert result.root_event_id == "evt-1"
        assert result.conversation_id == "evt-1"


class TestMultiRelationMissingFirstTarget:
    """Event with multiple relations where the first target is missing.

    The authority must iterate through ALL relations before self-rooting.
    If the first relation's target is absent from storage but the second
    relation's target is present, the root must be inherited from the
    second target.
    """

    @pytest.mark.asyncio
    async def test_first_missing_second_present_inherits_root(self) -> None:
        # A known root already stored with identity fields.
        root = _make_event(
            event_id="root-1",
            root_event_id="root-1",
            conversation_id="root-1",
        )
        # Second target is present and carries root.
        target_b = _make_event(
            event_id="target-b",
            root_event_id="root-1",
            conversation_id="root-1",
        )

        # Event has 2 relations: first target is NOT in storage,
        # second target IS in storage.
        rel_missing = EventRelation(
            relation_type="reply",
            target_event_id="target-a",  # not in storage
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        rel_present = EventRelation(
            relation_type="reply",
            target_event_id="target-b",  # in storage
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            event_id="evt-1",
            relations=(rel_missing, rel_present),
        )

        storage = FakeStorage(events={"root-1": root, "target-b": target_b})
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(event)

        # Should inherit from target-b → root-1, NOT self-root.
        assert result.root_event_id == "root-1"
        assert result.conversation_id == "root-1"

    @pytest.mark.asyncio
    async def test_all_targets_missing_self_roots(self) -> None:
        """When ALL relation targets are missing, self-root."""
        rel_a = EventRelation(
            relation_type="reply",
            target_event_id="missing-a",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        rel_b = EventRelation(
            relation_type="reply",
            target_event_id="missing-b",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            event_id="evt-1",
            relations=(rel_a, rel_b),
        )

        storage = FakeStorage()  # empty
        authority = ConversationGraphAuthority(storage=storage)

        result = await authority.resolve_conversation_identity(event)

        assert result.root_event_id == "evt-1"
        assert result.conversation_id == "evt-1"
