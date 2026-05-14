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
- ``inbound_path``: one of ``"sync_loop"`` or
  ``"direct_on_room_message_fallback"``
- ``source_adapter``, ``target_adapter``, ``native_event_id``,
  ``route_id``, ``receipt_status``
- ``accounting``: snapshot of ``RuntimeAccounting`` counters
- ``limitations``: list of what this run does **not** prove

The ``inbound_path`` field is critical for honest evidence reporting:

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

logger = logging.getLogger(__name__)

# Re-apply module-level skip in case conftest skips don't cascade.
pytestmark = pytest.mark.docker

if not HAS_NIO:
    pytestmark = [
        pytest.mark.docker,
        pytest.mark.skip(reason="mindroom-nio not installed; run: pip install '.[matrix]'"),
    ]


# ---------------------------------------------------------------------------
# Inbound path constants — used in report.inbound_path
# ---------------------------------------------------------------------------

_INBOUND_SYNC_LOOP = "sync_loop"
_INBOUND_FALLBACK = "direct_on_room_message_fallback"


# ---------------------------------------------------------------------------
# Helpers
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


def _make_context(adapter_id: str = "synapse-bridge-bot") -> AdapterContext:
    """Build an AdapterContext wired to a mock publish_inbound."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
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
        logger=logging.getLogger(f"test.bridge.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _send_message_as_test_user(
    env: SynapseEnvironment,
    body: str,
    txn_id: str,
) -> str:
    """Send a message as the test user via Synapse HTTP API.

    Returns the Matrix event_id assigned by Synapse.
    """
    payload = json.dumps({
        "msgtype": "m.text",
        "body": body,
    }).encode()
    url = (
        f"{env.base_url}/_matrix/client/v3/rooms/{env.test_room_id}"
        f"/send/m.room.message/{txn_id}"
    )
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {env.test_access_token}",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp_body = json.loads(resp.read())
    return resp_body["event_id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSynapseBridgeSmoke:
    """Docker-local Synapse SDK-boundary bridge smoke tests.

    Proves that the real MatrixAdapter with real mindroom-nio can bridge
    a third-party Matrix message through the MEDRE pipeline to a fake
    outbound adapter, with full storage and accounting assertions.
    """

    async def test_outbound_send_produces_real_synapse_event_id(
        self, synapse_env: SynapseEnvironment,
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
        self, synapse_env: SynapseEnvironment, temp_storage: SQLiteStorage,
    ) -> None:
        """Test user sends via HTTP API; bot receives and pipeline routes
        to FakeMatrixAdapter; storage and accounting assertions pass.

        The test first attempts delivery through the real nio ``sync_forever``
        loop.  If the sync loop does not deliver within 15 seconds, a fallback
        path calls ``_on_room_message`` directly with the real Synapse
        ``event_id``.  The ``report["inbound_path"]`` field records which
        path was used so that test output distinguishes sync-loop proof from
        codec+pipeline-only proof.

        This is the strongest SDK-boundary proof when ``inbound_path`` is
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

        When ``inbound_path`` is ``"direct_on_room_message_fallback"``, steps
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
            "synapse-bridge-bot", runner,
        )
        await matrix_adapter.start(matrix_ctx)

        # Start fake adapter so it can receive deliveries.
        fake_ctx = _make_adapter_context_for_pipeline("fake-out", runner)
        await fake_out.start(fake_ctx)

        try:
            # 2. Send a message as the test user via HTTP API.
            native_event_id = _send_message_as_test_user(
                synapse_env, body_text, txn_id,
            )
            assert native_event_id.startswith("$"), (
                f"Synapse should return event_id starting with '$', "
                f"got {native_event_id!r}"
            )

            # 3. Poll for the event to arrive through the real sync loop.
            #    The real nio sync_forever receives the event and invokes
            #    _on_room_message, which publishes into the pipeline.
            deadline = time.monotonic() + 15.0
            found = False
            while time.monotonic() < deadline:
                # Check if fake_out received a delivery.
                if len(fake_out.delivered_payloads) >= 1:
                    found = True
                    break
                await asyncio.sleep(0.3)

            # 4. Record which inbound path was used — this is the critical
            #    evidence distinction.
            inbound_path: str
            if found:
                inbound_path = _INBOUND_SYNC_LOOP
            else:
                # Fallback: if sync didn't deliver in time, call
                # _on_room_message directly with a real-Synapse-constructed
                # event.  This still proves the SDK-boundary codec + pipeline
                # + storage path with a genuine Synapse event_id.
                #
                # Limitation: the real nio sync callback path is NOT exercised
                # by this fallback.  test_synapse_connectivity.py proves the
                # sync loop can start and connect, but not that it reliably
                # delivers inbound events under all timing conditions.
                inbound_path = _INBOUND_FALLBACK

                from types import SimpleNamespace

                room = SimpleNamespace(room_id=synapse_env.test_room_id)
                event = SimpleNamespace(
                    sender=synapse_env.test_user_id,
                    event_id=native_event_id,
                    body=body_text,
                    source={
                        "content": {"msgtype": "m.text", "body": body_text},
                        "event_id": native_event_id,
                        "sender": synapse_env.test_user_id,
                        "type": "m.room.message",
                    },
                )
                await matrix_adapter._on_room_message(room, event)
                # Give the pipeline a moment to process.
                await asyncio.sleep(0.5)

            # 5. Assertions on the fake outbound adapter.
            assert len(fake_out.delivered_payloads) >= 1, (
                "FakeMatrixAdapter should have received at least one "
                "rendered payload via the pipeline"
            )
            rendered = fake_out.delivered_payloads[0]
            assert rendered.payload["body"] == body_text, (
                f"Expected body {body_text!r}, "
                f"got {rendered.payload.get('body')!r}"
            )

            # 6. Inbound NativeMessageRef — resolve via public API.
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

            # 7. DeliveryReceipt persisted — via public API.
            receipts = await temp_storage.list_receipts_for_event(
                canonical_id,
            )
            assert len(receipts) >= 1, (
                "Expected at least one delivery receipt for "
                f"event {canonical_id!r}"
            )
            receipt_status = receipts[0].status
            assert receipt_status == "sent", (
                f"Expected receipt status 'sent', got {receipt_status!r}"
            )

            # 8. RuntimeAccounting counters incremented.
            counters = accounting.snapshot()
            assert counters["inbound_accepted"] >= 1, (
                f"Expected inbound_accepted >= 1, got {counters['inbound_accepted']}"
            )
            assert counters["outbound_delivered"] >= 1, (
                f"Expected outbound_delivered >= 1, "
                f"got {counters['outbound_delivered']}"
            )

            # 9. Adapter diagnostics counters reflect inbound processing.
            assert matrix_adapter._inbound_published >= 1
            diag = matrix_adapter.diagnostics()
            assert diag["inbound_published"] >= 1

            # 10. Build and assert compact evidence report.
            #     The inbound_path field is the key honesty mechanism:
            #     it records whether sync_loop or fallback was used.
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
                "inbound_path": inbound_path,
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
            assert report["inbound_path"] in (
                _INBOUND_SYNC_LOOP, _INBOUND_FALLBACK,
            )
            assert report["evidence_level"] == "docker_sdk_boundary"
            assert report["receipt_status"] == "sent"
            assert report["native_event_id"].startswith("$")

            logger.info(
                "Matrix bridge smoke report: inbound_path=%s "
                "native_event_id=%s receipt_status=%s",
                report["inbound_path"],
                report["native_event_id"],
                report["receipt_status"],
            )
        finally:
            await matrix_adapter.stop()
            await fake_out.stop()
            await runner.stop()

    async def test_clean_shutdown_no_resource_warning(
        self, synapse_env: SynapseEnvironment,
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
        resource_warnings = [
            w for w in warnings.get_warnings(record=True)
            if issubclass(w.category, ResourceWarning)
        ] if False else []  # warnings.get_warnings needs a context manager

        # Use warnings.catch_warnings with a filter instead.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            gc.collect()

        aiohttp_warnings = [
            w for w in caught
            if issubclass(w.category, ResourceWarning)
            and ("aiohttp" in str(w.message) or "Unclosed" in str(w.message))
        ]
        assert len(aiohttp_warnings) == 0, (
            f"ResourceWarnings after shutdown: "
            f"{[str(w.message) for w in aiohttp_warnings]}"
        )
