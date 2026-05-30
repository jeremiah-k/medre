"""ReplayEngine plan filtering helpers and capability-aware delivery.

Tests _filter_plans_by_adapter, _filter_plans_by_capability,
_stage_deliver BEST_EFFORT/DRY_RUN capability filtering, and
relation-aware capability filtering.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.replay import StubPipeline, make_engine

# ===================================================================
# Shared helpers
# ===================================================================


def _make_delivery_plan(
    adapter: str | None = "matrix-bridge",
) -> Any:
    """Build a minimal DeliveryPlan-like object for filter tests."""
    from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
    from medre.core.routing.models import RouteTarget

    target = RouteTarget(adapter=adapter, channel="ch-out")
    return DeliveryPlan(
        plan_id="plan-001",
        event_id="evt-001",
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )


def _make_capability_route_and_pipeline(
    event_kind: str,
    target_adapter: str,
    caps: AdapterCapabilities,
    source_adapter: str = "fake_transport",
    source_channel: str = "ch-0",
) -> tuple[Router, StubPipeline]:
    """Build a router + pipeline with a single route and adapter caps."""
    route = Route(
        id="cap-route",
        source=RouteSource(
            adapter=source_adapter,
            event_kinds=(event_kind,),
            channel=source_channel,
        ),
        targets=[RouteTarget(adapter=target_adapter)],
    )
    router = Router(routes=[route])

    class _CapAdapter:
        _capabilities = caps

    class _Config:
        adapters = {target_adapter: _CapAdapter()}

    class CapStubPipeline(StubPipeline):
        _config = _Config()

    return router, CapStubPipeline(router=router)


# ===================================================================
# _filter_plans_by_adapter
# ===================================================================


class TestFilterPlansByAdapter:
    """Tests for _filter_plans_by_adapter matching logic."""

    def test_matching_adapter_included(self) -> None:
        """Plan with matching target adapter is included."""
        from medre.core.engine.replay.delivery import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter="matrix-bridge")
        result = _filter_plans_by_adapter([plan], ["matrix-bridge"])
        assert len(result) == 1

    def test_non_matching_adapter_excluded(self) -> None:
        """Plan with non-matching adapter is excluded."""
        from medre.core.engine.replay.delivery import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter="matrix-bridge")
        result = _filter_plans_by_adapter([plan], ["other-adapter"])
        assert len(result) == 0

    def test_none_adapter_included_conservatively(self) -> None:
        """Plan with adapter=None is included (conservative)."""
        from medre.core.engine.replay.delivery import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter=None)
        result = _filter_plans_by_adapter([plan], ["matrix-bridge"])
        assert len(result) == 1

    def test_tuple_plan_matching_adapter(self) -> None:
        """Tuple (route, DeliveryPlan) with matching adapter is included."""
        from medre.core.engine.replay.delivery import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter="matrix-bridge")
        result = _filter_plans_by_adapter([("route-stub", plan)], ["matrix-bridge"])
        assert len(result) == 1

    def test_tuple_plan_non_matching_excluded(self) -> None:
        """Tuple (route, DeliveryPlan) with non-matching adapter excluded."""
        from medre.core.engine.replay.delivery import _filter_plans_by_adapter

        plan = _make_delivery_plan(adapter="matrix-bridge")
        result = _filter_plans_by_adapter([("route-stub", plan)], ["other-adapter"])
        assert len(result) == 0


# ===================================================================
# _filter_plans_by_capability
# ===================================================================


class TestFilterPlansByCapability:
    """Tests for _filter_plans_by_capability early-return paths."""

    def _make_event(self) -> CanonicalEvent:
        return CanonicalEvent(
            event_id="cap-001",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="t-0",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
        )

    def test_returns_plans_when_pipeline_is_none(self) -> None:
        """When adapters is None, all plans pass through."""
        from medre.core.engine.replay.delivery import _filter_plans_by_capability

        plans = [_make_delivery_plan()]
        result = _filter_plans_by_capability(self._make_event(), plans, adapters=None)
        assert result.kept == plans
        assert result.suppressed == []

    def test_returns_plans_when_pipeline_lacks_method(self) -> None:
        """Empty adapters dict passes everything conservatively."""
        from medre.core.engine.replay.delivery import _filter_plans_by_capability

        plans = [_make_delivery_plan()]
        result = _filter_plans_by_capability(self._make_event(), plans, adapters={})
        assert result.kept == plans
        assert result.suppressed == []

    def test_supported_event_kind_passes(self) -> None:
        """Plan with adapter that supports the event kind is included."""
        from medre.core.engine.replay.delivery import _filter_plans_by_capability

        caps = AdapterCapabilities(text=True)

        class _CapAdapter:
            _capabilities = caps

        adapters = {"adapter-1": _CapAdapter()}
        plan = _make_delivery_plan(adapter="adapter-1")
        result = _filter_plans_by_capability(
            self._make_event(), [plan], adapters=adapters
        )
        assert len(result.kept) == 1
        assert result.suppressed == []

    def test_unsupported_event_kind_filtered(self) -> None:
        """Plan with adapter that doesn't support event kind is excluded."""
        from medre.core.engine.replay.delivery import _filter_plans_by_capability

        caps = AdapterCapabilities(text=False)

        class _CapAdapter:
            _capabilities = caps

        adapters = {"adapter-1": _CapAdapter()}
        plan = _make_delivery_plan(adapter="adapter-1")
        result = _filter_plans_by_capability(
            self._make_event(), [plan], adapters=adapters
        )
        assert len(result.kept) == 0
        assert len(result.suppressed) == 1
        assert result.suppressed[0].delivery_plan_id == "plan-001"
        assert result.suppressed[0].target_adapter == "adapter-1"
        assert result.suppressed[0].reason is not None

    def test_missing_adapter_included_conservatively(self) -> None:
        """Plan targeting adapter NOT in adapters dict is included (conservative)."""
        from medre.core.engine.replay.delivery import _filter_plans_by_capability

        # Plan targets "adapter-unknown" which is absent from adapters dict.
        plan = _make_delivery_plan(adapter="adapter-unknown")
        result = _filter_plans_by_capability(
            self._make_event(),
            [plan],
            adapters={},
        )
        assert result.kept == [plan]
        assert result.suppressed == []


# ===================================================================
# _stage_deliver capability filtering
# ===================================================================


class TestStageDeliverCapabilityFilter:
    """Tests for _stage_deliver BEST_EFFORT capability-aware filtering."""

    async def test_best_effort_filters_by_capability(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT filters unsupported event kinds."""
        caps = AdapterCapabilities(text=False)
        router, pipeline = _make_capability_route_and_pipeline(
            "message.created",
            "target-adapter",
            caps,
        )

        engine = make_engine(temp_storage, pipeline=pipeline)
        await temp_storage.append(sample_event)

        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        results = [r async for r in engine.replay(request)]

        # Find the deliver-stage result
        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "skipped"
        assert "capability_suppressed" in (deliver_results[0].error or "")

    async def test_dry_run_skips_capability_filter(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """DRY_RUN mode doesn't filter by capability."""
        caps = AdapterCapabilities(text=False)
        router, pipeline = _make_capability_route_and_pipeline(
            "message.created",
            "target-adapter",
            caps,
        )

        engine = make_engine(temp_storage, pipeline=pipeline)
        await temp_storage.append(sample_event)

        request = ReplayRequest(mode=ReplayMode.DRY_RUN)
        results = [r async for r in engine.replay(request)]

        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "skipped"
        assert "dry_run" in (deliver_results[0].error or "")

    async def test_accounting_recorded_when_all_plans_filtered(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """When all plans filtered by capability, accounting is called."""
        from medre.core.supervision.accounting import RuntimeAccounting

        caps = AdapterCapabilities(text=False)
        router, pipeline = _make_capability_route_and_pipeline(
            "message.created",
            "target-adapter",
            caps,
        )

        accounting = RuntimeAccounting()
        engine = make_engine(temp_storage, pipeline=pipeline, accounting=accounting)
        await temp_storage.append(sample_event)

        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        results = [r async for r in engine.replay(request)]

        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "skipped"

        snap = accounting.snapshot()
        assert snap["capability_suppressed"] >= 1

    async def test_partial_suppression_accounting(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Partial suppression: only SOME plans filtered, accounting counts correctly."""
        from medre.core.supervision.accounting import RuntimeAccounting

        # Build a message.file event — capability check uses caps.attachments.
        file_event = CanonicalEvent(
            event_id="file-001",
            event_kind="message.file",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "see attached", "url": "https://example.com/f.pdf"},
            metadata=EventMetadata(),
        )

        # Route with TWO targets: one supports attachments, one does not.
        route = Route(
            id="dual-target-route",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.file",),
                channel="ch-0",
            ),
            targets=[
                RouteTarget(adapter="adapter-with-attachments", channel="ch-ok"),
                RouteTarget(adapter="adapter-no-attachments", channel="ch-skip"),
            ],
        )
        router = Router(routes=[route])

        class _AdapterWithAttachments:
            _capabilities = AdapterCapabilities(attachments=True)

        class _AdapterNoAttachments:
            _capabilities = AdapterCapabilities(attachments=False)

        class _Config:
            adapters = {
                "adapter-with-attachments": _AdapterWithAttachments(),
                "adapter-no-attachments": _AdapterNoAttachments(),
            }

        class CapStubPipeline(StubPipeline):
            _config = _Config()

        accounting = RuntimeAccounting()
        pipeline = CapStubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline, accounting=accounting)
        await temp_storage.append(file_event)

        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        results = [r async for r in engine.replay(request)]

        # Accounting snapshot: exactly 1 plan suppressed (not 2).
        snap = accounting.snapshot()
        assert snap["capability_suppressed"] == 1

        # The supported target should have delivered successfully.
        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "passed"

        # The unsupported target is filtered out — only 1 plan survives
        # in the replay envelope output.
        output = deliver_results[0].output
        assert output["replay"] is True
        adapter_results = output["adapter_results"]
        assert len(adapter_results) == 1


# ===================================================================
# Relation-aware BEST_EFFORT capability filtering
# ===================================================================


class TestReplayRelationCapabilityFiltering:
    """Verify BEST_EFFORT replay filters by relation capability."""

    @staticmethod
    def _make_reply_event() -> CanonicalEvent:
        from medre.core.events.canonical import EventRelation, NativeRef

        return CanonicalEvent(
            event_id="evt-rel-reply-001",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id="parent-001",
                    target_native_ref=NativeRef(
                        adapter="src",
                        native_channel_id="ch-0",
                        native_message_id="native-001",
                    ),
                    key=None,
                    fallback_text="original",
                ),
            ),
            payload={"text": "reply content"},
            metadata=EventMetadata(),
        )

    async def test_reply_unsupported_filters_plan(self, temp_storage: Any) -> None:
        """Reply relation with replies unsupported -> plan filtered."""
        from medre.core.engine.pipeline.runner import PipelineConfig
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing.router import Router

        caps = AdapterCapabilities(replies="unsupported", text=True)

        class _NoReplyAdapter:
            adapter_id = "no-reply"
            platform = "test"
            _capabilities = caps

            async def deliver(self, rendering_result: Any) -> Any:
                from medre.core.contracts.adapter import AdapterDeliveryResult

                return AdapterDeliveryResult(native_message_id="$delivered")

        adapters = {"no-reply": _NoReplyAdapter()}
        target = RouteTarget(adapter="no-reply")
        route = Route(
            id="reply-filter-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[target],
        )
        router = Router(routes=[route])

        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters=adapters,
            event_bus=EventBus(),
        )
        from medre.core.engine.pipeline import PipelineRunner

        runner = PipelineRunner(config)
        await runner.start()
        try:
            engine = ReplayEngine(
                temp_storage,
                pipeline=runner,
            )
            event = self._make_reply_event()
            await temp_storage.append(event)

            request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
            results = [r async for r in engine.replay(request)]

            deliver_results = [r for r in results if r.stage == "deliver"]
            assert len(deliver_results) == 1
            assert deliver_results[0].status == "skipped"
            assert "capability_suppressed" in (deliver_results[0].error or "")
        finally:
            await runner.stop()

    async def test_fallback_relation_not_filtered(self, temp_storage: Any) -> None:
        """Fallback relation capability remains deliverable."""
        from medre.core.engine.pipeline.runner import PipelineConfig
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing.router import Router

        caps = AdapterCapabilities(replies="fallback", text=True)

        class _FallbackReplyAdapter:
            adapter_id = "fallback-reply"
            platform = "test"
            _capabilities = caps

            async def deliver(self, rendering_result: Any) -> Any:
                from medre.core.contracts.adapter import AdapterDeliveryResult

                return AdapterDeliveryResult(native_message_id="$fb-delivered")

        adapters = {"fallback-reply": _FallbackReplyAdapter()}
        target = RouteTarget(adapter="fallback-reply")
        route = Route(
            id="fallback-reply-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[target],
        )
        router = Router(routes=[route])

        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters=adapters,
            event_bus=EventBus(),
        )
        from medre.core.engine.pipeline import PipelineRunner

        runner = PipelineRunner(config)
        await runner.start()
        try:
            engine = ReplayEngine(
                temp_storage,
                pipeline=runner,
            )
            event = self._make_reply_event()
            await temp_storage.append(event)

            request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
            results = [r async for r in engine.replay(request)]

            deliver_results = [r for r in results if r.stage == "deliver"]
            assert len(deliver_results) == 1
            # Fallback is supported, so delivery should pass
            assert deliver_results[0].status == "passed"
        finally:
            await runner.stop()


# ===================================================================
# Capability evidence parity tests
# ===================================================================


class TestCapabilityEvidenceParity:
    """Verify replay capability decisions match live resolver and
    suppressed results carry evidence (delivery_plan_id, replay_run_id,
    reason).

    These tests target the audit finding that pre-filtered capability
    suppressions in replay can be ephemeral in ReplayResult only.
    """

    @staticmethod
    def _make_text_event(event_id: str = "cap-parity-001") -> CanonicalEvent:
        return CanonicalEvent(
            event_id=event_id,
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "parity check"},
            metadata=EventMetadata(),
        )

    def test_resolver_decision_same_for_live_and_replay(self) -> None:
        """Replay and live paths produce the same CapabilityDecision.

        The CapabilityDecisionResolver is the single source of truth.
        _filter_plans_by_capability delegates to it, so the decision
        for a given (event, adapter_caps) pair must be identical
        regardless of whether it comes from live Phase 2.5 or replay
        pre-filtering.
        """
        from medre.core.planning.capability_decision import resolver

        event = self._make_text_event()
        # Adapter that does NOT support text.
        caps = AdapterCapabilities(text=False)

        # Live-style direct resolver call (Phase 2.5 equivalent).
        live_decision = resolver.decide(event, caps, target_adapter="adapter-x")
        # Replay: filter using _filter_plans_by_capability.
        from medre.core.engine.replay.delivery import _filter_plans_by_capability

        plan = _make_delivery_plan(adapter="adapter-x")

        class _Adapter:
            _capabilities = caps

        adapters = {"adapter-x": _Adapter()}
        result = _filter_plans_by_capability(event, [plan], adapters=adapters)

        assert len(result.kept) == 0
        assert len(result.suppressed) == 1

        # The suppressed record must match the live resolver's decision.
        sup = result.suppressed[0]
        assert sup.capability_level == live_decision.capability_level
        assert sup.capability_field == live_decision.capability_field
        assert sup.reason == live_decision.reason

    async def test_suppressed_result_has_delivery_plan_id_and_replay_run_id(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """All-suppressed replay result carries delivery_plan_id and
        replay_run_id in its output (evidence linkage parity with live
        Phase 2.5 suppression receipts).
        """
        caps = AdapterCapabilities(text=False)
        router, pipeline = _make_capability_route_and_pipeline(
            "message.created",
            "target-adapter",
            caps,
        )

        engine = make_engine(temp_storage, pipeline=pipeline)
        event = CanonicalEvent(
            event_id="evidence-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "evidence check"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        run_id = "evidence-run-001"
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            run_id=run_id,
        )
        results = [r async for r in engine.replay(request)]

        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) == 1
        dr = deliver_results[0]
        assert dr.status == "skipped"
        assert "capability_suppressed" in (dr.error or "")

        # Evidence enrichment.
        assert dr.output is not None
        output = dr.output
        assert output["replay_run_id"] == run_id
        assert output["source"] == "replay"
        assert len(output["delivery_plan_ids"]) >= 1
        assert len(output["capability_suppressed_plans"]) >= 1

        sup = output["capability_suppressed_plans"][0]
        assert "delivery_plan_id" in sup
        assert sup["target_adapter"] == "target-adapter"
        assert sup["reason"] is not None

    async def test_fallback_capability_not_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with fallback capability is NOT suppressed."""
        caps = AdapterCapabilities(reactions="fallback", text=True)
        router, pipeline = _make_capability_route_and_pipeline(
            "message.reacted",
            "target-adapter",
            caps,
        )

        engine = make_engine(temp_storage, pipeline=pipeline)
        event = CanonicalEvent(
            event_id="fallback-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"key": "👍"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        results = [r async for r in engine.replay(request)]

        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        # Fallback is supported, so delivery should pass (not skipped).
        assert deliver_results[0].status == "passed"

    async def test_native_capability_not_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with native capability is NOT suppressed."""
        caps = AdapterCapabilities(text=True)
        router, pipeline = _make_capability_route_and_pipeline(
            "message.created",
            "target-adapter",
            caps,
        )

        engine = make_engine(temp_storage, pipeline=pipeline)
        event = CanonicalEvent(
            event_id="native-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "native check"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        results = [r async for r in engine.replay(request)]

        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "passed"

    async def test_missing_adapter_does_not_pretend_capability(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Missing adapter is included conservatively (not suppressed).

        A missing adapter should NOT produce a capability-suppressed
        result; it should be passed through so the downstream delivery
        can produce the correct ADAPTER_MISSING outcome.
        """
        # Empty adapters dict: target-adapter is missing.
        caps = AdapterCapabilities(text=True)
        router, pipeline = _make_capability_route_and_pipeline(
            "message.created",
            "target-adapter",
            caps,
        )
        # Override adapters to be empty so target-adapter is missing.
        pipeline._config.adapters = {}

        engine = make_engine(temp_storage, pipeline=pipeline)
        event = CanonicalEvent(
            event_id="missing-adapter-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "missing adapter check"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        results = [r async for r in engine.replay(request)]

        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        # The plan is NOT capability-suppressed; it falls through to
        # the stub pipeline delivery path (passed or error depending
        # on the stub), but never capability_suppressed.
        dr = deliver_results[0]
        assert "capability_suppressed" not in (dr.error or "")

    async def test_partial_suppression_preserves_evidence_for_suppressed_only(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Partial suppression: only some plans suppressed; evidence
        preserved for the suppressed ones while others deliver normally.
        """
        from medre.core.supervision.accounting import RuntimeAccounting

        file_event = CanonicalEvent(
            event_id="partial-evidence-001",
            event_kind="message.file",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "see attached", "url": "https://example.com/f.pdf"},
            metadata=EventMetadata(),
        )

        route = Route(
            id="dual-target-route",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.file",),
                channel="ch-0",
            ),
            targets=[
                RouteTarget(adapter="adapter-with-attachments", channel="ch-ok"),
                RouteTarget(adapter="adapter-no-attachments", channel="ch-skip"),
            ],
        )
        router = Router(routes=[route])

        class _AdapterWithAttachments:
            _capabilities = AdapterCapabilities(attachments=True)

        class _AdapterNoAttachments:
            _capabilities = AdapterCapabilities(attachments=False)

        class _Config:
            adapters = {
                "adapter-with-attachments": _AdapterWithAttachments(),
                "adapter-no-attachments": _AdapterNoAttachments(),
            }

        class CapStubPipeline(StubPipeline):
            _config = _Config()

        accounting = RuntimeAccounting()
        pipeline = CapStubPipeline(router=router)
        engine = make_engine(temp_storage, pipeline=pipeline, accounting=accounting)
        await temp_storage.append(file_event)

        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            run_id="partial-run-001",
        )
        results = [r async for r in engine.replay(request)]

        # One plan should survive (the one with attachments support).
        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) >= 1
        assert deliver_results[0].status == "passed"

        # Exactly 1 plan suppressed.
        snap = accounting.snapshot()
        assert snap["capability_suppressed"] == 1

        # The delivered output has 1 adapter result (not 2).
        output = deliver_results[0].output
        assert output["replay"] is True
        adapter_results = output["adapter_results"]
        assert len(adapter_results) == 1

        # Mixed outcome: suppressed-plan evidence must be present alongside
        # the delivered adapter results (parity with all-suppressed branch).
        assert output["source"] == "replay"
        assert output["replay_run_id"] == "partial-run-001"
        assert len(output["capability_suppressed_plans"]) == 1

        sup = output["capability_suppressed_plans"][0]
        assert "delivery_plan_id" in sup
        assert sup["target_adapter"] == "adapter-no-attachments"
        assert sup["reason"] is not None

        assert len(output["delivery_plan_ids"]) == 1
