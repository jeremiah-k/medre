"""Integration tests that verify the full pipeline enforces capability
decisions end-to-end.

Proves that capability suppression fires through PipelineRunner._deliver_one()
and ReplayEngine._stage_deliver(), not just the pure capability-check helpers
in ``medre.core.planning.capabilities.capability_unsupported``.  These tests
exercise the real pipeline with fake adapters and storage, verifying:

* Unsupported event kinds produce status="skipped" with
  failure_kind=CAPABILITY_SUPPRESSED.
* Suppressed receipt is persisted with route_id, delivery_plan_id,
  target_adapter, target_channel.
* Renderer and adapter delivery path are NOT invoked on capability
  suppression.
* Adapter capabilities (``max_text_chars``) are enforced by the renderer.
* ReplayEngine skips unsupported event kinds in BEST_EFFORT mode.
"""

from __future__ import annotations

import dataclasses

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ===================================================================
# TestCapabilitySuppressionReceipt
# ===================================================================


class TestCapabilitySuppressionReceipt:
    """Verify the pipeline produces CAPABILITY_SUPPRESSED receipts."""

    @pytest.mark.asyncio
    async def test_unsupported_reaction_produces_capability_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with reactions='unsupported' suppresses message.reacted."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        # Override capabilities: reactions unsupported.
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="cap-reaction-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cap-react-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
            assert outcome.target_adapter == "dest"
            assert outcome.route_id == "cap-reaction-route"
            assert outcome.event_id == "cap-react-001"
            assert outcome.error is not None
            assert "capability_suppressed" in outcome.error
            assert "reactions unsupported" in outcome.error

            # Adapter never called.
            assert len(adapter.delivered_payloads) == 0

            # Receipt persisted.
            receipt = outcome.receipt
            assert receipt is not None
            assert receipt.status == "suppressed"
            assert receipt.failure_kind == "capability_suppressed"

            stored = await temp_storage.list_receipts_for_event("cap-react-001")
            assert len(stored) == 1
            assert stored[0].status == "suppressed"
            assert stored[0].failure_kind == "capability_suppressed"
            assert stored[0].target_adapter == "dest"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_unsupported_attachment_produces_capability_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with attachments=False suppresses message.file."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        # Default AdapterCapabilities already has attachments=False,
        # but be explicit for clarity.
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            attachments=False,
        )

        route = Route(
            id="cap-file-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.file",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cap-file-001",
            event_kind="message.file",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"filename": "photo.jpg", "url": "https://example.com/photo.jpg"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
            assert outcome.target_adapter == "dest"
            assert outcome.error is not None
            assert "capability_suppressed" in outcome.error
            assert "attachments unsupported" in outcome.error

            # Adapter never called.
            assert len(adapter.delivered_payloads) == 0
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_supported_kind_produces_normal_delivery(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Control: adapter with reactions='native' delivers message.reacted normally."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        # Explicit: reactions are natively supported.
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="cap-ok-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cap-ok-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.failure_kind is None
            assert outcome.target_adapter == "dest"

            # Adapter was called.
            assert len(adapter.delivered_payloads) == 1
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_fallback_reaction_not_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with reactions='fallback' receives message.reacted normally."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="fallback",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="cap-fallback-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cap-fallback-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.failure_kind is None
            assert outcome.target_adapter == "dest"

            # Adapter received the event.
            assert len(adapter.delivered_payloads) == 1

            # No CAPABILITY_SUPPRESSED receipt.
            receipt = outcome.receipt
            assert receipt is not None
            assert receipt.status != "suppressed"
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_mixed_capability_targets(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Route with two targets: one native, one unsupported.

        Verifies that the native target receives the event normally while
        the unsupported target gets CAPABILITY_SUPPRESSED.
        """
        native_adapter = FakePresentationAdapter(adapter_id="dest_native")
        native_adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        unsupported_adapter = FakePresentationAdapter(adapter_id="dest_unsupported")
        unsupported_adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
            replies="native",
            edits="native",
            deletes="native",
            attachments=False,
            delivery_receipts=True,
        )

        route = Route(
            id="cap-mixed-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="dest_native"),
                RouteTarget(adapter="dest_unsupported"),
            ],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={
                "dest_native": native_adapter,
                "dest_unsupported": unsupported_adapter,
            },
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cap-mixed-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 2

            # Separate outcomes by adapter.
            native_outcome = next(
                o for o in outcomes if o.target_adapter == "dest_native"
            )
            suppressed_outcome = next(
                o for o in outcomes if o.target_adapter == "dest_unsupported"
            )

            # dest_native: success
            assert native_outcome.status == "success"
            assert native_outcome.failure_kind is None
            assert len(native_adapter.delivered_payloads) == 1

            # dest_unsupported: CAPABILITY_SUPPRESSED
            assert suppressed_outcome.status == "skipped"
            assert (
                suppressed_outcome.failure_kind
                is DeliveryFailureKind.CAPABILITY_SUPPRESSED
            )
            assert len(unsupported_adapter.delivered_payloads) == 0

            # Receipts: one suppressed, one successful.
            assert native_outcome.receipt is not None
            assert native_outcome.receipt.status != "suppressed"

            assert suppressed_outcome.receipt is not None
            assert suppressed_outcome.receipt.status == "suppressed"
            assert suppressed_outcome.receipt.failure_kind == "capability_suppressed"
        finally:
            await runner.stop()


# ===================================================================
# TestCapabilityRenderingConstraint
# ===================================================================


class TestCapabilityRenderingConstraint:
    """Verify max_text_chars is enforced by the renderer."""

    @pytest.mark.asyncio
    async def test_text_truncated_when_exceeds_max_text_chars(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with max_text_chars=10 truncates long text to 10 chars."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            max_text_chars=10,
        )

        route = Route(
            id="truncate-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        long_text = "A" * 50
        event = make_event(
            event_id="truncate-001",
            event_kind="message.created",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": long_text},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # Adapter received the rendered payload.
            assert len(adapter.delivered_payloads) == 1
            rendered = adapter.delivered_payloads[0]
            rendered_text = rendered.payload.get("text", "")

            # Text is truncated to max_text_chars.
            assert len(rendered_text) == 10
            assert rendered_text == "A" * 10
            # Truncation flag set.
            assert rendered.truncated is True
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_text_not_truncated_when_within_limit(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Text within max_text_chars is not truncated."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            max_text_chars=100,
        )

        route = Route(
            id="no-trunc-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        short_text = "hello"
        event = make_event(
            event_id="no-trunc-001",
            event_kind="message.created",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": short_text},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            assert len(adapter.delivered_payloads) == 1
            rendered = adapter.delivered_payloads[0]
            rendered_text = rendered.payload.get("text", "")

            assert rendered_text == short_text
            assert len(rendered_text) == len(short_text)
            assert rendered.truncated is False
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_no_truncation_when_max_text_chars_is_none(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """max_text_chars=None falls back to the default 500-char limit."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            max_text_chars=None,
        )

        route = Route(
            id="none-trunc-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # 200 chars — under the default 500-char limit.
        medium_text = "B" * 200
        event = make_event(
            event_id="none-trunc-001",
            event_kind="message.created",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": medium_text},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            assert len(adapter.delivered_payloads) == 1
            rendered = adapter.delivered_payloads[0]
            rendered_text = rendered.payload.get("text", "")

            # Text is NOT truncated — full 200 chars preserved.
            assert rendered_text == medium_text
            assert len(rendered_text) == 200
            assert rendered.truncated is False
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_truncation_at_500_when_no_max_set(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """500 chars passes, 501 truncates when max_text_chars=None."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            max_text_chars=None,
        )

        route = Route(
            id="boundary-500-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        text_500 = "C" * 500
        event_500 = make_event(
            event_id="boundary-500-pass",
            event_kind="message.created",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": text_500},
        )

        text_501 = "D" * 501
        event_501 = make_event(
            event_id="boundary-501-trunc",
            event_kind="message.created",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": text_501},
        )

        try:
            # 500 chars → no truncation
            outcomes_500 = await runner.handle_ingress(event_500)
            assert len(outcomes_500) == 1
            assert outcomes_500[0].status == "success"
            assert len(adapter.delivered_payloads) == 1
            rendered_500 = adapter.delivered_payloads[0]
            assert len(rendered_500.payload.get("text", "")) == 500
            assert rendered_500.truncated is False

            # Reset adapter state for second event
            adapter.delivered_payloads.clear()

            # 501 chars → truncated to 500
            outcomes_501 = await runner.handle_ingress(event_501)
            assert len(outcomes_501) == 1
            assert outcomes_501[0].status == "success"
            assert len(adapter.delivered_payloads) == 1
            rendered_501 = adapter.delivered_payloads[0]
            assert len(rendered_501.payload.get("text", "")) == 500
            assert rendered_501.truncated is True
        finally:
            await runner.stop()


# ===================================================================
# TestReplayCapabilityAwareness
# ===================================================================


class TestReplayCapabilityAwareness:
    """Verify ReplayEngine skips unsupported event kinds."""

    @pytest.mark.asyncio
    async def test_best_effort_replay_skips_unsupported_kind(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """BEST_EFFORT replay skips message.file when adapter has
        attachments=False."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            attachments=False,
        )

        route = Route(
            id="replay-cap-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.file",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Seed the file event directly into storage (orphan).
        file_event = make_event(
            event_id="replay-cap-001",
            event_kind="message.file",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"filename": "doc.pdf", "url": "https://example.com/doc.pdf"},
        )
        await temp_storage.append(file_event)

        try:
            # Run BEST_EFFORT replay targeting the seeded event.
            replay = ReplayEngine(
                storage=temp_storage,
                pipeline=runner,
            )
            request = ReplayRequest(
                mode=ReplayMode.BEST_EFFORT,
                run_id="run-replay-cap-001",
                correlation_ids=["replay-cap-001"],
            )
            # Materialise results once — do not call replay() twice.
            results: list = []
            async for r in replay.replay(request):
                results.append(r)

            # Event was replayed (at least store stage).
            store_results = [r for r in results if r.stage == "store"]
            assert len(store_results) >= 1

            # The deliver stage should be skipped due to capability
            # suppression — all plans filtered out.
            deliver_results = [r for r in results if r.stage == "deliver"]
            assert len(deliver_results) >= 1

            deliver_result = deliver_results[0]
            assert deliver_result.status == "skipped"
            assert deliver_result.error is not None
            assert "capability_suppressed" in deliver_result.error

            # Adapter never called.
            assert len(adapter.delivered_payloads) == 0
        finally:
            await runner.stop()


# ===================================================================
# TestNegativeMaxTextChars
# ===================================================================


class TestNegativeMaxTextChars:
    """Regression: negative max_text_chars is clamped to zero."""

    def test_negative_max_text_chars_clamped_to_zero(self) -> None:
        """max_text_chars=-1 → empty output, truncated=True."""
        renderer = TextRenderer()
        text, truncated = renderer._truncate("hello", max_text_chars=-1)
        assert text == ""
        assert truncated is True

    def test_zero_max_text_chars_returns_empty(self) -> None:
        """max_text_chars=0 with non-empty text → empty string, truncated=True."""
        renderer = TextRenderer()
        text, truncated = renderer._truncate("hello", max_text_chars=0)
        assert text == ""
        assert truncated is True

    def test_zero_max_text_chars_empty_input(self) -> None:
        """max_text_chars=0 with empty text → empty string, truncated=False."""
        renderer = TextRenderer()
        text, truncated = renderer._truncate("", max_text_chars=0)
        assert text == ""
        assert truncated is False


# ===================================================================
# TestCapabilitySuppressedCapacityAccounting
# ===================================================================


class TestCapabilitySuppressedCapacityAccounting:
    """Regression: capability suppression does not increment capacity_rejections."""

    @pytest.mark.asyncio
    async def test_capability_suppressed_does_not_increment_capacity_rejections(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """capability_suppressed=1 and capacity_rejections=0 after suppression."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="cap-accounting-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        config = dataclasses.replace(config, runtime_accounting=accounting)
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cap-accounting-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            await runner.handle_ingress(event)

            snap = accounting.snapshot()
            assert snap["capability_suppressed"] == 1
            assert snap["capacity_rejections"] == 0
        finally:
            await runner.stop()


# ===================================================================
# TestCapabilitySuppressionRecording
# ===================================================================


class TestCapabilitySuppressionRecording:
    """Verify route stats and runtime accounting are updated on
    capability suppression (lines 1617-1618, 1619-1620).
    """

    @pytest.mark.asyncio
    async def test_route_stats_recorded_on_capability_suppression(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RouteStats.record_capability_suppressed is called."""
        from medre.core.routing.stats import RouteStats

        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="cap-stats-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])
        stats = RouteStats()

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        config = dataclasses.replace(config, route_stats=stats)
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cap-stats-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            await runner.handle_ingress(event)

            snap = stats.snapshot()
            assert "cap-stats-route" in snap
            assert snap["cap-stats-route"]["capability_suppressed"] == 1
            assert snap["cap-stats-route"]["delivered"] == 0
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_runtime_accounting_recorded_on_capability_suppression(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RuntimeAccounting.record_capability_suppressed is called."""
        acc = RuntimeAccounting()
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            attachments=False,
        )

        route = Route(
            id="cap-acc-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.file",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        config = dataclasses.replace(config, runtime_accounting=acc)
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cap-acc-001",
            event_kind="message.file",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"filename": "doc.pdf", "url": "https://example.com/doc.pdf"},
        )

        try:
            await runner.handle_ingress(event)

            snap = acc.snapshot()
            assert snap["capability_suppressed"] == 1
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_no_crash_when_stats_and_accounting_are_none(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Capability suppression doesn't crash when stats/accounting are None."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="cap-none-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        # route_stats and runtime_accounting default to None in PipelineConfig.
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="cap-none-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            # Should still produce a valid outcome, not crash.
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
            assert outcomes[0].failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
        finally:
            await runner.stop()


# ===================================================================
# TestRouteStatsCapabilitySuppressed
# ===================================================================


class TestRouteStatsCapabilitySuppressed:
    """Verify RouteStats.record_capability_suppressed behavior."""

    def test_new_route_increments_only_capability(self):
        stats = RouteStats()
        stats.record_capability_suppressed("r1")
        snap = stats.snapshot()
        assert snap["r1"]["capability_suppressed"] == 1
        assert snap["r1"]["delivered"] == 0
        assert snap["r1"]["failed"] == 0

    def test_existing_route_preserves_other_counters(self):
        stats = RouteStats()
        stats.record_delivered("r1")
        stats.record_capability_suppressed("r1")
        stats.record_capability_suppressed("r1")
        snap = stats.snapshot()
        assert snap["r1"]["delivered"] == 1
        assert snap["r1"]["capability_suppressed"] == 2

    def test_multiple_routes_independent(self):
        stats = RouteStats()
        stats.record_capability_suppressed("r1")
        stats.record_capability_suppressed("r2")
        snap = stats.snapshot()
        assert snap["r1"]["capability_suppressed"] == 1
        assert snap["r2"]["capability_suppressed"] == 1


# ===================================================================
# TestUnknownAdapterNotCapabilitySuppressed
# ===================================================================


class TestUnknownAdapterNotCapabilitySuppressed:
    """Missing adapter produces ADAPTER_MISSING, not CAPABILITY_SUPPRESSED."""

    @pytest.mark.asyncio
    async def test_unknown_adapter_not_capability_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Missing adapter produces ADAPTER_MISSING, not CAPABILITY_SUPPRESSED.

        Routes an adapter ID that is NOT present in the adapters dict.
        The event kind is message.file (which default caps would suppress),
        but because the adapter is missing, the pipeline should return
        ADAPTER_MISSING rather than CAPABILITY_SUPPRESSED.
        """
        route = Route(
            id="missing-adapter-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.file",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="nonexistent_adapter")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={},  # Empty — no adapters registered at all.
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="missing-adapter-001",
            event_kind="message.file",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"filename": "photo.jpg", "url": "https://example.com/photo.jpg"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "permanent_failure"
            assert outcome.failure_kind is DeliveryFailureKind.ADAPTER_MISSING
            assert outcome.failure_kind is not DeliveryFailureKind.CAPABILITY_SUPPRESSED
            assert outcome.target_adapter == "nonexistent_adapter"
            assert outcome.error is not None
            assert "not registered" in outcome.error
        finally:
            await runner.stop()


# ===================================================================
# TestMaxTextBytesThreading
# ===================================================================


class _ContextCapturingRenderer(TextRenderer):
    """TextRenderer subclass that captures the RenderingContext for inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.captured_ctx: RenderingContext | None = None

    async def render(  # type: ignore[override]
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> RenderingResult:
        self.captured_ctx = ctx
        return await super().render(event, ctx)


class TestMaxTextBytesThreading:
    """Verify max_text_bytes from adapter capabilities reaches RenderingContext."""

    @pytest.mark.asyncio
    async def test_ctx_max_text_bytes_populated_from_adapter_caps(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter with max_text_bytes=512 causes ctx.max_text_bytes == 512."""
        capturing_renderer = _ContextCapturingRenderer()
        pipeline = RenderingPipeline()
        pipeline.register(capturing_renderer, priority=1)

        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            max_text_bytes=512,
        )

        route = Route(
            id="bytes-thread-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"dest": adapter},
            event_bus=EventBus(),
            rendering_pipeline=pipeline,
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="bytes-thread-001",
            event_kind="message.created",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": "short message"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # The renderer received the context with max_text_bytes populated.
            assert capturing_renderer.captured_ctx is not None
            assert capturing_renderer.captured_ctx.max_text_bytes == 512
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_ctx_max_text_bytes_none_when_caps_unset(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Adapter without max_text_bytes causes ctx.max_text_bytes == None."""
        capturing_renderer = _ContextCapturingRenderer()
        pipeline = RenderingPipeline()
        pipeline.register(capturing_renderer, priority=1)

        adapter = FakePresentationAdapter(adapter_id="dest")
        # Default capabilities have max_text_bytes=None.
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
        )

        route = Route(
            id="bytes-none-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"dest": adapter},
            event_bus=EventBus(),
            rendering_pipeline=pipeline,
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="bytes-none-001",
            event_kind="message.created",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": "hello"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            assert capturing_renderer.captured_ctx is not None
            assert capturing_renderer.captured_ctx.max_text_bytes is None
        finally:
            await runner.stop()


# ===================================================================
# Test validate_strategy_method
# ===================================================================


class TestValidateStrategyMethod:
    """Direct unit tests for the module-level strategy validation."""

    def test_rejects_unknown_strategy(self) -> None:
        """_validate_strategy_method raises ValueError for unknown strings."""
        from medre.core.engine.pipeline.target_delivery import _validate_strategy_method

        with pytest.raises(ValueError, match="Unknown delivery strategy"):
            _validate_strategy_method("bogus_strategy")

    def test_accepts_all_known_strategies(self) -> None:
        """All DeliveryStrategyMethod literals pass validation."""
        from typing import get_args

        from medre.core.engine.pipeline.target_delivery import _validate_strategy_method
        from medre.core.rendering.renderer import DeliveryStrategyMethod

        for method in get_args(DeliveryStrategyMethod):
            result = _validate_strategy_method(method)
            assert result == method

    def test_rejects_arbitrary_strings(self) -> None:
        """Empty string, None-like, and case-variant strings are rejected."""
        from medre.core.engine.pipeline.target_delivery import _validate_strategy_method

        for bogus in ("", "  ", "FALLBACK_TEXT", "Skip", "native"):
            with pytest.raises(ValueError, match="Unknown delivery strategy"):
                _validate_strategy_method(bogus)


# ===================================================================
# TestUnknownStrategyPlannerFailure
# ===================================================================


class TestUnknownStrategyPlannerFailure:
    """Verifies that unknown strategy in deliver_to_target produces
    PLANNER_FAILURE, not RENDERER_FAILURE."""

    @pytest.mark.asyncio
    async def test_unknown_strategy_is_planner_failure(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Unknown strategy classified as PLANNER_FAILURE, not RENDERER_FAILURE."""
        from medre.core.engine.pipeline.target_delivery import _RendererDeliveryError
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy

        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(text=True)

        target = RouteTarget(adapter="dest")
        route = Route(
            id="bogus-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[target],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="bogus-001",
            event_kind="message.created",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": "hello"},
        )

        # Build a delivery plan with an invalid strategy method.
        bad_plan = DeliveryPlan(
            plan_id="plan-bogus",
            event_id=event.event_id,
            target=target,
            primary_strategy=DeliveryStrategy(method="not_a_real_strategy"),
        )

        try:
            with pytest.raises(_RendererDeliveryError) as exc_info:
                await runner.deliver_to_target(event, route, bad_plan)

            receipt = exc_info.value.receipt
            assert receipt is not None
            # Receipt records PLANNER_FAILURE, never RENDERER_FAILURE.
            assert receipt.failure_kind == DeliveryFailureKind.PLANNER_FAILURE.value
            # Error message should reference the bad strategy.
            assert "not_a_real_strategy" in (exc_info.value.error or "")
        finally:
            await runner.stop()
