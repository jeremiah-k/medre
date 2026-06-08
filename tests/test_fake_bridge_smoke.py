"""Fake cross-adapter bridge smoke tests.

Proves that the MEDRE runtime bridges events between fake adapters
end-to-end through the actual pipeline, without mocks.

Flows covered
-------------
1. Matrix -> Meshtastic (presentation -> transport)
2. Meshtastic -> Matrix (transport -> presentation)
3. Bidirectional Matrix <-> Meshtastic via config-declared routes
4. Fanout: one inbound -> two outbound adapters
5. Self-loop prevention (pipeline self-loop guard)
6. Reply relation preservation across the bridge

Every test uses **fake adapters** and **in-memory storage** -- no live
transports, no SDKs, no filesystem I/O beyond temp dirs for MedrePaths.

Rendering note
--------------
``TextRenderer`` reads ``event.payload.get("text", "")``.  The fake
adapters store body text under the ``"body"`` key.  Tests that verify
rendered text content create events with an explicit ``"text"`` key.
Tests that focus on routing mechanics accept the default (empty string)
rendered output -- the key assertion is that a ``RenderingResult`` was
delivered, not its text content.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.config.routes import (
    BridgePolicy,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot

# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def wait_until(
    predicate: Any,
    timeout: float = 5.0,
    interval: float = 0.05,
) -> None:
    """Poll *predicate* until True or *timeout* expires."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"wait_until timed out after {timeout}s: "
                f"predicate {predicate!r} never satisfied"
            )
        await asyncio.sleep(min(interval, remaining))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Shared config builders
# ---------------------------------------------------------------------------


def _mx_mesh_config(
    *,
    mx_id: str = "fake_matrix",
    mesh_id: str = "fake_meshtastic",
) -> RuntimeConfig:
    """Two-adapter config: fake Matrix + fake Meshtastic."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="bridge-mx-mesh"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                mx_id: MatrixRuntimeConfig(
                    adapter_id=mx_id,
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                mesh_id: MeshtasticRuntimeConfig(
                    adapter_id=mesh_id,
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _mx_mesh_core_config() -> RuntimeConfig:
    """Three-adapter config for fanout: Matrix + Meshtastic + MeshCore."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="bridge-fanout"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "fake_matrix": MatrixRuntimeConfig(
                    adapter_id="fake_matrix",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "fake_meshtastic": MeshtasticRuntimeConfig(
                    adapter_id="fake_meshtastic",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshcore={
                "fake_meshcore": MeshCoreRuntimeConfig(
                    adapter_id="fake_meshcore",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _route_mx_to_mesh(
    *,
    route_id: str = "mx-to-mesh",
    mx_id: str = "fake_matrix",
    mesh_id: str = "fake_meshtastic",
    event_kinds: tuple[str, ...] = (),
) -> Route:
    """Route: Matrix -> Meshtastic."""
    return Route(
        id=route_id,
        source=RouteSource(
            adapter=mx_id,
            event_kinds=event_kinds,
            channel=None,
        ),
        targets=[RouteTarget(adapter=mesh_id)],
    )


def _route_mesh_to_mx(
    *,
    route_id: str = "mesh-to-mx",
    mx_id: str = "fake_matrix",
    mesh_id: str = "fake_meshtastic",
    event_kinds: tuple[str, ...] = (),
) -> Route:
    """Route: Meshtastic -> Matrix."""
    return Route(
        id=route_id,
        source=RouteSource(
            adapter=mesh_id,
            event_kinds=event_kinds,
            channel=None,
        ),
        targets=[RouteTarget(adapter=mx_id)],
    )


# ---------------------------------------------------------------------------
# Shared lifecycle helpers
# ---------------------------------------------------------------------------


async def _build_and_start(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp from config and start it."""
    builder = RuntimeBuilder(config, paths)
    app = builder.build()
    await app.start()
    return app


async def _clean_stop(app: MedreApp) -> None:
    """Stop a running MedreApp, asserting it reaches STOPPED."""
    await app.stop()
    assert app.state is RuntimeState.STOPPED


def _make_matrix_event(
    adapter: FakeMatrixAdapter,
    text: str,
    *,
    event_kind: str = EventKind.MESSAGE_TEXT,
    channel: str | None = None,
) -> CanonicalEvent:
    """Create a Matrix-sourced event with both 'body' and 'text' payload keys.

    TextRenderer reads ``payload["text"]``, but FakeMatrixAdapter.make_event
    stores text under ``"body"`` only.  This helper includes ``"text"`` so that
    rendered output is non-empty and inspectable.
    """
    event = adapter.make_event(
        text=text,
        event_kind=event_kind,
        channel=channel,
    )
    # TextRenderer reads payload["text"], but FakeMatrixAdapter.make_event stores
    # text under "body" only.  Inject "text" into the payload so rendered output
    # is non-empty and inspectable.
    merged = dict(event.payload)
    merged["text"] = text
    event = CanonicalEvent(
        event_id=event.event_id,
        event_kind=event.event_kind,
        schema_version=event.schema_version,
        timestamp=event.timestamp,
        source_adapter=event.source_adapter,
        source_transport_id=event.source_transport_id,
        source_channel_id=event.source_channel_id,
        parent_event_id=event.parent_event_id,
        lineage=event.lineage,
        relations=event.relations,
        payload=merged,
        metadata=event.metadata,
        source_native_ref=event.source_native_ref,
    )
    return event


def _make_meshtastic_event(
    adapter: FakeMeshtasticAdapter,
    body: str,
    *,
    sender: str = "!bridge-test",
    channel: int = 0,
    packet_id: int = 9001,
) -> CanonicalEvent:
    """Create a Meshtastic-sourced canonical event with 'text' payload key.

    MeshtasticCodec stores body under ``payload["body"]``.  We rebuild the
    event to include ``payload["text"]`` so the TextRenderer output is
    inspectable.  This helper exercises the real codec for the original
    decode, then reconstructs with the added key.
    """
    base = adapter.make_text_event(
        body=body,
        sender=sender,
        channel=channel,
        packet_id=packet_id,
    )
    # Rebuild with "text" key added to payload.
    merged_payload = dict(base.payload)
    merged_payload["text"] = body
    return CanonicalEvent(
        event_id=base.event_id,
        event_kind=base.event_kind,
        schema_version=base.schema_version,
        timestamp=base.timestamp,
        source_adapter=base.source_adapter,
        source_transport_id=base.source_transport_id,
        source_channel_id=base.source_channel_id,
        parent_event_id=base.parent_event_id,
        lineage=base.lineage,
        relations=base.relations,
        payload=merged_payload,
        metadata=base.metadata,
        source_native_ref=base.source_native_ref,
    )


# ===================================================================
# 1. MATRIX -> MESHTATIC UNIDIRECTIONAL
# ===================================================================


class TestMatrixToMeshtastic:
    """Matrix inbound -> Meshtastic outbound via the runtime pipeline."""

    @pytest.mark.asyncio
    async def test_event_stored_and_delivered(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Full pipeline: Matrix event stored, routed, rendered, delivered to Meshtastic."""
        config = _mx_mesh_config()
        route = _route_mx_to_mesh()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            mesh = app.adapters["fake_meshtastic"]
            assert isinstance(mx, FakeMatrixAdapter)
            assert isinstance(mesh, FakeMeshtasticAdapter)

            event = _make_matrix_event(mx, "Bridge me to mesh")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            # -- Inbound canonical event persisted --
            assert app.storage is not None
            stored = await app.storage.get(event.event_id)
            assert stored is not None
            assert stored.event_id == event.event_id
            assert stored.source_adapter == "fake_matrix"

            # -- Pipeline returned success --
            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "success"
            assert outcome.target_adapter == "fake_meshtastic"

            # -- DeliveryReceipt persisted --
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            receipt = receipts[0]
            assert receipt.status == "sent"
            assert receipt.target_adapter == "fake_meshtastic"
            assert receipt.route_id == "mx-to-mesh"
            assert receipt.source == "live"

            # -- NativeMessageRef persisted (adapter returns native ID) --
            # FakeMeshtasticClient generates sequential packet IDs.
            native_id = "1"  # first packet
            resolved = await app.storage.resolve_native_ref(
                "fake_meshtastic",
                "0",
                native_id,
            )
            assert resolved is not None
            assert resolved == event.event_id

            # -- Outbound adapter received rendered payload --
            assert len(mesh.delivered_payloads) == 1
            payload = mesh.delivered_payloads[0]
            assert isinstance(payload, RenderingResult)
            assert payload.target_adapter == "fake_meshtastic"
            assert "text" in payload.payload

            # -- Runtime accounting incremented --
            acc = app._runtime_accounting.snapshot()
            assert acc["inbound_accepted"] == 1
            assert acc["outbound_attempts"] == 1
            assert acc["outbound_delivered"] == 1
            assert acc["outbound_failed"] == 0

            # -- RouteStats updated --
            stats = app.route_stats.snapshot()
            assert "mx-to-mesh" in stats
            assert stats["mx-to-mesh"]["delivered"] == 1

            # -- No duplicate delivery --
            assert len(mesh.delivered_payloads) == 1

            # -- Source adapter identity preserved in stored event --
            assert stored.source_adapter == "fake_matrix"
        finally:
            await _clean_stop(app)


# ===================================================================
# 2. MESHTASTIC -> MATRIX UNIDIRECTIONAL
# ===================================================================


class TestMeshtasticToMatrix:
    """Meshtastic inbound -> Matrix outbound via the runtime pipeline."""

    @pytest.mark.asyncio
    async def test_mesh_inbound_routes_to_matrix(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Meshtastic text event bridges to Matrix with receipts and native refs."""
        config = _mx_mesh_config()
        route = _route_mesh_to_mx()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            mesh = app.adapters["fake_meshtastic"]
            assert isinstance(mx, FakeMatrixAdapter)
            assert isinstance(mesh, FakeMeshtasticAdapter)

            event = _make_meshtastic_event(mesh, "Radio message to Matrix")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            # -- Event stored --
            assert app.storage is not None
            stored = await app.storage.get(event.event_id)
            assert stored is not None
            assert stored.source_adapter == "fake_meshtastic"

            # -- Success outcome --
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"
            assert outcomes[0].target_adapter == "fake_matrix"

            # -- Receipt persisted --
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "sent"
            assert receipts[0].target_adapter == "fake_matrix"

            # -- NativeMessageRef for outbound Matrix delivery --
            native_id = f"$fake_{event.event_id}"
            resolved = await app.storage.resolve_native_ref(
                "fake_matrix",
                "",
                native_id,
            )
            assert resolved is not None
            assert resolved == event.event_id

            # -- Inbound native ref persisted (Meshtastic source_native_ref) --
            if event.source_native_ref is not None:
                inbound_resolved = await app.storage.resolve_native_ref(
                    event.source_native_ref.adapter,
                    event.source_native_ref.native_channel_id,
                    event.source_native_ref.native_message_id,
                )
                assert inbound_resolved == event.event_id

            # -- Matrix adapter received rendering result --
            assert len(mx.delivered_payloads) == 1
            result = mx.delivered_payloads[0]
            assert isinstance(result, RenderingResult)
            assert result.target_adapter == "fake_matrix"

            # -- Accounting --
            acc = app._runtime_accounting.snapshot()
            assert acc["inbound_accepted"] == 1
            assert acc["outbound_delivered"] == 1

            # -- RouteStats --
            stats = app.route_stats.snapshot()
            assert stats["mesh-to-mx"]["delivered"] == 1
        finally:
            await _clean_stop(app)


# ===================================================================
# 3. BIDIRECTIONAL BRIDGE VIA CONFIG-DECLARED ROUTES
# ===================================================================


class TestBidirectionalBridge:
    """Bidirectional Matrix <-> Meshtastic bridge via RouteConfigSet."""

    @pytest.mark.asyncio
    async def test_bidirectional_routes_registered(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Bidirectional route config expands to two core Routes on the Router."""
        config = _mx_mesh_config()
        route_config = RouteConfig(
            route_id="mx-mesh-bidir",
            source_adapters=("fake_matrix",),
            dest_adapters=("fake_meshtastic",),
            directionality=RouteDirectionality.BIDIRECTIONAL,
            enabled=True,
        )
        config = RuntimeConfig(
            runtime=config.runtime,
            logging=config.logging,
            storage=config.storage,
            limits=config.limits,
            adapters=config.adapters,
            routes=RouteConfigSet(routes=(route_config,)),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        await app.start()

        try:
            # Two routes registered: forward + reverse.
            assert app.router is not None
            # The forward route keeps the original ID.
            # The reverse route gets "__rev_0" suffix.
            assert app._registered_routes is not None
            assert len(app._registered_routes) == 2
            route_ids = sorted(r.id for r in app._registered_routes)
            assert "mx-mesh-bidir" in route_ids
            assert "mx-mesh-bidir__rev_0" in route_ids

            # RouteEligibility shows both as REGISTERED.
            assert app.route_eligibility is not None
            states = app.route_eligibility.route_states
            assert states.get("mx-mesh-bidir") is not None
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_both_directions_deliver(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Events flow Matrix->Meshtastic AND Meshtastic->Matrix."""
        config = _mx_mesh_config()
        # Register both directional routes manually for clarity.
        fwd_route = _route_mx_to_mesh(route_id="fwd")
        rev_route = _route_mesh_to_mx(route_id="rev")
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(fwd_route)
        app.router.add_route(rev_route)

        try:
            mx = app.adapters["fake_matrix"]
            mesh = app.adapters["fake_meshtastic"]
            assert isinstance(mx, FakeMatrixAdapter)
            assert isinstance(mesh, FakeMeshtasticAdapter)

            # -- Forward: Matrix -> Meshtastic --
            fwd_event = _make_matrix_event(mx, "Forward direction")
            fwd_outcomes = await app.pipeline_runner.handle_ingress(fwd_event)
            assert len(fwd_outcomes) == 1
            assert fwd_outcomes[0].status == "success"
            assert fwd_outcomes[0].target_adapter == "fake_meshtastic"
            assert len(mesh.delivered_payloads) == 1

            # -- Reverse: Meshtastic -> Matrix --
            rev_event = _make_meshtastic_event(mesh, "Reverse direction")
            rev_outcomes = await app.pipeline_runner.handle_ingress(rev_event)
            assert len(rev_outcomes) == 1
            assert rev_outcomes[0].status == "success"
            assert rev_outcomes[0].target_adapter == "fake_matrix"
            assert len(mx.delivered_payloads) == 1

            # -- No cross-contamination --
            # Matrix received only the reverse delivery.
            assert len(mx.delivered_payloads) == 1
            # Meshtastic received only the forward delivery.
            assert len(mesh.delivered_payloads) == 1

            # -- Two receipts total --
            fwd_receipts = await app.storage.list_receipts_for_event(fwd_event.event_id)
            assert len(fwd_receipts) == 1
            assert fwd_receipts[0].target_adapter == "fake_meshtastic"

            rev_receipts = await app.storage.list_receipts_for_event(rev_event.event_id)
            assert len(rev_receipts) == 1
            assert rev_receipts[0].target_adapter == "fake_matrix"

            # -- Accounting reflects both --
            acc = app._runtime_accounting.snapshot()
            assert acc["inbound_accepted"] == 2
            assert acc["outbound_attempts"] == 2
            assert acc["outbound_delivered"] == 2
        finally:
            await _clean_stop(app)


# ===================================================================
# 4. FANOUT: ONE INBOUND -> TWO OUTBOUND
# ===================================================================


class TestFanoutDelivery:
    """Single inbound event delivers to multiple target adapters."""

    @pytest.mark.asyncio
    async def test_one_inbound_two_outbound(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Matrix event fans out to Meshtastic and MeshCore."""
        config = _mx_mesh_core_config()
        fanout_route = Route(
            id="fanout",
            source=RouteSource(
                adapter="fake_matrix",
                event_kinds=(),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="fake_meshtastic"),
                RouteTarget(adapter="fake_meshcore"),
            ],
        )
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(fanout_route)

        try:
            mx = app.adapters["fake_matrix"]
            mesh = app.adapters["fake_meshtastic"]
            core = app.adapters["fake_meshcore"]
            assert isinstance(mx, FakeMatrixAdapter)
            assert isinstance(mesh, FakeMeshtasticAdapter)
            assert isinstance(core, FakeMeshCoreAdapter)

            event = _make_matrix_event(mx, "Fanout message")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            # -- Two successful deliveries --
            assert len(outcomes) == 2
            target_adapters = {o.target_adapter for o in outcomes}
            assert target_adapters == {"fake_meshtastic", "fake_meshcore"}
            assert all(o.status == "success" for o in outcomes)

            # -- Both adapters received rendering results --
            assert len(mesh.delivered_payloads) == 1
            assert len(core.delivered_payloads) == 1

            # -- Two receipts persisted --
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 2
            receipt_targets = {r.target_adapter for r in receipts}
            assert receipt_targets == {"fake_meshtastic", "fake_meshcore"}
            assert all(r.status == "sent" for r in receipts)

            # -- Two native refs persisted --
            # Meshtastic: sequential packet ID "1"
            mesh_resolved = await app.storage.resolve_native_ref(
                "fake_meshtastic",
                "0",
                "1",
            )
            assert mesh_resolved == event.event_id

            # MeshCore: first packet from its own sequential counter.
            core_resolved = await app.storage.resolve_native_ref(
                "fake_meshcore",
                "0",
                "1",
            )
            assert core_resolved == event.event_id

            # -- Accounting: 1 inbound, 2 outbound attempts, 2 delivered --
            acc = app._runtime_accounting.snapshot()
            assert acc["inbound_accepted"] == 1
            assert acc["outbound_attempts"] == 2
            assert acc["outbound_delivered"] == 2

            # -- RouteStats --
            stats = app.route_stats.snapshot()
            assert stats["fanout"]["delivered"] == 2
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_fanout_one_target_fails_other_succeeds(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Error isolation: one fanout target fails, the other succeeds."""
        config = _mx_mesh_core_config()
        fanout_route = Route(
            id="fanout-partial",
            source=RouteSource(adapter="fake_matrix", event_kinds=(), channel=None),
            targets=[
                RouteTarget(adapter="fake_meshtastic"),
                RouteTarget(adapter="fake_meshcore"),
            ],
        )
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(fanout_route)

        try:
            mesh = app.adapters["fake_meshtastic"]
            assert isinstance(mesh, FakeMeshtasticAdapter)
            mesh.set_deliver_failure(True)

            mx = app.adapters["fake_matrix"]
            event = _make_matrix_event(mx, "Partial fanout")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 2
            statuses = {o.target_adapter: o.status for o in outcomes}
            assert statuses["fake_meshcore"] == "success"
            assert statuses["fake_meshtastic"] != "success"

            # MeshCore still got its delivery.
            core = app.adapters["fake_meshcore"]
            assert len(core.delivered_payloads) == 1

            # Accounting: 1 failed, 1 delivered.
            acc = app._runtime_accounting.snapshot()
            assert acc["outbound_delivered"] == 1
            assert acc["outbound_failed"] == 1
        finally:
            await _clean_stop(app)


# ===================================================================
# 5. LOOP PREVENTION
# ===================================================================


class TestLoopPrevention:
    """Self-loop guard prevents delivery back to the source adapter."""

    @pytest.mark.asyncio
    async def test_self_loop_guard_skips_delivery(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Pipeline skips delivery when target adapter == source adapter.

        The config-level RouteConfig rejects overlapping source/dest
        adapters, so this test manually registers a Route that would
        create a self-loop.  The pipeline's self-loop guard in
        ``_deliver_to_targets_fan_out`` catches this at runtime.
        """
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="loop-test"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "mx_a": MatrixRuntimeConfig(
                        adapter_id="mx_a",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
        )
        # Manually register a route that routes mx_a -> mx_a (self-loop).
        loop_route = Route(
            id="self-loop",
            source=RouteSource(
                adapter="mx_a",
                event_kinds=(),
                channel=None,
            ),
            targets=[RouteTarget(adapter="mx_a")],
        )
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(loop_route)

        try:
            mx = app.adapters["mx_a"]
            assert isinstance(mx, FakeMatrixAdapter)

            event = _make_matrix_event(mx, "Should not loop back")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            # -- Outcome is skipped, not success --
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
            assert outcomes[0].target_adapter == "mx_a"
            assert "loop_prevented" in (outcomes[0].error or "")

            # -- No delivery to the adapter --
            assert len(mx.delivered_payloads) == 0

            # -- Accounting: loop_prevented incremented --
            acc = app._runtime_accounting.snapshot()
            assert acc["loop_prevented"] == 1

            # -- RouteStats: loop_prevented counter --
            stats = app.route_stats.snapshot()
            assert stats["self-loop"]["loop_prevented"] == 1
            assert stats["self-loop"]["delivered"] == 0

            # -- Event was still stored (ingestion succeeded) --
            assert app.storage is not None
            stored = await app.storage.get(event.event_id)
            assert stored is not None

            # -- Suppressed evidence receipt for self-loop target --
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "suppressed"
            assert receipts[0].target_adapter == "mx_a"
            assert receipts[0].failure_kind == "loop_suppressed"
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_no_loop_when_separate_adapters(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Cross-adapter routing (A -> B) does NOT trigger loop prevention."""
        config = _mx_mesh_config()
        route = _route_mx_to_mesh()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            event = _make_matrix_event(mx, "Not a loop")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            acc = app._runtime_accounting.snapshot()
            assert acc["loop_prevented"] == 0
        finally:
            await _clean_stop(app)


# ===================================================================
# 6. REPLY RELATION PRESERVATION
# ===================================================================


class TestReplyRelationPreservation:
    """Reply relations survive the full pipeline bridge."""

    @pytest.mark.asyncio
    async def test_reply_event_stored_with_relations(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Reply event's relation tuple is preserved in storage after bridge."""
        config = _mx_mesh_config()
        route = _route_mx_to_mesh()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            mesh = app.adapters["fake_meshtastic"]
            assert isinstance(mx, FakeMatrixAdapter)
            assert isinstance(mesh, FakeMeshtasticAdapter)

            # Create original message and a reply to it.
            original = mx.make_event("Original message")
            reply = mx.make_reply_event(
                original,
                text="Reply to original",
                channel=None,
            )
            # Ensure "text" key is in payload for rendering.
            reply = CanonicalEvent(
                event_id=reply.event_id,
                event_kind=reply.event_kind,
                schema_version=reply.schema_version,
                timestamp=reply.timestamp,
                source_adapter=reply.source_adapter,
                source_transport_id=reply.source_transport_id,
                source_channel_id=reply.source_channel_id,
                parent_event_id=reply.parent_event_id,
                lineage=reply.lineage,
                relations=reply.relations,
                payload={"body": "Reply to original", "text": "Reply to original"},
                metadata=reply.metadata,
                source_native_ref=reply.source_native_ref,
            )

            outcomes = await app.pipeline_runner.handle_ingress(reply)

            # -- Event stored with relations intact --
            assert app.storage is not None
            stored = await app.storage.get(reply.event_id)
            assert stored is not None
            assert len(stored.relations) == 1
            assert stored.relations[0].relation_type == "reply"
            assert stored.relations[0].target_event_id == original.event_id

            # -- Bridge delivered successfully --
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # -- Meshtastic received rendered reply --
            assert len(mesh.delivered_payloads) == 1
            rendered = mesh.delivered_payloads[0]
            assert isinstance(rendered, RenderingResult)
            # TextRenderer augments reply text with fallback_text prefix
            # when the reply relation has fallback_text.  Our reply event
            # does not set fallback_text, so rendered text is the raw
            # payload text.
            assert "text" in rendered.payload
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_reply_without_native_ref_uses_plain_text(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Reply without native ref uses plain text, no '[replying to: ...]' prefix."""
        config = _mx_mesh_config()
        route = _route_mx_to_mesh()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            assert isinstance(mx, FakeMatrixAdapter)

            original = mx.make_event("Original")

            # Build a reply with explicit fallback_text.
            from medre.core.events.canonical import EventRelation, NativeRef

            reply_relation = EventRelation(
                relation_type="reply",
                target_event_id=original.event_id,
                target_native_ref=NativeRef(
                    adapter="fake_matrix",
                    native_channel_id=original.source_channel_id,
                    native_message_id=original.event_id,
                ),
                key=None,
                fallback_text="Original message",
            )
            reply = CanonicalEvent(
                event_id="reply-fallback-001",
                event_kind=EventKind.MESSAGE_TEXT,
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="fake_matrix",
                source_transport_id="fake_matrix",
                source_channel_id=original.source_channel_id,
                parent_event_id=None,
                lineage=(),
                relations=(reply_relation,),
                payload={"body": "My reply", "text": "My reply"},
                metadata=EventMetadata(),
            )

            outcomes = await app.pipeline_runner.handle_ingress(reply)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            mesh = app.adapters["fake_meshtastic"]
            assert isinstance(mesh, FakeMeshtasticAdapter)
            assert len(mesh.delivered_payloads) == 1
            rendered = mesh.delivered_payloads[0]
            rendered_text = str(rendered.payload.get("text", ""))
            assert rendered_text == "My reply"
        finally:
            await _clean_stop(app)


# ===================================================================
# RENDERING CONTRACT ASSERTIONS
# ===================================================================


class TestRenderingContract:
    """Rendering boundary assertions: format, escaping, failure modes."""

    @pytest.mark.asyncio
    async def test_rendering_result_shape(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """RenderingResult has deterministic fields: event_id, target_adapter, payload."""
        config = _mx_mesh_config()
        route = _route_mx_to_mesh()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            event = _make_matrix_event(mx, "Check rendering shape")
            await app.pipeline_runner.handle_ingress(event)

            mesh = app.adapters["fake_meshtastic"]
            assert isinstance(mesh, FakeMeshtasticAdapter)
            result = mesh.delivered_payloads[0]

            # RenderingResult fields.
            assert result.event_id == event.event_id
            assert result.target_adapter == "fake_meshtastic"
            assert isinstance(result.payload, dict)
            assert "text" in result.payload
            assert isinstance(result.metadata, dict)
            assert result.metadata.get("renderer") == "meshtastic"
            assert "original_length" in result.metadata
            assert "rendered_length" in result.metadata
            # No truncation — short message, truncated defaults to False.
            assert result.truncated is False
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_empty_payload_renders_empty_text(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Event with 'body' key (no 'text') renders body text via fallback."""
        config = _mx_mesh_config()
        route = _route_mx_to_mesh()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            assert isinstance(mx, FakeMatrixAdapter)
            # make_event stores body only, no "text" key.
            event = mx.make_event("Payload body only")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            # Delivery succeeds — TextRenderer reads payload["body"] as fallback.
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            mesh = app.adapters["fake_meshtastic"]
            assert isinstance(mesh, FakeMeshtasticAdapter)
            result = mesh.delivered_payloads[0]
            # TextRenderer now checks "body" when "text" is absent.
            assert result.payload.get("text") == "Payload body only"
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_unsupported_event_kind_rejected_by_renderer(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Event kind not handled by any renderer produces RENDERER_FAILURE."""
        config = _mx_mesh_config()
        route = _route_mx_to_mesh(event_kinds=())
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            assert isinstance(mx, FakeMatrixAdapter)
            event = mx.make_event(
                text="telemetry data",
                event_kind=EventKind.TELEMETRY_RECEIVED,
            )
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            # No renderer matches TELEMETRY_RECEIVED for the meshtastic
            # platform -> ValueError in RenderingPipeline.render
            # -> caught as RENDERER_FAILURE.
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is not None
            assert "renderer" in (outcomes[0].error or "").lower()

            # Receipt persisted with failed status.
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            assert receipts[0].status == "failed"
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_long_text_truncated_by_byte_budget(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Long text is truncated to the Meshtastic byte budget (227 bytes)."""
        config = _mx_mesh_config()
        route = _route_mx_to_mesh()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            long_text = "A" * 600
            event = _make_matrix_event(mx, long_text)
            await app.pipeline_runner.handle_ingress(event)

            mesh = app.adapters["fake_meshtastic"]
            result = mesh.delivered_payloads[0]
            assert result.truncated is True
            assert len(result.payload.get("text", "").encode("utf-8")) <= 227
            assert result.metadata.get("original_length") == 600
            assert result.metadata.get("rendered_length") == 227
        finally:
            await _clean_stop(app)


# ===================================================================
# SNAPSHOT REFLECTS BRIDGE FLOW
# ===================================================================


class TestSnapshotReflectsBridgeFlow:
    """Runtime snapshot captures accounting and routes after bridge activity."""

    @pytest.mark.asyncio
    async def test_snapshot_after_bridge_delivery(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Snapshot includes accounting counters and route stats after flow."""
        config = _mx_mesh_config()
        route = _route_mx_to_mesh()
        app = await _build_and_start(config, tmp_paths)
        app.router.add_route(route)

        try:
            mx = app.adapters["fake_matrix"]
            event = _make_matrix_event(mx, "Snapshot test")
            await app.pipeline_runner.handle_ingress(event)

            snap = build_runtime_snapshot(app)

            # -- Schema version --
            assert snap["schema_version"] == SCHEMA_VERSION

            # -- Lifecycle --
            assert snap["lifecycle"]["runtime_state"] == "running"

            # -- Accounting counters reflect flow --
            counters = snap["accounting"]["counters"]
            assert counters is not None
            assert counters["inbound_accepted"] == 1
            assert counters["outbound_delivered"] == 1

            # -- Route stats --
            route_stats = snap["routes"]["stats"]["per_route"]
            assert "mx-to-mesh" in route_stats
            assert route_stats["mx-to-mesh"]["delivered"] == 1

            # -- Adapters populated --
            assert "fake_matrix" in snap["adapters"]
            assert "fake_meshtastic" in snap["adapters"]

            # -- JSON-safe --
            serialized = json.dumps(snap, sort_keys=True)
            assert isinstance(serialized, str)
        finally:
            await _clean_stop(app)


# ===================================================================
# ROUTE CONFIG THROUGH RUNTIME
# ===================================================================


class TestRouteConfigThroughRuntime:
    """Route configs parse, validate, and register through RuntimeBuilder."""

    @pytest.mark.asyncio
    async def test_config_route_registers_and_delivers(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """RouteConfig with source_to_dest registers and delivers events."""
        config = _mx_mesh_config()
        route_config = RouteConfig(
            route_id="cfg-mx-mesh",
            source_adapters=("fake_matrix",),
            dest_adapters=("fake_meshtastic",),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            enabled=True,
        )
        config = RuntimeConfig(
            runtime=config.runtime,
            logging=config.logging,
            storage=config.storage,
            limits=config.limits,
            adapters=config.adapters,
            routes=RouteConfigSet(routes=(route_config,)),
        )
        app = await _build_and_start(config, tmp_paths)

        try:
            # Route was registered by the builder.
            assert len(app._registered_routes) == 1
            assert app._registered_routes[0].id == "cfg-mx-mesh"

            # Exercise the route.
            mx = app.adapters["fake_matrix"]
            event = _make_matrix_event(mx, "Config route delivery")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "success"
            assert outcomes[0].route_id == "cfg-mx-mesh"

            # Verify route eligibility metadata.
            assert app.route_eligibility is not None
            assert "cfg-mx-mesh" in app.route_eligibility.registered
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_bidirectional_config_expands_two_routes(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Bidirectional RouteConfig produces two registered Routes."""
        config = _mx_mesh_config()
        route_config = RouteConfig(
            route_id="bidir",
            source_adapters=("fake_matrix",),
            dest_adapters=("fake_meshtastic",),
            directionality=RouteDirectionality.BIDIRECTIONAL,
            enabled=True,
        )
        config = RuntimeConfig(
            runtime=config.runtime,
            logging=config.logging,
            storage=config.storage,
            limits=config.limits,
            adapters=config.adapters,
            routes=RouteConfigSet(routes=(route_config,)),
        )
        app = await _build_and_start(config, tmp_paths)

        try:
            assert len(app._registered_routes) == 2
            ids = sorted(r.id for r in app._registered_routes)
            assert ids == ["bidir", "bidir__rev_0"]

            # Forward route: matrix -> meshtastic.
            fwd = (
                app._registered_routes[0]
                if app._registered_routes[0].id == "bidir"
                else app._registered_routes[1]
            )
            assert fwd.source.adapter == "fake_matrix"
            assert fwd.targets[0].adapter == "fake_meshtastic"

            # Reverse route: meshtastic -> matrix.
            rev = (
                app._registered_routes[1]
                if app._registered_routes[0].id == "bidir"
                else app._registered_routes[0]
            )
            assert rev.source.adapter == "fake_meshtastic"
            assert rev.targets[0].adapter == "fake_matrix"
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_policy_allowed_event_types_filter(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """BridgePolicy allowed_event_types maps to RouteSource.event_kinds."""
        config = _mx_mesh_config()
        route_config = RouteConfig(
            route_id="filtered",
            source_adapters=("fake_matrix",),
            dest_adapters=("fake_meshtastic",),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            enabled=True,
            policy=BridgePolicy(allowed_event_types=("message.text",)),
        )
        config = RuntimeConfig(
            runtime=config.runtime,
            logging=config.logging,
            storage=config.storage,
            limits=config.limits,
            adapters=config.adapters,
            routes=RouteConfigSet(routes=(route_config,)),
        )
        app = await _build_and_start(config, tmp_paths)

        try:
            assert len(app._registered_routes) == 1
            route = app._registered_routes[0]
            assert route.source.event_kinds == ("message.text",)

            # message.text event matches.
            mx = app.adapters["fake_matrix"]
            text_event = _make_matrix_event(mx, "Matches filter")
            outcomes = await app.pipeline_runner.handle_ingress(text_event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"

            # message.created event does NOT match.
            base_evt = mx.make_event(
                text="Wrong kind",
                event_kind=EventKind.MESSAGE_CREATED,
            )
            # Inject "text" into payload for consistent rendering.
            _p = dict(base_evt.payload)
            _p["text"] = "Wrong kind"
            created_event = CanonicalEvent(
                event_id=base_evt.event_id,
                event_kind=base_evt.event_kind,
                schema_version=base_evt.schema_version,
                timestamp=base_evt.timestamp,
                source_adapter=base_evt.source_adapter,
                source_transport_id=base_evt.source_transport_id,
                source_channel_id=base_evt.source_channel_id,
                parent_event_id=base_evt.parent_event_id,
                lineage=base_evt.lineage,
                relations=base_evt.relations,
                payload=_p,
                metadata=base_evt.metadata,
                source_native_ref=base_evt.source_native_ref,
            )
            outcomes2 = await app.pipeline_runner.handle_ingress(created_event)
            assert len(outcomes2) == 0  # no route match
        finally:
            await _clean_stop(app)

    @pytest.mark.asyncio
    async def test_disabled_route_not_registered(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Disabled route config is skipped during registration."""
        config = _mx_mesh_config()
        route_config = RouteConfig(
            route_id="disabled-route",
            source_adapters=("fake_matrix",),
            dest_adapters=("fake_meshtastic",),
            enabled=False,
        )
        config = RuntimeConfig(
            runtime=config.runtime,
            logging=config.logging,
            storage=config.storage,
            limits=config.limits,
            adapters=config.adapters,
            routes=RouteConfigSet(routes=(route_config,)),
        )
        app = await _build_and_start(config, tmp_paths)

        try:
            assert len(app._registered_routes) == 0
            assert app.route_eligibility is not None
            assert "disabled-route" in app.route_eligibility.disabled
        finally:
            await _clean_stop(app)
