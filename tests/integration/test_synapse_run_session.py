"""Docker-gated run-session integration test for the MEDRE Matrix bridge.

This test exercises the real MEDRE runtime (adapters + pipeline + storage)
against a Docker-local Synapse homeserver.  It mirrors the shape of
``run_bridge_session`` reports but uses the real Matrix adapter sync path
instead of fake adapter injection.

The test is tagged ``pytest.mark.docker`` and excluded from default runs.

Running locally::

    pip install -e ".[matrix]"
    pytest tests/integration/test_synapse_run_session.py -m docker -v

Evidence classification
-----------------------
This test produces a compact ``report`` dict matching the run_session report
shape:

- ``status``: ``"passed"`` or ``"failed"``
- ``event_id``: canonical event ID persisted in storage
- ``receipts``: list of delivery receipt summaries (status, target_adapter, route_id)
- ``native_refs``: list of native message references
- ``ingress_path``: ``"sync_loop"`` or ``"direct_on_room_message_fallback"``
- ``limitations``: list of what this run does **not** prove

The ``ingress_path`` field tracks whether inbound events arrived via the
real nio sync_forever callback or the direct _on_room_message fallback.
Only ``"sync_loop"`` proves full Matrix adapter ingress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from medre.adapters.base import AdapterContext
from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_NIO
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend

from .conftest import SynapseEnvironment
from .test_synapse_bridge_smoke import (
    IngressResult,
    _INBOUND_FALLBACK,
    _INBOUND_SYNC_LOOP,
    _wait_for_sync_or_fallback,
    _classify_fallback_reason,
)

logger = logging.getLogger(__name__)

# Re-apply module-level skip in case conftest skips don't cascade.
pytestmark = pytest.mark.docker

if not HAS_NIO:
    pytestmark = [
        pytest.mark.docker,
        pytest.mark.skip(reason="mindroom-nio not installed; run: pip install '.[matrix]'"),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_matrix_config(env: SynapseEnvironment) -> MatrixConfig:
    """Build a MatrixConfig pointing at the Docker Synapse."""
    return MatrixConfig(
        adapter_id="synapse-run-session-bot",
        homeserver=env.base_url,
        user_id=env.bot_user_id,
        access_token=env.bot_access_token,
        room_allowlist={env.test_room_id},
        encryption_mode="plaintext",
    ).validate()


def _make_adapter_context_for_pipeline(
    adapter_id: str, runner: PipelineRunner,
) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""
    async def _publish(event: Any) -> None:
        await runner.ingress_handler(event)

    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_publish,
        logger=logging.getLogger(f"test.run_session.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_pipeline_config(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict[str, Any] | None = None,
    event_bus: EventBus | None = None,
    runtime_accounting: RuntimeAccounting | None = None,
) -> PipelineConfig:
    """Build a PipelineConfig with MatrixRenderer registered."""
    rp = RenderingPipeline()
    rp.register(MatrixRenderer(), priority=50)
    rp.register(TextRenderer(), priority=100)

    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=event_bus or EventBus(),
        rendering_pipeline=rp,
        runtime_accounting=runtime_accounting,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSynapseRunSession:
    """Docker-gated run-session test with real Matrix adapter sync path.

    Exercises the full MEDRE pipeline against Docker Synapse:
    start adapters → send Matrix message → ingress through real sync path
    → canonical event persisted → delivery to fake target → receipt status
    = "sent" → final report with ingress_path tracking.
    """

    async def test_run_session_matrix_sync_ingress(
        self, synapse_env: SynapseEnvironment, temp_storage: SQLiteStorage,
    ) -> None:
        """Full run-session: Matrix source (Synapse) -> fake target.

        Proves:
        1. Real MatrixAdapter + nio sync connects to Docker Synapse.
        2. Test user sends message via Synapse HTTP API.
        3. Bot receives via real sync path (or fallback with logging).
        4. Canonical event persisted in storage.
        5. Pipeline routes to FakeMatrixAdapter (fake target).
        6. DeliveryReceipt with status="sent".
        7. NativeMessageRef maps (room_id, event_id) -> canonical ID.
        8. RuntimeAccounting counters increment.
        9. Report shape matches run_session contract.
        10. ingress_path recorded in report output.
        """
        ts = int(time.time())
        body_text = f"MEDRE run-session test (ts={ts})"
        txn_id = f"run-session-txn-{ts}"

        # -- Setup: pipeline with real Matrix source, fake target --
        accounting = RuntimeAccounting()
        fake_out = FakeMatrixAdapter("fake-out-session", channel="ch-0")

        route = Route(
            id="run-session-route",
            source=RouteSource(
                adapter="synapse-run-session-bot",
                event_kinds=("message.created",),
                channel=synapse_env.test_room_id,
            ),
            targets=[RouteTarget(adapter="fake-out-session")],
        )
        router = Router(routes=[route])

        config = _make_matrix_config(synapse_env)
        matrix_adapter = MatrixAdapter(config)

        pipeline_config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fake-out-session": fake_out,
                "synapse-run-session-bot": matrix_adapter,
            },
            runtime_accounting=accounting,
        )
        runner = PipelineRunner(pipeline_config)
        await runner.start()

        # Wire adapters to pipeline ingress.
        matrix_ctx = _make_adapter_context_for_pipeline(
            "synapse-run-session-bot", runner,
        )
        await matrix_adapter.start(matrix_ctx)

        fake_ctx = _make_adapter_context_for_pipeline(
            "fake-out-session", runner,
        )
        await fake_out.start(fake_ctx)

        try:
            # -- Ingress: send message and wait for delivery --
            ingress = await _wait_for_sync_or_fallback(
                synapse_env=synapse_env,
                matrix_adapter=matrix_adapter,
                fake_out=fake_out,
                body_text=body_text,
                txn_id=txn_id,
            )

            if ingress.ingress_path == _INBOUND_FALLBACK:
                logger.warning(
                    "Run session: sync loop did not deliver; "
                    "used direct _on_room_message fallback. "
                    "reason=%s native_event_id=%s",
                    ingress.fallback_reason,
                    ingress.native_event_id,
                )

            # -- Assert: fake target received delivery --
            assert len(fake_out.delivered_payloads) >= 1, (
                "FakeMatrixAdapter should have received at least one "
                "rendered payload via the pipeline"
            )
            rendered = fake_out.delivered_payloads[0]
            assert rendered.payload["body"] == body_text

            # -- Assert: canonical event persisted --
            canonical_id = await temp_storage.resolve_native_ref(
                adapter="synapse-run-session-bot",
                native_channel_id=synapse_env.test_room_id,
                native_message_id=ingress.native_event_id,
            )
            assert canonical_id is not None, (
                f"Expected native ref for event {ingress.native_event_id!r}"
            )

            # -- Assert: delivery receipt with status="sent" --
            receipts = await temp_storage.list_receipts_for_event(
                canonical_id,
            )
            assert len(receipts) >= 1, (
                "Expected at least one delivery receipt"
            )
            receipt_status = receipts[0].status
            assert receipt_status == "sent", (
                f"Expected receipt status 'sent', got {receipt_status!r}"
            )

            # -- Assert: accounting counters --
            counters = accounting.snapshot()
            assert counters["inbound_accepted"] >= 1
            assert counters["outbound_delivered"] >= 1

            # -- Assert: adapter diagnostics --
            assert matrix_adapter._inbound_published >= 1
            diag = matrix_adapter.diagnostics()
            assert diag["inbound_published"] >= 1

            # -- Build final report (matches run_session shape) --
            limitations = [
                "Not a live-network proof (Docker loopback only).",
                "Single message only (not a throughput test).",
                "Outbound target is fake (real cross-transport delivery not proven).",
            ]
            if ingress.ingress_path == _INBOUND_FALLBACK:
                limitations.append(
                    "Real nio sync_forever callback dispatch NOT proven "
                    "(direct _on_room_message fallback used)."
                )

            report: dict[str, Any] = {
                "status": "passed",
                "command": "run_session",
                "evidence_level": "docker_sdk_boundary",
                "ingress_path": ingress.ingress_path,
                "fallback_reason": ingress.fallback_reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_id": canonical_id,
                "source_adapter": "synapse-run-session-bot",
                "target_adapters": ["fake-out-session"],
                "route_id": route.id,
                "receipts": [
                    {
                        "receipt_id": r.receipt_id,
                        "target_adapter": r.target_adapter,
                        "status": r.status,
                        "route_id": r.route_id,
                    }
                    for r in receipts
                ],
                "native_refs": [
                    {
                        "adapter": "synapse-run-session-bot",
                        "native_channel_id": synapse_env.test_room_id,
                        "native_message_id": ingress.native_event_id,
                    },
                ],
                "accounting": {
                    "inbound": counters.get("inbound_accepted", 0),
                    "outbound_delivered": counters.get("outbound_delivered", 0),
                    "outbound_failed": counters.get("outbound_failed", 0),
                    "loop_prevented": counters.get("loop_prevented", 0),
                    "capacity_rejections": counters.get("capacity_rejections", 0),
                },
                "diagnostics": diag,
                "limitations": limitations,
            }

            # Assert report shape.
            assert report["status"] == "passed"
            assert report["ingress_path"] in (
                _INBOUND_SYNC_LOOP, _INBOUND_FALLBACK,
            )
            assert report["event_id"] is not None
            assert len(report["receipts"]) >= 1
            assert report["receipts"][0]["status"] == "sent"
            assert len(report["native_refs"]) >= 1
            assert report["native_refs"][0]["native_message_id"].startswith("$")

            logger.info(
                "Run session report: status=%s ingress_path=%s "
                "fallback_reason=%s event_id=%s receipts=%d",
                report["status"],
                report["ingress_path"],
                report["fallback_reason"],
                report["event_id"],
                len(report["receipts"]),
            )
        finally:
            await matrix_adapter.stop()
            await fake_out.stop()
            await runner.stop()

    # ------------------------------------------------------------------
    # Explicit ingress-path tests
    # ------------------------------------------------------------------

    @pytest.mark.xfail(
        reason="Strict sync_loop ingress is not yet reliable; "
               "xfail proves the test exists and tracks progress",
        strict=False,
    )
    async def test_strict_sync_loop(
        self, synapse_env: SynapseEnvironment, temp_storage: SQLiteStorage,
    ) -> None:
        """FAILS (xfail) if fallback is used — proves strict sync goal.

        Mirrors ``test_synapse_bridge_smoke.test_strict_sync_loop`` but
        exercises the run-session report shape.  When this test passes,
        the sync loop reliably delivers inbound events within the
        timeout window for the run-session code path.
        """
        ts = int(time.time())
        body_text = f"MEDRE strict run-session (ts={ts})"
        txn_id = f"strict-session-txn-{ts}"

        accounting = RuntimeAccounting()
        fake_out = FakeMatrixAdapter("fake-out-strict-sess", channel="ch-0")

        route = Route(
            id="strict-session-route",
            source=RouteSource(
                adapter="synapse-run-session-bot",
                event_kinds=("message.created",),
                channel=synapse_env.test_room_id,
            ),
            targets=[RouteTarget(adapter="fake-out-strict-sess")],
        )
        router = Router(routes=[route])

        config = _make_matrix_config(synapse_env)
        matrix_adapter = MatrixAdapter(config)

        pipeline_config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fake-out-strict-sess": fake_out,
                "synapse-run-session-bot": matrix_adapter,
            },
            runtime_accounting=accounting,
        )
        runner = PipelineRunner(pipeline_config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline(
            "synapse-run-session-bot", runner,
        )
        await matrix_adapter.start(matrix_ctx)

        fake_ctx = _make_adapter_context_for_pipeline(
            "fake-out-strict-sess", runner,
        )
        await fake_out.start(fake_ctx)

        try:
            ingress = await _wait_for_sync_or_fallback(
                synapse_env=synapse_env,
                matrix_adapter=matrix_adapter,
                fake_out=fake_out,
                body_text=body_text,
                txn_id=txn_id,
            )

            assert ingress.ingress_path == _INBOUND_SYNC_LOOP, (
                f"Strict sync test requires sync_loop ingress, "
                f"got {ingress.ingress_path!r} "
                f"(reason={ingress.fallback_reason!r})"
            )
        finally:
            await matrix_adapter.stop()
            await fake_out.stop()
            await runner.stop()

    async def test_sync_with_fallback(
        self, synapse_env: SynapseEnvironment, temp_storage: SQLiteStorage,
    ) -> None:
        """Passes regardless of ingress path; reports which path was used.

        Mirrors ``test_synapse_bridge_smoke.test_sync_with_fallback`` for
        the run-session code path.  Always passes but explicitly logs
        ingress_path and fallback_reason.
        """
        ts = int(time.time())
        body_text = f"MEDRE fallback run-session (ts={ts})"
        txn_id = f"fb-session-txn-{ts}"

        accounting = RuntimeAccounting()
        fake_out = FakeMatrixAdapter("fake-out-fb-sess", channel="ch-0")

        route = Route(
            id="fb-session-route",
            source=RouteSource(
                adapter="synapse-run-session-bot",
                event_kinds=("message.created",),
                channel=synapse_env.test_room_id,
            ),
            targets=[RouteTarget(adapter="fake-out-fb-sess")],
        )
        router = Router(routes=[route])

        config = _make_matrix_config(synapse_env)
        matrix_adapter = MatrixAdapter(config)

        pipeline_config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fake-out-fb-sess": fake_out,
                "synapse-run-session-bot": matrix_adapter,
            },
            runtime_accounting=accounting,
        )
        runner = PipelineRunner(pipeline_config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline(
            "synapse-run-session-bot", runner,
        )
        await matrix_adapter.start(matrix_ctx)

        fake_ctx = _make_adapter_context_for_pipeline(
            "fake-out-fb-sess", runner,
        )
        await fake_out.start(fake_ctx)

        try:
            ingress = await _wait_for_sync_or_fallback(
                synapse_env=synapse_env,
                matrix_adapter=matrix_adapter,
                fake_out=fake_out,
                body_text=body_text,
                txn_id=txn_id,
            )

            assert len(fake_out.delivered_payloads) >= 1
            rendered = fake_out.delivered_payloads[0]
            assert rendered.payload["body"] == body_text

            logger.info(
                "Run-session fallback-tolerant: ingress_path=%s "
                "fallback_reason=%s native_event_id=%s",
                ingress.ingress_path,
                ingress.fallback_reason,
                ingress.native_event_id,
            )

            assert ingress.ingress_path in (
                _INBOUND_SYNC_LOOP, _INBOUND_FALLBACK,
            )

            if ingress.ingress_path == _INBOUND_FALLBACK:
                assert ingress.fallback_reason in (
                    "sync_not_running", "sync_error", "sync_timeout",
                ), (
                    f"Unexpected fallback_reason: {ingress.fallback_reason!r}"
                )
        finally:
            await matrix_adapter.stop()
            await fake_out.stop()
            await runner.stop()
