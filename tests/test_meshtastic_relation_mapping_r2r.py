"""Reaction-to-reaction suppression tests.

Verifies that reactions targeting other reactions are suppressed by the
pipeline (no delivery) while inbound native refs are still stored, and
that normal reactions to non-reaction messages still route and deliver
correctly.

Test categories
---------------
Test A
    Meshtastic reaction targeting another Meshtastic reaction (pre-resolved
    ``target_event_id``) — suppressed at routing but inbound native ref is
    still persisted.

Test B
    Matrix reaction targeting another Matrix reaction (pre-resolved
    ``target_event_id``) — suppressed at routing but inbound native ref is
    still persisted.

Test A2
    Meshtastic reaction arriving with ``target_native_ref`` and
    ``target_event_id=None``.  RelationResolver resolves the native ref
    before suppression, so the pipeline still returns [] and persists the
    inbound native ref.

Test B2
    Matrix reaction arriving with ``target_native_ref`` and
    ``target_event_id=None``.  Same resolution-then-suppression path as
    Test A2, exercising the Matrix adapter ID space.

Test C
    Normal reaction targeting a non-reaction message — routes and delivers
    as expected (regression guard).

All tests use ``FakeMeshtasticAdapter`` / ``FakeMatrixAdapter``.  No live
services required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage

# Fixed IDs shared with test_meshtastic_relation_mapping.py.
_RADIO_ADAPTER = "radio"
_MATRIX_ADAPTER = "matrix"
_MATRIX_ROOM = "!room:server"


# ===================================================================
# Test A: Meshtastic reaction → Meshtastic reaction (suppressed)
# ===================================================================


class TestMeshtasticReactionToReactionSuppressed:
    """Test A: Meshtastic reaction targeting another Meshtastic reaction
    is suppressed at routing but inbound ref is still stored."""

    async def test_reaction_to_reaction_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Seed a stored Meshtastic reaction; ingest a new reaction targeting
        it — pipeline returns [] but inbound native ref is persisted."""
        ts = datetime.now(timezone.utc)

        # 1. Seed a prior canonical reaction event from Meshtastic.
        prior_reaction_event = CanonicalEvent(
            event_id="prior-mesh-reaction-1",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=ts,
            source_adapter=_RADIO_ADAPTER,
            source_transport_id="!meshnode1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="some-original-msg",
                    target_native_ref=None,
                    key="👍",
                    fallback_text=None,
                ),
            ),
            payload={"body": "👍"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_RADIO_ADAPTER,
                native_channel_id="0",
                native_message_id="111111",
            ),
        )
        await temp_storage.append(prior_reaction_event)

        # Store inbound native ref for the prior reaction.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-prior-rxn",
                event_id="prior-mesh-reaction-1",
                adapter=_RADIO_ADAPTER,
                native_channel_id="0",
                native_message_id="111111",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # 2. Build a new reaction event targeting the prior reaction.
        new_reaction_rel = EventRelation(
            relation_type="reaction",
            target_event_id="prior-mesh-reaction-1",
            target_native_ref=None,
            key="❤️",
            fallback_text=None,
        )
        new_reaction_event = CanonicalEvent(
            event_id="new-mesh-reaction-2",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_RADIO_ADAPTER,
            source_transport_id="!meshnode2",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(new_reaction_rel,),
            payload={"body": "❤️"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_RADIO_ADAPTER,
                native_channel_id="0",
                native_message_id="222222",
            ),
        )

        # Route that would deliver to Matrix.
        matrix_adapter = FakeMatrixAdapter(
            adapter_id=_MATRIX_ADAPTER, channel=_MATRIX_ROOM
        )
        route = Route(
            id="radio-to-matrix-r2r",
            source=RouteSource(
                adapter=_RADIO_ADAPTER,
                event_kinds=("message.reacted",),
                channel="0",
            ),
            targets=[RouteTarget(adapter=_MATRIX_ADAPTER, channel=_MATRIX_ROOM)],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MATRIX_ADAPTER: matrix_adapter},
                event_bus=EventBus(),
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(new_reaction_event)

            # 3. Pipeline returns [] — reaction-to-reaction suppressed.
            assert outcomes == []

            # 4. Inbound native ref IS still stored (Stage 4 ran before suppression).
            resolved = await temp_storage.resolve_native_ref(
                _RADIO_ADAPTER, "0", "222222"
            )
            assert resolved == "new-mesh-reaction-2"

            # 5. _is_reaction_to_reaction returns True directly.
            assert await runner._is_reaction_to_reaction(new_reaction_event) is True
        finally:
            await runner.stop()


# ===================================================================
# Test B: Matrix reaction → Matrix reaction (suppressed)
# ===================================================================


class TestMatrixReactionToReactionSuppressed:
    """Test B: Matrix reaction targeting another Matrix reaction is
    suppressed but inbound ref is still stored."""

    async def test_matrix_reaction_to_reaction_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Seed a stored Matrix reaction; ingest a new Matrix m.reaction
        targeting it — no delivery but inbound ref stored."""
        ts = datetime.now(timezone.utc)

        # 1. Seed a prior Matrix reaction canonical event.
        prior_reaction = CanonicalEvent(
            event_id="prior-mx-reaction-1",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MATRIX_ADAPTER,
            source_transport_id="@user1:server",
            source_channel_id=_MATRIX_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="some-orig-msg",
                    target_native_ref=None,
                    key="👍",
                    fallback_text=None,
                ),
            ),
            payload={"body": "👍"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$prior-rxn-mx",
            ),
        )
        await temp_storage.append(prior_reaction)

        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-prior-mx-rxn",
                event_id="prior-mx-reaction-1",
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$prior-rxn-mx",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # 2. New Matrix reaction targeting the prior reaction.
        new_rel = EventRelation(
            relation_type="reaction",
            target_event_id="prior-mx-reaction-1",
            target_native_ref=None,
            key="❤️",
            fallback_text=None,
        )
        new_event = CanonicalEvent(
            event_id="new-mx-reaction-2",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_MATRIX_ADAPTER,
            source_transport_id="@user2:server",
            source_channel_id=_MATRIX_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(new_rel,),
            payload={"body": "❤️"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$new-rxn-mx",
            ),
        )

        radio_config = MeshtasticConfig(adapter_id=_RADIO_ADAPTER)
        radio_adapter = FakeMeshtasticAdapter(radio_config)
        route = Route(
            id="matrix-to-radio-r2r",
            source=RouteSource(
                adapter=_MATRIX_ADAPTER,
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_RADIO_ADAPTER, channel="0")],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_RADIO_ADAPTER: radio_adapter},
                event_bus=EventBus(),
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(new_event)

            # No delivery — reaction-to-reaction suppressed.
            assert outcomes == []

            # Inbound ref still stored.
            resolved = await temp_storage.resolve_native_ref(
                _MATRIX_ADAPTER, _MATRIX_ROOM, "$new-rxn-mx"
            )
            assert resolved == "new-mx-reaction-2"
        finally:
            await runner.stop()


# ===================================================================
# Test A2: Meshtastic native-ref resolution → r2r suppression
# ===================================================================


class TestMeshtasticNativeRefResolutionR2R:
    """Test A2: Reaction arrives with target_native_ref (no target_event_id).
    RelationResolver resolves it before suppression check.  Pipeline
    returns [] but inbound native ref is still persisted."""

    async def test_native_ref_resolved_then_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Seed a prior reaction; new reaction uses target_native_ref only."""
        ts = datetime.now(timezone.utc)

        # 1. Seed a prior canonical reaction event from Meshtastic.
        prior_reaction_event = CanonicalEvent(
            event_id="prior-mesh-reaction-nr1",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=ts,
            source_adapter=_RADIO_ADAPTER,
            source_transport_id="!meshnode1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="some-original-msg",
                    target_native_ref=None,
                    key="👍",
                    fallback_text=None,
                ),
            ),
            payload={"body": "👍"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_RADIO_ADAPTER,
                native_channel_id="0",
                native_message_id="333333",
            ),
        )
        await temp_storage.append(prior_reaction_event)

        # Store inbound native ref for the prior reaction so
        # resolve_native_ref can find it.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-prior-rxn-nr1",
                event_id="prior-mesh-reaction-nr1",
                adapter=_RADIO_ADAPTER,
                native_channel_id="0",
                native_message_id="333333",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # 2. New reaction with target_native_ref but NO target_event_id.
        new_reaction_rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter=_RADIO_ADAPTER,
                native_channel_id="0",
                native_message_id="333333",
            ),
            key="❤️",
            fallback_text=None,
        )
        new_reaction_event = CanonicalEvent(
            event_id="new-mesh-reaction-nr2",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_RADIO_ADAPTER,
            source_transport_id="!meshnode2",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(new_reaction_rel,),
            payload={"body": "❤️"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_RADIO_ADAPTER,
                native_channel_id="0",
                native_message_id="444444",
            ),
        )

        matrix_adapter = FakeMatrixAdapter(
            adapter_id=_MATRIX_ADAPTER, channel=_MATRIX_ROOM
        )
        route = Route(
            id="radio-to-matrix-r2r-nr",
            source=RouteSource(
                adapter=_RADIO_ADAPTER,
                event_kinds=("message.reacted",),
                channel="0",
            ),
            targets=[RouteTarget(adapter=_MATRIX_ADAPTER, channel=_MATRIX_ROOM)],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_MATRIX_ADAPTER: matrix_adapter},
                event_bus=EventBus(),
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(new_reaction_event)

            # Suppressed — reaction-to-reaction.
            assert outcomes == []

            # Inbound native ref still persisted.
            resolved = await temp_storage.resolve_native_ref(
                _RADIO_ADAPTER, "0", "444444"
            )
            assert resolved == "new-mesh-reaction-nr2"
        finally:
            await runner.stop()


# ===================================================================
# Test B2: Matrix native-ref resolution → r2r suppression
# ===================================================================


class TestMatrixNativeRefResolutionR2R:
    """Test B2: Matrix reaction arrives with target_native_ref only.
    RelationResolver resolves native ref → event_id, then r2r suppression
    kicks in.  Pipeline returns [] but inbound ref is persisted."""

    async def test_matrix_native_ref_resolved_then_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Seed a prior Matrix reaction; new reaction uses native ref only."""
        ts = datetime.now(timezone.utc)

        # 1. Seed a prior Matrix reaction.
        prior_reaction = CanonicalEvent(
            event_id="prior-mx-reaction-nr1",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MATRIX_ADAPTER,
            source_transport_id="@user1:server",
            source_channel_id=_MATRIX_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="some-orig-msg",
                    target_native_ref=None,
                    key="👍",
                    fallback_text=None,
                ),
            ),
            payload={"body": "👍"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$prior-rxn-mx-nr",
            ),
        )
        await temp_storage.append(prior_reaction)

        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-prior-mx-rxn-nr1",
                event_id="prior-mx-reaction-nr1",
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$prior-rxn-mx-nr",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # 2. New Matrix reaction targeting via native ref only.
        new_rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$prior-rxn-mx-nr",
            ),
            key="❤️",
            fallback_text=None,
        )
        new_event = CanonicalEvent(
            event_id="new-mx-reaction-nr2",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_MATRIX_ADAPTER,
            source_transport_id="@user2:server",
            source_channel_id=_MATRIX_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(new_rel,),
            payload={"body": "❤️"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$new-rxn-mx-nr",
            ),
        )

        radio_config = MeshtasticConfig(adapter_id=_RADIO_ADAPTER)
        radio_adapter = FakeMeshtasticAdapter(radio_config)
        route = Route(
            id="matrix-to-radio-r2r-nr",
            source=RouteSource(
                adapter=_MATRIX_ADAPTER,
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_RADIO_ADAPTER, channel="0")],
        )
        router = Router(routes=[route])

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_RADIO_ADAPTER: radio_adapter},
                event_bus=EventBus(),
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(new_event)

            # Suppressed — reaction-to-reaction.
            assert outcomes == []

            # Inbound ref still stored.
            resolved = await temp_storage.resolve_native_ref(
                _MATRIX_ADAPTER, _MATRIX_ROOM, "$new-rxn-mx-nr"
            )
            assert resolved == "new-mx-reaction-nr2"
        finally:
            await runner.stop()


# ===================================================================
# Test C: Normal reaction to a normal message (not suppressed)
# ===================================================================


class TestNormalReactionStillRoutes:
    """Test C: Normal reaction to a normal message still routes correctly."""

    async def test_normal_reaction_routes_and_delivers(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A reaction to a non-reaction message routes and delivers normally."""
        ts = datetime.now(timezone.utc)

        # Seed a normal message.created event.
        orig_event = CanonicalEvent(
            event_id="normal-msg-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=ts,
            source_adapter=_MATRIX_ADAPTER,
            source_transport_id="@user1:server",
            source_channel_id=_MATRIX_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "Hello world"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$normal-msg-mx",
            ),
        )
        await temp_storage.append(orig_event)

        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-normal-msg",
                event_id="normal-msg-1",
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$normal-msg-mx",
                native_thread_id=None,
                native_relation_id=None,
                direction="inbound",
                created_at=ts,
            )
        )

        # Outbound Meshtastic native ref for enrichment.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-normal-msg-out",
                event_id="normal-msg-1",
                adapter=_RADIO_ADAPTER,
                native_channel_id="0",
                native_message_id="555555",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                created_at=ts,
            )
        )

        # Normal reaction targeting the normal message.
        reaction_rel = EventRelation(
            relation_type="reaction",
            target_event_id="normal-msg-1",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        reaction_event = CanonicalEvent(
            event_id="normal-reaction-1",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_MATRIX_ADAPTER,
            source_transport_id="@user2:server",
            source_channel_id=_MATRIX_ROOM,
            parent_event_id=None,
            lineage=(),
            relations=(reaction_rel,),
            payload={"body": "👍"},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter=_MATRIX_ADAPTER,
                native_channel_id=_MATRIX_ROOM,
                native_message_id="$normal-rxn-mx",
            ),
        )

        radio_config = MeshtasticConfig(adapter_id=_RADIO_ADAPTER)
        radio_adapter = FakeMeshtasticAdapter(radio_config)
        route = Route(
            id="matrix-to-radio-normal-rxn",
            source=RouteSource(
                adapter=_MATRIX_ADAPTER,
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=_RADIO_ADAPTER, channel="0")],
        )
        router = Router(routes=[route])

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={_RADIO_ADAPTER: MeshtasticConfig(adapter_id=_RADIO_ADAPTER)}
            ),
            priority=50,
        )
        rp.register_adapter_platform(_RADIO_ADAPTER, "meshtastic")
        rp.register(TextRenderer(), priority=100)

        runner = PipelineRunner(
            PipelineConfig(
                storage=temp_storage,
                router=router,
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=temp_storage),
                adapters={_RADIO_ADAPTER: radio_adapter},
                event_bus=EventBus(),
                rendering_pipeline=rp,
            )
        )
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(reaction_event)

            # Should deliver — NOT suppressed.
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Adapter received the payload.
            assert len(radio_adapter.delivered_payloads) == 1

            # _is_reaction_to_reaction returns False.
            assert await runner._is_reaction_to_reaction(reaction_event) is False
        finally:
            await runner.stop()
