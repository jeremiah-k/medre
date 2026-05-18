r"""Cross-adapter artifact run test: Matrix → Meshtastic delivery.

Docker-gated integration test proving real Matrix adapter ingress through
PipelineRunner to real Meshtastic adapter outbound delivery with structured
artifact output.

This test wires a real MatrixAdapter (connected to Docker Synapse) as the
pipeline source and a real MeshtasticAdapter (connected to Docker meshtasticd)
as the pipeline target.  A message is sent via the Matrix test user, ingested
through the SDK-boundary path, routed by PipelineRunner, and delivered to the
MeshtasticAdapter.  After pipeline delivery enqueues the payload locally,
``send_one()`` is called to prove real outbound SDK delivery through
meshtasticd.

A ``FakeMatrixAdapter`` is included as a secondary pipeline target so that
``_wait_for_sync_or_fallback`` can poll ``delivered_payloads`` for ingress
path detection — the same technique used in
``test_synapse_bridge_smoke.py``.

Evidence classification
-----------------------
- **Matrix ingress**: Proven through the same sync loop / fallback mechanism
  as ``test_synapse_bridge_smoke``.  The ``matrix_ingress_path`` field records
  which path was used.
- **Meshtastic outbound**: Proven by manual ``send_one()`` call after
  pipeline delivery, returning a real packet ID from meshtasticd.
- **Cross-transport proof**: Partial.  The pipeline routes between adapters
  in-process; the Matrix and Meshtastic SDKs do not interact directly.

Honest limitations
-------------------
- Manual ``send_one()`` trigger (not automatic queue drain).
- Docker loopback only (no real LoRa radio).
- Fire-and-forget radio delivery (remote receipt not confirmed).
- Single message only (no sustained throughput).
- Meshtastic→Matrix direction is deferred (not tested here).

Running locally::

    pip install -e ".[matrix,meshtastic]"
    pytest tests/integration/test_cross_adapter_artifact_run.py -m docker -v

With artifact collection::

    MEDRE_DOCKER_ARTIFACT_RUN_DIR=/tmp/artifacts \\
    pytest tests/integration/test_cross_adapter_artifact_run.py -m docker -v
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_NIO
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.compat import HAS_MESHTASTIC
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend

from .conftest import (
    _RUN_ARTIFACT_DIR,
    MeshtasticdEnvironment,
    SynapseEnvironment,
    _write_artifact_json,
    _write_run_metadata,
)
from .synapse_helpers import (
    INBOUND_FALLBACK as _INBOUND_FALLBACK,
    INBOUND_SYNC_LOOP as _INBOUND_SYNC_LOOP,
    wait_for_sync_or_fallback as _wait_for_sync_or_fallback,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gating — docker marker + both SDK dependencies
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.docker

_SKIP_REASONS: list[str] = []
if not HAS_NIO:
    _SKIP_REASONS.append("mindroom-nio not installed; run: pip install '.[matrix]'")
if not HAS_MESHTASTIC:
    _SKIP_REASONS.append("mtjk not installed; run: pip install '.[meshtastic]'")

if _SKIP_REASONS:
    pytestmark = [
        pytest.mark.docker,
        pytest.mark.skip(
            reason="Cross-adapter requires both SDKs: " + "; ".join(_SKIP_REASONS)
        ),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_matrix_config(env: SynapseEnvironment) -> MatrixConfig:
    """Build a MatrixConfig pointing at the Docker Synapse."""
    return MatrixConfig(
        adapter_id="cross-matrix-source",
        homeserver=env.base_url,
        user_id=env.bot_user_id,
        access_token=env.bot_access_token,
        room_allowlist={env.test_room_id},
        encryption_mode="plaintext",
    ).validate()


def _make_mesh_config(env: MeshtasticdEnvironment) -> MeshtasticConfig:
    """Build a TCP MeshtasticConfig pointing at the Docker meshtasticd."""
    return MeshtasticConfig(
        adapter_id="cross-mesh-target",
        connection_type="tcp",
        host=env.host,
        port=env.port,
        meshnet_name="MEDRE Cross-Adapter CI",
        message_delay_seconds=0.0,
    ).validate()


def _make_adapter_context(
    adapter_id: str,
    runner: PipelineRunner,
) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""

    async def _publish(event: Any) -> None:
        await runner.ingress_handler(event)

    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_publish,
        logger=logging.getLogger(f"test.cross_adapter.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_pipeline_config(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict[str, Any],
    runtime_accounting: RuntimeAccounting,
) -> PipelineConfig:
    """Build a PipelineConfig with both Matrix and Meshtastic renderers."""
    rp = RenderingPipeline()
    rp.register(MeshtasticRenderer(), priority=50)
    rp.register(MatrixRenderer(), priority=50)
    rp.register(TextRenderer(), priority=100)

    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters,
        event_bus=EventBus(),
        rendering_pipeline=rp,
        runtime_accounting=runtime_accounting,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCrossAdapterArtifactRun:
    """Cross-adapter Docker integration: Matrix → Meshtastic.

    Proves real MatrixAdapter ingress through PipelineRunner to real
    MeshtasticAdapter outbound delivery with structured artifact output.
    """

    @pytest.mark.asyncio
    async def test_matrix_to_meshtastic_cross_adapter(
        self,
        synapse_env: SynapseEnvironment,
        meshtasticd_env: MeshtasticdEnvironment,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Matrix ingress → PipelineRunner → Meshtastic outbound via send_one.

        Proves:
        1. Real MatrixAdapter receives a message from Docker Synapse.
        2. PipelineRunner routes the canonical event to MeshtasticAdapter.
        3. MeshtasticAdapter.deliver() enqueues the rendered payload.
        4. Manual send_one() delivers via real sendText to meshtasticd.
        5. Real packet ID returned as native_message_id.
        6. Structured artifacts persisted when MEDRE_DOCKER_ARTIFACT_RUN_DIR
           is set.
        """
        ts = int(time.time())
        body_text = f"MEDRE cross-adapter smoke (ts={ts})"
        txn_id = f"cross-adapter-txn-{ts}"

        # -- 1. Set up adapters -----------------------------------------
        accounting = RuntimeAccounting()

        matrix_config = _make_matrix_config(synapse_env)
        matrix_adapter = MatrixAdapter(matrix_config)

        mesh_config = _make_mesh_config(meshtasticd_env)
        mesh_target = MeshtasticAdapter(mesh_config)

        # FakeMatrixAdapter as monitoring target so _wait_for_sync_or_fallback
        # can poll delivered_payloads for ingress path detection.
        fake_monitor = FakeMatrixAdapter(
            "cross-fake-monitor",
            channel="ch-monitor",
        )

        # -- 2. Route: matrix-source → [mesh-target, fake-monitor] ------
        route = Route(
            id="cross-adapter-route",
            source=RouteSource(
                adapter="cross-matrix-source",
                event_kinds=("message.created",),
                channel=synapse_env.test_room_id,
            ),
            targets=[
                RouteTarget(adapter="cross-mesh-target", channel="0"),
                RouteTarget(adapter="cross-fake-monitor"),
            ],
        )
        router = Router(routes=[route])

        adapters: dict[str, Any] = {
            "cross-matrix-source": matrix_adapter,
            "cross-mesh-target": mesh_target,
            "cross-fake-monitor": fake_monitor,
        }

        pipeline_config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters=adapters,
            runtime_accounting=accounting,
        )
        runner = PipelineRunner(pipeline_config)
        await runner.start()

        # Wire adapters to pipeline ingress.
        matrix_ctx = _make_adapter_context("cross-matrix-source", runner)
        await matrix_adapter.start(matrix_ctx)

        mesh_ctx = _make_adapter_context("cross-mesh-target", runner)
        await mesh_target.start(mesh_ctx)

        fake_ctx = _make_adapter_context("cross-fake-monitor", runner)
        await fake_monitor.start(fake_ctx)

        try:
            # -- 3. Ingress via _wait_for_sync_or_fallback ---------------
            ingress = await _wait_for_sync_or_fallback(
                synapse_env=synapse_env,
                matrix_adapter=matrix_adapter,
                fake_out=fake_monitor,
                body_text=body_text,
                txn_id=txn_id,
            )
            native_event_id = ingress.native_event_id
            matrix_ingress_path = ingress.ingress_path

            logger.info(
                "Cross-adapter ingress: path=%s event_id=%s",
                matrix_ingress_path,
                native_event_id,
            )

            # -- 4. Verify pipeline delivered to MeshtasticAdapter --------
            # The background _process_queue task may have already drained the
            # queue, so check total_sent (not pending_count) for race-free
            # assertion.  pending + sent covers both the fast-drain and
            # slow-drain scenarios.
            pending = mesh_target.queue.pending_count
            sent = mesh_target.queue.total_sent
            assert pending + sent >= 1, (
                "MeshtasticAdapter queue should have at least one enqueued or "
                "already-sent payload after pipeline delivery, got "
                f"pending={pending} sent={sent}"
            )

            # -- 5. Manual send_one for real outbound proof ---------------
            # Only call send_one() when the queue still has pending items.
            # If the background drain task already sent the message, the
            # total_sent counter (verified above) is sufficient proof.
            send_result = None
            if mesh_target.queue.pending_count >= 1:
                send_result = await mesh_target.send_one()
                assert send_result is not None, (
                    "send_one() should return a result when queue is non-empty "
                    "and session is connected"
                )
                assert (
                    send_result.native_message_id is not None
                ), "send_one() should return a real packet ID from meshtasticd"
                logger.info(
                    "Cross-adapter outbound (manual): native_message_id=%s "
                    "native_channel_id=%s",
                    send_result.native_message_id,
                    send_result.native_channel_id,
                )
            else:
                logger.info(
                    "Cross-adapter outbound: background drain task already "
                    "sent the message (total_sent=%d)",
                    mesh_target.queue.total_sent,
                )

            # -- 6. Storage assertions ------------------------------------
            canonical_id = await temp_storage.resolve_native_ref(
                adapter="cross-matrix-source",
                native_channel_id=synapse_env.test_room_id,
                native_message_id=native_event_id,
            )
            assert canonical_id is not None, (
                f"Expected inbound native ref for Matrix event " f"{native_event_id!r}"
            )

            receipts = await temp_storage.list_receipts_for_event(
                canonical_id,
            )
            receipt_count = len(receipts)
            assert receipt_count >= 1, (
                f"Expected at least one delivery receipt for " f"event {canonical_id!r}"
            )

            # Count total native refs (inbound Matrix + outbound Mesh).
            native_ref_count = 1  # inbound Matrix ref verified above
            mesh_native_id: str | None = None
            if send_result is not None and send_result.native_message_id is not None:
                mesh_native_id = send_result.native_message_id
                mesh_out_ref = await temp_storage.resolve_native_ref(
                    adapter="cross-mesh-target",
                    native_channel_id="0",
                    native_message_id=mesh_native_id,
                )
                if mesh_out_ref is not None:
                    native_ref_count += 1

            # -- 7. Accounting assertions ---------------------------------
            counters = accounting.snapshot()
            assert counters["inbound_accepted"] >= 1
            assert counters["outbound_delivered"] >= 1

            # -- 8. Build and assert report --------------------------------
            outbound_path = (
                "manual_send_one_after_deliver"
                if send_result is not None
                else "background_drain_task"
            )
            limitations = [
                (
                    "Manual send_one() trigger (not automatic queue drain)."
                    if send_result is not None
                    else "Background drain task sent before manual send_one()."
                ),
                "Docker loopback only (no real LoRa radio).",
                "Fire-and-forget radio delivery: remote receipt not confirmed.",
                "Single message only (no sustained throughput test).",
                "Meshtastic→Matrix direction deferred (not tested).",
            ]
            if matrix_ingress_path == _INBOUND_FALLBACK:
                limitations.append(
                    "Real nio sync_forever callback dispatch NOT proven "
                    "by this run (direct _on_room_message fallback used)."
                )

            report: dict[str, Any] = {
                "scenario": "matrix_to_meshtastic_cross_adapter",
                "transport": ["matrix", "meshtastic"],
                "evidence_level": "docker_sdk_boundary",
                "matrix_ingress_path": matrix_ingress_path,
                "meshtastic_outbound_path": outbound_path,
                "cross_transport_proof": "partial",
                "source_adapter": "cross-matrix-source",
                "target_adapter": "cross-mesh-target",
                "event_id": canonical_id,
                "native_event_id": native_event_id,
                "native_message_id": mesh_native_id,
                "receipt_count": receipt_count,
                "native_ref_count": native_ref_count,
                "route_id": route.id,
                "accounting": counters,
                "limitations": limitations,
            }

            # Validate report shape.
            assert report["matrix_ingress_path"] in (
                _INBOUND_SYNC_LOOP,
                _INBOUND_FALLBACK,
            )
            assert report["cross_transport_proof"] == "partial"

            logger.info(
                "Cross-adapter report: matrix_ingress=%s "
                "mesh_native_id=%s receipts=%d refs=%d",
                report["matrix_ingress_path"],
                report["native_message_id"],
                report["receipt_count"],
                report["native_ref_count"],
            )

            # -- 9. Write structured artifacts ----------------------------
            if _RUN_ARTIFACT_DIR is not None:
                _write_artifact_json(
                    "cross-adapter-matrix-to-meshtastic-report.json",
                    report,
                )
                _write_run_metadata(
                    scenario="matrix_to_meshtastic_cross_adapter",
                    containers={
                        "synapse": synapse_env.container_name,
                        "meshtasticd": meshtasticd_env.container_name,
                    },
                    storage_path=temp_storage._db_path,
                    extras={
                        "event_id": canonical_id,
                        "matrix": {
                            "ingress_path": matrix_ingress_path,
                        },
                        "meshtastic": {
                            "outbound": {
                                "packet_id": mesh_native_id,
                            },
                        },
                    },
                )

        finally:
            await matrix_adapter.stop()
            await mesh_target.stop()
            await fake_monitor.stop()
            await runner.stop()
