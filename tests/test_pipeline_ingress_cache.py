"""Per-ingress lookup cache tests for PipelineRunner.

Tests verify that request-scoped memoization caches for
``storage.get(event_id)`` and ``storage.list_native_refs_for_event(event_id)``
avoid redundant lookups across reaction-to-reaction checks and per-target
relation enrichment without changing relation semantics.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
)
from medre.core.events.bus import EventBus
from medre.core.events.kinds import EventKind
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.relation_enricher import RelationEnricher
from medre.core.routing import Router
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CallCountingStorage:
    """Wrapper around real storage that counts get/list_native_refs calls."""

    def __init__(self, real_storage: SQLiteStorage) -> None:
        self._real = real_storage
        self.get_call_count: int = 0
        self.get_call_ids: list[str] = []
        self.list_refs_call_count: int = 0
        self.list_refs_call_ids: list[str] = []

    async def get(self, event_id: str) -> CanonicalEvent | None:
        self.get_call_count += 1
        self.get_call_ids.append(event_id)
        return await self._real.get(event_id)

    async def list_native_refs_for_event(self, event_id: str) -> list[NativeMessageRef]:
        self.list_refs_call_count += 1
        self.list_refs_call_ids.append(event_id)
        return await self._real.list_native_refs_for_event(event_id)

    # Delegate remaining storage methods to the real storage.

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _BlockingStorage:
    """Storage wrapper that blocks on storage.get(blocked_id) until released.

    All other get calls and all other methods delegate immediately to the
    real storage.  Used to force deterministic interleaving of concurrent
    ``handle_ingress`` calls.
    """

    def __init__(
        self,
        real_storage: SQLiteStorage,
        blocked_id: str,
        block_event: asyncio.Event,
        entered_event: asyncio.Event,
    ) -> None:
        self._real = real_storage
        self._blocked_id = blocked_id
        self._block_event = block_event
        self._entered_event = entered_event
        self.get_call_ids: list[str] = []

    async def get(self, event_id: str) -> CanonicalEvent | None:
        self.get_call_ids.append(event_id)
        if event_id == self._blocked_id:
            self._entered_event.set()
            await self._block_event.wait()
        return await self._real.get(event_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _make_event(
    event_id: str = "src-001",
    event_kind: str = "message.created",
    source_adapter: str = "src",
    relations: tuple[EventRelation, ...] = (),
    payload: dict[str, Any] | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload=payload or {"text": "hello"},
        metadata=EventMetadata(),
    )


def _make_cached_get(storage: _CallCountingStorage):
    """Create a call-local event cache and memoized get closure."""
    cache: dict[str, CanonicalEvent | None] = {}

    async def cached_get(event_id: str) -> CanonicalEvent | None:
        if event_id in cache:
            return cache[event_id]
        result = await storage.get(event_id)
        cache[event_id] = result
        return result

    return cached_get


def _make_cached_list_refs(storage: _CallCountingStorage):
    """Create a call-local refs cache and memoized list closure."""
    cache: dict[str, list[NativeMessageRef]] = {}

    async def cached_list(event_id: str) -> list[NativeMessageRef]:
        if event_id in cache:
            return cache[event_id]
        result = await storage.list_native_refs_for_event(event_id)
        cache[event_id] = result
        return result

    return cached_list


# ===================================================================
# Cached get: repeated enrichment for same target_event_id reuses cache
# ===================================================================


class TestCachedGetDeduplicates:
    """storage.get() for the same target_event_id is called only once
    across reaction-to-reaction check and per-target enrichment."""

    async def test_get_called_once_for_same_target_across_enrichments(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When enriching relations for two different target adapters,
        storage.get(target_event_id) is called only once because the
        second enrichment reuses the cached result."""
        # Store a prior event that relations point to.
        prior = _make_event(event_id="prior-001", payload={"body": "original"})
        await temp_storage.append(prior)

        counting = _CallCountingStorage(temp_storage)
        config = PipelineConfig(
            storage=counting,  # type: ignore[arg-type]
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=counting),  # type: ignore[arg-type]
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="src-001", relations=(rel,))

        # Create a call-local cached get closure.
        cached_get = _make_cached_get(counting)

        # First enrichment — should call storage.get once.
        result1 = await runner._enrich_relations_for_target(
            event, "adapter-a", get_fn=cached_get
        )
        assert result1.relations[0].fallback_text == "original"
        first_count = counting.get_call_count

        # Second enrichment for a different target adapter — should NOT
        # call storage.get again because the result is cached.
        result2 = await runner._enrich_relations_for_target(
            event, "adapter-b", get_fn=cached_get
        )
        assert result2.relations[0].fallback_text == "original"

        assert counting.get_call_count == first_count
        # The target_event_id should appear only once in the call log.
        assert counting.get_call_ids.count("prior-001") == 1

    async def test_get_cache_hits_for_reaction_check_then_enrichment(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """_is_reaction_to_reaction and _enrich_relations_for_target
        share the same cache, so storage.get is called once."""
        prior = _make_event(event_id="prior-react", payload={"body": "target"})
        await temp_storage.append(prior)

        counting = _CallCountingStorage(temp_storage)
        config = PipelineConfig(
            storage=counting,  # type: ignore[arg-type]
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=counting),  # type: ignore[arg-type]
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reaction",
            target_event_id="prior-react",
            target_native_ref=None,
            key="\U0001f44d",
            fallback_text=None,
        )
        event = _make_event(
            event_id="src-react",
            event_kind=EventKind.MESSAGE_REACTED,
            relations=(rel,),
        )

        # Create a shared cached get closure.
        cached_get = _make_cached_get(counting)

        # Call reaction check — should call storage.get once.
        is_reaction = await runner._is_reaction_to_reaction(event, get_fn=cached_get)
        assert is_reaction is False  # prior is not a reaction
        count_after_check = counting.get_call_count
        assert count_after_check == 1

        # Now enrich — should reuse cached get, not call storage again.
        result = await runner._enrich_relations_for_target(
            event, "adapter-a", get_fn=cached_get
        )
        assert result.relations[0].fallback_text == "target"
        assert counting.get_call_count == count_after_check


# ===================================================================
# Cached list_native_refs: deduplicates across enrichment calls
# ===================================================================


class TestCachedListRefsDeduplicates:
    """list_native_refs_for_event for the same event_id is called only
    once across multiple enrichment calls."""

    async def test_list_refs_called_once_for_same_target(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Enriching for two different target adapters reuses cached
        list_native_refs_for_event result."""
        # Store a native ref for the prior event.
        nref = NativeMessageRef(
            id="nref-001",
            event_id="prior-refs",
            adapter="adapter-a",
            native_channel_id="ch-1",
            native_message_id="msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(nref)

        counting = _CallCountingStorage(temp_storage)
        config = PipelineConfig(
            storage=counting,  # type: ignore[arg-type]
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=counting),  # type: ignore[arg-type]
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-refs",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="src-refs", relations=(rel,))

        # Create a call-local cached list closure.
        cached_list = _make_cached_list_refs(counting)

        # First enrichment.
        result1 = await runner._enrich_relations_for_target(
            event, "adapter-a", list_fn=cached_list
        )
        assert result1.relations[0].target_native_ref is not None
        first_count = counting.list_refs_call_count

        # Second enrichment for a different adapter — cached result reused.
        await runner._enrich_relations_for_target(
            event, "adapter-b", list_fn=cached_list
        )
        assert counting.list_refs_call_count == first_count
        assert counting.list_refs_call_ids.count("prior-refs") == 1


# ===================================================================
# Missing targets cached safely
# ===================================================================


class TestMissingTargetsCachedSafely:
    """storage.get returning None is cached and not re-fetched."""

    async def test_missing_target_cached_as_none(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When storage.get returns None (target doesn't exist), the
        None is cached and subsequent lookups skip storage.get entirely."""
        counting = _CallCountingStorage(temp_storage)
        config = PipelineConfig(
            storage=counting,  # type: ignore[arg-type]
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=counting),  # type: ignore[arg-type]
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="nonexistent-target",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="src-missing", relations=(rel,))

        # Create a call-local cached get closure.
        cached_get = _make_cached_get(counting)

        # First enrichment — storage.get called once, returns None.
        result1 = await runner._enrich_relations_for_target(
            event, "adapter-a", get_fn=cached_get
        )
        assert result1.relations[0].fallback_text is None
        assert counting.get_call_count == 1

        # Second enrichment — cached None reused.
        result2 = await runner._enrich_relations_for_target(
            event, "adapter-b", get_fn=cached_get
        )
        assert result2.relations[0].fallback_text is None
        assert counting.get_call_count == 1  # No additional call

    async def test_missing_refs_cached_as_empty(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When list_native_refs_for_event returns [], the empty list
        is cached and reused."""
        counting = _CallCountingStorage(temp_storage)
        config = PipelineConfig(
            storage=counting,  # type: ignore[arg-type]
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=counting),  # type: ignore[arg-type]
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="no-refs-target",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="src-no-refs", relations=(rel,))

        # Create a call-local cached list closure.
        cached_list = _make_cached_list_refs(counting)

        # First enrichment.
        await runner._enrich_relations_for_target(
            event, "adapter-a", list_fn=cached_list
        )
        assert counting.list_refs_call_count == 1

        # Second enrichment — cached empty list reused.
        await runner._enrich_relations_for_target(
            event, "adapter-b", list_fn=cached_list
        )
        assert counting.list_refs_call_count == 1


# ===================================================================
# Cache isolation between ingress calls (call-local scoping)
# ===================================================================


class TestCacheIsolationBetweenIngress:
    """Per-ingress caches are call-local — each handle_ingress gets
    fresh caches without sharing or contamination."""

    async def test_sequential_ingress_calls_get_fresh_caches(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Two sequential handle_ingress calls each get their own
        caches. The second call does not reuse cached data from the first."""
        prior = _make_event(event_id="prior-clear", payload={"body": "cached"})
        await temp_storage.append(prior)

        counting = _CallCountingStorage(temp_storage)

        from medre.adapters.fakes.presentation import FakePresentationAdapter

        adapter = FakePresentationAdapter(adapter_id="target")
        from medre.core.routing import Route, RouteSource, RouteTarget

        route = Route(
            id="cache-clear-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        config = make_pipeline_config_for_pipeline(
            storage=counting,  # type: ignore[arg-type]
            router=Router(routes=[route]),
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event1 = _make_event(event_id="src-clear-1", source_adapter="src")
        event2 = _make_event(event_id="src-clear-2", source_adapter="src")

        try:
            await runner.handle_ingress(event1)
            await runner.handle_ingress(event2)
            # Both calls completed without error. Each call used its own
            # call-local cache — no shared state between them.
        finally:
            await runner.stop()

    async def test_no_instance_level_cache_attributes(self) -> None:
        """PipelineRunner no longer has instance-level ingress cache
        attributes (caches are call-local)."""
        config = PipelineConfig(
            storage=None,  # type: ignore[arg-type]
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=None),  # type: ignore[arg-type]
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)
        assert not hasattr(runner, "_ingress_event_cache")
        assert not hasattr(runner, "_ingress_refs_cache")
        assert not hasattr(runner, "_cached_get")
        assert not hasattr(runner, "_cached_list_native_refs")


# ===================================================================
# Concurrent handle_ingress calls do not share caches
# ===================================================================


class TestConcurrentIngressCacheIsolation:
    """Concurrent handle_ingress calls on the same PipelineRunner
    instance cannot share, clear, or contaminate each other's caches."""

    async def test_concurrent_ingress_no_cache_contamination(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Two handle_ingress calls running concurrently with different
        events do not share or contaminate each other's lookup caches."""
        # Store two distinct prior events as relation targets.
        prior_a = _make_event(event_id="prior-conc-a", payload={"body": "target-a"})
        prior_b = _make_event(event_id="prior-conc-b", payload={"body": "target-b"})
        await temp_storage.append(prior_a)
        await temp_storage.append(prior_b)

        counting = _CallCountingStorage(temp_storage)

        from medre.adapters.fakes.presentation import FakePresentationAdapter

        adapter = FakePresentationAdapter(adapter_id="target")
        from medre.core.routing import Route, RouteSource, RouteTarget

        route = Route(
            id="concurrency-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        config = make_pipeline_config_for_pipeline(
            storage=counting,  # type: ignore[arg-type]
            router=Router(routes=[route]),
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Two events with relations to different targets.
        rel_a = EventRelation(
            relation_type="reply",
            target_event_id="prior-conc-a",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event_a = _make_event(
            event_id="src-conc-a",
            source_adapter="src",
            relations=(rel_a,),
        )

        rel_b = EventRelation(
            relation_type="reply",
            target_event_id="prior-conc-b",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event_b = _make_event(
            event_id="src-conc-b",
            source_adapter="src",
            relations=(rel_b,),
        )

        try:
            results = await asyncio.gather(
                runner.handle_ingress(event_a),
                runner.handle_ingress(event_b),
            )
        finally:
            await runner.stop()

        # Both calls complete successfully with outcomes.
        outcomes_a, outcomes_b = results
        assert len(outcomes_a) == 1
        assert len(outcomes_b) == 1

        # Each target event_id was looked up exactly once (no cross-call
        # contamination, no double-lookups from cache clearing).
        assert counting.get_call_ids.count("prior-conc-a") == 1
        assert counting.get_call_ids.count("prior-conc-b") == 1

    async def test_blocking_storage_proves_cache_isolation(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Forces true interleaving: one ingress blocks mid-storage-get
        while the other proceeds through its own call-local cache.

        Uses ``asyncio.Event`` (no sleeps) to guarantee that
        ``handle_ingress(event_a)`` is suspended inside
        ``storage.get("prior-a")`` while ``handle_ingress(event_b)``
        completes its entire lifecycle — proving the two calls never
        share lookup caches.
        """
        # -- Arrange: two prior events stored --------------------------------
        prior_a = _make_event(event_id="prior-a", payload={"body": "target-a"})
        prior_b = _make_event(event_id="prior-b", payload={"body": "target-b"})
        await temp_storage.append(prior_a)
        await temp_storage.append(prior_b)

        # storage.get("prior-a") blocks until we release it;
        # storage.get("prior-b") returns immediately.
        block_event = asyncio.Event()
        entered_event = asyncio.Event()
        blocking_storage = _BlockingStorage(
            real_storage=temp_storage,
            blocked_id="prior-a",
            block_event=block_event,
            entered_event=entered_event,
        )

        from medre.adapters.fakes.presentation import FakePresentationAdapter
        from medre.core.routing import Route, RouteSource, RouteTarget

        adapter = FakePresentationAdapter(adapter_id="target")
        route = Route(
            id="blocking-concurrency-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        config = make_pipeline_config_for_pipeline(
            storage=blocking_storage,  # type: ignore[arg-type]
            router=Router(routes=[route]),
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Two inbound events with relations to different targets.
        event_a = _make_event(
            event_id="src-a",
            source_adapter="src",
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id="prior-a",
                    target_native_ref=None,
                    key=None,
                    fallback_text=None,
                ),
            ),
        )
        event_b = _make_event(
            event_id="src-b",
            source_adapter="src",
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id="prior-b",
                    target_native_ref=None,
                    key=None,
                    fallback_text=None,
                ),
            ),
        )

        try:
            # -- Act: launch task_a, wait for it to enter the blocked get --
            task_a = asyncio.create_task(runner.handle_ingress(event_a))

            # Wait until task_a is suspended inside storage.get("prior-a").
            await asyncio.wait_for(entered_event.wait(), timeout=2.0)
            assert not task_a.done()

            # task_b completes immediately (no blocking on "prior-b").
            task_b = asyncio.create_task(runner.handle_ingress(event_b))
            outcomes_b = await asyncio.wait_for(task_b, timeout=2.0)
            assert len(outcomes_b) == 1

            # task_a is still suspended — task_b finished independently.
            assert not task_a.done()

            # Release the block so task_a can proceed.
            block_event.set()
            outcomes_a = await asyncio.wait_for(task_a, timeout=2.0)
            assert len(outcomes_a) == 1
        finally:
            await runner.stop()

        # -- Assert ----------------------------------------------------------
        # Each target was looked up exactly once from the real storage.
        assert blocking_storage.get_call_ids.count("prior-a") == 1
        assert blocking_storage.get_call_ids.count("prior-b") == 1

        # event_b finished *before* block_event was set, proving the
        # interleaving was genuine — event_a was mid-storage-get while
        # event_b ran its full course through an independent call-local
        # cache.  If caches were shared, event_a's blocked lookup for
        # "prior-a" could have been polluted by event_b's "prior-b"
        # result (or vice versa), but each lookup happened once and only
        # for its own target.


# ===================================================================
# RelationEnricher with cached callables: no behavior change
# ===================================================================


class TestEnricherWithCachedCallables:
    """RelationEnricher produces identical results with or without
    cached callables — only lookup efficiency changes."""

    async def test_enrichment_same_with_and_without_cache(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Enrichment results are identical with cached vs uncached callables."""
        prior = _make_event(event_id="prior-eq", payload={"body": "same result"})
        await temp_storage.append(prior)

        nref = NativeMessageRef(
            id="nref-eq",
            event_id="prior-eq",
            adapter="adapter-a",
            native_channel_id="ch-1",
            native_message_id="msg-eq",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(nref)

        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-eq",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="src-eq", relations=(rel,))

        # Without cached callables.
        enricher = RelationEnricher(
            storage=temp_storage,
            logger=logging.getLogger("test.cache"),
        )
        result_uncached = await enricher.enrich_for_target(
            event, target_adapter="adapter-a"
        )

        # With cached callables that delegate to storage.
        async def _cached_get(eid: str) -> CanonicalEvent | None:
            return await temp_storage.get(eid)

        async def _cached_list(eid: str) -> list[NativeMessageRef]:
            return await temp_storage.list_native_refs_for_event(eid)

        result_cached = await enricher.enrich_for_target(
            event,
            target_adapter="adapter-a",
            cached_get_fn=_cached_get,
            cached_list_fn=_cached_list,
        )

        # Both results should have identical enrichment.
        assert result_cached.relations[0].target_native_ref is not None
        assert (
            result_cached.relations[0].target_native_ref
            == result_uncached.relations[0].target_native_ref
        )
        assert result_cached.relations[0].fallback_text == "same result"
        assert result_uncached.relations[0].fallback_text == "same result"

    async def test_enricher_without_cached_callables_default_behavior(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When cached callables are not provided, RelationEnricher
        falls back to getattr(storage, ...) — preserving default behavior."""
        prior = _make_event(event_id="prior-default", payload={"body": "default"})
        await temp_storage.append(prior)

        enricher = RelationEnricher(
            storage=temp_storage,
            logger=logging.getLogger("test.cache"),
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="prior-default",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(event_id="src-default", relations=(rel,))

        result = await enricher.enrich_for_target(event, target_adapter="adapter-a")
        assert result.relations[0].fallback_text == "default"


# ===================================================================
# Cache does not change relation semantics
# ===================================================================


class TestCacheDoesNotChangeSemantics:
    """Caching is transparent — relation outcomes are identical."""

    async def test_reaction_to_reaction_still_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Reaction-to-reaction suppression still works with cached get."""
        # Store a reaction event (target is itself a reaction).
        target_reaction = CanonicalEvent(
            event_id="target-reaction-001",
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "reacted"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(target_reaction)

        counting = _CallCountingStorage(temp_storage)
        config = PipelineConfig(
            storage=counting,  # type: ignore[arg-type]
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=counting),  # type: ignore[arg-type]
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reaction",
            target_event_id="target-reaction-001",
            target_native_ref=None,
            key="\U0001f44d",
            fallback_text=None,
        )
        event = _make_event(
            event_id="src-r2r",
            event_kind=EventKind.MESSAGE_REACTED,
            relations=(rel,),
        )

        # With cached get — suppression still works.
        cached_get = _make_cached_get(counting)
        is_reaction = await runner._is_reaction_to_reaction(event, get_fn=cached_get)
        assert is_reaction is True
        # Only one storage.get call — result cached.
        assert counting.get_call_count == 1

    async def test_non_reaction_not_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A regular message is not suppressed even with caching."""
        prior = _make_event(event_id="prior-not-react")
        await temp_storage.append(prior)

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        rel = EventRelation(
            relation_type="reaction",
            target_event_id="prior-not-react",
            target_native_ref=None,
            key="\U0001f44d",
            fallback_text=None,
        )
        event = _make_event(
            event_id="src-not-r2r",
            event_kind=EventKind.MESSAGE_REACTED,
            relations=(rel,),
        )

        is_reaction = await runner._is_reaction_to_reaction(event)
        assert is_reaction is False
