"""Cross-transport mesh interop pipeline tests (Meshtastic / MeshCore / LXMF).

Device-free fast regression for the user-facing core: MT, MC, and LX
relaying to *each other* through the real codecs, packet classifiers,
renderers, and router — using the production fake adapters, whose inbound
path runs the real classifier + codec and whose outbound path consumes
real renderer output.

Topology under test: one runtime, three radio adapters, three
bidirectional radio<->radio routes (six directed edges).  Each source
native packet must arrive exactly once at BOTH far transports with the
target channel mapped, attribution rendered, and body fidelity preserved
(unicode + multiline).  The source transport itself must never receive
its own relay back (source exclusion), traffic on an unmapped source
channel must not leak, oversized bodies must respect the target's
``max_text_bytes``, and LXMF deliveries must embed the MEDRE envelope.

No Matrix adapter participates: mesh transports are the coverage target;
Matrix has its own dedicated suites.  Everything here is deterministic
and default-collected — the fast rung of the interop ladder.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from medre.runtime.builder import SourceAttributionConfig

logger = logging.getLogger(__name__)

# Route-space channel keys.  MT and MC key routes on their channel-index
# string; LX keys on the peer delivery-destination hash (the value the
# codec stamps as source_channel_id and the route carries as dest_channel).
MT_CH = "0"
MC_CH = "1"
LX_PEER_DEST = "cd" * 16

MT_ADAPTER = "mt_radio"
MC_ADAPTER = "mc_radio"
LX_ADAPTER = "lx_radio"

_PREFIX_TEMPLATE = "{sender_id}/{origin_label}: "
MC_MAX_BYTES = 160


def _mt_packet(body: str, *, channel: int = 0, packet_id: int = 4242) -> dict[str, Any]:
    """Native Meshtastic text packet as the real serial SDK delivers it."""
    return {
        "fromId": "!mtbnode",
        "toId": "",
        "channel": channel,
        "id": packet_id,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": body},
    }


def _mc_packet(body: str, *, channel: int = 1, timestamp: int = 99) -> dict[str, Any]:
    """Native MeshCore channel-text payload as the real event carries it."""
    return {
        "text": body,
        "pubkey_prefix": "mcbpeer",
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": 0,
        "channel_idx": channel,
    }


def _lx_packet(body: str) -> dict[str, Any]:
    """Native LXMF message payload as the real router delivers it."""
    return {
        "content": body,
        "source_hash": LX_PEER_DEST,
        "destination_hash": "00" * 16,
        "message_id": "ff" * 32,
        "timestamp": 1700000000.0,
        "title": "",
        "source_name": "LXB Peer",
    }


class _MeshInteropHarness:
    """One runtime, three radio fakes, three bidirectional mesh routes."""

    def __init__(
        self, storage: SQLiteStorage, prefix_template: str = _PREFIX_TEMPLATE
    ) -> None:
        self.mt = FakeMeshtasticAdapter(
            MeshtasticConfig(
                adapter_id=MT_ADAPTER,
                default_channel=0,
                radio_relay_prefix=prefix_template,
                origin_label="mtlab",
            )
        )
        self.mc = FakeMeshCoreAdapter(
            MeshCoreConfig(
                adapter_id=MC_ADAPTER,
                default_channel=1,
                meshcore_relay_prefix=prefix_template,
                origin_label="mclab",
                max_text_bytes=MC_MAX_BYTES,
            )
        )
        self.lx = FakeLxmfAdapter(
            LxmfConfig(
                adapter_id=LX_ADAPTER,
                lxmf_relay_prefix=prefix_template,
                origin_label="lxlab",
            )
        )

        routes = [
            Route(
                id="r_mt",
                source=RouteSource(
                    adapter=MT_ADAPTER,
                    event_kinds=("message.created",),
                    channel=MT_CH,
                ),
                targets=[
                    RouteTarget(adapter=MC_ADAPTER, channel=MC_CH),
                    RouteTarget(adapter=LX_ADAPTER, channel=LX_PEER_DEST),
                ],
            ),
            Route(
                id="r_mc",
                source=RouteSource(
                    adapter=MC_ADAPTER,
                    event_kinds=("message.created",),
                    channel=MC_CH,
                ),
                targets=[
                    RouteTarget(adapter=MT_ADAPTER, channel=MT_CH),
                    RouteTarget(adapter=LX_ADAPTER, channel=LX_PEER_DEST),
                ],
            ),
            Route(
                id="r_lx",
                source=RouteSource(
                    adapter=LX_ADAPTER,
                    event_kinds=("message.created",),
                    channel=LX_PEER_DEST,
                ),
                targets=[
                    RouteTarget(adapter=MT_ADAPTER, channel=MT_CH),
                    RouteTarget(adapter=MC_ADAPTER, channel=MC_CH),
                ],
            ),
        ]

        # Mirror RuntimeBuilder's production wiring: a source-attribution
        # registry keyed by adapter id with platform + origin label, handed
        # to every renderer so cross-mesh attribution resolves.
        attribution = {
            MT_ADAPTER: SourceAttributionConfig(
                adapter_id=MT_ADAPTER, platform="meshtastic", origin_label="mtlab"
            ),
            MC_ADAPTER: SourceAttributionConfig(
                adapter_id=MC_ADAPTER, platform="meshcore", origin_label="mclab"
            ),
            LX_ADAPTER: SourceAttributionConfig(
                adapter_id=LX_ADAPTER, platform="lxmf", origin_label="lxlab"
            ),
        }

        rp = RenderingPipeline()
        rp.register(
            MeshtasticRenderer(
                configs={MT_ADAPTER: self.mt._config}, source_attribution=attribution
            ),
            priority=50,
        )
        rp.register(
            MeshCoreRenderer(
                configs={MC_ADAPTER: self.mc._config}, source_attribution=attribution
            ),
            priority=50,
        )
        rp.register(
            LxmfRenderer(
                configs={LX_ADAPTER: self.lx._config}, source_attribution=attribution
            ),
            priority=50,
        )
        rp.register(TextRenderer(), priority=100)

        self.runner = PipelineRunner(
            PipelineConfig(
                storage=storage,
                router=Router(routes=routes),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=storage),
                adapters={
                    MT_ADAPTER: self.mt,
                    MC_ADAPTER: self.mc,
                    LX_ADAPTER: self.lx,
                },
                event_bus=EventBus(),
                rendering_pipeline=rp,
                runtime_accounting=RuntimeAccounting(),
            )
        )

    async def start(self) -> None:
        await self.runner.start()
        for adapter_id, adapter in (
            (MT_ADAPTER, self.mt),
            (MC_ADAPTER, self.mc),
            (LX_ADAPTER, self.lx),
        ):
            await adapter.start(self._context(adapter_id))

    def _context(self, adapter_id: str) -> AdapterContext:
        return AdapterContext(
            adapter_id=adapter_id,
            event_bus=None,
            publish_inbound=self.runner.handle_ingress,
            logger=logging.getLogger(f"test.mesh_interop.{adapter_id}"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

    async def stop(self) -> None:
        for adapter in (self.mt, self.mc, self.lx):
            try:
                await adapter.stop()
            except Exception:  # pragma: no cover - defensive teardown
                logger.debug("adapter stop failed during teardown", exc_info=True)
        await self.runner.stop()

    async def inject(self, source: str, body: str) -> None:
        """Inject a native packet through the source adapter's real
        classifier + codec + publish path (NOT a hand-built event)."""
        if source == MT_ADAPTER:
            await self.mt.simulate_inbound(_mt_packet(body))
        elif source == MC_ADAPTER:
            await self.mc.simulate_inbound(_mc_packet(body))
        elif source == LX_ADAPTER:
            await self.lx.simulate_inbound(_lx_packet(body))
        else:  # pragma: no cover - test-authoring guard
            raise ValueError(f"unknown source {source!r}")

    def delivered_bodies(self, adapter_id: str) -> list[str]:
        """Extract the rendered text from every delivered payload."""
        adapter = {MT_ADAPTER: self.mt, MC_ADAPTER: self.mc, LX_ADAPTER: self.lx}[
            adapter_id
        ]
        bodies: list[str] = []
        for result in adapter.delivered_payloads:
            payload = dict(result.payload)
            body = payload.get("text") or payload.get("content") or ""
            bodies.append(str(body))
        return bodies


_SOURCES = [MT_ADAPTER, MC_ADAPTER, LX_ADAPTER]
_FAR_TARGETS = {
    MT_ADAPTER: (MC_ADAPTER, LX_ADAPTER),
    MC_ADAPTER: (MT_ADAPTER, LX_ADAPTER),
    LX_ADAPTER: (MT_ADAPTER, MC_ADAPTER),
}
# {sender_id} is the raw native sender (node id / pubkey prefix / source
# hash); {origin_label} comes from the source adapter's registry entry.
_EXPECTED_PREFIX = {
    MT_ADAPTER: "!mtbnode/mtlab: ",
    MC_ADAPTER: "mcbpeer/mclab: ",
    LX_ADAPTER: f"{LX_PEER_DEST}/lxlab: ",
}


class TestMeshInteropPipeline:
    """Six directed radio<->radio edges through codecs/renderers/routing."""

    @pytest.mark.parametrize("source", _SOURCES)
    async def test_mesh_to_mesh_relayed_with_fidelity(
        self, temp_storage: SQLiteStorage, source: str
    ) -> None:
        """One native packet relays to BOTH far meshes, once, faithfully."""
        nonce = uuid.uuid4().hex[:8]
        body = f"MESH-{source}-{nonce} ünïcode ✓\nline2"
        harness = _MeshInteropHarness(temp_storage)
        await harness.start()
        try:
            await harness.inject(source, body)
            # Fanout is awaited inside handle_ingress; a short settle keeps
            # this robust to future async-delivery scheduling.
            await asyncio.sleep(0.2)

            prefix = _EXPECTED_PREFIX[source]
            for target in _FAR_TARGETS[source]:
                bodies = harness.delivered_bodies(target)
                matches = [b for b in bodies if body in b]
                assert len(matches) == 1, (
                    f"{source}->{target}: expected exactly one delivery "
                    f"carrying the body, got {len(matches)} of {len(bodies)} "
                    f"total: {bodies!r}"
                )
                assert matches[0].startswith(prefix), (
                    f"{source}->{target}: attribution prefix {prefix!r} "
                    f"missing from {matches[0]!r}"
                )

            # Source exclusion: the source transport never receives its
            # own relay back through the pipeline.
            assert harness.delivered_bodies(source) == [], (
                f"{source} received its own relay back (fanout loop): "
                f"{harness.delivered_bodies(source)!r}"
            )
        finally:
            await harness.stop()

    @pytest.mark.parametrize(
        ("target", "expected_channel"),
        [(MC_ADAPTER, 1), (LX_ADAPTER, LX_PEER_DEST)],
    )
    async def test_target_channel_mapping_from_meshtastic(
        self, temp_storage: SQLiteStorage, target: str, expected_channel: str
    ) -> None:
        """The MT route's per-target channel keys the native send."""
        harness = _MeshInteropHarness(temp_storage)
        await harness.start()
        try:
            await harness.inject(MT_ADAPTER, "channel-map-probe")
            await asyncio.sleep(0.2)
            adapter = {MC_ADAPTER: harness.mc, LX_ADAPTER: harness.lx}[target]
            deliveries = [r for r in adapter.delivered_payloads]
            assert (
                len(deliveries) == 1
            ), f"expected one delivery at {target}, got {len(deliveries)}"
            if target == MC_ADAPTER:
                assert deliveries[0].payload["channel_index"] == expected_channel
            else:
                assert (
                    deliveries[0].payload["destination_hash"] == expected_channel
                ), "LXMF delivery must target the routed peer dest hash"
        finally:
            await harness.stop()

    async def test_unmapped_source_channel_does_not_leak(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MT text on a channel with no route reaches no transport."""
        harness = _MeshInteropHarness(temp_storage)
        await harness.start()
        try:
            before = {aid: len(harness.delivered_bodies(aid)) for aid in _SOURCES}
            await harness.mt.simulate_inbound(_mt_packet("wrong-channel", channel=2))
            await asyncio.sleep(0.2)
            after = {aid: len(harness.delivered_bodies(aid)) for aid in _SOURCES}
            assert (
                after == before
            ), f"unmapped-channel MT packet leaked to routes: {before} -> {after}"
        finally:
            await harness.stop()

    async def test_oversize_body_truncated_to_target_limit(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A body larger than the MC target's max_text_bytes is delivered
        truncated to exactly the configured byte budget (prefix included)."""
        harness = _MeshInteropHarness(temp_storage)
        await harness.start()
        try:
            body = "O" * 400
            await harness.inject(MT_ADAPTER, body)
            await asyncio.sleep(0.2)
            mc_texts = [t for t in harness.delivered_bodies(MC_ADAPTER) if "OOO" in t]
            assert len(mc_texts) == 1, f"expected one MC delivery, got {mc_texts!r}"
            text = mc_texts[0]
            assert len(text.encode("utf-8")) == MC_MAX_BYTES, (
                f"MC delivery must respect max_text_bytes={MC_MAX_BYTES}, "
                f"got {len(text.encode('utf-8'))} bytes"
            )
            assert text.startswith(_EXPECTED_PREFIX[MT_ADAPTER])
        finally:
            await harness.stop()

    async def test_lxmf_delivery_embeds_medre_envelope(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MT->LX deliveries carry the MEDRE envelope fields so far-side
        peers can reconstruct lineage (source adapter + transport id)."""
        harness = _MeshInteropHarness(temp_storage)
        await harness.start()
        try:
            await harness.inject(MC_ADAPTER, "envelope-probe")
            await asyncio.sleep(0.2)
            lx_deliveries = harness.lx.delivered_payloads
            assert len(lx_deliveries) == 1
            fields = dict(lx_deliveries[0].payload).get("fields")
            assert (
                isinstance(fields, dict) and fields
            ), "LXMF delivery missing fields envelope"
            # Field payloads are keyed by integer field id; find any medre
            # envelope regardless of the id constant.
            envelopes = [
                v.get("medre")
                for v in fields.values()
                if isinstance(v, dict) and isinstance(v.get("medre"), dict)
            ]
            assert envelopes, f"no medre envelope in fields {fields!r}"
            envelope = envelopes[0]
            assert envelope.get("source_adapter") == MC_ADAPTER
            assert envelope.get("source_transport_id") == "mcbpeer"
        finally:
            await harness.stop()

    async def test_meshcore_wire_sender_name_flows_cross_mesh(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """MC group texts carry the sender's node name on the wire.

        The channel protocol has no sender identity, so the firmware
        embeds "<name>: <text>" (user-observed live: MT relays showed the
        sender while MC relays rendered an empty "{sender}").  The codec
        lifts the wire name into the attribution label; far transports
        must render it in a ``{sender}``-based prefix while the body
        stays verbatim.
        """
        harness = _MeshInteropHarness(
            temp_storage, prefix_template="{sender}/{origin_label}: "
        )
        await harness.start()
        try:
            body = "MEDRE-MC-B: wire-name ünïcode ✓\nline2"
            await harness.mc.simulate_inbound(_mc_packet(body))
            await asyncio.sleep(0.2)
            for target in _FAR_TARGETS[MC_ADAPTER]:
                bodies = harness.delivered_bodies(target)
                matches = [b for b in bodies if body in b]
                assert len(matches) == 1, (
                    f"mc->{target}: expected the wire-named body once, "
                    f"got {matches!r} of {bodies!r}"
                )
                assert matches[0].startswith("MEDRE-MC-B/mclab: "), (
                    f"mc->{target}: wire sender name missing from prefix: "
                    f"{matches[0]!r}"
                )
        finally:
            await harness.stop()
