# medre-test-tier: compatibility
"""Docker-gated exact-target native acceptance leg on the current Matrix pin.

This module qualifies the native-acceptance step of the exact-target
property (register rows G01/G02) against a real Synapse homeserver at
the pinned image from ``docker-compose.integration.yaml``.  An
authority-authorized scoped replay send reaches the real provider and
acceptance is observed **Synapse-side**: the test polls the room
timeline through the Matrix Client-Server API with a second client
(the test user), never by reading MEDRE's own receipts back.  MEDRE's
replay outcome and receipt are compared against that independent
observation, not substituted for it.

Three properties are exercised:

1. **Admission produces a real send.**  A scoped replay whose exact
   target names one persisted outbox generation is delivered through
   the real Matrix adapter; the homeserver timeline independently shows
   the sent event under the event id the send produced.
2. **Replaying a completed target stays provider-confirmed.**  A
   completed live delivery replayed under its exact identity reaches
   the homeserver again; the timeline independently confirms the event
   id the replay send reported.  The pinned adapter sends with
   deterministic transaction ids, so the provider's idempotency window
   may collapse the replay into the original event; both that behavior
   and a distinct second event independently confirm acceptance, and
   the observed behavior is recorded.
3. **Refusal stages no send.**  A scoped selection whose identity has
   no persisted outbox generation is refused by the admission gate
   (``exact_target_refused`` evidence) and nothing reaches the
   homeserver for the refused target.

What this tier does NOT prove: recipient receipt on an independent
recipient account (the observation client is the CI test user's own
client session against the same homeserver, not a separate human's
client), encrypted-room Megolm handoff, and sibling-channel behavior
beyond the not-widened monitor target asserted here.  Those remain
later legs of the native evidence campaign (see
``docs/dev/native-evidence-campaign.md``).

Running locally::

    pip install medre-adapter-matrix
    pytest tests/integration/test_synapse_exact_target_acceptance.py -m docker -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import msgspec
import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_NIO
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.engine.pipeline.retry_plan import OutboxDispatchContext
from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.types import ReplayMode, ReplayRequest, ReplayResult
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.events.metadata import RoutingMetadata
from medre.core.observability.exact_target import (
    ExactTargetSelection,
    preview_exact_target,
)
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    stable_delivery_plan_id,
)
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
    StorageBackend,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.async_utils import wait_until

from .conftest import (
    _RUN_ARTIFACT_DIR,
    SynapseEnvironment,
    _write_artifact_json,
    _write_run_metadata,
)

logger = logging.getLogger(__name__)

# Re-apply module-level skip in case conftest skips don't cascade.
pytestmark = pytest.mark.docker

if not HAS_NIO:
    pytestmark = [
        pytest.mark.docker,
        pytest.mark.skip(
            reason="mindroom-nio not installed; run: pip install 'medre-adapter-matrix'"
        ),
    ]

# ---------------------------------------------------------------------------
# Fixed assembly identity — one route, the exact target first.
# ---------------------------------------------------------------------------

_MATRIX_ADAPTER_ID = "synapse-exact-target-bot"
_MONITOR_ADAPTER_ID = "exact-target-monitor"
_SOURCE_ADAPTER_ID = "exact-target-ci-source"
_ROUTE_ID = "exact-target-acceptance-route"

_POLL_TIMEOUT = 20.0
_POLL_INTERVAL = 0.5


def _route_for(room_id: str) -> Route:
    """Build the route: the exact Matrix target first, monitor sibling second.

    The matrix target's positional index is load-bearing: the delivery
    plan id freezes it (``stable_delivery_plan_id`` folds the target
    index), and the staged/replayed identities below must agree.
    """
    return Route(
        id=_ROUTE_ID,
        source=RouteSource(
            adapter=_SOURCE_ADAPTER_ID,
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[
            RouteTarget(adapter=_MATRIX_ADAPTER_ID, channel=room_id),
            RouteTarget(adapter=_MONITOR_ADAPTER_ID),
        ],
    )


def _matrix_plan_id(event_id: str, room_id: str) -> str:
    """The deterministic routed plan id for the exact Matrix target."""
    return stable_delivery_plan_id(
        event_id,
        RouteTarget(adapter=_MATRIX_ADAPTER_ID, channel=room_id),
        route_id=_ROUTE_ID,
        target_index=0,
    )


def _make_event(event_id: str, body: str) -> CanonicalEvent:
    """Build the canonical event whose replay send is qualified."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter=_SOURCE_ADAPTER_ID,
        source_transport_id="ci-source-1",
        source_channel_id="ci-channel-1",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": body},
        metadata=EventMetadata(),
    )


# ---------------------------------------------------------------------------
# Provider-side observation — the independent Matrix client
# ---------------------------------------------------------------------------


def _fetch_room_messages(env: SynapseEnvironment, room_id: str) -> list[dict[str, Any]]:
    """Read the room timeline from Synapse as the test user (second client).

    This never touches MEDRE storage or the adapter's own session: the
    request authenticates with the CI test user's token against the
    homeserver's Client-Server API, so what comes back is the
    provider's own account of the room.
    """
    quoted_room = urllib.parse.quote(room_id, safe="")
    url = (
        f"{env.base_url}/_matrix/client/v3/rooms/{quoted_room}"
        "/messages?dir=b&limit=100"
    )
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {env.test_access_token}"},
        method="GET",
    )
    with urllib.request.urlopen(
        req, timeout=10
    ) as resp:  # nosec: localhost test container
        body = json.loads(resp.read())
    return list(body.get("chunk", []))


def _observed_messages_for_body(
    env: SynapseEnvironment, room_id: str, body: str
) -> list[dict[str, Any]]:
    """All ``m.room.message`` events with *body* in the provider timeline."""
    chunk = _fetch_room_messages(env, room_id)
    return [
        event
        for event in chunk
        if event.get("type") == "m.room.message"
        and isinstance(event.get("content"), dict)
        and event["content"].get("body") == body
    ]


async def _wait_for_body_events(
    env: SynapseEnvironment, room_id: str, body: str
) -> list[dict[str, Any]]:
    """Poll the provider timeline until *body* appears; return the events.

    A transient homeserver hiccup during polling is treated as
    not-ready-yet rather than aborting the wait; the assertion below
    still fails with its own message when the body never appears.
    """
    found: list[dict[str, Any]] = []

    def _poll() -> bool:
        try:
            observed = _observed_messages_for_body(env, room_id, body)
        except (OSError, json.JSONDecodeError):
            return False
        found.extend(observed)
        return bool(found)

    assert await wait_until(
        _poll, timeout=_POLL_TIMEOUT, interval=_POLL_INTERVAL
    ), f"the Synapse timeline never showed the replayed body {body!r}"
    return found


# ---------------------------------------------------------------------------
# Pipeline assembly
# ---------------------------------------------------------------------------


def _make_matrix_config(env: SynapseEnvironment) -> MatrixConfig:
    """Build a MatrixConfig pointing at the Docker Synapse."""
    return MatrixConfig(
        adapter_id=_MATRIX_ADAPTER_ID,
        homeserver=env.base_url,
        user_id=env.bot_user_id,
        access_token=env.bot_access_token,
        room_allowlist={env.test_room_id},
        encryption_mode="plaintext",
    ).validate()


def _make_adapter_context(adapter_id: str, runner: PipelineRunner) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""

    async def _publish(event: Any) -> None:
        await runner.handle_ingress(event)

    return AdapterContext(
        adapter_id=adapter_id,
        publish_inbound=_publish,
        logger=logging.getLogger(f"test.exact_target.{adapter_id}"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )


def _make_pipeline(
    storage: SQLiteStorage,
    env: SynapseEnvironment,
    matrix_adapter: MatrixAdapter,
    monitor: FakeMatrixAdapter,
) -> PipelineConfig:
    """Build a PipelineConfig with the real Matrix renderer registered."""
    rp = RenderingPipeline()
    rp.register(MatrixRenderer(), priority=50)
    rp.register(TextRenderer(), priority=100)

    router = Router(routes=[_route_for(env.test_room_id)])
    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={
            _MATRIX_ADAPTER_ID: matrix_adapter,
            _MONITOR_ADAPTER_ID: monitor,
        },
        rendering_pipeline=rp,
        runtime_accounting=RuntimeAccounting(),
        event_bus=EventBus(),
    )


async def _start_pipeline(
    runner: PipelineRunner,
    matrix_adapter: MatrixAdapter,
    monitor: FakeMatrixAdapter,
) -> None:
    """Start the runner and both adapters wired to its ingress handler."""
    await runner.start()
    await matrix_adapter.start(_make_adapter_context(_MATRIX_ADAPTER_ID, runner))
    await monitor.start(_make_adapter_context(_MONITOR_ADAPTER_ID, runner))


def _outcome_for_adapter(outcomes: Any, adapter_id: str) -> Any:
    """The delivery outcome recorded for *adapter_id* inside a replay envelope."""
    assert isinstance(
        outcomes, list
    ), f"replay envelope should carry the outcome list, got {type(outcomes)!r}"
    for outcome in outcomes:
        if getattr(outcome, "target_adapter", None) == adapter_id:
            return outcome
    return None


# ---------------------------------------------------------------------------
# Staged-generation scaffolding (test A)
# ---------------------------------------------------------------------------


def _staged_snapshot(route: Route, plan: DeliveryPlan) -> OutboxDispatchSnapshotWrite:
    """Freeze the accepted route/plan/routing the way initial staging does."""
    return OutboxDispatchSnapshotWrite(
        kind="staged",
        context=msgspec.to_builtins(
            OutboxDispatchContext(
                route=route,
                plan=plan,
                routing=RoutingMetadata(
                    matched_routes=(route.id,),
                    route_trace=(route.id,),
                ),
            )
        ),
    )


async def _stage_pending_generation(
    storage: SQLiteStorage,
    *,
    event_id: str,
    room_id: str,
) -> DeliveryOutboxItem:
    """Persist one never-dispatched outbox generation for the exact target.

    This stands in for an earlier unfinished delivery generation so the
    replay below performs the *first* real native send of the identity
    with a fresh transaction id — making the homeserver-side acceptance
    observation unambiguous.
    """
    plan_id = _matrix_plan_id(event_id, room_id)
    route = _route_for(room_id)
    item = DeliveryOutboxItem(
        outbox_id=f"obox-{uuid.uuid4()}",
        event_id=event_id,
        route_id=route.id,
        delivery_plan_id=plan_id,
        target_adapter=_MATRIX_ADAPTER_ID,
        target_channel=room_id,
        attempt_number=1,
        status="pending",
    )
    plan = DeliveryPlan(
        plan_id=plan_id,
        event_id=event_id,
        target=RouteTarget(adapter=_MATRIX_ADAPTER_ID, channel=room_id),
        primary_strategy=DeliveryStrategy(method="direct"),
        route_id=route.id,
    )
    created = await storage.create_outbox_item(
        item,
        dispatch_snapshot=_staged_snapshot(route, plan),
    )
    return created


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_exact_target_replay_send_is_observed_by_synapse(
    synapse_env: SynapseEnvironment,
    temp_storage: SQLiteStorage,
) -> None:
    """A scoped replay send is accepted by the real homeserver.

    Proves:
    1. The exact-target admission gate resolves one persisted
       generation (``pending`` preview) for the selected identity.
    2. The scoped replay delivers through the real Matrix adapter
       to the real homeserver with the replay provenance recorded.
    3. The Synapse room timeline — read independently through the
       test user's client — shows the sent event under the event id
       the send reported, from the bot sender, in the target room.
    4. The unselected monitor sibling receives nothing: one exact
       target never widens into sibling channels.
    """
    ts = int(time.time())
    event_id = f"evt-exact-accept-{ts}"
    body = "MEDRE exact-target acceptance replay " f"(ts={ts} nonce={uuid.uuid4().hex})"
    room_id = synapse_env.test_room_id

    # -- Stage: admitted event plus one pending generation -----------
    await temp_storage.append(_make_event(event_id, body))
    staged = await _stage_pending_generation(
        temp_storage, event_id=event_id, room_id=room_id
    )

    selection = ExactTargetSelection(
        event_id=event_id,
        delivery_plan_id=staged.delivery_plan_id,
        target_adapter=_MATRIX_ADAPTER_ID,
        target_channel=room_id,
    )
    preview = await preview_exact_target(temp_storage, selection)
    assert preview.category == "pending", (
        "staged generation should classify as pending, got " f"{preview.category!r}"
    )

    # -- Replay through the real pipeline ----------------------------
    matrix_adapter = MatrixAdapter(_make_matrix_config(synapse_env))
    monitor = FakeMatrixAdapter(_MONITOR_ADAPTER_ID)
    runner = PipelineRunner(
        _make_pipeline(temp_storage, synapse_env, matrix_adapter, monitor)
    )
    await _start_pipeline(runner, matrix_adapter, monitor)

    run_id = f"exact-accept-{ts}"
    try:
        engine = ReplayEngine(
            storage=cast(StorageBackend, temp_storage),
            pipeline=runner,
        )
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[event_id],
            run_id=run_id,
            exact_target_selections=(selection,),
        )
        results = [result async for result in engine.replay(request)]
        deliver = _single_deliver_result(event_id, results)

        assert deliver.status == "passed", (
            f"scoped replay deliver should pass, got {deliver.status!r} "
            f"({deliver.error!r})"
        )
        assert deliver.output is not None
        assert deliver.output["replay"] is True
        assert "exact_target_refusals" not in deliver.output

        outcome = _outcome_for_adapter(
            deliver.output["adapter_results"], _MATRIX_ADAPTER_ID
        )
        assert outcome is not None, "the exact target must be delivered"
        assert outcome.status == "success", (
            f"replay send should succeed, got {outcome.status!r} "
            f"({outcome.error!r})"
        )
        receipt = outcome.receipt
        assert receipt is not None and receipt.status == "sent"
        assert receipt.source == "replay"
        assert receipt.replay_run_id == run_id
        native_event_id = receipt.adapter_message_id
        assert native_event_id, "the send must report a native event id"

        # -- Independent provider-side acceptance observation --------
        observed = await _wait_for_body_events(synapse_env, room_id, body)
        assert len(observed) == 1, (
            "the provider timeline should show exactly one event for "
            f"the replayed body, got {len(observed)}"
        )
        observed_event = observed[0]
        assert observed_event.get("event_id") == native_event_id, (
            f"provider event {observed_event.get('event_id')!r} must match "
            f"the replay send's native id {native_event_id!r}"
        )
        assert observed_event.get("sender") == synapse_env.bot_user_id

        # -- Sibling boundary ----------------------------------------
        assert (
            len(monitor.delivered_payloads) == 0
        ), "the unselected monitor sibling must not receive the replay"

        logger.info(
            "Exact-target native acceptance: event=%s native_event=%s "
            "attempt=%s provider_observed=yes",
            event_id,
            native_event_id,
            receipt.attempt_number,
        )
        if _RUN_ARTIFACT_DIR is not None:
            _write_artifact_json(
                "synapse-exact-target-acceptance.json",
                {
                    "scenario": "matrix_exact_target_native_acceptance",
                    "transport": "matrix",
                    "evidence_level": "docker_provider_observed",
                    "event_id": event_id,
                    "native_event_id": native_event_id,
                    "replay_run_id": run_id,
                    "attempt_number": receipt.attempt_number,
                    "observed_event_id": observed_event.get("event_id"),
                    "observed_sender": observed_event.get("sender"),
                    "sibling_deliveries": len(monitor.delivered_payloads),
                    "limitations": [
                        "Second client shares the CI test user account; "
                        "not a separate human recipient.",
                        "Plaintext room; encrypted-room handoff is a "
                        "separate campaign leg.",
                        "Docker loopback homeserver, not a federated " "deployment.",
                    ],
                },
            )
            _write_run_metadata(
                scenario="synapse_exact_target_acceptance",
                containers={"synapse": synapse_env.container_name},
                storage_path=temp_storage._db_path,
                extras={"event_id": event_id},
            )
    finally:
        await matrix_adapter.stop()
        await monitor.stop()
        await runner.stop()


async def test_completed_target_replay_is_provider_confirmed(
    synapse_env: SynapseEnvironment,
    temp_storage: SQLiteStorage,
) -> None:
    """Replaying a completed live target reaches the provider again.

    The live pipeline delivery sends through the real adapter first;
    the scoped replay then re-delivers the completed identity.  The
    provider timeline must independently confirm the event id the
    replay send reported — whether the pinned adapter's
    deterministic transaction id falls inside the provider's
    idempotency window (same event returned) or produces a distinct
    second event.  The monitor sibling must never be re-sent.
    """
    ts = int(time.time())
    event_id = f"evt-exact-replay-{ts}"
    body = f"MEDRE exact-target completed replay (ts={ts} nonce={uuid.uuid4().hex})"
    room_id = synapse_env.test_room_id

    await temp_storage.append(_make_event(event_id, body))

    # -- Live delivery through the real pipeline ----------------------
    matrix_adapter = MatrixAdapter(_make_matrix_config(synapse_env))
    monitor = FakeMatrixAdapter(_MONITOR_ADAPTER_ID)
    runner = PipelineRunner(
        _make_pipeline(temp_storage, synapse_env, matrix_adapter, monitor)
    )
    await _start_pipeline(runner, matrix_adapter, monitor)

    run_id = f"exact-replay-{ts}"
    try:
        routed_event, plans = await runner.route_event(_make_event(event_id, body))
        live_outcomes = await runner.deliver_to_targets(
            routed_event, plans, source="live"
        )
        live = _outcome_for_adapter(live_outcomes, _MATRIX_ADAPTER_ID)
        assert (
            live is not None and live.status == "success"
        ), f"live send should succeed, got {getattr(live, 'status', None)!r}"
        live_receipt = live.receipt
        assert live_receipt is not None and live_receipt.status == "sent"
        live_event_id = live_receipt.adapter_message_id
        assert live_event_id, "the live send must report a native event id"

        live_observed = await _wait_for_body_events(synapse_env, room_id, body)
        assert [e.get("event_id") for e in live_observed] == [
            live_event_id
        ], "the provider timeline should show exactly the live event"

        # -- The completed target is replayed under its identity ------
        selection = ExactTargetSelection(
            event_id=event_id,
            delivery_plan_id=_matrix_plan_id(event_id, room_id),
            target_adapter=_MATRIX_ADAPTER_ID,
            target_channel=room_id,
        )
        preview = await preview_exact_target(temp_storage, selection)
        assert preview.category == "completed", (
            "delivered target should classify as completed, got "
            f"{preview.category!r}"
        )

        engine = ReplayEngine(
            storage=cast(StorageBackend, temp_storage),
            pipeline=runner,
        )
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[event_id],
            run_id=run_id,
            exact_target_selections=(selection,),
        )
        results = [result async for result in engine.replay(request)]
        deliver = _single_deliver_result(event_id, results)

        assert deliver.status == "passed", (
            f"scoped replay deliver should pass, got {deliver.status!r} "
            f"({deliver.error!r})"
        )
        outcome = _outcome_for_adapter(
            deliver.output["adapter_results"], _MATRIX_ADAPTER_ID
        )
        assert (
            outcome is not None and outcome.status == "success"
        ), "the replay send should be delivered through the real adapter"
        receipt = outcome.receipt
        assert receipt is not None
        assert receipt.source == "replay"
        assert receipt.replay_run_id == run_id
        assert (
            receipt.attempt_number == live_receipt.attempt_number + 1
        ), "replay must allocate a fresh generation for a completed target"
        replay_event_id = receipt.adapter_message_id
        assert replay_event_id, "the replay send must report a native event id"

        # -- Provider-side confirmation of the replay send ------------
        observed = await _wait_for_body_events(synapse_env, room_id, body)
        observed_ids = [e.get("event_id") for e in observed]
        assert replay_event_id in observed_ids, (
            "the provider timeline must confirm the replay send's event "
            f"id {replay_event_id!r}; observed {observed_ids!r}"
        )
        assert all(e.get("sender") == synapse_env.bot_user_id for e in observed)
        if replay_event_id == live_event_id:
            logger.info(
                "Completed-target replay: provider idempotency collapsed "
                "the replay send into the original event %s",
                live_event_id,
            )
            assert observed_ids == [live_event_id]
        else:
            logger.info(
                "Completed-target replay: provider accepted a distinct "
                "second event %s (live %s)",
                replay_event_id,
                live_event_id,
            )
            assert sorted(observed_ids) == sorted([live_event_id, replay_event_id])

        # -- Sibling boundary ----------------------------------------
        assert len(monitor.delivered_payloads) == 1, (
            "the monitor sibling must receive only the live delivery, "
            "never the exact-target replay"
        )
    finally:
        await matrix_adapter.stop()
        await monitor.stop()
        await runner.stop()


async def test_refused_exact_target_stages_no_send(
    synapse_env: SynapseEnvironment,
    temp_storage: SQLiteStorage,
) -> None:
    """A selection without a persisted generation is refused, not sent.

    The refusal evidence carries the observed category and the
    homeserver timeline never gains the refused body: the gate
    refuses before anything is staged at the provider.
    """
    ts = int(time.time())
    event_id = f"evt-exact-refused-{ts}"
    body = f"MEDRE exact-target refused body (ts={ts} nonce={uuid.uuid4().hex})"
    room_id = synapse_env.test_room_id

    # Admitted, but never delivered: no outbox generation exists.
    await temp_storage.append(_make_event(event_id, body))

    selection = ExactTargetSelection(
        event_id=event_id,
        delivery_plan_id=_matrix_plan_id(event_id, room_id),
        target_adapter=_MATRIX_ADAPTER_ID,
        target_channel=room_id,
    )

    matrix_adapter = MatrixAdapter(_make_matrix_config(synapse_env))
    monitor = FakeMatrixAdapter(_MONITOR_ADAPTER_ID)
    runner = PipelineRunner(
        _make_pipeline(temp_storage, synapse_env, matrix_adapter, monitor)
    )
    await _start_pipeline(runner, matrix_adapter, monitor)

    try:
        engine = ReplayEngine(
            storage=cast(StorageBackend, temp_storage),
            pipeline=runner,
        )
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[event_id],
            run_id=f"exact-refused-{ts}",
            exact_target_selections=(selection,),
        )
        results = [result async for result in engine.replay(request)]
        deliver = _single_deliver_result(event_id, results)

        assert (
            deliver.status == "skipped"
        ), f"an unstaged identity must be refused, got {deliver.status!r}"
        assert deliver.error is not None
        assert deliver.error.startswith(
            "exact_target_refused: ambiguous"
        ), f"unexpected refusal reason: {deliver.error!r}"
        refusals = deliver.output["exact_target_refusals"]
        assert len(refusals) == 1
        assert refusals[0]["category"] == "ambiguous"
        assert refusals[0]["target_adapter"] == _MATRIX_ADAPTER_ID

        # Nothing may reach the provider for the refused target: the
        # replay completed synchronously, so the provider timeline is
        # read directly without waiting.
        observed = _observed_messages_for_body(synapse_env, room_id, body)
        assert observed == [], "a refused exact target must stage no provider send"
        assert len(monitor.delivered_payloads) == 0
    finally:
        await matrix_adapter.stop()
        await monitor.stop()
        await runner.stop()


def _single_deliver_result(event_id: str, results: list[ReplayResult]) -> ReplayResult:
    """Extract the one deliver-stage result produced for *event_id*."""
    deliver_results = [result for result in results if result.stage == "deliver"]
    assert len(deliver_results) == 1, (
        f"expected exactly one deliver result for {event_id}, got "
        f"{len(deliver_results)}"
    )
    return deliver_results[0]
