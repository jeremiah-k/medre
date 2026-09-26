"""End-to-end runtime routing tests for context_map routes.

Covers the full expansion -> registration -> match -> resolve_targets
pipeline for ``context_map`` routes (bidirectional mesh<->matrix pair and
synthetic transports unknown to the runtime), plus registration
provenance.  No real services are spawned and no adapter platform
knowledge is involved anywhere in the runtime engine.

Split from ``test_runtime_routing.py`` to keep both files under the
1500-line ceiling; pure move, no content changes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.config.routes import (
    ContextMapEntry,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.routing import Router
from medre.runtime.route_engine import register_routes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    source_adapter: str = "adapter_a",
    event_kind: str = "message.created",
    source_channel_id: str | None = "ch-0",
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for routing tests."""
    return CanonicalEvent(
        event_id="evt-1",
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hi"},
        metadata=EventMetadata(),
    )


# ===================================================================
# context_map — End-to-end route behavior tests
# ===================================================================


# Adapter IDs used throughout these tests.
_MATRIX_ID = "matrix_adapter"
_MESH_ID = "mesh_adapter"


def _ctx_rc(
    route_id: str = "ctx_bridge",
    *,
    directionality: RouteDirectionality = RouteDirectionality.BIDIRECTIONAL,
    context_map: dict[str, ContextMapEntry] | None = None,
) -> RouteConfig:
    """Build a RouteConfig with context_map for mesh channels 0 + 1.

    The mesh side is the config source, so the map keys are the mesh
    channel contexts and the dest contexts are the Matrix rooms.
    """
    if context_map is None:
        context_map = {
            "0": ContextMapEntry(dest_context="!channel-0:example.com"),
            "1": ContextMapEntry(dest_context="!channel-1:example.com"),
        }
    return RouteConfig(
        route_id=route_id,
        source_adapters=(_MESH_ID,),
        dest_adapters=(_MATRIX_ID,),
        directionality=directionality,
        context_map=context_map,
    )


def _build_ctx_router(
    rc: RouteConfig | None = None,
) -> Router:
    """Build and return a Router with context_map routes registered."""
    if rc is None:
        rc = _ctx_rc()
    rcs = RouteConfigSet(routes=(rc,))
    router = Router()
    register_routes(
        router,
        rcs,
        frozenset({_MATRIX_ID, _MESH_ID}),
    )
    return router


class TestContextMapEndToEnd:
    """End-to-end route behaviour tests for context_map.

    Tests exercise the full expansion → registration → match →
    resolve_targets pipeline using deterministic routing-only assertions.
    No real services are spawned and no adapter platform knowledge is
    involved anywhere in the runtime engine.
    """

    # ------------------------------------------------------------------
    # 1. Meshtastic ingress routing (channel 1)
    # ------------------------------------------------------------------

    def test_meshtastic_ch1_delivers_to_correct_matrix_room(self) -> None:
        """Meshtastic event from channel 1 delivers only to the mapped
        Matrix room ``!channel-1:example.com``."""
        router = _build_ctx_router()

        evt = _make_event(source_adapter=_MESH_ID, source_channel_id="1")
        matched = router.match(evt)

        # Exactly one meshtastic-to-matrix route for channel 1.
        assert len(matched) == 1
        targets = router.resolve_targets(evt, matched[0])
        assert len(targets) == 1
        assert targets[0].adapter == _MATRIX_ID
        assert targets[0].channel == "!channel-1:example.com"

    def test_meshtastic_ch1_no_cross_channel_delivery(self) -> None:
        """Meshtastic ch1 event does NOT match any other room routes."""
        router = _build_ctx_router()

        evt = _make_event(source_adapter=_MESH_ID, source_channel_id="1")
        matched = router.match(evt)
        assert len(matched) > 0

        for route in matched:
            targets = router.resolve_targets(evt, route)
            for t in targets:
                assert t.channel == "!channel-1:example.com"
                assert t.channel != "!channel-0:example.com"

    # ------------------------------------------------------------------
    # 2. Multiple channel test (channel 0)
    # ------------------------------------------------------------------

    def test_meshtastic_ch0_delivers_to_correct_matrix_room(self) -> None:
        """Meshtastic event from channel 0 delivers to ``!channel-0:example.com``."""
        router = _build_ctx_router()

        evt = _make_event(source_adapter=_MESH_ID, source_channel_id="0")
        matched = router.match(evt)

        assert len(matched) == 1
        targets = router.resolve_targets(evt, matched[0])
        assert len(targets) == 1
        assert targets[0].adapter == _MATRIX_ID
        assert targets[0].channel == "!channel-0:example.com"

    # ------------------------------------------------------------------
    # 3. Unmapped channel produces no delivery
    # ------------------------------------------------------------------

    def test_meshtastic_unmapped_channel_no_delivery(self) -> None:
        """Meshtastic event from unmapped channel 2 matches no routes."""
        router = _build_ctx_router()

        evt = _make_event(source_adapter=_MESH_ID, source_channel_id="2")
        matched = router.match(evt)
        assert matched == []

    # ------------------------------------------------------------------
    # 4. Matrix → Meshtastic routing
    # ------------------------------------------------------------------

    def test_matrix_room_routes_to_meshtastic_channel(self) -> None:
        """Matrix event from room ``!channel-1:example.com`` delivers to
        Meshtastic target with target_channel ``"1"``."""
        router = _build_ctx_router()

        evt = _make_event(
            source_adapter=_MATRIX_ID,
            source_channel_id="!channel-1:example.com",
        )
        matched = router.match(evt)

        assert len(matched) == 1
        targets = router.resolve_targets(evt, matched[0])
        assert len(targets) == 1
        assert targets[0].adapter == _MESH_ID
        assert targets[0].channel == "1"

    def test_matrix_room_0_routes_to_meshtastic_ch0(self) -> None:
        """Matrix event from room ``!channel-0:example.com`` delivers to
        Meshtastic channel 0."""
        router = _build_ctx_router()

        evt = _make_event(
            source_adapter=_MATRIX_ID,
            source_channel_id="!channel-0:example.com",
        )
        matched = router.match(evt)

        assert len(matched) == 1
        targets = router.resolve_targets(evt, matched[0])
        assert len(targets) == 1
        assert targets[0].adapter == _MESH_ID
        assert targets[0].channel == "0"

    def test_matrix_unmapped_room_no_delivery(self) -> None:
        """Matrix event from unmapped room matches no routes."""
        router = _build_ctx_router()

        evt = _make_event(
            source_adapter=_MATRIX_ID,
            source_channel_id="!unmapped:example.com",
        )
        matched = router.match(evt)
        assert matched == []

    # ------------------------------------------------------------------
    # 5. Channel-strict reply enrichment
    # ------------------------------------------------------------------

    async def test_reply_enrichment_is_channel_strict(
        self,
        temp_storage,
    ) -> None:
        """Reply/reaction native-ref mapping is channel-strict.

        Seed a Meshtastic outbound native ref on channel 0 and a Matrix
        outbound ref in room ``!channel-0:example.com`` for the same
        canonical event.  A Matrix reply in room 0 must enrich with the
        Meshtastic ref whose ``native_channel_id == "0"``, never a ref
        from a different channel.
        """
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events import (
            EventMetadata,
            EventRelation,
            NativeMessageRef,
        )
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver

        prior_event_id = "prior-ch0-001"

        # Also store the prior canonical event (for text enrichment).
        prior_event = CanonicalEvent(
            event_id=prior_event_id,
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_MESH_ID,
            source_transport_id="node-1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Original message on ch0"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(prior_event)

        # Seed Meshtastic outbound ref on channel 0.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-mesh-ch0",
                event_id=prior_event_id,
                adapter=_MESH_ID,
                native_channel_id="0",
                native_message_id="mesh-pkt-42",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
            )
        )

        # Seed a *wrong-channel* Meshtastic outbound ref on channel 1
        # for the same event — the enrichment must NOT pick this one.
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-mesh-ch1-wrong",
                event_id=prior_event_id,
                adapter=_MESH_ID,
                native_channel_id="1",
                native_message_id="mesh-pkt-99",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
            )
        )

        # Build a Matrix reply event targeting the prior event.
        rel = EventRelation(
            relation_type="reply",
            target_event_id=prior_event_id,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        reply_event = CanonicalEvent(
            event_id="reply-matrix-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_MATRIX_ID,
            source_transport_id="bot",
            source_channel_id="!channel-0:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "A reply from Matrix"},
            metadata=EventMetadata(),
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

        # Enrich for Meshtastic target on channel "0".
        result = await runner._enrich_relations_for_target(
            reply_event, _MESH_ID, target_channel="0"
        )
        enriched_rel = result.relations[0]

        # The enriched native ref must be from channel 0.
        assert enriched_rel.target_native_ref is not None
        assert enriched_rel.target_native_ref.adapter == _MESH_ID
        assert enriched_rel.target_native_ref.native_channel_id == "0"
        assert enriched_rel.target_native_ref.native_message_id == "mesh-pkt-42"

    async def test_reply_enrichment_rejects_wrong_channel_ref(
        self,
        temp_storage,
    ) -> None:
        """When only a wrong-channel native ref exists, enrichment does
        NOT attach it."""
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events import (
            EventMetadata,
            EventRelation,
            NativeMessageRef,
        )
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver

        prior_event_id = "prior-wrongch-001"

        # Store the prior event for text enrichment.
        prior_event = CanonicalEvent(
            event_id=prior_event_id,
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_MESH_ID,
            source_transport_id="node-1",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "Original on ch0"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(prior_event)

        # Seed ONLY a wrong-channel Meshtastic ref (channel 7).
        await temp_storage.store_native_ref(
            NativeMessageRef(
                id="nref-mesh-ch7",
                event_id=prior_event_id,
                adapter=_MESH_ID,
                native_channel_id="7",
                native_message_id="mesh-pkt-777",
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
            )
        )

        rel = EventRelation(
            relation_type="reply",
            target_event_id=prior_event_id,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        reply_event = CanonicalEvent(
            event_id="reply-wrongch-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=_MATRIX_ID,
            source_transport_id="bot",
            source_channel_id="!channel-0:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "Reply"},
            metadata=EventMetadata(),
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

        result = await runner._enrich_relations_for_target(
            reply_event, _MESH_ID, target_channel="0"
        )
        enriched_rel = result.relations[0]

        # Must NOT be enriched with the channel-7 ref.
        assert enriched_rel.target_native_ref is None


# ===================================================================
# Genericity — synthetic transports unknown to the runtime
# ===================================================================

_DISCORD_ID = "discord-bot"
_MQTT_ID = "mqtt-client"


class TestSyntheticTransportGenericity:
    """A route between adapters on transports the runtime has no built-in
    knowledge of registers and routes purely from configuration.

    The runtime engine contains no platform branches: expansion is
    compiled from config, registration validates only adapter IDs, and
    Router matching is source-channel string equality.  These tests use
    direct config construction (no such transports exist as adapters).
    """

    def _build_router(self) -> Router:
        rc = RouteConfig(
            route_id="synthetic_bridge",
            source_adapters=(_DISCORD_ID,),
            dest_adapters=(_MQTT_ID,),
            directionality=RouteDirectionality.BIDIRECTIONAL,
            context_map={
                "general-chat": ContextMapEntry(
                    dest_context="home/sensors/temperature"
                ),
                "ops-chat": ContextMapEntry(dest_context="home/sensors/humidity"),
            },
        )
        router = Router()
        register_routes(
            router,
            RouteConfigSet(routes=(rc,)),
            frozenset({_DISCORD_ID, _MQTT_ID}),
        )
        return router

    def test_unknown_transports_register_and_route(self) -> None:
        """A discord-style event routes to the mapped MQTT-style topic."""
        router = self._build_router()

        evt = _make_event(source_adapter=_DISCORD_ID, source_channel_id="general-chat")
        matched = router.match(evt)
        assert len(matched) == 1
        assert matched[0].id == "synthetic_bridge__map0__fwd"

        targets = router.resolve_targets(evt, matched[0])
        assert [(t.adapter, t.channel) for t in targets] == [
            (_MQTT_ID, "home/sensors/temperature")
        ]

    def test_reverse_direction_routes_back(self) -> None:
        """An MQTT-style event routes back to the mapped discord-style context."""
        router = self._build_router()

        evt = _make_event(
            source_adapter=_MQTT_ID,
            source_channel_id="home/sensors/humidity",
        )
        matched = router.match(evt)
        assert len(matched) == 1
        assert matched[0].id == "synthetic_bridge__map1__rev"

        targets = router.resolve_targets(evt, matched[0])
        assert [(t.adapter, t.channel) for t in targets] == [(_DISCORD_ID, "ops-chat")]

    def test_unmapped_context_does_not_route(self) -> None:
        """An unknown context on either side matches no legs."""
        router = self._build_router()

        evt = _make_event(
            source_adapter=_DISCORD_ID, source_channel_id="random-channel"
        )
        assert router.match(evt) == []

        evt = _make_event(
            source_adapter=_MQTT_ID,
            source_channel_id="home/sensors/pressure",
        )
        assert router.match(evt) == []

    def test_provenance_maps_every_leg_to_config_route(self) -> None:
        """The registration result exposes explicit provenance for all legs."""
        rc = RouteConfig(
            route_id="synthetic_bridge",
            source_adapters=(_DISCORD_ID,),
            dest_adapters=(_MQTT_ID,),
            directionality=RouteDirectionality.BIDIRECTIONAL,
            context_map={
                "general-chat": ContextMapEntry(
                    dest_context="home/sensors/temperature"
                ),
            },
        )
        result = register_routes(
            Router(),
            RouteConfigSet(routes=(rc,)),
            frozenset({_DISCORD_ID, _MQTT_ID}),
        )
        assert result.provenance == {
            "synthetic_bridge__map0__fwd": "synthetic_bridge",
            "synthetic_bridge__map0__rev": "synthetic_bridge",
        }
