"""Pipeline-level dedup: duplicate native refs suppressed, echo suppressed.

Proves that:
1. Events whose native ref already exists in storage are silently dropped
   (no second canonical event, no second delivery).
2. Events without a source_native_ref are never deduplicated.
3. Outbound native refs that echo back inbound are detected and suppressed.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.bridge import (
    make_adapter_context,
    make_pipeline_config,
)


class TestSelfMessagePrevention:
    """Pipeline-level dedup: if an inbound event's native ref already maps
    to an existing canonical event, the pipeline skips store + delivery."""

    async def test_duplicate_native_ref_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Inject event with native ref already in storage -> no second event."""
        fake_target = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="dedup-target"))

        route = Route(
            id="dedup-route",
            source=RouteSource(
                adapter="dedup-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dedup-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"dedup-target": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Pre-store a canonical event and its native ref
        original_event_id = f"orig-{uuid.uuid4()}"
        original_event = CanonicalEvent(
            event_id=original_event_id,
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="dedup-src",
            source_transport_id="dedup-src",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "original message"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="dedup-src",
                native_channel_id="ch-0",
                native_message_id="native-msg-001",
            ),
        )
        await runner.handle_ingress(original_event)

        # Verify the first event was stored and delivered
        assert accounting.snapshot()["inbound_accepted"] == 1
        assert accounting.snapshot()["outbound_delivered"] == 1
        assert len(fake_target.delivered_payloads) == 1

        # Now inject a SECOND event with the SAME native ref
        duplicate_event = CanonicalEvent(
            event_id=f"dup-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="dedup-src",
            source_transport_id="dedup-src",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "duplicate message"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="dedup-src",
                native_channel_id="ch-0",
                native_message_id="native-msg-001",
            ),
        )
        outcomes = await runner.handle_ingress(duplicate_event)

        # Pipeline suppressed the duplicate
        assert outcomes == []

        # No second canonical event stored
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events"
        )
        assert len(all_events) == 1
        assert all_events[0]["event_id"] == original_event_id

        # No second delivery receipt
        receipts = await temp_storage._read_all(
            "SELECT event_id FROM delivery_receipts"
        )
        assert len(receipts) == 1

        # Still only one delivered payload
        assert len(fake_target.delivered_payloads) == 1

        # Accounting: inbound_accepted still 1, loop_prevented incremented
        snap = accounting.snapshot()
        assert (
            snap["inbound_accepted"] == 1
        ), "Duplicate should not increment inbound_accepted"
        assert snap["loop_prevented"] == 1, "Duplicate should increment loop_prevented"

        await runner.stop()

    async def test_no_dedup_when_native_ref_absent(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Events without source_native_ref are never deduplicated."""
        fake_target = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="nodup-target"))

        route = Route(
            id="nodup-route",
            source=RouteSource(
                adapter="nodup-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="nodup-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"nodup-target": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Two events with NO source_native_ref -- both should go through
        for i in range(2):
            event = CanonicalEvent(
                event_id=f"nodup-{i}",
                event_kind=EventKind.MESSAGE_CREATED,
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="nodup-src",
                source_transport_id="nodup-src",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"body": f"msg {i}"},
                metadata=EventMetadata(),
                # No source_native_ref
            )
            await runner.handle_ingress(event)

        await runner.stop()

        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 2
        assert snap["outbound_delivered"] == 2
        assert snap["loop_prevented"] == 0
        assert len(fake_target.delivered_payloads) == 2

    async def test_null_channel_id_dedup_works(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """NativeRef with None channel_id is still deduplicated."""
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="nullch-target")
        )

        route = Route(
            id="nullch-route",
            source=RouteSource(
                adapter="nullch-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="nullch-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"nullch-target": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # First event with NativeRef(native_channel_id=None)
        first_event = CanonicalEvent(
            event_id=f"nullch-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="nullch-src",
            source_transport_id="nullch-src",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "first with null channel"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="nullch-src",
                native_channel_id=None,
                native_message_id="null-ch-test",
            ),
        )
        outcomes_first = await runner.handle_ingress(first_event)

        # First event accepted
        assert len(outcomes_first) == 1
        assert accounting.snapshot()["inbound_accepted"] == 1

        # Second event with same None-channel ref → suppressed
        second_event = CanonicalEvent(
            event_id=f"nullch-dup-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="nullch-src",
            source_transport_id="nullch-src",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "duplicate with null channel"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="nullch-src",
                native_channel_id=None,
                native_message_id="null-ch-test",
            ),
        )
        outcomes_second = await runner.handle_ingress(second_event)

        # Second event suppressed
        assert outcomes_second == []

        # Only one event in storage
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events"
        )
        assert len(all_events) == 1

        snap = accounting.snapshot()
        assert snap["loop_prevented"] == 1

        await runner.stop()

    async def test_empty_string_native_message_id_passes_through(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """native_message_id="" (falsy) bypasses dedup."""
        fake_target = FakeMeshtasticAdapter(
            MeshtasticConfig(adapter_id="emptymid-target")
        )

        route = Route(
            id="emptymid-route",
            source=RouteSource(
                adapter="emptymid-src",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="emptymid-target", channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"emptymid-target": fake_target},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Inject two events with native_message_id="" (falsy)
        for i in range(2):
            event = CanonicalEvent(
                event_id=f"emptymid-{i}",
                event_kind=EventKind.MESSAGE_CREATED,
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="emptymid-src",
                source_transport_id="emptymid-src",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"body": f"msg {i}"},
                metadata=EventMetadata(),
                source_native_ref=NativeRef(
                    adapter="emptymid-src",
                    native_channel_id="ch-0",
                    native_message_id="",
                ),
            )
            await runner.handle_ingress(event)

        await runner.stop()

        # Both events accepted (empty string bypasses dedup)
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events"
        )
        assert len(all_events) == 2

        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 2
        assert snap["loop_prevented"] == 0


class TestLoopPreventionExistingRef:
    """Outbound produces native ref. Simulate that native ref reappearing
    inbound (echo). Verify loop_prevented increments and no new delivery."""

    async def test_echo_native_ref_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Outbound delivery creates native ref. Same ref inbound -> suppressed."""
        fake_matrix = FakeMatrixAdapter("echo-mx", channel="!echo:fake")
        fake_mesh = FakeMeshtasticAdapter(MeshtasticConfig(adapter_id="echo-mesh"))

        route_a = Route(
            id="echo-mx-mesh",
            source=RouteSource(
                adapter="echo-mx",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="echo-mesh", channel="0")],
        )
        route_b = Route(
            id="echo-mesh-mx",
            source=RouteSource(
                adapter="echo-mesh",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="echo-mx", channel="!echo:fake")],
        )
        router = Router(routes=[route_a, route_b])

        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        accounting = RuntimeAccounting()
        config = make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"echo-mx": fake_matrix, "echo-mesh": fake_mesh},
            rendering_pipeline=rp,
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        await fake_matrix.start(make_adapter_context("echo-mx", runner))
        await fake_mesh.start(make_adapter_context("echo-mesh", runner))

        # Step 1: Inject a message from Matrix -> Meshtastic
        event = fake_matrix.make_event(
            text="echo test",
            event_kind=EventKind.MESSAGE_CREATED,
        )
        await fake_matrix.simulate_inbound(event)

        # After delivery, meshtastic adapter creates an outbound native ref.
        # Now simulate that native ref coming back inbound (echo).
        # The outbound delivery to mesh creates a native ref with
        # adapter="echo-mesh", native_message_id=1, native_channel_id="0"
        echo_event = CanonicalEvent(
            event_id=f"echo-{uuid.uuid4()}",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="echo-mesh",
            source_transport_id="!node1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "echo test"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="echo-mesh",
                native_channel_id="0",
                native_message_id="1",  # matches outbound ref from fake_mesh
            ),
        )
        outcomes = await runner.handle_ingress(echo_event)

        # Echo was suppressed
        assert outcomes == []

        snap = accounting.snapshot()
        assert snap["inbound_accepted"] == 1, "Only the first message accepted"
        assert snap["outbound_delivered"] == 1, "Only one outbound delivery"
        assert snap["loop_prevented"] == 1, "Echo suppressed as loop"

        # Only one canonical event stored
        all_events = await temp_storage._read_all(
            "SELECT event_id FROM canonical_events"
        )
        assert len(all_events) == 1

        # Only one delivery receipt
        receipts = await temp_storage._read_all(
            "SELECT target_adapter FROM delivery_receipts"
        )
        assert len(receipts) == 1

        await fake_matrix.stop()
        await fake_mesh.stop()
        await runner.stop()
