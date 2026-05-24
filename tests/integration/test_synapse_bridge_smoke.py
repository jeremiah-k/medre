"""Synapse SDK-boundary bridge smoke tests for the MEDRE Matrix adapter.

These tests start a real Synapse homeserver in Docker, register two users
(bot + test user), create a test room, and exercise the complete MEDRE
bridge pipeline against it:

1. Real ``mindroom-nio`` ``AsyncClient`` connects to local Synapse.
2. Test user sends a message via Synapse HTTP API.
3. Bot's real nio ``sync_forever`` loop receives the event.
4. ``MatrixAdapter._on_room_message`` decodes it via ``MatrixCodec``.
5. ``PipelineRunner`` routes the canonical event to a ``FakeMatrixAdapter``.
6. ``DeliveryReceipt`` and ``NativeMessageRef`` are persisted in storage.
7. ``RuntimeAccounting`` counters increment.
8. Clean shutdown without ``aiohttp`` ``ResourceWarning``.

This is **not** a live-network proof — it exercises the real SDK boundary
against a Docker-local Synapse only.  It is tagged ``pytest.mark.docker``
and excluded from default runs.

Running locally::

    pip install -e ".[matrix]"
    pytest tests/integration/test_synapse_bridge_smoke.py -m docker -v

Evidence classification
-----------------------
Each inbound test produces a compact ``report`` dict containing at least:

- ``transport``: ``"matrix"``
- ``evidence_level``: ``"docker_sdk_boundary"``
- ``ingress_path``: one of ``"sync_loop"`` or
  ``"direct_on_room_message_fallback"``
- ``source_adapter``, ``target_adapter``, ``native_event_id``,
  ``route_id``, ``receipt_status``
- ``accounting``: snapshot of ``RuntimeAccounting`` counters
- ``limitations``: list of what this run does **not** prove

The ``ingress_path`` field is critical for honest evidence reporting:

- ``"sync_loop"`` — the real nio ``sync_forever`` callback fired and
  dispatched the event through the normal SDK path.  This proves the
  full SDK-boundary inbound chain.
- ``"direct_on_room_message_fallback"`` — the sync loop did not deliver
  within 15 seconds, so the test called ``_on_room_message`` directly
  with a real Synapse ``event_id``.  This proves codec + pipeline +
  storage but **does not** prove the real nio sync callback dispatch.
  ``test_synapse_connectivity.py`` proves the sync loop can start and
  connect, but not that it reliably delivers inbound events under all
  timing conditions.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import time
import urllib.request
import warnings
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_NIO
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
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

from .conftest import SynapseEnvironment
from .synapse_helpers import (
    INBOUND_FALLBACK as _INBOUND_FALLBACK,
    INBOUND_SYNC_LOOP as _INBOUND_SYNC_LOOP,
    IngressResult,
    capture_sync_health as _capture_sync_health,
    classify_fallback_reason as _classify_fallback_reason,
    make_context as _make_context,
    send_message_as_test_user as _send_message_as_test_user,
    wait_for_sync_or_fallback as _wait_for_sync_or_fallback,
)

logger = logging.getLogger(__name__)

# Re-apply module-level skip in case conftest skips don't cascade.
pytestmark = pytest.mark.docker

if not HAS_NIO:
    pytestmark = [
        pytest.mark.docker,
        pytest.mark.skip(
            reason="mindroom-nio not installed; run: pip install '.[matrix]'"
        ),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _make_matrix_config(env: SynapseEnvironment) -> MatrixConfig:
    """Build a MatrixConfig pointing at the Docker Synapse."""
    return MatrixConfig(
        adapter_id="synapse-bridge-bot",
        homeserver=env.base_url,
        user_id=env.bot_user_id,
        access_token=env.bot_access_token,
        room_allowlist={env.test_room_id},
        encryption_mode="plaintext",
    ).validate()


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


def _make_adapter_context_for_pipeline(
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
        logger=logging.getLogger(f"test.bridge.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


class TestSynapseBridgeSmoke:
    """Docker-local Synapse SDK-boundary bridge smoke tests.

    Proves that the real MatrixAdapter with real mindroom-nio can bridge
    a third-party Matrix message through the MEDRE pipeline to a fake
    outbound adapter, with full storage and accounting assertions.
    """

    async def test_outbound_send_produces_real_synapse_event_id(
        self,
        synapse_env: SynapseEnvironment,
    ) -> None:
        """Bot sends a message through the real SDK to Docker Synapse.

        Validates:
        - ``room_send`` succeeds against the real homeserver.
        - ``native_message_id`` is a real Matrix event_id (``$...``).
        - ``native_channel_id`` matches the target room.
        """
        config = _make_matrix_config(synapse_env)
        ctx = _make_context()
        adapter = MatrixAdapter(config)

        await adapter.start(ctx)
        try:
            from medre.core.rendering.renderer import RenderingResult

            ts = int(time.time())
            result = RenderingResult(
                event_id=f"bridge-out-{ts}",
                target_adapter="synapse-bridge-bot",
                target_channel=synapse_env.test_room_id,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE bridge outbound smoke (ts={ts})",
                },
                metadata={"renderer": "matrix", "test": "bridge-smoke"},
            )
            delivery = await adapter.deliver(result)
            assert delivery is not None, "deliver() returned None"
            assert delivery.native_message_id is not None
            assert delivery.native_message_id.startswith("$"), (
                f"Expected Matrix event_id starting with '$', "
                f"got {delivery.native_message_id!r}"
            )
            assert delivery.native_channel_id == synapse_env.test_room_id
        finally:
            await adapter.stop()

    async def test_inbound_routes_to_fake_adapter(
        self,
        synapse_env: SynapseEnvironment,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Test user sends via HTTP API; bot receives and pipeline routes
        to FakeMatrixAdapter; storage and accounting assertions pass.

        The test first attempts delivery through the real nio ``sync_forever``
        loop.  If the sync loop does not deliver within 15 seconds, a fallback
        path calls ``_on_room_message`` directly with the real Synapse
        ``event_id``.  The ``report["ingress_path"]`` field records which
        path was used so that test output distinguishes sync-loop proof from
        codec+pipeline-only proof.

        This is the strongest SDK-boundary proof when ``ingress_path`` is
        ``"sync_loop"``:

        1. Real nio AsyncClient + sync_forever running against Docker Synapse.
        2. Test user sends message via Synapse HTTP API.
        3. Bot's sync loop picks up the event → real nio callback fires.
        4. MatrixCodec.decode() produces a CanonicalEvent.
        5. PipelineRunner routes through MatrixRenderer to FakeMatrixAdapter.
        6. DeliveryReceipt persisted with ``status="sent"``.
        7. Inbound NativeMessageRef maps (room_id, event_id) → canonical ID.
        8. RuntimeAccounting increments ``inbound_accepted`` and
           ``outbound_delivered``.
        9. Adapter diagnostics counters reflect the inbound event.

        When ``ingress_path`` is ``"direct_on_room_message_fallback"``, steps
        1 and 3 are skipped — the test proves codec + pipeline + storage but
        does NOT prove the real nio sync callback dispatch.
        ``test_synapse_connectivity.py`` proves the sync loop can start and
        connect, but not that it reliably delivers inbound events under all
        timing conditions.
        """
        ts = int(time.time())
        body_text = f"MEDRE bridge inbound smoke (ts={ts})"
        txn_id = f"bridge-inbound-txn-{ts}"

        # 1. Set up pipeline with real MatrixAdapter as source and
        #    FakeMatrixAdapter as target.
        accounting = RuntimeAccounting()
        fake_out = FakeMatrixAdapter("fake-out", channel="ch-0")

        route = Route(
            id="bridge-sdk-route",
            source=RouteSource(
                adapter="synapse-bridge-bot",
                event_kinds=("message.created",),
                channel=synapse_env.test_room_id,
            ),
            targets=[RouteTarget(adapter="fake-out")],
        )
        router = Router(routes=[route])

        config = _make_matrix_config(synapse_env)
        matrix_adapter = MatrixAdapter(config)

        pipeline_config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={"fake-out": fake_out, "synapse-bridge-bot": matrix_adapter},
            runtime_accounting=accounting,
        )
        runner = PipelineRunner(pipeline_config)
        await runner.start()

        # Wire matrix adapter's publish_inbound to the pipeline ingress.
        matrix_ctx = _make_adapter_context_for_pipeline(
            "synapse-bridge-bot",
            runner,
        )
        await matrix_adapter.start(matrix_ctx)

        # Start fake adapter so it can receive deliveries.
        fake_ctx = _make_adapter_context_for_pipeline("fake-out", runner)
        await fake_out.start(fake_ctx)

        try:
            # 2. Send a message and wait for ingress — records which path
            #    was used (sync_loop or fallback).
            ingress = await _wait_for_sync_or_fallback(
                synapse_env=synapse_env,
                matrix_adapter=matrix_adapter,
                fake_out=fake_out,
                body_text=body_text,
                txn_id=txn_id,
            )
            native_event_id = ingress.native_event_id
            inbound_path = ingress.ingress_path

            # 3. If fallback was used, log a warning — sync_loop is the
            #    preferred ingress path for full SDK-boundary proof.
            if inbound_path == _INBOUND_FALLBACK:
                logger.warning(
                    "Matrix bridge smoke: sync loop did not deliver within "
                    "timeout; used direct _on_room_message fallback. "
                    "reason=%s native_event_id=%s",
                    ingress.fallback_reason,
                    native_event_id,
                )

            # 4. Assertions on the fake outbound adapter.
            assert len(fake_out.delivered_payloads) >= 1, (
                "FakeMatrixAdapter should have received at least one "
                "rendered payload via the pipeline"
            )
            rendered = next(
                (
                    p
                    for p in fake_out.delivered_payloads
                    if p.payload.get("body") == body_text
                ),
                None,
            )
            assert rendered is not None, (
                f"Expected a rendered payload with body {body_text!r}, "
                f"got bodies: {[p.payload.get('body') for p in fake_out.delivered_payloads]}"
            )

            # 5. Inbound NativeMessageRef — resolve via public API.
            #    Maps the real Synapse event_id to the canonical event_id.
            canonical_id = await temp_storage.resolve_native_ref(
                adapter="synapse-bridge-bot",
                native_channel_id=synapse_env.test_room_id,
                native_message_id=native_event_id,
            )
            assert canonical_id is not None, (
                f"Expected inbound native ref for event {native_event_id!r} "
                f"in room {synapse_env.test_room_id!r}"
            )

            # 6. DeliveryReceipt persisted — via public API.
            receipts = await temp_storage.list_receipts_for_event(
                canonical_id,
            )
            assert len(receipts) >= 1, (
                "Expected at least one delivery receipt for " f"event {canonical_id!r}"
            )
            receipt_status = receipts[0].status
            assert (
                receipt_status == "sent"
            ), f"Expected receipt status 'sent', got {receipt_status!r}"

            # 7. RuntimeAccounting counters incremented.
            counters = accounting.snapshot()
            assert (
                counters["inbound_accepted"] >= 1
            ), f"Expected inbound_accepted >= 1, got {counters['inbound_accepted']}"
            assert counters["outbound_delivered"] >= 1, (
                f"Expected outbound_delivered >= 1, "
                f"got {counters['outbound_delivered']}"
            )

            # 8. Adapter diagnostics counters reflect inbound processing.
            assert matrix_adapter._inbound_published >= 1
            diag = matrix_adapter.diagnostics()
            assert diag["inbound_published"] >= 1

            # 9. Build and assert compact evidence report.
            #    The ingress_path field is the key honesty mechanism:
            #    it records whether sync_loop or fallback was used.
            limitations = [
                "Not a live-network proof (Docker loopback only).",
                "Single message only (not a throughput test).",
            ]
            if inbound_path == _INBOUND_FALLBACK:
                limitations.append(
                    "Real nio sync_forever callback dispatch NOT proven "
                    "by this run (direct _on_room_message fallback used)."
                )

            report: dict[str, Any] = {
                "transport": "matrix",
                "evidence_level": "docker_sdk_boundary",
                "ingress_path": inbound_path,
                "fallback_reason": ingress.fallback_reason,
                "source_adapter": "synapse-bridge-bot",
                "target_adapter": "fake-out",
                "native_event_id": native_event_id,
                "route_id": route.id,
                "receipt_status": receipt_status,
                "accounting": counters,
                "diagnostics": diag,
                "limitations": limitations,
            }
            # Assert report shape is well-formed.
            assert report["ingress_path"] in (
                _INBOUND_SYNC_LOOP,
                _INBOUND_FALLBACK,
            )
            assert report["evidence_level"] == "docker_sdk_boundary"
            assert report["receipt_status"] == "sent"
            assert report["native_event_id"].startswith("$")

            logger.info(
                "Matrix bridge smoke report: ingress_path=%s "
                "native_event_id=%s receipt_status=%s",
                report["ingress_path"],
                report["native_event_id"],
                report["receipt_status"],
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
        self,
        synapse_env: SynapseEnvironment,
        temp_storage: SQLiteStorage,
    ) -> None:
        """FAILS (xfail) if fallback is used — proves strict sync goal.

        This test runs the same pipeline as
        ``test_sync_with_fallback`` but asserts that the ingress path
        MUST be ``"sync_loop"``.  It is marked ``xfail(strict=False)``
        so the suite stays green while the sync-loop reliability work
        is in progress.  When this test starts passing, it proves that
        the real nio ``sync_forever`` callback delivers inbound events
        reliably within the timeout window.

        When this test passes consistently, remove the ``xfail`` marker
        and promote it to a required test.
        """
        ts = int(time.time())
        body_text = f"MEDRE strict sync test (ts={ts})"
        txn_id = f"strict-sync-txn-{ts}"

        accounting = RuntimeAccounting()
        fake_out = FakeMatrixAdapter("fake-out-strict", channel="ch-0")

        route = Route(
            id="strict-sync-route",
            source=RouteSource(
                adapter="synapse-bridge-bot",
                event_kinds=("message.created",),
                channel=synapse_env.test_room_id,
            ),
            targets=[RouteTarget(adapter="fake-out-strict")],
        )
        router = Router(routes=[route])

        config = _make_matrix_config(synapse_env)
        matrix_adapter = MatrixAdapter(config)

        pipeline_config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fake-out-strict": fake_out,
                "synapse-bridge-bot": matrix_adapter,
            },
            runtime_accounting=accounting,
        )
        runner = PipelineRunner(pipeline_config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline(
            "synapse-bridge-bot",
            runner,
        )
        await matrix_adapter.start(matrix_ctx)

        fake_ctx = _make_adapter_context_for_pipeline("fake-out-strict", runner)
        await fake_out.start(fake_ctx)

        try:
            ingress = await _wait_for_sync_or_fallback(
                synapse_env=synapse_env,
                matrix_adapter=matrix_adapter,
                fake_out=fake_out,
                body_text=body_text,
                txn_id=txn_id,
            )

            # STRICT assertion: sync_loop MUST deliver.
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
        self,
        synapse_env: SynapseEnvironment,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Passes regardless of ingress path; reports which path was used.

        This test always passes but explicitly logs the ingress path and
        fallback reason so CI output shows exactly what was proven:

        - ``ingress_path="sync_loop"``: full SDK-boundary proof.
        - ``ingress_path="direct_on_room_message_fallback"``: codec +
          pipeline + storage proven; sync callback dispatch NOT proven.

        The ``fallback_reason`` field explains WHY fallback was needed:
        ``"sync_not_running"``, ``"sync_error"``, or ``"sync_timeout"``.
        """
        ts = int(time.time())
        body_text = f"MEDRE fallback-tolerant test (ts={ts})"
        txn_id = f"fallback-txn-{ts}"

        accounting = RuntimeAccounting()
        fake_out = FakeMatrixAdapter("fake-out-fb", channel="ch-0")

        route = Route(
            id="fallback-route",
            source=RouteSource(
                adapter="synapse-bridge-bot",
                event_kinds=("message.created",),
                channel=synapse_env.test_room_id,
            ),
            targets=[RouteTarget(adapter="fake-out-fb")],
        )
        router = Router(routes=[route])

        config = _make_matrix_config(synapse_env)
        matrix_adapter = MatrixAdapter(config)

        pipeline_config = _make_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fake-out-fb": fake_out,
                "synapse-bridge-bot": matrix_adapter,
            },
            runtime_accounting=accounting,
        )
        runner = PipelineRunner(pipeline_config)
        await runner.start()

        matrix_ctx = _make_adapter_context_for_pipeline(
            "synapse-bridge-bot",
            runner,
        )
        await matrix_adapter.start(matrix_ctx)

        fake_ctx = _make_adapter_context_for_pipeline("fake-out-fb", runner)
        await fake_out.start(fake_ctx)

        try:
            ingress = await _wait_for_sync_or_fallback(
                synapse_env=synapse_env,
                matrix_adapter=matrix_adapter,
                fake_out=fake_out,
                body_text=body_text,
                txn_id=txn_id,
            )

            # Pipeline + codec + storage always works regardless of path.
            assert len(fake_out.delivered_payloads) >= 1, (
                "FakeMatrixAdapter should have received at least one "
                "rendered payload"
            )
            rendered = next(
                (
                    p
                    for p in fake_out.delivered_payloads
                    if p.payload.get("body") == body_text
                ),
                None,
            )
            assert rendered is not None, (
                f"Expected a rendered payload with body {body_text!r}, "
                f"got bodies: {[p.payload.get('body') for p in fake_out.delivered_payloads]}"
            )

            canonical_id = await temp_storage.resolve_native_ref(
                adapter="synapse-bridge-bot",
                native_channel_id=synapse_env.test_room_id,
                native_message_id=ingress.native_event_id,
            )
            assert canonical_id is not None

            # Explicit report: always log which path was used.
            logger.info(
                "Fallback-tolerant test: ingress_path=%s "
                "fallback_reason=%s native_event_id=%s",
                ingress.ingress_path,
                ingress.fallback_reason,
                ingress.native_event_id,
            )

            # Validate ingress_path value.
            assert ingress.ingress_path in (
                _INBOUND_SYNC_LOOP,
                _INBOUND_FALLBACK,
            )

            # If fallback, validate reason is one of the known causes.
            if ingress.ingress_path == _INBOUND_FALLBACK:
                assert ingress.fallback_reason in (
                    "sync_not_running",
                    "sync_error",
                    "sync_timeout",
                ), f"Unexpected fallback_reason: {ingress.fallback_reason!r}"
        finally:
            await matrix_adapter.stop()
            await fake_out.stop()
            await runner.stop()

    async def test_clean_shutdown_no_resource_warning(
        self,
        synapse_env: SynapseEnvironment,
    ) -> None:
        """Adapter starts against real Synapse, sends a message, stops, and
        no ``ResourceWarning`` is raised during garbage collection.

        Validates that ``MatrixSession.stop()`` properly drains the
        ``aiohttp`` ``ClientSession`` via the ``await asyncio.sleep(0)``
        yield, preventing ``Unclosed client session`` warnings.
        """
        config = _make_matrix_config(synapse_env)
        ctx = _make_context()
        adapter = MatrixAdapter(config)

        await adapter.start(ctx)
        try:
            from medre.core.rendering.renderer import RenderingResult

            ts = int(time.time())
            result = RenderingResult(
                event_id=f"bridge-shutdown-{ts}",
                target_adapter="synapse-bridge-bot",
                target_channel=synapse_env.test_room_id,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE shutdown test (ts={ts})",
                },
                metadata={"renderer": "matrix", "test": "shutdown"},
            )
            delivery = await adapter.deliver(result)
            assert delivery is not None
        finally:
            await adapter.stop()

        # Force garbage collection and check for ResourceWarnings.
        gc.collect()
        (
            [
                w
                for w in warnings.get_warnings(record=True)
                if issubclass(w.category, ResourceWarning)
            ]
            if False
            else []
        )  # warnings.get_warnings needs a context manager

        # Use warnings.catch_warnings with a filter instead.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            gc.collect()

        aiohttp_warnings = [
            w
            for w in caught
            if issubclass(w.category, ResourceWarning)
            and ("aiohttp" in str(w.message) or "Unclosed" in str(w.message))
        ]
        assert len(aiohttp_warnings) == 0, (
            f"ResourceWarnings after shutdown: "
            f"{[str(w.message) for w in aiohttp_warnings]}"
        )
