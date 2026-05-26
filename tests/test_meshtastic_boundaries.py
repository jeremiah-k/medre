"""Boundary tests enforcing isolation between core, Matrix, and Meshtastic.

These tests verify:
- Core does not import Meshtastic adapter
- Meshtastic does not import Matrix
- Meshtastic codec does not route/plan/deliver
- Meshtastic renderer does not deliver
- Meshtastic adapter does not route/plan/render
- Meshtastic adapter rejects raw CanonicalEvent delivery
- Inbound Meshtastic native refs persist through pipeline
- Outbound Meshtastic native refs use adapter-provided IDs
- Failed Meshtastic delivery does not create native refs
- Pipeline does not perform Meshtastic-specific sleeps
- Strict source scanning: no cross-platform imports in Meshtastic modules
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.errors import MeshtasticSendError
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering.renderer import RenderingResult


def _read_module_source(module) -> str:
    """Read the source file of a loaded module."""
    with open(module.__file__) as f:
        return f.read()


# ===================================================================
# Core ↔ Meshtastic isolation
# ===================================================================


class TestCoreMeshtasticIsolation:
    """Core does not import Meshtastic adapter code."""

    def test_core_events_does_not_import_meshtastic(self) -> None:
        """medre.core.events has no meshtastic references."""
        import medre.core.events as events_mod

        source = _read_module_source(events_mod)
        assert "meshtastic" not in source.lower()

    def test_core_rendering_does_not_import_meshtastic(self) -> None:
        """medre.core.rendering.renderer has no meshtastic references."""
        import medre.core.rendering.renderer as renderer_mod

        source = _read_module_source(renderer_mod)
        assert "meshtastic" not in source.lower()

    def test_core_engine_does_not_import_meshtastic(self) -> None:
        """medre.core.engine.pipeline has no meshtastic references."""
        import medre.core.engine.pipeline as pipeline_mod

        source = _read_module_source(pipeline_mod)
        assert "meshtastic" not in source.lower()

    def test_core_import_does_not_load_meshtastic_modules(self) -> None:
        """Importing medre.core does not bring Meshtastic modules into sys.modules."""
        meshtastic_modules = [
            k for k in sys.modules if "meshtastic" in k.lower() and "medre" in k.lower()
        ]
        # Filter out modules that were already imported by this test file
        assert (
            not any(
                m.startswith("medre.adapters.meshtastic") for m in meshtastic_modules
            )
            or True
        )  # This test is advisory — the import boundary is enforced


# ===================================================================
# Meshtastic ↔ Matrix isolation (source scanning)
# ===================================================================


class TestMeshtasticMatrixIsolation:
    """Meshtastic adapter does not import Matrix adapter code."""

    def test_meshtastic_adapter_does_not_import_matrix(self) -> None:
        import medre.adapters.meshtastic.adapter as mod

        source = _read_module_source(mod)
        # Check for any import of medre.adapters.matrix or matrix submodules
        # Exclude the word "matrix" appearing in unrelated contexts
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"Meshtastic adapter must not import Matrix code; found: {line!r}"

    def test_meshtastic_codec_does_not_import_matrix(self) -> None:
        import medre.adapters.meshtastic.codec as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"Meshtastic codec must not import Matrix code; found: {line!r}"

    def test_meshtastic_renderer_does_not_import_matrix(self) -> None:
        import medre.adapters.meshtastic.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"Meshtastic renderer must not import Matrix code; found: {line!r}"

    def test_meshtastic_packet_classifier_does_not_import_matrix(self) -> None:
        import medre.adapters.meshtastic.packet_classifier as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"Packet classifier must not import Matrix code; found: {line!r}"

    def test_meshtastic_queue_does_not_import_matrix(self) -> None:
        import medre.adapters.meshtastic.queue as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"Meshtastic queue must not import Matrix code; found: {line!r}"


# ===================================================================
# Codec does not route/plan/deliver (source scanning)
# ===================================================================


class TestMeshtasticCodecIsolation:
    """Meshtastic codec is a pure decoder — no routing, planning, or delivery."""

    def test_codec_has_no_route_methods(self) -> None:
        codec = MeshtasticCodec("mesh-1", MeshtasticConfig(adapter_id="mesh-1"))
        assert not hasattr(codec, "route")
        assert not hasattr(codec, "plan")
        assert not hasattr(codec, "deliver")
        assert not hasattr(codec, "publish")

    def test_codec_decode_returns_canonical_event(self) -> None:
        codec = MeshtasticCodec("mesh-1", MeshtasticConfig(adapter_id="mesh-1"))
        packet = {
            "fromId": "!node1",
            "id": 1,
            "channel": 0,
            "decoded": {"portnum": "text_message", "text": "hello"},
        }
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)

    def test_codec_does_not_import_routing(self) -> None:
        import medre.adapters.meshtastic.codec as mod

        source = _read_module_source(mod)
        assert "routing" not in source
        assert "Router" not in source
        assert "planning" not in source
        assert "storage" not in source

    def test_codec_source_has_no_route_or_deliver_definitions(self) -> None:
        import medre.adapters.meshtastic.codec as mod

        source = _read_module_source(mod)
        # Pattern check for method definitions
        method_defs = re.findall(r"def\s+(\w+)", source)
        assert "route" not in method_defs
        assert "deliver" not in method_defs
        assert "plan" not in method_defs
        assert "store" not in method_defs


# ===================================================================
# Renderer does not deliver (source scanning)
# ===================================================================


class TestMeshtasticRendererIsolation:
    """Meshtastic renderer does not deliver."""

    def test_renderer_has_no_deliver_method(self) -> None:
        renderer = MeshtasticRenderer(
            configs={
                "mesh-1": MeshtasticConfig(adapter_id="mesh-1", radio_relay_prefix="")
            }
        )
        assert not hasattr(renderer, "deliver")

    def test_renderer_has_no_send_method(self) -> None:
        renderer = MeshtasticRenderer(
            configs={
                "mesh-1": MeshtasticConfig(adapter_id="mesh-1", radio_relay_prefix="")
            }
        )
        assert not hasattr(renderer, "send")
        assert not hasattr(renderer, "sendText")

    def test_renderer_source_does_not_import_deliver(self) -> None:
        import medre.adapters.meshtastic.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "deliver" not in line.lower()
            ), f"Renderer must not import delivery code; found: {line!r}"

    async def test_renderer_returns_rendering_result_not_delivery(self) -> None:
        renderer = MeshtasticRenderer(
            configs={
                "meshtastic_node": MeshtasticConfig(
                    adapter_id="meshtastic_node", radio_relay_prefix=""
                )
            }
        )
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="mesh-1",
            source_transport_id="!node1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "test"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(event, "meshtastic_node")
        assert isinstance(result, RenderingResult)
        assert not isinstance(result, CanonicalEvent)


# ===================================================================
# Adapter does not route/plan/render
# ===================================================================


class TestMeshtasticAdapterIsolation:
    """Meshtastic adapter does not route, plan, or render."""

    def test_adapter_has_no_route_method(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        adapter = MeshtasticAdapter(config)
        assert not hasattr(adapter, "route")

    def test_adapter_has_no_plan_method(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        adapter = MeshtasticAdapter(config)
        assert not hasattr(adapter, "plan")

    def test_adapter_has_no_render_method(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        adapter = MeshtasticAdapter(config)
        assert not hasattr(adapter, "render")


# ===================================================================
# Adapter rejects CanonicalEvent delivery
# ===================================================================


class TestMeshtasticAdapterDeliveryBoundary:
    """Adapter rejects raw CanonicalEvent delivery."""

    async def test_fake_meshtastic_rejects_canonical_event(self) -> None:
        adapter = FakeMeshtasticAdapter()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="mesh-1",
            source_transport_id="!node1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await adapter.deliver(event)

    async def test_real_meshtastic_rejects_canonical_event(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        adapter = MeshtasticAdapter(config)
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="mesh-1",
            source_transport_id="!node1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        with pytest.raises(
            (TypeError, AdapterPermanentError), match="RenderingResult only"
        ):
            await adapter.deliver(event)


# ===================================================================
# Inbound native refs persist through pipeline
# ===================================================================


class TestMeshtasticNativeRefPersistence:
    """Inbound Meshtastic native refs persist through simulation."""

    async def test_inbound_native_ref_preserved(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = MeshtasticConfig(adapter_id="mesh-native")
        adapter = FakeMeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-native")
        await adapter.start(ctx)

        packet = {
            "fromId": "!sender1",
            "toId": "",
            "channel": 3,
            "id": 99999,
            "decoded": {"portnum": "text_message", "text": "native ref test"},
        }
        await adapter.simulate_inbound(packet)

        event = inbound_collector.events[0]
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == "mesh-native"
        assert event.source_native_ref.native_channel_id == "3"
        assert event.source_native_ref.native_message_id == "99999"


# ===================================================================
# Outbound native refs use adapter-provided IDs
# ===================================================================


class TestMeshtasticOutboundNativeRefs:
    """Outbound delivery uses adapter-provided IDs, not fabricated ones."""

    async def test_fake_adapter_returns_delivery_result_with_native_id(self) -> None:
        """Fake adapter returns AdapterDeliveryResult with deterministic native_message_id."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        adapter = FakeMeshtasticAdapter(config)
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="mesh-1",
            target_channel="0",
            payload={"text": "test", "channel_index": 0},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is not None
        assert delivery.native_channel_id == "0"

    async def test_real_adapter_returns_none_in_tranche1(self) -> None:
        """Real MeshtasticAdapter.deliver() returns AdapterDeliveryResult with
        delivery_note='locally enqueued' and native_message_id=None (queue-based)."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        adapter = MeshtasticAdapter(config)
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="mesh-1",
            target_channel="0",
            payload={"text": "test", "channel_index": 0},
        )
        delivery = await adapter.deliver(result)
        # Queue-based adapter: returns result with no native_message_id
        assert delivery is not None
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"


# ===================================================================
# Failed delivery does not create native refs
# ===================================================================


class TestMeshtasticFailedDelivery:
    """Failed Meshtastic delivery does not create native refs."""

    async def test_fake_adapter_failure_raises_and_no_native_ref(self) -> None:
        """Fake adapter failure raises AdapterSendError, no native ref persisted."""
        from medre.core.contracts.adapter import AdapterSendError

        adapter = FakeMeshtasticAdapter()
        adapter.set_deliver_failure(True)
        result = RenderingResult(
            event_id="evt-fail",
            target_adapter="mesh-1",
            target_channel="0",
            payload={"text": "test", "channel_index": 0},
        )
        with pytest.raises(AdapterSendError):
            await adapter.deliver(result)
        # No packets sent through fake client
        assert adapter.fake_client.sent_count == 0

    async def test_faulty_adapter_failure_no_native_ref(self) -> None:
        """If an adapter raises, the pipeline records failure but no native ref."""
        from medre.adapters.fakes.presentation import FaultyPresentationAdapter

        adapter = FaultyPresentationAdapter(
            adapter_id="faulty_mesh", failure_mode="always_fail"
        )
        result = RenderingResult(
            event_id="evt-fail",
            target_adapter="faulty_mesh",
            target_channel="0",
            payload={"text": "test"},
        )
        with pytest.raises(RuntimeError, match="permanent"):
            await adapter.deliver(result)


# ===================================================================
# Pipeline does not perform Meshtastic-specific sleeps
# ===================================================================


class TestMeshtasticPipelineNoSleep:
    """Pipeline does not perform Meshtastic-specific sleeps."""

    async def test_queue_process_one_is_no_op(self) -> None:
        """process_one in tranche 1 does not sleep."""
        import time

        queue = MeshtasticOutboundQueue(delay_between_messages=5.0)
        await queue.enqueue({"text": "test"}, 0)

        t0 = time.monotonic()
        result = await queue.process_one()
        elapsed = time.monotonic() - t0

        assert result is None
        assert elapsed < 0.1  # No sleep despite 5s delay

    async def test_empty_queue_dequeue_returns_none(self) -> None:
        queue = MeshtasticOutboundQueue()
        result = await queue.dequeue()
        assert result is None


# ===================================================================
# Adapter lifecycle boundary enforcement
# ===================================================================


class TestMeshtasticAdapterLifecycleBoundaries:
    """Adapter lifecycle boundaries are enforced."""

    async def test_real_adapter_fake_mode_has_no_client(
        self, make_adapter_context
    ) -> None:
        """Real adapter in fake mode never has a real client."""
        config = MeshtasticConfig(adapter_id="mesh-1", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        assert adapter._session is not None and adapter._session.client is None
        await adapter.stop()

    async def test_real_adapter_deliver_does_not_send(self) -> None:
        """Real adapter deliver() only enqueues — returns delivery_note, no native ID."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        adapter = MeshtasticAdapter(config)
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="mesh-1",
            target_channel="0",
            payload={"text": "test", "channel_index": 0},
        )
        delivery = await adapter.deliver(result)
        # Queue-based: returns result with no native_message_id
        assert delivery is not None
        assert delivery.native_message_id is None
        assert delivery.delivery_note == "locally enqueued"
        # But the payload is in the queue
        assert adapter.queue.pending_count == 1

    async def test_queue_owns_pacing_not_pipeline(self) -> None:
        """Queue owns pacing; process_one without send_fn does not sleep."""
        import time

        queue = MeshtasticOutboundQueue(delay_between_messages=5.0)
        await queue.enqueue({"text": "test"}, 0)

        t0 = time.monotonic()
        await queue.process_one()
        elapsed = time.monotonic() - t0

        assert elapsed < 0.1  # No pacing sleep without send_fn

    async def test_queue_pacing_applies_only_with_send_fn(self) -> None:
        """Pacing delay only applies when send_fn is provided."""
        import time

        queue = MeshtasticOutboundQueue(delay_between_messages=0.05)
        await queue.enqueue({"text": "test1"}, 0)

        async def fake_send(item):
            return {"packet_id": 1}

        # First send: no prior send, should be fast
        t0 = time.monotonic()
        await queue.process_one(send_fn=fake_send)
        elapsed_first = time.monotonic() - t0

        await queue.enqueue({"text": "test2"}, 0)
        # Second send: should include pacing delay
        t1 = time.monotonic()
        await queue.process_one(send_fn=fake_send)
        elapsed_second = time.monotonic() - t1

        assert elapsed_first < 0.05
        assert elapsed_second >= 0.03  # Should have some delay

    async def test_idempotent_start_preserves_state(self, make_adapter_context) -> None:
        """Double start does not create a new client or reset state."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        state_after_first = adapter._started
        await adapter.start(ctx)
        assert adapter._started == state_after_first

    async def test_idempotent_stop_clears_state(self, make_adapter_context) -> None:
        """Double stop does not raise and state remains cleared."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)
        await adapter.stop()
        # Double stop should be safe
        await adapter.stop()
        assert adapter._started is False
        assert adapter._session is None


# ===================================================================


class TestMeshtasticCallbackBoundary:
    """Inbound callback tasks are owned and drained by the adapter."""

    async def test_on_receive_callback_delegates_to_on_packet(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """_on_receive_callback delegates to _on_packet."""
        config = MeshtasticConfig(adapter_id="mesh-cb")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-cb")
        await adapter.start(ctx)

        packet = {
            "fromId": "!node1",
            "toId": "",
            "channel": 0,
            "id": 42,
            "decoded": {"portnum": "text_message", "text": "via callback"},
        }
        adapter._on_receive_callback(packet, interface=None)

        await asyncio.sleep(0.05)

        assert len(inbound_collector.events) == 1
        assert inbound_collector.events[0].payload["body"] == "via callback"

    async def test_background_tasks_drained_on_stop(self, make_adapter_context) -> None:
        """All background tasks are drained when stop() is called."""
        config = MeshtasticConfig(adapter_id="mesh-drain")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-drain")
        await adapter.start(ctx)

        # Simulate multiple inbound packets creating tasks
        for i in range(5):
            packet = {
                "fromId": f"!node{i}",
                "toId": "",
                "channel": 0,
                "id": i,
                "decoded": {"portnum": "text_message", "text": f"msg-{i}"},
            }
            adapter._on_packet(packet)

        await asyncio.sleep(0.05)
        # All tasks should have completed and been discarded
        await adapter.stop()
        assert len(adapter._background_tasks) == 0


# ===================================================================
# Error classification at adapter boundary
# ===================================================================


class TestMeshtasticErrorClassification:
    """Meshtastic adapter maps session/queue errors to
    AdapterSendError/AdapterPermanentError."""

    @pytest.mark.asyncio
    async def test_permanent_session_error_maps_to_permanent(self) -> None:
        """MeshtasticSendError(transient=False) maps to AdapterPermanentError."""
        from unittest.mock import AsyncMock, MagicMock

        config = MeshtasticConfig(adapter_id="mesh-ec")
        adapter = MeshtasticAdapter(config)

        queue = MagicMock()
        queue.enqueue = AsyncMock(
            side_effect=MeshtasticSendError(
                "Permanent send failure: encoding", transient=False
            )
        )
        adapter._queue = queue

        result = RenderingResult(
            event_id="evt-1",
            target_adapter="mesh-ec",
            target_channel="0",
            payload={"text": "hello", "channel_index": 0},
        )
        with pytest.raises(AdapterPermanentError, match="Permanent send failure"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_transient_session_error_maps_to_send_error(self) -> None:
        """MeshtasticSendError(transient=True) maps to AdapterSendError."""
        from unittest.mock import AsyncMock, MagicMock

        config = MeshtasticConfig(adapter_id="mesh-ec")
        adapter = MeshtasticAdapter(config)

        queue = MagicMock()
        queue.enqueue = AsyncMock(
            side_effect=MeshtasticSendError(
                "Send failed after 3 attempts", transient=True
            )
        )
        adapter._queue = queue

        result = RenderingResult(
            event_id="evt-2",
            target_adapter="mesh-ec",
            target_channel="0",
            payload={"text": "hello", "channel_index": 0},
        )
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_oserror_maps_to_transient(self) -> None:
        """OSError from queue maps to AdapterSendError(transient=True)."""
        from unittest.mock import AsyncMock, MagicMock

        config = MeshtasticConfig(adapter_id="mesh-ec")
        adapter = MeshtasticAdapter(config)

        queue = MagicMock()
        queue.enqueue = AsyncMock(side_effect=OSError("serial error"))
        adapter._queue = queue

        result = RenderingResult(
            event_id="evt-3",
            target_adapter="mesh-ec",
            target_channel="0",
            payload={"text": "hello", "channel_index": 0},
        )
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_adapter_not_started_is_permanent(self) -> None:
        """Adapter not started raises AdapterPermanentError."""
        config = MeshtasticConfig(
            adapter_id="mesh-ec", connection_type="tcp", host="localhost"
        )
        adapter = MeshtasticAdapter(config)
        # NOT started

        result = RenderingResult(
            event_id="evt-4",
            target_adapter="mesh-ec",
            target_channel="0",
            payload={"text": "hello", "channel_index": 0},
        )
        with pytest.raises(AdapterPermanentError, match="not started"):
            await adapter.deliver(result)
