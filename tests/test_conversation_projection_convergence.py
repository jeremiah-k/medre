"""Convergence tests for the rebuildable conversation-membership projection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from medre.core.events import CanonicalEvent, EventRelation, NativeMessageRef, NativeRef
from medre.core.events.metadata import EventMetadata
from medre.core.planning.conversation_graph import (
    _REBUILD_PAGE_SIZE,
    ConversationProjectionService,
)
from medre.core.storage.backend import (
    ConversationMembership,
    ConversationProjectionState,
)
from medre.core.storage.sqlite.storage import SQLiteStorage


_TS = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _event(
    event_id: str,
    *,
    relations: tuple[EventRelation, ...] = (),
    source_native_ref: NativeRef | None = None,
    root_event_id: str | None = None,
    conversation_id: str | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=_TS,
        source_adapter="test",
        source_transport_id="transport",
        source_channel_id="channel",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"text": event_id},
        metadata=EventMetadata(),
        root_event_id=root_event_id,
        conversation_id=conversation_id,
        source_native_ref=source_native_ref,
    )


def _reply_to_event(target_event_id: str) -> EventRelation:
    return EventRelation(
        relation_type="reply",
        target_event_id=target_event_id,
        target_native_ref=None,
        key=None,
        fallback_text=None,
    )


def _reply_to_native(
    native_message_id: str,
    *,
    adapter: str = "matrix",
    channel: str | None = "!room:test",
) -> EventRelation:
    return EventRelation(
        relation_type="reply",
        target_event_id=None,
        target_native_ref=NativeRef(
            adapter=adapter,
            native_channel_id=channel,
            native_message_id=native_message_id,
            native_thread_id=None,
        ),
        key=None,
        fallback_text=None,
    )


def _native_mapping(
    event_id: str,
    native_message_id: str,
    *,
    adapter: str = "matrix",
    channel: str | None = "!room:test",
) -> NativeMessageRef:
    return NativeMessageRef(
        id=f"nref-{event_id}-{native_message_id}",
        event_id=event_id,
        adapter=adapter,
        native_channel_id=channel,
        native_message_id=native_message_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
        metadata={},
        created_at=_TS,
    )


def _semantic(membership: ConversationMembership) -> tuple[object, ...]:
    return (
        membership.event_id,
        membership.root_event_id,
        membership.conversation_id,
        membership.resolved_target_event_id,
        membership.relation_type,
        membership.depth,
        membership.resolution_state,
    )


async def _open_memory_storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    await storage.initialize()
    return storage


class _MemoryProjectionStorage:
    """Minimal in-memory projection backend for long-chain algorithm tests."""

    def __init__(self, events: list[CanonicalEvent]) -> None:
        self.events = {event.event_id: event for event in events}
        self.memberships: dict[str, ConversationMembership] = {}
        self.get_call_ids: list[str] = []
        self.event_id_pages: list[tuple[str | None, int]] = []
        self.projection_state: ConversationProjectionState | None = None
        self.projection_state_history: list[ConversationProjectionState] = []

    async def get(self, event_id: str) -> CanonicalEvent | None:
        self.get_call_ids.append(event_id)
        return self.events.get(event_id)

    async def list_event_ids_page(
        self, *, after_event_id: str | None, limit: int
    ) -> list[str]:
        self.event_id_pages.append((after_event_id, limit))
        event_ids = sorted(self.events)
        if after_event_id is not None:
            event_ids = [event_id for event_id in event_ids if event_id > after_event_id]
        return event_ids[:limit]

    async def resolve_native_ref(
        self,
        adapter: str,
        native_channel_id: str | None,
        native_message_id: str,
    ) -> str | None:
        del adapter, native_channel_id, native_message_id
        return None

    async def list_native_refs_for_event(
        self, event_id: str
    ) -> list[NativeMessageRef]:
        del event_id
        return []

    async def list_relation_sources(self, target_event_id: str) -> list[str]:
        return [
            event.event_id
            for event in self.events.values()
            if any(
                relation.target_event_id == target_event_id
                for relation in event.relations
            )
        ]

    async def list_relation_sources_for_native_ref(
        self,
        adapter: str,
        native_channel_id: str | None,
        native_message_id: str,
    ) -> list[str]:
        del adapter, native_channel_id, native_message_id
        return []

    async def put_conversation_membership(
        self, membership: ConversationMembership
    ) -> bool:
        previous = self.memberships.get(membership.event_id)
        self.memberships[membership.event_id] = membership
        return previous != membership

    async def get_conversation_membership(
        self, event_id: str
    ) -> ConversationMembership | None:
        return self.memberships.get(event_id)

    async def get_conversation_projection_state(
        self,
    ) -> ConversationProjectionState | None:
        return self.projection_state

    async def put_conversation_projection_state(
        self, state: ConversationProjectionState
    ) -> None:
        self.projection_state = state
        self.projection_state_history.append(state)


async def test_child_before_parent_and_parent_before_child_converge() -> None:
    """Arrival order does not affect the final derived membership."""
    child_first = await _open_memory_storage()
    parent_first = await _open_memory_storage()
    try:
        child = _event(
            "child",
            relations=(_reply_to_event("parent"),),
            root_event_id="child",
            conversation_id="child",
        )
        parent = _event(
            "parent",
            root_event_id="parent",
            conversation_id="parent",
        )

        child_service = ConversationProjectionService(child_first)
        await child_first.append(child)
        initial = await child_service.repair_after_event_available("child")
        assert initial.membership.resolution_state == "unresolved"
        assert initial.membership.root_event_id == "child"
        await child_first.append(parent)
        await child_service.repair_after_event_available("parent")

        parent_service = ConversationProjectionService(parent_first)
        await parent_first.append(parent)
        await parent_service.repair_after_event_available("parent")
        await parent_first.append(child)
        await parent_service.repair_after_event_available("child")

        child_first_membership = await child_first.get_conversation_membership(
            "child"
        )
        parent_first_membership = await parent_first.get_conversation_membership(
            "child"
        )
        assert child_first_membership is not None
        assert parent_first_membership is not None
        assert _semantic(child_first_membership) == _semantic(parent_first_membership)
        assert child_first_membership.root_event_id == "parent"
        assert child_first_membership.resolved_target_event_id == "parent"
        assert child_first_membership.depth == 1
        assert child_first_membership.resolution_state == "resolved"

        # Canonical ingress evidence remains unchanged even though the current
        # projection has converged to the late parent.
        stored_child = await child_first.get("child")
        assert stored_child is not None
        assert stored_child.root_event_id == "child"
        assert stored_child.conversation_id == "child"
        projected = await child_service.project_event(stored_child)
        assert projected.root_event_id == "parent"
        assert projected.conversation_id == "parent"
    finally:
        await child_first.close()
        await parent_first.close()


async def test_late_native_target_resolves_without_rewriting_relation() -> None:
    """A native-only child edge repairs when the target mapping appears later."""
    storage = await _open_memory_storage()
    try:
        service = ConversationProjectionService(storage)
        relation = _reply_to_native("$parent")
        child = _event("child", relations=(relation,))
        await storage.append(child)
        await service.repair_after_event_available("child")

        before = await storage.get_conversation_membership("child")
        assert before is not None
        assert before.resolution_state == "unresolved"
        assert before.resolved_target_event_id is None

        parent = _event("parent")
        await storage.append(parent)
        await storage.store_native_ref(_native_mapping("parent", "$parent"))
        await service.repair_after_event_available("parent")

        after = await storage.get_conversation_membership("child")
        assert after is not None
        assert after.root_event_id == "parent"
        assert after.resolved_target_event_id == "parent"
        assert after.resolution_state == "resolved"

        stored_child = await storage.get("child")
        assert stored_child is not None
        assert stored_child.relations == (relation,)
        assert stored_child.relations[0].target_event_id is None
    finally:
        await storage.close()


async def test_new_native_ref_repairs_only_waiting_dependents() -> None:
    """A newly persisted native identity repairs children without anchor churn."""
    storage = await _open_memory_storage()
    try:
        service = ConversationProjectionService(storage)
        parent = _event("parent")
        child = _event("child", relations=(_reply_to_native("$parent"),))
        await storage.append(parent)
        await storage.append(child)
        await service.repair_after_event_available("parent")
        await service.repair_after_event_available("child")

        before_parent = await storage.get_conversation_membership("parent")
        before_child = await storage.get_conversation_membership("child")
        assert before_parent is not None
        assert before_child is not None
        assert before_child.resolution_state == "unresolved"

        await storage.store_native_ref(_native_mapping("parent", "$parent"))
        changed = await service.repair_after_native_ref_available("parent")

        after_parent = await storage.get_conversation_membership("parent")
        after_child = await storage.get_conversation_membership("child")
        assert after_parent == before_parent
        assert after_child is not None
        assert after_child.root_event_id == "parent"
        assert after_child.resolved_target_event_id == "parent"
        assert after_child.resolution_state == "resolved"
        assert changed == ("child",)
    finally:
        await storage.close()


async def test_explicit_missing_target_does_not_fallback_to_native_ref() -> None:
    """An explicit canonical target remains authoritative until it exists."""
    storage = await _open_memory_storage()
    try:
        service = ConversationProjectionService(storage)
        await storage.append(_event("native-parent"))
        await storage.store_native_ref(_native_mapping("native-parent", "$parent"))
        relation = EventRelation(
            relation_type="reply",
            target_event_id="canonical-parent",
            target_native_ref=NativeRef(
                adapter="matrix",
                native_channel_id="!room:test",
                native_message_id="$parent",
                native_thread_id=None,
            ),
            key=None,
            fallback_text=None,
        )
        await storage.append(_event("child", relations=(relation,)))

        repair = await service.repair_after_event_available("child")

        assert repair.membership.resolution_state == "unresolved"
        assert repair.membership.resolved_target_event_id is None
        assert repair.membership.root_event_id == "child"
    finally:
        await storage.close()


async def test_late_root_repairs_multi_hop_descendants() -> None:
    """A→B→C ancestry converges when events arrive C, B, A."""
    storage = await _open_memory_storage()
    try:
        service = ConversationProjectionService(storage)
        c = _event("c", relations=(_reply_to_event("b"),))
        b = _event("b", relations=(_reply_to_event("a"),))
        a = _event("a")

        await storage.append(c)
        await service.repair_after_event_available("c")
        await storage.append(b)
        await service.repair_after_event_available("b")

        c_mid = await storage.get_conversation_membership("c")
        assert c_mid is not None
        assert c_mid.root_event_id == "b"
        assert c_mid.resolution_state == "unresolved"

        await storage.append(a)
        repair = await service.repair_after_event_available("a")
        assert set(repair.changed_event_ids) >= {"a", "b", "c"}

        memberships = {
            event_id: await storage.get_conversation_membership(event_id)
            for event_id in ("a", "b", "c")
        }
        assert memberships["a"] == ConversationMembership(
            event_id="a",
            root_event_id="a",
            conversation_id="a",
            resolved_target_event_id=None,
            relation_type=None,
            depth=0,
            resolution_state="root",
        )
        assert memberships["b"] is not None
        assert memberships["b"].root_event_id == "a"
        assert memberships["b"].depth == 1
        assert memberships["b"].resolution_state == "resolved"
        assert memberships["c"] is not None
        assert memberships["c"].root_event_id == "a"
        assert memberships["c"].depth == 2
        assert memberships["c"].resolution_state == "resolved"
    finally:
        await storage.close()


async def test_earlier_relation_wins_when_its_target_arrives_late() -> None:
    """Repair re-evaluates relation order instead of freezing the first result."""
    storage = await _open_memory_storage()
    try:
        service = ConversationProjectionService(storage)
        await storage.append(_event("b"))
        await service.repair_after_event_available("b")

        child = _event(
            "child",
            relations=(_reply_to_event("a"), _reply_to_event("b")),
        )
        await storage.append(child)
        await service.repair_after_event_available("child")
        before = await storage.get_conversation_membership("child")
        assert before is not None
        assert before.resolved_target_event_id == "b"
        assert before.root_event_id == "b"

        await storage.append(_event("a"))
        await service.repair_after_event_available("a")
        after = await storage.get_conversation_membership("child")
        assert after is not None
        assert after.resolved_target_event_id == "a"
        assert after.root_event_id == "a"
    finally:
        await storage.close()


async def test_cycle_projection_has_deterministic_root() -> None:
    """Cycle members converge to the lexicographically smallest cycle ID."""
    storage = await _open_memory_storage()
    try:
        service = ConversationProjectionService(storage)
        await storage.append(_event("b", relations=(_reply_to_event("a"),)))
        await service.repair_after_event_available("b")
        await storage.append(_event("a", relations=(_reply_to_event("b"),)))
        await service.repair_after_event_available("a")

        a = await storage.get_conversation_membership("a")
        b = await storage.get_conversation_membership("b")
        assert a is not None
        assert b is not None
        assert a.root_event_id == b.root_event_id == "a"
        assert a.conversation_id == b.conversation_id == "a"
        assert a.resolution_state == b.resolution_state == "cycle"
        assert a.depth == b.depth == 0
        assert a.resolved_target_event_id == "b"
        assert b.resolved_target_event_id == "a"
    finally:
        await storage.close()


async def test_rebuild_is_idempotent() -> None:
    storage = await _open_memory_storage()
    try:
        service = ConversationProjectionService(storage)
        await storage.append(_event("a"))
        await storage.append(_event("b", relations=(_reply_to_event("a"),)))

        first = await service.rebuild_all()
        second = await service.rebuild_all()

        assert first.scanned_events == 2
        assert first.changed_events == 2
        assert second.scanned_events == 2
        assert second.changed_events == 0
    finally:
        await storage.close()


async def test_clean_projection_skips_the_next_startup_rebuild() -> None:
    storage = _MemoryProjectionStorage([_event("a")])
    first_service = ConversationProjectionService(storage)

    first = await first_service.rebuild_all()
    await first_service.mark_clean()
    pages_before_restart = len(storage.event_id_pages)
    second = await ConversationProjectionService(storage).rebuild_all()

    assert first.skipped_current is False
    assert second.skipped_current is True
    assert second.scanned_events == 0
    assert second.changed_events == 0
    assert len(storage.event_id_pages) == pages_before_restart
    assert storage.projection_state == ConversationProjectionState(
        projection_revision=1,
        status="dirty",
    )


async def test_projection_revision_mismatch_forces_rebuild() -> None:
    storage = _MemoryProjectionStorage([_event("a")])
    storage.projection_state = ConversationProjectionState(
        projection_revision=2,
        status="clean",
    )

    summary = await ConversationProjectionService(storage).rebuild_all()

    assert summary.skipped_current is False
    assert summary.scanned_events == 1
    assert storage.projection_state == ConversationProjectionState(
        projection_revision=1,
        status="dirty",
    )


async def test_concurrent_repairs_cannot_leave_older_projection_last() -> None:
    """A stale repair cannot overwrite a newer fact's completed repair."""
    child = _event("child", relations=(_reply_to_event("parent"),))
    storage = _MemoryProjectionStorage([child])
    unresolved_write_entered = asyncio.Event()
    release_unresolved_write = asyncio.Event()
    real_put = storage.put_conversation_membership
    gated = False
    write_order: list[tuple[str, str, str | None]] = []

    async def _gated_put(membership: ConversationMembership) -> bool:
        nonlocal gated
        if (
            not gated
            and membership.event_id == "child"
            and membership.resolution_state == "unresolved"
        ):
            gated = True
            unresolved_write_entered.set()
            await release_unresolved_write.wait()
        changed = await real_put(membership)
        write_order.append(
            (
                membership.event_id,
                membership.resolution_state,
                membership.resolved_target_event_id,
            )
        )
        return changed

    storage.put_conversation_membership = _gated_put  # type: ignore[method-assign]
    service = ConversationProjectionService(storage)
    child_repair = asyncio.create_task(service.repair_after_event_available("child"))
    await unresolved_write_entered.wait()

    # The canonical parent fact arrives while the older child repair is paused.
    storage.events["parent"] = _event("parent")
    parent_repair = asyncio.create_task(
        service.repair_after_event_available("parent")
    )

    release_unresolved_write.set()
    await asyncio.gather(child_repair, parent_repair)

    child_writes = [write for write in write_order if write[0] == "child"]
    assert child_writes[-1] == ("child", "resolved", "parent")
    final = storage.memberships["child"]
    assert final.root_event_id == "parent"
    assert final.resolved_target_event_id == "parent"
    assert final.resolution_state == "resolved"


async def test_rebuild_handles_chain_beyond_python_recursion_limit() -> None:
    """Projection ancestry is iterative even for very deep conversations."""
    chain_length = 1_500
    events = [_event("node-0000")]
    events.extend(
        _event(
            f"node-{index:04d}",
            relations=(_reply_to_event(f"node-{index - 1:04d}"),),
        )
        for index in range(1, chain_length)
    )
    storage = _MemoryProjectionStorage(events)
    service = ConversationProjectionService(storage)

    summary = await service.rebuild_all()

    deepest = storage.memberships[f"node-{chain_length - 1:04d}"]
    assert summary.scanned_events == chain_length
    assert summary.changed_events == chain_length
    assert deepest.root_event_id == "node-0000"
    assert deepest.depth == chain_length - 1
    assert deepest.resolution_state == "resolved"


async def test_rebuild_reads_event_ids_in_bounded_pages() -> None:
    events = [_event(f"event-{index:04d}") for index in range(600)]
    storage = _MemoryProjectionStorage(events)

    summary = await ConversationProjectionService(storage).rebuild_all()

    expected_pages = (len(events) + _REBUILD_PAGE_SIZE - 1) // _REBUILD_PAGE_SIZE
    expected_progress = [
        events[min(end, len(events)) - 1].event_id
        for end in range(
            _REBUILD_PAGE_SIZE,
            len(events) + _REBUILD_PAGE_SIZE,
            _REBUILD_PAGE_SIZE,
        )
    ]

    assert summary.scanned_events == len(events)
    # One final empty-page read terminates the cursor scan.
    assert len(storage.event_id_pages) == expected_pages + 1
    assert all(limit == _REBUILD_PAGE_SIZE for _, limit in storage.event_id_pages)
    progress = [
        state.last_event_id
        for state in storage.projection_state_history
        if state.status == "rebuilding" and state.last_event_id is not None
    ]
    assert progress == expected_progress


async def test_interrupted_rebuild_resumes_after_persisted_cursor() -> None:
    event_count = _REBUILD_PAGE_SIZE + 37
    events = [_event(f"event-{index:04d}") for index in range(event_count)]
    storage = _MemoryProjectionStorage(events)
    completed = events[:_REBUILD_PAGE_SIZE]
    for event in completed:
        storage.memberships[event.event_id] = ConversationMembership(
            event_id=event.event_id,
            root_event_id=event.event_id,
            conversation_id=event.event_id,
            resolved_target_event_id=None,
            relation_type=None,
            depth=0,
            resolution_state="root",
        )
    resume_cursor = completed[-1].event_id
    storage.projection_state = ConversationProjectionState(
        projection_revision=1,
        status="rebuilding",
        last_event_id=resume_cursor,
    )

    summary = await ConversationProjectionService(storage).rebuild_all()

    assert summary.scanned_events == event_count - _REBUILD_PAGE_SIZE
    assert storage.event_id_pages[0] == (resume_cursor, _REBUILD_PAGE_SIZE)
    assert set(storage.memberships) == {event.event_id for event in events}
    assert storage.projection_state == ConversationProjectionState(
        projection_revision=1,
        status="dirty",
    )


async def test_dependent_repair_reuses_one_ancestry_cache_per_run() -> None:
    chain_length = 128
    events = [_event("node-0000")]
    events.extend(
        _event(
            f"node-{index:04d}",
            relations=(_reply_to_event(f"node-{index - 1:04d}"),),
        )
        for index in range(1, chain_length)
    )
    storage = _MemoryProjectionStorage(events)

    result = await ConversationProjectionService(storage).repair_after_event_available(
        "node-0000"
    )

    assert len(storage.get_call_ids) == chain_length
    assert set(storage.get_call_ids) == {event.event_id for event in events}
    assert len(result.changed_event_ids) == chain_length
    deepest = storage.memberships[f"node-{chain_length - 1:04d}"]
    assert deepest.root_event_id == "node-0000"
    assert deepest.depth == chain_length - 1


async def test_restart_after_partial_repair_converges(tmp_path: Path) -> None:
    """A crash between projection writes is repaired by the startup rebuild."""
    db_path = tmp_path / "conversation-repair.sqlite3"
    storage = SQLiteStorage(str(db_path))
    await storage.initialize()
    try:
        service = ConversationProjectionService(storage)

        await storage.append(_event("c", relations=(_reply_to_event("b"),)))
        await service.repair_after_event_available("c")
        await storage.append(_event("b", relations=(_reply_to_event("a"),)))
        await service.repair_after_event_available("b")
        await storage.append(_event("a"))

        real_put = storage.put_conversation_membership
        writes = 0

        async def _fail_second_write(membership: ConversationMembership) -> bool:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise RuntimeError("injected projection write failure")
            return await real_put(membership)

        storage.put_conversation_membership = _fail_second_write  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="injected projection write failure"):
            await service.repair_after_event_available("a")
    finally:
        await storage.close()

    reopened = SQLiteStorage(str(db_path))
    await reopened.initialize()
    try:
        rebuilt = await ConversationProjectionService(reopened).rebuild_all()
        assert rebuilt.scanned_events == 3

        a = await reopened.get_conversation_membership("a")
        b = await reopened.get_conversation_membership("b")
        c = await reopened.get_conversation_membership("c")
        assert a is not None
        assert b is not None
        assert c is not None
        assert (a.root_event_id, a.depth, a.resolution_state) == ("a", 0, "root")
        assert (b.root_event_id, b.depth, b.resolution_state) == (
            "a",
            1,
            "resolved",
        )
        assert (c.root_event_id, c.depth, c.resolution_state) == (
            "a",
            2,
            "resolved",
        )

        again = await ConversationProjectionService(reopened).rebuild_all()
        assert again.changed_events == 0
    finally:
        await reopened.close()


async def test_native_reverse_lookup_is_unique_and_deterministic(
    temp_storage: SQLiteStorage,
) -> None:
    """Native relation sources provide the reverse edge required for repair."""
    relation = _reply_to_native("$target")
    for event_id in ("child-a", "child-b"):
        await temp_storage.append(_event(event_id))
    await temp_storage.store_relation("child-a", relation)
    await temp_storage.store_relation("child-a", relation)
    await temp_storage.store_relation("child-b", relation)

    sources = await temp_storage.list_relation_sources_for_native_ref(
        "matrix", "!room:test", "$target"
    )

    assert sources == ["child-a", "child-b"]


def test_resolved_membership_requires_positive_depth() -> None:
    with pytest.raises(ValueError, match="depth must be positive"):
        ConversationMembership(
            event_id="child",
            root_event_id="parent",
            conversation_id="parent",
            resolved_target_event_id="parent",
            relation_type="reply",
            depth=0,
            resolution_state="resolved",
        )


def test_root_membership_rejects_relation_type() -> None:
    with pytest.raises(ValueError, match="root membership cannot have a relation type"):
        ConversationMembership(
            event_id="root",
            root_event_id="root",
            conversation_id="root",
            resolved_target_event_id=None,
            relation_type="reply",
            depth=0,
            resolution_state="root",
        )


def test_unresolved_membership_requires_relation_type() -> None:
    with pytest.raises(
        ValueError, match="unresolved membership requires a relation type"
    ):
        ConversationMembership(
            event_id="child",
            root_event_id="child",
            conversation_id="child",
            resolved_target_event_id=None,
            relation_type=None,
            depth=0,
            resolution_state="unresolved",
        )
