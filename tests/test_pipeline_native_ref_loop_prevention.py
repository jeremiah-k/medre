"""Pipeline native reference persistence and loop prevention tests.

Proves that native refs persist correctly, duplicates are detected,
bridge loops are suppressed with evidence, replay uses stored refs,
and relation mapping works across adapter types at the planning layer.

No transport SDK imports — all tests exercise the planning/evidence layer
using fake adapters and SQLiteStorage.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.contracts.adapter import AdapterDeliveryResult
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.bus import EventBus
from medre.core.events.metadata import RoutingMetadata
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SpyAdapter:
    """Minimal adapter that records whether deliver() was called."""

    adapter_id: str
    platform: str = "test"
    deliver_calls: list[object]

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.deliver_calls = []

    async def deliver(self, payload: object) -> AdapterDeliveryResult:
        self.deliver_calls.append(payload)
        return AdapterDeliveryResult(
            native_message_id=f"native-{self.adapter_id}-001",
            native_channel_id=f"ch-{self.adapter_id}",
        )


def _make_event_with_native_ref(
    event_id: str = "evt-nref-001",
    source_adapter: str = "src",
    nref_adapter: str = "matrix",
    nref_channel: str = "!room:server",
    nref_message_id: str = "$event-001",
    relations: tuple[EventRelation, ...] = (),
    metadata: EventMetadata | None = None,
) -> CanonicalEvent:
    """Create a CanonicalEvent with a source_native_ref attached."""
    nref = NativeRef(
        adapter=nref_adapter,
        native_channel_id=nref_channel,
        native_message_id=nref_message_id,
    )
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"text": "hello"},
        metadata=metadata or EventMetadata(),
        source_native_ref=nref,
    )


# ===================================================================
# a) Native message ref persistence
# ===================================================================


class TestNativeMessageRefPersistence:
    """Pipeline persists inbound NativeMessageRef and it is retrievable."""

    async def test_native_message_ref_persisted_and_retrievable(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Inbound native ref is stored via SQLiteStorage and resolvable."""
        spy = _SpyAdapter("target")

        route = Route(
            id="persist-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": spy},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event_with_native_ref(
            event_id="persist-001",
            source_adapter="src",
            nref_adapter="matrix",
            nref_channel="!persist:server",
            nref_message_id="$persist-msg-001",
        )

        try:
            await runner.handle_ingress(event)

            # resolve_native_ref returns the canonical event_id
            resolved = await temp_storage.resolve_native_ref(
                "matrix", "!persist:server", "$persist-msg-001"
            )
            assert resolved == "persist-001"

            # NativeMessageRef row exists with correct fields
            rows = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'inbound'",
                ("persist-001",),
            )
            assert len(rows) == 1
            row = rows[0]
            assert row["adapter"] == "matrix"
            assert row["native_channel_id"] == "!persist:server"
            assert row["native_message_id"] == "$persist-msg-001"
            assert row["direction"] == "inbound"
        finally:
            await runner.stop()

    async def test_outbound_native_ref_persisted_after_delivery(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Outbound native ref from adapter delivery result is persisted."""

        class _AdapterWithNativeRef:
            adapter_id = "out_target"
            platform = "test"

            async def deliver(self, payload: object) -> AdapterDeliveryResult:
                return AdapterDeliveryResult(
                    native_message_id="out-msg-42",
                    native_channel_id="ch-out",
                )

        adapter = _AdapterWithNativeRef()
        route = Route(
            id="out-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="out_target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"out_target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="out-persist-001", source_adapter="src")

        try:
            await runner.handle_ingress(event)

            rows = await temp_storage._read_all(
                "SELECT * FROM native_message_refs "
                "WHERE event_id = ? AND direction = 'outbound'",
                ("out-persist-001",),
            )
            assert len(rows) == 1
            assert rows[0]["adapter"] == "out_target"
            assert rows[0]["native_message_id"] == "out-msg-42"
            assert rows[0]["native_channel_id"] == "ch-out"
        finally:
            await runner.stop()


# ===================================================================
# b) Duplicate native message detection
# ===================================================================


class TestDuplicateNativeMessageDetection:
    """Duplicate native messages are detected and suppressed."""

    async def test_duplicate_native_ref_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Second event with the same native ref triple is suppressed."""
        spy = _SpyAdapter("dedup-target")

        route = Route(
            id="dedup-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="dedup-target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dedup-target": spy},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event1 = _make_event_with_native_ref(
            event_id="dedup-001",
            source_adapter="src",
            nref_adapter="matrix",
            nref_channel="!dedup:server",
            nref_message_id="$dedup-msg-001",
        )
        event2 = _make_event_with_native_ref(
            event_id="dedup-002",
            source_adapter="src",
            nref_adapter="matrix",
            nref_channel="!dedup:server",
            nref_message_id="$dedup-msg-001",  # same triple
        )

        try:
            outcomes1 = await runner.handle_ingress(event1)
            assert len(outcomes1) >= 1, "First event should be accepted"

            outcomes2 = await runner.handle_ingress(event2)
            assert outcomes2 == [], "Duplicate should be suppressed"

            # Only first event stored
            stored1 = await temp_storage.get("dedup-001")
            assert stored1 is not None
            stored2 = await temp_storage.get("dedup-002")
            assert stored2 is None

            # Adapter called exactly once
            assert len(spy.deliver_calls) == 1
        finally:
            await runner.stop()

    async def test_different_native_refs_both_accepted(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Events with different native ref triples are both accepted."""
        spy = _SpyAdapter("twice-target")

        route = Route(
            id="twice-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="twice-target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"twice-target": spy},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event_a = _make_event_with_native_ref(
            event_id="twice-a",
            nref_message_id="$msg-a",
        )
        event_b = _make_event_with_native_ref(
            event_id="twice-b",
            nref_message_id="$msg-b",
        )

        try:
            await runner.handle_ingress(event_a)
            await runner.handle_ingress(event_b)

            stored_a = await temp_storage.get("twice-a")
            stored_b = await temp_storage.get("twice-b")
            assert stored_a is not None
            assert stored_b is not None

            assert len(spy.deliver_calls) == 2
        finally:
            await runner.stop()


# ===================================================================
# c) Bridge loop avoidance
# ===================================================================


class TestBridgeLoopAvoidance:
    """Bridge loops are suppressed — event not sent back to source adapter."""

    async def test_self_loop_suppresses_delivery(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event from adapter A routed back to adapter A is suppressed."""
        spy = _SpyAdapter("bridge-loop-a")

        # Route where source adapter == target adapter
        route = Route(
            id="self-loop-route",
            source=RouteSource(
                adapter="bridge-loop-a",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bridge-loop-a", channel="ch-loop")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"bridge-loop-a": spy},
        )
        config.runtime_accounting = accounting
        config.route_stats = route_stats
        runner = PipelineRunner(config)
        await runner.start()

        event = CanonicalEvent(
            event_id="self-loop-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="bridge-loop-a",
            source_transport_id="node-1",
            source_channel_id="ch-loop",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "loop test"},
            metadata=EventMetadata(),
        )

        try:
            outcomes = await runner.handle_ingress(event)

            # Outcome is skipped
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
            assert outcomes[0].failure_kind is DeliveryFailureKind.LOOP_SUPPRESSED

            # Adapter NOT called
            assert len(spy.deliver_calls) == 0

            # Accounting reflects loop_prevented
            snap = accounting.snapshot()
            assert snap["loop_prevented"] == 1
        finally:
            await runner.stop()

    async def test_cross_adapter_does_not_trigger_loop(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event from A delivered to B (different adapters) is NOT suppressed."""
        spy_a = _SpyAdapter("cross-a")
        spy_b = _SpyAdapter("cross-b")

        route = Route(
            id="cross-route",
            source=RouteSource(
                adapter="cross-a",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="cross-b")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"cross-a": spy_a, "cross-b": spy_b},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cross-001",
            source_adapter="cross-a",
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Source adapter NOT called; target adapter called
            assert len(spy_a.deliver_calls) == 0
            assert len(spy_b.deliver_calls) == 1
        finally:
            await runner.stop()

    async def test_route_trace_loop_suppresses(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event with route_trace showing route already traversed is suppressed."""
        spy = _SpyAdapter("trace-target")

        route = Route(
            id="trace-loop-route",
            source=RouteSource(
                adapter="trace-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="trace-target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"trace-target": spy},
        )
        config.runtime_accounting = accounting
        config.route_stats = route_stats
        runner = PipelineRunner(config)
        await runner.start()

        # Event with route_trace showing this route already traversed twice
        event = CanonicalEvent(
            event_id="trace-loop-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="trace-src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "trace loop"},
            metadata=EventMetadata(
                routing=RoutingMetadata(
                    route_trace=("trace-loop-route", "trace-loop-route"),
                ),
            ),
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
            assert outcomes[0].failure_kind is DeliveryFailureKind.LOOP_SUPPRESSED
            assert "route already traversed" in (outcomes[0].error or "")

            # Adapter NOT called
            assert len(spy.deliver_calls) == 0

            snap = accounting.snapshot()
            assert snap["loop_prevented"] == 1
        finally:
            await runner.stop()


# ===================================================================
# d) Route trace loop suppression evidence
# ===================================================================


class TestRouteTraceLoopSuppressionEvidence:
    """Loop suppression produces evidence with event, route, target, reason."""

    async def test_suppression_evidence_includes_all_fields(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Suppressed outcome carries event_id, route_id, target_adapter,
        failure_kind, reason, and adapter was NOT called."""
        spy = _SpyAdapter("evidence-target")

        route = Route(
            id="evidence-route",
            source=RouteSource(
                adapter="evidence-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="evidence-target", channel="ch-ev")],
        )
        router = Router(routes=[route])
        route_stats = RouteStats()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"evidence-target": spy},
        )
        config.route_stats = route_stats
        runner = PipelineRunner(config)
        await runner.start()

        event = CanonicalEvent(
            event_id="evidence-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="evidence-src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "evidence test"},
            metadata=EventMetadata(
                routing=RoutingMetadata(
                    route_trace=("evidence-route", "evidence-route"),
                ),
            ),
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]

            # event_id present and correct
            assert outcome.event_id == "evidence-001"

            # route_id present and correct
            assert outcome.route_id == "evidence-route"

            # target_adapter known
            assert outcome.target_adapter == "evidence-target"

            # Reason: failure_kind is LOOP_SUPPRESSED
            assert outcome.failure_kind is DeliveryFailureKind.LOOP_SUPPRESSED
            assert "route already traversed" in (outcome.error or "")

            # Suppressed receipt persisted
            assert outcome.receipt is not None
            assert outcome.receipt.status == "suppressed"
            assert outcome.receipt.failure_kind == "loop_suppressed"
            assert outcome.receipt.event_id == "evidence-001"
            assert outcome.receipt.target_adapter == "evidence-target"
            assert outcome.receipt.route_id == "evidence-route"

            # Adapter.send was NOT called
            assert len(spy.deliver_calls) == 0

            # RouteStats shows loop_prevented
            stats = route_stats.snapshot()
            assert stats["evidence-route"]["loop_prevented"] == 1
            assert stats["evidence-route"]["delivered"] == 0
        finally:
            await runner.stop()

    async def test_self_loop_suppression_evidence_complete(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Self-loop suppression evidence has event_id, route_id,
        target_adapter, failure_kind, and adapter NOT called."""
        spy = _SpyAdapter("self-ev-target")

        route = Route(
            id="self-ev-route",
            source=RouteSource(
                adapter="self-ev-target",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="self-ev-target", channel="ch-se")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"self-ev-target": spy},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = CanonicalEvent(
            event_id="self-ev-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="self-ev-target",
            source_transport_id="node-1",
            source_channel_id="ch-se",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "self-loop evidence"},
            metadata=EventMetadata(),
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            o = outcomes[0]

            # event_id
            assert o.event_id == "self-ev-001"
            # route_id
            assert o.route_id == "self-ev-route"
            # target_adapter
            assert o.target_adapter == "self-ev-target"
            # failure_kind + reason
            assert o.failure_kind is DeliveryFailureKind.LOOP_SUPPRESSED
            assert "loop_prevented" in (o.error or "")
            # receipt persisted
            assert o.receipt is not None
            assert o.receipt.failure_kind == "loop_suppressed"
            # adapter NOT called
            assert len(spy.deliver_calls) == 0
        finally:
            await runner.stop()


# ===================================================================
# e) Replay uses stored native refs consistently
# ===================================================================


class TestReplayUsesStoredNativeRefsConsistently:
    """Stored native refs are stable and retrievable for replay scenarios."""

    async def test_resolve_native_ref_returns_original_event_id(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """After pipeline stores an event with native ref, resolve_native_ref
        consistently returns the same canonical event_id."""
        spy = _SpyAdapter("replay-target")

        route = Route(
            id="replay-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="replay-target")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"replay-target": spy},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event_with_native_ref(
            event_id="replay-001",
            nref_adapter="matrix",
            nref_channel="!replay:server",
            nref_message_id="$replay-msg-001",
        )

        try:
            await runner.handle_ingress(event)

            # Resolve multiple times — always same result
            for _ in range(3):
                resolved = await temp_storage.resolve_native_ref(
                    "matrix", "!replay:server", "$replay-msg-001"
                )
                assert resolved == "replay-001"
        finally:
            await runner.stop()

    async def test_inbound_and_outbound_refs_both_stored(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Both inbound and outbound native refs are stored for the same event
        and retrievable consistently."""

        class _OutAdapter:
            adapter_id = "out-replay"
            platform = "test"

            async def deliver(self, payload: object) -> AdapterDeliveryResult:
                return AdapterDeliveryResult(
                    native_message_id="out-rp-001",
                    native_channel_id="ch-out-rp",
                )

        adapter = _OutAdapter()
        route = Route(
            id="rp-dual-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="out-replay")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"out-replay": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event_with_native_ref(
            event_id="rp-dual-001",
            nref_adapter="matrix",
            nref_channel="!dual:server",
            nref_message_id="$dual-msg-001",
        )

        try:
            await runner.handle_ingress(event)

            # Inbound ref resolvable
            inbound = await temp_storage.resolve_native_ref(
                "matrix", "!dual:server", "$dual-msg-001"
            )
            assert inbound == "rp-dual-001"

            # Both directions stored
            rows = await temp_storage._read_all(
                "SELECT direction FROM native_message_refs WHERE event_id = ?",
                ("rp-dual-001",),
            )
            directions = {r["direction"] for r in rows}
            assert "inbound" in directions
            assert "outbound" in directions
        finally:
            await runner.stop()

    async def test_native_ref_survives_multiple_lookups(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Native refs are durable — repeated lookups yield the same result."""
        nref = NativeMessageRef(
            id="nref-dur-001",
            event_id="dur-001",
            adapter="meshtastic",
            native_channel_id="ch-3",
            native_message_id="mesh-msg-99",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        await temp_storage.store_native_ref(nref)

        for _ in range(5):
            result = await temp_storage.resolve_native_ref(
                "meshtastic", "ch-3", "mesh-msg-99"
            )
            assert result == "dur-001"


# ===================================================================
# f) Relation mapping with native refs (planning layer)
# ===================================================================


class TestRelationMappingWithNativeRefs:
    """Relation mapping across adapter-style targets at the planning layer.

    Tests that _enrich_relations_for_target resolves native refs correctly
    for Matrix-style and Meshtastic-style adapter targets without importing
    any transport SDKs in core.
    """

    async def test_relation_enriched_for_matrix_style_target(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Relation gets correct native ref for a Matrix-style target adapter."""
        # Pre-store outbound native ref for prior event on matrix target
        prior_ref = NativeMessageRef(
            id="nref-matrix-rel",
            event_id="prior-matrix-001",
            adapter="matrix_bridge",
            native_channel_id="!matrix:server.org",
            native_message_id="$matrix-msg-001",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(prior_ref)

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
            relation_type="reply",
            target_event_id="prior-matrix-001",
            target_native_ref=None,
            key=None,
            fallback_text="original message",
        )
        event = CanonicalEvent(
            event_id="rel-matrix-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"text": "reply to matrix"},
            metadata=EventMetadata(),
        )

        enriched = await runner._enrich_relations_for_target(event, "matrix_bridge")
        assert enriched.relations[0].target_native_ref is not None
        nref = enriched.relations[0].target_native_ref
        assert nref.adapter == "matrix_bridge"
        assert nref.native_channel_id == "!matrix:server.org"
        assert nref.native_message_id == "$matrix-msg-001"

        # Preserved original relation fields
        assert enriched.relations[0].target_event_id == "prior-matrix-001"
        assert enriched.relations[0].fallback_text == "original message"

    async def test_relation_enriched_for_meshtastic_style_target(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Relation gets correct native ref for a Meshtastic-style target."""
        prior_ref = NativeMessageRef(
            id="nref-mesh-rel",
            event_id="prior-mesh-001",
            adapter="mesh_bridge",
            native_channel_id="3",
            native_message_id="mesh-pkt-42",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        await temp_storage.store_native_ref(prior_ref)

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
            relation_type="reply",
            target_event_id="prior-mesh-001",
            target_native_ref=None,
            key=None,
            fallback_text="mesh original",
        )
        event = CanonicalEvent(
            event_id="rel-mesh-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"text": "reply to mesh"},
            metadata=EventMetadata(),
        )

        enriched = await runner._enrich_relations_for_target(event, "mesh_bridge")
        assert enriched.relations[0].target_native_ref is not None
        nref = enriched.relations[0].target_native_ref
        assert nref.adapter == "mesh_bridge"
        assert nref.native_channel_id == "3"
        assert nref.native_message_id == "mesh-pkt-42"

    async def test_relation_enrichment_no_cross_contamination(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Enriching for one adapter does not leak another adapter's ref."""
        # Store refs for two different adapters
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-x-matrix",
                event_id="prior-cross-001",
                adapter="matrix_a",
                native_channel_id="!mx:server",
                native_message_id="$mx-001",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
            )
        )
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-x-mesh",
                event_id="prior-cross-001",
                adapter="mesh_b",
                native_channel_id="0",
                native_message_id="mesh-001",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
            )
        )

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
            relation_type="reply",
            target_event_id="prior-cross-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="rel-cross-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"text": "reply"},
            metadata=EventMetadata(),
        )

        # Enrich for matrix_a — should get matrix_a ref, not mesh_b
        enriched_mx = await runner._enrich_relations_for_target(event, "matrix_a")
        assert enriched_mx.relations[0].target_native_ref is not None
        assert enriched_mx.relations[0].target_native_ref.adapter == "matrix_a"
        assert enriched_mx.relations[0].target_native_ref.native_message_id == "$mx-001"

        # Enrich for mesh_b — should get mesh_b ref, not matrix_a
        enriched_mesh = await runner._enrich_relations_for_target(event, "mesh_b")
        assert enriched_mesh.relations[0].target_native_ref is not None
        assert enriched_mesh.relations[0].target_native_ref.adapter == "mesh_b"
        assert (
            enriched_mesh.relations[0].target_native_ref.native_message_id == "mesh-001"
        )

    async def test_relation_channel_aware_enrichment(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Channel-aware enrichment selects the correct ref when multiple
        channels exist for the same adapter (Meshtastic multi-channel)."""
        # Two outbound refs for the same event/adapter but different channels
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-ch0",
                event_id="prior-ch-001",
                adapter="mesh_multi",
                native_channel_id="0",
                native_message_id="ch0-msg",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
            )
        )
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-ch3",
                event_id="prior-ch-001",
                adapter="mesh_multi",
                native_channel_id="3",
                native_message_id="ch3-msg",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
            )
        )

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
            relation_type="reply",
            target_event_id="prior-ch-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="rel-ch-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"text": "channel reply"},
            metadata=EventMetadata(),
        )

        # Enrich for channel "3" — should get ch3-msg
        enriched = await runner._enrich_relations_for_target(
            event, "mesh_multi", target_channel="3"
        )
        assert enriched.relations[0].target_native_ref is not None
        assert enriched.relations[0].target_native_ref.native_message_id == "ch3-msg"
        assert enriched.relations[0].target_native_ref.native_channel_id == "3"
