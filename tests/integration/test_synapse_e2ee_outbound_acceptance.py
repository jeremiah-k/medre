# medre-test-tier: compatibility
"""Required Docker-tier proof: routed Matrix encrypted outbound, independently decrypted.

This module holds the encrypted outbound acceptance harness. It extends the existing
Docker Synapse E2EE infrastructure (``tests/integration/conftest.py``) with
the property the inbound-only smoke cannot show: **an encrypted message that
MEDRE sends is decrypted and observed by an independent second client**.

What one positive proof drives, in order, all through real components:

1. A durable production-shape rig: ``SQLiteStorage``, ``PipelineRunner``,
   ``DurableIngressWorker``, ``RetryWorker`` (initial dispatch) and the real
   ``MatrixAdapter`` with ``encryption_mode="e2ee_required"`` and the
   default quiet-permissive peer policy. No fake retry, no plaintext
   fallback, no direct adapter-call shortcut.
2. An independent recipient: a dedicated peer account (its own Matrix
   account, a fresh server device per test, and its own nio crypto store)
   that MEDRE never controls.
3. Deterministic readiness gates before any send: the peer client has
   uploaded its device keys, and the bot's device store demonstrably holds
   the peer's device. Every wait is ``wait_until`` on observable state.
4. One canonical ``message.created`` event through ``admit_ingress`` ->
   durable outbox -> retry worker -> the adapter's real Megolm send.
5. The required verdict asserts ALL of: every outbox row terminal ``sent``;
   an attempt receipt with ``status="sent"``, ``receipt_kind="attempt"``,
   ``confirmation_level="remote_service"`` and the homeserver event id; an
   outbound ``NativeMessageRef`` whose native message id equals the receipt's
   native id and whose channel equals the encrypted room; and a peer-side
   observation of a ``RoomMessageText`` with ``decrypted is True`` whose
   event id equals that same native id and whose body equals the sent text.

The proof FAILS (no ``xfail``, no skip-path) if any link is missing.

Negative cases in this module prove the verdict cannot be reached dishonestly:
a recipient without the room key, an observer in the wrong room, a wrong
event id, and a stale (foreign-event) attempt each refuse.

Separately reported properties, never merged into the required proof:

- Adapter restart on the same key store keeps delivering and decrypting.
- Opt-in ``verification_policy="strict"`` refuses an unverified peer device
  and delivers only after local verification. The default stays quiet
  permissive: the required proof carries a tripwire proving the permissive path performs no
  peer-device trust scan at all.

Inbound decrypt convergence remains the *separate* property covered by
``tests/integration/test_synapse_e2ee_smoke.py`` (which may ``xfail``);
nothing here reads or depends on that file.

What this harness does NOT prove: Docker loopback only — no federation,
external network, cross-signing ceremony, or recipient-side user action
(read receipts). Evidence classification: ``docker_encrypted_room_qualification``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_E2EE
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig
from medre.config.model import RetryConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.ingress import DurableIngressWorker
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from medre.runtime.retry import RetryWorker
from tests.helpers.async_utils import wait_until

from .conftest import (
    E2EETestEnvironment,
    SynapseEnvironment,
    close_nio_client,
    close_nio_store,
    login_synapse_device,
)

pytestmark: list[Any] = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not HAS_E2EE,
        reason='mindroom-nio[e2e] not installed; pip install "medre-adapter-matrix[e2e]"',
    ),
]

logger = logging.getLogger(__name__)

_SOURCE_ADAPTER = "synapse-outbound-source"
_SOURCE_CHANNEL = "ch-0"
_TARGET_ADAPTER = "synapse-outbound-bot"

_PEER_LOCALPART = "medre-out-peer"
_PEER_PASSWORD = "medre-out-peer-password"

#: Readiness/observation budgets. Generous enough for Megolm key exchange on
#: a loaded CI runner, but every wait exits at the first observable state.
_KEY_EXCHANGE_TIMEOUT = 90.0
_DELIVERY_TIMEOUT = 90.0
_OBSERVATION_TIMEOUT = 90.0


# ---------------------------------------------------------------------------
# HTTP and account helpers
# ---------------------------------------------------------------------------


def _http_json(
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    method: str = "GET",
    timeout: int = 15,
) -> dict[str, Any]:
    """One JSON Matrix client-server API call against the local Synapse."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode()
        finally:
            exc.close()
        raise RuntimeError(f"HTTP {exc.code} for {method} {url}: {body}") from exc


def _register_peer_account(env: SynapseEnvironment) -> None:
    """Register the dedicated independent peer account on the fixture server.

    The session Synapse container is created fresh per pytest session and the
    config enables open registration, so the account cannot pre-exist.
    """
    result = subprocess.run(
        [
            "docker",
            "exec",
            env.container_name,
            "register_new_matrix_user",
            "-u",
            _PEER_LOCALPART,
            "-p",
            _PEER_PASSWORD,
            "--no-admin",
            "-c",
            "/data/homeserver.yaml",
            "http://localhost:8008",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"peer account registration failed: {result.stdout} {result.stderr}"
        )


def _domain_of(env: SynapseEnvironment) -> str:
    """The server_name portion of the fixture bot's user id."""
    return env.bot_user_id.rsplit(":", 1)[1]


@pytest.fixture(scope="module")
def peer_account(synapse_env: SynapseEnvironment) -> tuple[str, str]:
    """A dedicated, MEDRE-independent recipient account for this module."""
    _register_peer_account(synapse_env)
    return (f"@{_PEER_LOCALPART}:{_domain_of(synapse_env)}", _PEER_PASSWORD)


async def _init_peer_client(
    env: SynapseEnvironment,
    peer_user_id: str,
    peer_password: str,
    store_path: str,
    room_id: str,
) -> Any:
    """Create the independent recipient nio client with real Megolm crypto.

    A fresh server device is logged in per call, joined to *room_id*, and the
    client performs the full crypto bootstrap (initial sync, key upload,
    key query, key claim, to-device flush) before returning.
    """
    import nio

    login = login_synapse_device(env, peer_user_id, peer_password)
    _http_json(
        f"{env.base_url}/_matrix/client/v3/join/{room_id}",
        {},
        token=login["access_token"],
        method="POST",
    )
    client = nio.AsyncClient(
        homeserver=env.base_url,
        user=login["user_id"],
        device_id=login["device_id"],
        store_path=store_path,
        config=nio.AsyncClientConfig(
            encryption_enabled=True,
            max_timeouts=0,
            max_limit_exceeded=0,
            request_timeout=10,
        ),
    )
    try:
        client.restore_login(
            user_id=login["user_id"],
            device_id=login["device_id"],
            access_token=login["access_token"],
        )
        initial = await asyncio.wait_for(client.sync(full_state=True), 30)
        assert isinstance(initial, nio.SyncResponse), "peer initial sync failed"
        _record_peer_sync(client, room_id, initial)
        if client.should_upload_keys:
            await client.keys_upload()
        if client.should_query_keys:
            await client.keys_query()
        if client.should_claim_keys:
            with contextlib.suppress(Exception):
                users = client.get_users_for_key_claiming()
                if users:
                    await client.keys_claim(users)
        await client.send_to_device_messages()
    except BaseException:
        await close_nio_client(client)
        raise
    return client


# ---------------------------------------------------------------------------
# The acceptance verdict: one pure function over ledger + observation facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedEvent:
    """One peer-side observation: event id, body content, room, decrypted flag."""

    event_id: str
    body: str
    room_id: str
    decrypted: bool


@dataclass(frozen=True)
class OutboundAcceptanceEvidence:
    """Every fact the required verdict needs, collected from real state.

    Attributes mirror the durable ledger (outbox rows, attempt receipts,
    outbound native refs) plus the independent client's observation.
    """

    event_id: str
    target_adapter: str
    target_room_id: str
    expected_body: str
    outbox_rows: tuple[dict[str, Any], ...]
    receipts: tuple[dict[str, Any], ...]
    outbound_refs: tuple[dict[str, Any], ...]
    observations: tuple[ObservedEvent, ...] = field(default_factory=tuple)


def evaluate_outbound_acceptance(evidence: OutboundAcceptanceEvidence) -> list[str]:
    """Return refusal reasons; an empty list is the only positive verdict.

    Every link of canonical event -> durable outbox -> attempt receipt ->
    native ref -> independent decrypted observation is required. A missing
    key, a wrong room, a wrong event id, or a stale (foreign-event) attempt
    each fail at least one check, so none can produce a positive verdict.
    """
    reasons: list[str] = []

    if not evidence.outbox_rows:
        reasons.append("outbox_rows_missing")
    elif not all(row.get("status") == "sent" for row in evidence.outbox_rows):
        reasons.append("outbox_not_terminal_sent")
    outbox_ids = {row.get("outbox_id") for row in evidence.outbox_rows}

    attempt_receipts = [
        receipt
        for receipt in evidence.receipts
        if receipt.get("status") == "sent" and receipt.get("receipt_kind") == "attempt"
    ]
    if not attempt_receipts:
        reasons.append("attempt_receipt_missing")
        return reasons

    receipt = attempt_receipts[0]
    if receipt.get("event_id") != evidence.event_id:
        reasons.append("receipt_belongs_to_foreign_event")
    if not receipt.get("adapter_message_id"):
        reasons.append("receipt_missing_native_message_id")
    if outbox_ids and receipt.get("outbox_id") not in outbox_ids:
        reasons.append("receipt_outbox_lineage_mismatch")
    if receipt.get("confirmation_level") != "remote_service":
        reasons.append("receipt_confirmation_not_remote_service")

    native_id = receipt.get("adapter_message_id")
    matching_refs = [
        ref for ref in evidence.outbound_refs if ref.get("native_message_id")
    ]
    if not matching_refs:
        reasons.append("outbound_native_ref_missing")
        return reasons

    ref = matching_refs[0]
    if ref.get("direction") != "outbound":
        reasons.append("native_ref_direction_not_outbound")
    if ref.get("adapter") != evidence.target_adapter:
        reasons.append("native_ref_foreign_adapter")
    if native_id is not None and ref.get("native_message_id") != native_id:
        reasons.append("native_ref_receipt_identity_mismatch")
    if ref.get("native_channel_id") != evidence.target_room_id:
        reasons.append("native_ref_channel_mismatch")

    decrypted_matches = [
        observation
        for observation in evidence.observations
        if observation.decrypted and observation.event_id == native_id
    ]
    if not decrypted_matches:
        reasons.append("independent_decrypted_observation_missing")
        return reasons

    observation = decrypted_matches[0]
    if observation.body != evidence.expected_body:
        reasons.append("observation_body_mismatch")
    if observation.room_id != evidence.target_room_id:
        reasons.append("observation_room_mismatch")
    return reasons


# ---------------------------------------------------------------------------
# Production-shape rig
# ---------------------------------------------------------------------------


@dataclass
class Rig:
    """One started production-shape stack around the real MatrixAdapter."""

    adapter: MatrixAdapter
    runner: PipelineRunner
    storage: SQLiteStorage
    ingress_worker: DurableIngressWorker
    retry_worker: RetryWorker
    adapter_id: str
    target_room_id: str


def _make_pipeline_config(
    storage: SQLiteStorage,
    target_room_id: str,
    matrix_adapter: MatrixAdapter,
    *,
    verification_policy: str | None = None,
) -> PipelineConfig:
    """Route canonical ingress from the fake source to the real encrypted bot."""
    rendering = RenderingPipeline()
    rendering.register(MatrixRenderer(), priority=50)
    rendering.register(TextRenderer(), priority=100)
    route = Route(
        id="synapse-outbound-route",
        source=RouteSource(
            adapter=_SOURCE_ADAPTER,
            event_kinds=("message.created",),
            channel=_SOURCE_CHANNEL,
        ),
        targets=[RouteTarget(adapter=_TARGET_ADAPTER, channel=target_room_id)],
    )
    return PipelineConfig(
        storage=storage,
        router=Router(routes=[route]),
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={_TARGET_ADAPTER: matrix_adapter},
        rendering_pipeline=rendering,
        runtime_accounting=RuntimeAccounting(),
        event_bus=EventBus(),
        # Without a route retry policy a transient delivery failure would
        # dead-letter instead of scheduling durable retry work. Deterministic
        # backoff keeps observation windows tight.
        route_retry_policies={route.id: RetryPolicy(jitter=False)},
    )


def _make_bot_config(
    e2ee_env: E2EETestEnvironment,
    *,
    store_path: str,
    room_id: str | None = None,
    verification_policy: str | None = None,
) -> MatrixConfig:
    """Build the bot adapter config; the default stays quiet permissive."""
    target_room = room_id or e2ee_env.encrypted_room_id
    kwargs: dict[str, Any] = {
        "adapter_id": _TARGET_ADAPTER,
        "homeserver": e2ee_env.base_url,
        "user_id": e2ee_env.bot_user_id,
        "access_token": e2ee_env.bot_access_token,
        "device_id": e2ee_env.bot_device_id,
        "room_allowlist": {target_room},
        "encryption_mode": "e2ee_required",
        "auto_join_rooms": (target_room,),
        "store_path": store_path,
    }
    if verification_policy is not None:
        kwargs["verification_policy"] = verification_policy
    return MatrixConfig(**kwargs).validate()


def _make_ctx(
    adapter_id: str, runner: PipelineRunner, storage: SQLiteStorage
) -> AdapterContext:
    """Wire production admission and checkpoint ownership to the pipeline."""

    async def _publish(event: Any) -> None:
        await runner.handle_ingress(event)

    async def _admit(event: Any, provenance: Any, attachment: Any = None) -> Any:
        return await runner.admit_ingress(event, provenance, attachment=attachment)

    async def _load_checkpoint(stream: str) -> Any:
        return await storage.get_adapter_checkpoint(adapter_id, stream)

    async def _commit_checkpoint(stream: str, cursor: str, metadata_json: str) -> None:
        await storage.put_adapter_checkpoint(
            adapter_id, stream, cursor, metadata_json=metadata_json
        )

    return AdapterContext(
        adapter_id=adapter_id,
        publish_inbound=_publish,
        admit_inbound=_admit,
        load_checkpoint=_load_checkpoint,
        commit_checkpoint=_commit_checkpoint,
        logger=logging.getLogger(f"test.e2ee-outbound.{adapter_id}"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )


async def _start_rig(
    e2ee_env: E2EETestEnvironment,
    storage: SQLiteStorage,
    tmp_path: Path,
    *,
    target_room_id: str | None = None,
    verification_policy: str | None = None,
) -> Rig:
    """Start the durable production-shape rig on real durable state.

    The adapter is fully started before the retry worker so a retained row
    claimed at startup can never meet an uninitialized session. The target
    room defaults to the fixture's encrypted room; the strict negative uses
    a dedicated room so the complete recipient view covers exactly the
    bot and peer accounts.
    """
    room_id = target_room_id or e2ee_env.encrypted_room_id
    config = _make_bot_config(
        e2ee_env,
        store_path=str(tmp_path / "bot-store"),
        room_id=room_id,
        verification_policy=verification_policy,
    )
    adapter = MatrixAdapter(config)
    runner = PipelineRunner(
        _make_pipeline_config(
            storage,
            room_id,
            adapter,
            verification_policy=verification_policy,
        )
    )
    ingress_worker = DurableIngressWorker(storage=storage, pipeline=runner)
    retry_worker = RetryWorker(
        storage,
        runner,
        None,
        retry_config=RetryConfig(
            enabled=True,
            interval_seconds=0.5,
            max_attempts=30,
        ),
        enabled=True,
        initial_dispatch=True,
        interval_seconds=0.5,
        max_attempts=30,
        available_target_adapters={_TARGET_ADAPTER},
    )
    async with contextlib.AsyncExitStack() as startup:
        # Ownership transfers to Rig only after every component started.
        startup.push_async_callback(runner.stop)
        await runner.start()
        startup.push_async_callback(ingress_worker.stop, grace_seconds=5.0)
        await ingress_worker.start()
        startup.push_async_callback(adapter.stop)
        await adapter.start(_make_ctx(_TARGET_ADAPTER, runner, storage))
        startup.push_async_callback(retry_worker.stop)
        await retry_worker.start()
        startup.pop_all()
    return Rig(
        adapter=adapter,
        runner=runner,
        storage=storage,
        ingress_worker=ingress_worker,
        retry_worker=retry_worker,
        adapter_id=_TARGET_ADAPTER,
        target_room_id=room_id,
    )


async def _stop_rig(rig: Rig) -> None:
    """Tear the rig down via production stop paths.

    A durable unsent obligation is allowed; a live abandoned dispatch task
    is not. Never close the adapter while the retry worker still owns one.
    """
    # Capture the adapter's nio client before teardown: the production stop
    # path runs nio close(), which never closes the store's SQLite connection
    # in the pinned nio surface (see conftest.close_nio_store).
    adapter_session = getattr(rig.adapter, "_session", None)
    adapter_client = (
        getattr(adapter_session, "_client", None)
        if adapter_session is not None
        else None
    )
    await rig.retry_worker.stop()
    assert (
        not rig.retry_worker.has_retained_work
    ), "retry worker stopped with live dispatch work; adapter teardown unsafe"
    for name, stop_coro in (
        ("adapter", rig.adapter.stop()),
        ("ingress_worker", rig.ingress_worker.stop(grace_seconds=5.0)),
        ("runner", rig.runner.stop()),
    ):
        try:
            await stop_coro
        except Exception:
            logger.debug("%s cleanup error", name, exc_info=True)
    if adapter_client is not None:
        with contextlib.suppress(Exception):
            close_nio_store(adapter_client)


# ---------------------------------------------------------------------------
# Ledger observation helpers
# ---------------------------------------------------------------------------


async def _admit_message(rig: Rig, event_id: str, text: str) -> CanonicalEvent:
    """Admit one canonical message.created event through durable ingress."""
    event = CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter=_SOURCE_ADAPTER,
        source_transport_id="node-1",
        source_channel_id=_SOURCE_CHANNEL,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": text},
        metadata=EventMetadata(),
    )
    await rig.runner.admit_ingress(event, "live")
    return event


async def _outbox_rows(rig: Rig, event_id: str) -> list[Any]:
    return await rig.storage.list_outbox_items_for_event(event_id)


async def _await_outbox_terminal_sent(rig: Rig, event_id: str) -> list[Any]:
    """Wait until every outbox row of *event_id* is terminal ``sent``."""

    async def _condition() -> bool:
        rows = await _outbox_rows(rig, event_id)
        return bool(rows) and all(row.status == "sent" for row in rows)

    assert await wait_until(
        _condition, timeout=_DELIVERY_TIMEOUT, interval=0.25
    ), f"outbox rows for {event_id} never reached terminal sent"
    return await _outbox_rows(rig, event_id)


async def _outbound_native_ref(rig: Rig, event_id: str) -> Any:
    """Return the outbound native ref recorded for *event_id*."""
    refs = await rig.storage.list_native_refs_for_event(event_id)
    outbound = [
        ref
        for ref in refs
        if ref.adapter == rig.adapter_id and ref.direction == "outbound"
    ]
    assert outbound, f"no outbound native ref recorded for {event_id}"
    return outbound[0]


async def _attempt_receipts(rig: Rig, event_id: str) -> list[Any]:
    """Return the durable delivery receipts recorded for *event_id*."""
    return await rig.storage.list_receipts_for_event(event_id)


async def _await_bot_knows_peer_device(
    rig: Rig, peer_user_id: str, peer_device_id: str
) -> None:
    """Gate: the bot's device store demonstrably holds the peer's device keys."""

    def _known() -> bool:
        session = rig.adapter._session
        client = getattr(session, "_client", None)
        device_store = getattr(client, "device_store", None)
        if device_store is None:
            return False
        try:
            return any(
                getattr(device, "id", None) == peer_device_id
                for device in device_store.active_user_devices(peer_user_id)
            )
        except Exception:
            return False

    assert await wait_until(_known, timeout=_KEY_EXCHANGE_TIMEOUT, interval=0.5), (
        "bot device store never learned the peer device keys; the required "
        "proof refuses to send without completed key exchange readiness"
    )


def _collect_evidence(
    rig: Rig,
    event_id: str,
    expected_body: str,
    outbox_rows: list[Any],
    receipts: list[Any],
    native_ref: Any,
    observations: list[ObservedEvent],
) -> OutboundAcceptanceEvidence:
    """Build the verdict evidence bundle from real durable and peer state."""

    def _receipt_dict(receipt: Any) -> dict[str, Any]:
        return {
            "event_id": receipt.event_id,
            "status": receipt.status,
            "receipt_kind": receipt.receipt_kind,
            "adapter_message_id": receipt.adapter_message_id,
            "outbox_id": receipt.outbox_id,
            "confirmation_level": receipt.confirmation_level,
            "attempt_number": receipt.attempt_number,
        }

    ref_dict = {
        "adapter": native_ref.adapter,
        "direction": native_ref.direction,
        "native_message_id": native_ref.native_message_id,
        "native_channel_id": native_ref.native_channel_id,
    }
    return OutboundAcceptanceEvidence(
        event_id=event_id,
        target_adapter=rig.adapter_id,
        target_room_id=rig.target_room_id,
        expected_body=expected_body,
        outbox_rows=tuple(
            {"outbox_id": row.outbox_id, "status": row.status} for row in outbox_rows
        ),
        receipts=tuple(_receipt_dict(receipt) for receipt in receipts),
        outbound_refs=(ref_dict,),
        observations=tuple(observations),
    )


def _olm_identity_keys(client: Any) -> tuple[str, str]:
    """Check actual public Olm account keys, not only the supplied device ID."""
    account = getattr(getattr(client, "olm", None), "account", None)
    keys = getattr(account, "identity_keys", None)
    if callable(keys):
        keys = keys()
    assert isinstance(keys, dict), "nio has no readable Olm identity keys"
    result = (keys.get("curve25519"), keys.get("ed25519"))
    assert all(isinstance(key, str) and key for key in result)
    return result


# ---------------------------------------------------------------------------
# Independent recipient observation
# ---------------------------------------------------------------------------


def _record_peer_sync(client: Any, room_id: str, response: Any) -> list[ObservedEvent]:
    """Keep initial and incremental timelines, including pending ciphertexts."""
    join = getattr(getattr(response, "rooms", None), "join", None) or {}
    info = join.get(room_id)
    timeline = getattr(info, "timeline", None) if info is not None else None
    if getattr(timeline, "limited", False):
        raise AssertionError("peer timeline limited; observation incomplete")
    seen = _peer_observation_store(client)
    pending = getattr(client, "_medre_pending_ciphertexts", None)
    if pending is None:
        pending = {}
        client._medre_pending_ciphertexts = pending
    observations: list[ObservedEvent] = []

    def record(event: Any) -> None:
        event_id = getattr(event, "event_id", "")
        if not event_id:
            return
        decrypted = (
            type(event).__name__ == "RoomMessageText"
            and getattr(event, "decrypted", False) is True
        )
        source = getattr(event, "source", None) or {}
        content = source.get("content") if isinstance(source, dict) else None
        body = (
            content.get("body", "") if decrypted and isinstance(content, dict) else ""
        )
        previous = seen.get(event_id)
        if previous is None or (decrypted and not previous["decrypted"]):
            seen[event_id] = {
                "body": body,
                "decrypted": decrypted,
                "kind": type(event).__name__,
            }
            observations.append(ObservedEvent(event_id, body, room_id, decrypted))
        if decrypted:
            pending.pop(event_id, None)
        elif type(event).__name__ == "MegolmEvent":
            pending[event_id] = event

    for event in getattr(timeline, "events", None) or []:
        record(event)
    # A room key can arrive in a later sync with no repeat timeline event.
    for encrypted in tuple(pending.values()):
        try:
            decrypted = client.decrypt_event(encrypted)
        except Exception:
            continue  # A missing key is expected in the negative case.
        if getattr(decrypted, "decrypted", False) is True:
            record(decrypted)
    return observations


def _peer_observation_store(client: Any) -> dict[str, dict[str, Any]]:
    """Cumulative decrypted-observation map carried on the peer client.

    Sync windows are incremental: an event that arrived during an earlier
    sync never reappears later, so observations accumulate here.
    """
    seen: dict[str, dict[str, Any]] = getattr(client, "_medre_seen", None) or {}
    client._medre_seen = seen
    return seen


async def _sync_peer_once(peer_client: Any, room_id: str) -> list[ObservedEvent]:
    """Sync the peer once; return newly decrypted observations from the batch.

    Only a ``RoomMessageText`` carrying the pinned SDK's explicit
    ``decrypted=True`` flag qualifies as genuine Megolm decryption of
    encrypted content. Undecrypted ``MegolmEvent`` entries are recorded as
    non-decrypted so a missing key is observable, never silently positive.
    """
    response = await asyncio.wait_for(peer_client.sync(timeout=3000), 20)
    if getattr(response, "next_batch", None) is None:
        raise AssertionError("peer sync failed; no observation was made")
    return _record_peer_sync(peer_client, room_id, response)


async def _collect_peer_observations(
    peer_client: Any, room_id: str, *, rounds: int = 3
) -> list[ObservedEvent]:
    """Perform *rounds* bounded peer syncs and return new observations."""
    collected: list[ObservedEvent] = []
    for _ in range(rounds):
        collected.extend(await _sync_peer_once(peer_client, room_id))
    return collected


async def _await_peer_decrypted_body(
    peer_client: Any, room_id: str, body: str
) -> list[ObservedEvent]:
    """Wait until the peer observes a decrypted event with exactly *body*."""
    matched: list[ObservedEvent] = []

    async def _condition() -> bool:
        nonlocal matched
        fresh = await _sync_peer_once(peer_client, room_id)
        matched = [o for o in fresh if o.decrypted and o.body == body]
        seen = _peer_observation_store(peer_client)
        if not matched:
            matched = [
                ObservedEvent(
                    event_id=event_id,
                    body=content.get("body", ""),
                    room_id=room_id,
                    decrypted=content.get("decrypted", False),
                )
                for event_id, content in seen.items()
                if content.get("decrypted") and content.get("body") == body
            ]
        return bool(matched)

    assert await wait_until(_condition, timeout=_OBSERVATION_TIMEOUT, interval=1.0), (
        f"the independent recipient never decrypted a message with body "
        f"{body!r}; the required decrypt-and-deliver property is not "
        "demonstrated"
    )
    return matched


# ---------------------------------------------------------------------------
# The required proof
# ---------------------------------------------------------------------------


async def test_routed_encrypted_outbound_is_decrypted_by_independent_client(
    synapse_e2ee_env: E2EETestEnvironment,
    peer_account: tuple[str, str],
    temp_storage: SQLiteStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required proof: canonical -> durable outbox -> Megolm -> peer decrypts.

    Fails, without xfail or skip, unless the independent client decrypts the
    exact plaintext under the exact native event id backed by the exact
    attempt receipt and outbox generation.
    """
    peer_user_id, peer_password = peer_account
    room_id = synapse_e2ee_env.encrypted_room_id
    peer_store = str(tmp_path / "peer-store")
    Path(peer_store).mkdir(parents=True, exist_ok=True)

    # The default policy performs no peer-device trust scan at all: the
    # quiet-permissive default is part of the required configuration.
    peer_scan = AsyncMock(
        side_effect=AssertionError(
            "default permissive delivery must not perform a peer-device " "trust scan"
        )
    )
    monkeypatch.setattr(MatrixSession, "peer_device_trust_report", peer_scan)

    peer_client = await _init_peer_client(
        synapse_e2ee_env, peer_user_id, peer_password, peer_store, room_id
    )
    rig: Rig | None = None
    try:
        rig = await _start_rig(synapse_e2ee_env, temp_storage, tmp_path)
        await _await_bot_knows_peer_device(rig, peer_user_id, peer_client.device_id)

        body = "medre encrypted outbound outbound acceptance body"
        event = await _admit_message(rig, "encrypted-outbound-evt-1", body)
        assert event.event_id == "encrypted-outbound-evt-1"

        outbox_rows = await _await_outbox_terminal_sent(rig, event.event_id)
        receipts = await _attempt_receipts(rig, event.event_id)
        native_ref = await _outbound_native_ref(rig, event.event_id)
        assert native_ref.native_message_id.startswith("$")
        assert native_ref.native_channel_id == room_id

        observed = await _await_peer_decrypted_body(peer_client, room_id, body)
        evidence = _collect_evidence(
            rig,
            event.event_id,
            body,
            outbox_rows,
            receipts,
            native_ref,
            observed,
        )
        reasons = evaluate_outbound_acceptance(evidence)
        assert reasons == [], (
            "required decrypt-and-deliver verdict refused: "
            f"{reasons}; evidence={evidence}"
        )

        # Message identity: the peer-observed event id IS the homeserver id
        # recorded by the attempt receipt and the outbound native ref.
        assert observed[0].event_id == native_ref.native_message_id
        assert any(
            receipt.adapter_message_id == native_ref.native_message_id
            for receipt in receipts
        ), "attempt receipt must carry the same native event id"

        peer_scan.assert_not_awaited()
    finally:
        try:
            if rig is not None:
                await _stop_rig(rig)
        finally:
            with contextlib.suppress(Exception):
                await close_nio_client(peer_client)


# ---------------------------------------------------------------------------
# Negative: missing recipient key
# ---------------------------------------------------------------------------


async def test_recipient_without_room_key_cannot_yield_positive_verdict(
    synapse_e2ee_env: E2EETestEnvironment,
    peer_account: tuple[str, str],
    temp_storage: SQLiteStorage,
    tmp_path: Path,
) -> None:
    """A recipient whose store lacks the room key observes nothing decryptable.

    The ledger may honestly record a remote-service handoff, but the verdict
    requires an INDEPENDENT decrypted observation, so key unavailability can
    never be reported as a positive decrypt-and-deliver verdict.
    """
    peer_user_id, peer_password = peer_account
    room_id = synapse_e2ee_env.encrypted_room_id
    peer_store = str(tmp_path / "peer-store")
    fresh_store = str(tmp_path / "peer-fresh-store")
    for store_dir in (peer_store, fresh_store):
        Path(store_dir).mkdir(parents=True, exist_ok=True)

    primary_client = await _init_peer_client(
        synapse_e2ee_env, peer_user_id, peer_password, peer_store, room_id
    )
    rig: Rig | None = None
    fresh_client: Any = None
    try:
        rig = await _start_rig(synapse_e2ee_env, temp_storage, tmp_path)
        await _await_bot_knows_peer_device(rig, peer_user_id, primary_client.device_id)

        body = "medre encrypted outbound missing-key negative body"
        event = await _admit_message(rig, "encrypted-outbound-evt-missing-key", body)
        outbox_rows = await _await_outbox_terminal_sent(rig, event.event_id)
        receipts = await _attempt_receipts(rig, event.event_id)
        native_ref = await _outbound_native_ref(rig, event.event_id)

        # The primary (keyed) recipient decrypts, proving the send itself
        # carried real Megolm content.
        await _await_peer_decrypted_body(primary_client, room_id, body)

        # Now the key-unavailable recipient: a brand-new device whose nio
        # store predates the room key. The primary client is closed first so
        # nothing on the peer account can answer an olm key request.
        await close_nio_client(primary_client)
        fresh_client = await _init_peer_client(
            synapse_e2ee_env, peer_user_id, peer_password, fresh_store, room_id
        )
        await _collect_peer_observations(fresh_client, room_id, rounds=4)
        seen = _peer_observation_store(fresh_client)
        target_id = native_ref.native_message_id
        assert target_id in seen, "fresh device never observed target ciphertext"
        assert (
            seen[target_id]["kind"] == "MegolmEvent"
        ), "fresh device did not observe a target MegolmEvent"
        assert (
            seen[target_id]["decrypted"] is False
        ), "fresh device decrypted target despite missing room key"
        fresh_observations = [
            ObservedEvent(event_id, row["body"], room_id, row["decrypted"])
            for event_id, row in seen.items()
        ]
        fresh_decrypted = [
            o for o in fresh_observations if o.event_id == target_id and o.decrypted
        ]

        evidence = _collect_evidence(
            rig,
            event.event_id,
            body,
            outbox_rows,
            receipts,
            native_ref,
            fresh_observations,
        )
        reasons = evaluate_outbound_acceptance(evidence)
        assert "independent_decrypted_observation_missing" in reasons, (
            "a recipient without the room key must not produce a positive "
            f"verdict; got reasons={reasons}"
        )
        assert not fresh_decrypted, (
            "a fresh-store recipient decrypted content it has no room key "
            "for; the missing-key negative is not actually negative"
        )
    finally:
        try:
            if rig is not None:
                await _stop_rig(rig)
        finally:
            with contextlib.suppress(Exception):
                await close_nio_client(primary_client)
            if fresh_client is not None:
                with contextlib.suppress(Exception):
                    await close_nio_client(fresh_client)


# ---------------------------------------------------------------------------
# Negative: wrong-room observer
# ---------------------------------------------------------------------------


async def test_wrong_room_observer_cannot_yield_positive_verdict(
    synapse_e2ee_env: E2EETestEnvironment,
    peer_account: tuple[str, str],
    temp_storage: SQLiteStorage,
    tmp_path: Path,
) -> None:
    """A peer observing only a different encrypted room sees nothing.

    Both refusal layers are exercised: the wrong room genuinely never
    receives the event, and a fabricated cross-room observation is refused
    by the verdict's channel check.
    """
    peer_user_id, peer_password = peer_account

    # A second encrypted room the routed send never targets.
    other_room = _http_json(
        f"{synapse_e2ee_env.base_url}/_matrix/client/v3/createRoom",
        {"name": "MEDRE encrypted outbound wrong-room", "preset": "public_chat"},
        token=synapse_e2ee_env.bot_access_token,
        method="POST",
    )["room_id"]
    _http_json(
        f"{synapse_e2ee_env.base_url}/_matrix/client/v3/rooms/{other_room}"
        "/state/m.room.encryption",
        {"algorithm": "m.megolm.v1.aes-sha2"},
        token=synapse_e2ee_env.bot_access_token,
        method="PUT",
    )

    peer_store = str(tmp_path / "peer-store")
    Path(peer_store).mkdir(parents=True, exist_ok=True)
    peer_client = await _init_peer_client(
        synapse_e2ee_env, peer_user_id, peer_password, peer_store, other_room
    )
    rig: Rig | None = None
    try:
        rig = await _start_rig(synapse_e2ee_env, temp_storage, tmp_path)
        await _await_bot_knows_peer_device(rig, peer_user_id, peer_client.device_id)

        body = "medre encrypted outbound wrong-room negative body"
        event = await _admit_message(rig, "encrypted-outbound-evt-wrong-room", body)
        outbox_rows = await _await_outbox_terminal_sent(rig, event.event_id)
        receipts = await _attempt_receipts(rig, event.event_id)
        native_ref = await _outbound_native_ref(rig, event.event_id)

        # The peer only ever syncs the OTHER room.
        wrong_room_observations = await _collect_peer_observations(
            peer_client, other_room, rounds=4
        )
        evidence = _collect_evidence(
            rig,
            event.event_id,
            body,
            outbox_rows,
            receipts,
            native_ref,
            wrong_room_observations,
        )
        reasons = evaluate_outbound_acceptance(evidence)
        assert "independent_decrypted_observation_missing" in reasons, (
            "an observer in the wrong room must not produce a positive "
            f"verdict; got reasons={reasons}"
        )

        # Fabricated cross-room sighting: even if a decrypted copy existed
        # in the wrong room, the channel check refuses it.
        fabricated = ObservedEvent(
            event_id=native_ref.native_message_id,
            body=body,
            room_id=other_room,
            decrypted=True,
        )
        fabricated_evidence = _collect_evidence(
            rig,
            event.event_id,
            body,
            outbox_rows,
            receipts,
            native_ref,
            [fabricated],
        )
        fabricated_reasons = evaluate_outbound_acceptance(fabricated_evidence)
        assert "observation_room_mismatch" in fabricated_reasons, (
            "a decrypted sighting in the wrong room must be refused by the "
            f"channel check; got reasons={fabricated_reasons}"
        )
    finally:
        try:
            if rig is not None:
                await _stop_rig(rig)
        finally:
            with contextlib.suppress(Exception):
                await close_nio_client(peer_client)


# ---------------------------------------------------------------------------
# Negatives: wrong event id and stale attempt
# ---------------------------------------------------------------------------


async def test_wrong_event_id_and_stale_attempt_cannot_yield_positive_verdict(
    synapse_e2ee_env: E2EETestEnvironment,
    peer_account: tuple[str, str],
    temp_storage: SQLiteStorage,
    tmp_path: Path,
) -> None:
    """A wrong native event id or a stale foreign attempt refuses.

    Two deliveries produce two honest evidence generations. The verdict then
    refuses: an observation whose event id differs from the receipt's native
    id, and event B's acceptance attempted with event A's receipts, outbox
    generation and observation.
    """
    peer_user_id, peer_password = peer_account
    room_id = synapse_e2ee_env.encrypted_room_id
    peer_store = str(tmp_path / "peer-store")
    Path(peer_store).mkdir(parents=True, exist_ok=True)

    peer_client = await _init_peer_client(
        synapse_e2ee_env, peer_user_id, peer_password, peer_store, room_id
    )
    rig: Rig | None = None
    try:
        rig = await _start_rig(synapse_e2ee_env, temp_storage, tmp_path)
        await _await_bot_knows_peer_device(rig, peer_user_id, peer_client.device_id)

        async def _deliver_and_collect(
            event_id: str, body: str
        ) -> OutboundAcceptanceEvidence:
            event = await _admit_message(rig, event_id, body)
            outbox_rows = await _await_outbox_terminal_sent(rig, event.event_id)
            receipts = await _attempt_receipts(rig, event.event_id)
            native_ref = await _outbound_native_ref(rig, event.event_id)
            observed = await _await_peer_decrypted_body(peer_client, room_id, body)
            return _collect_evidence(
                rig, event.event_id, body, outbox_rows, receipts, native_ref, observed
            )

        evidence_a = await _deliver_and_collect(
            "encrypted-outbound-evt-a", "medre encrypted outbound stale negative body A"
        )
        assert (
            evaluate_outbound_acceptance(evidence_a) == []
        ), "the control delivery must be a clean positive verdict"

        evidence_b = await _deliver_and_collect(
            "encrypted-outbound-evt-b", "medre encrypted outbound stale negative body B"
        )
        assert (
            evaluate_outbound_acceptance(evidence_b) == []
        ), "the second delivery must be a clean positive verdict"

        # Negative 1 — wrong event id: the observed event id is replaced by
        # a different (real-looking) native id. The verdict must refuse even
        # though the decrypted body text matches.
        wrong_observation = ObservedEvent(
            event_id="$wrong-event-id-not-produced-by-medre",
            body=evidence_a.expected_body,
            room_id=room_id,
            decrypted=True,
        )
        wrong_id_evidence = OutboundAcceptanceEvidence(
            event_id=evidence_a.event_id,
            target_adapter=evidence_a.target_adapter,
            target_room_id=evidence_a.target_room_id,
            expected_body=evidence_a.expected_body,
            outbox_rows=evidence_a.outbox_rows,
            receipts=evidence_a.receipts,
            outbound_refs=evidence_a.outbound_refs,
            observations=(wrong_observation,),
        )
        wrong_id_reasons = evaluate_outbound_acceptance(wrong_id_evidence)
        assert "independent_decrypted_observation_missing" in wrong_id_reasons, (
            "an observation with a wrong event id must not produce a "
            f"positive verdict; got reasons={wrong_id_reasons}"
        )

        # Negative 2 — stale attempt: event B evaluated with event A's
        # receipts, outbox generation and observation.
        stale_evidence = OutboundAcceptanceEvidence(
            event_id=evidence_b.event_id,
            target_adapter=evidence_b.target_adapter,
            target_room_id=evidence_b.target_room_id,
            expected_body=evidence_b.expected_body,
            outbox_rows=evidence_a.outbox_rows,
            receipts=evidence_a.receipts,
            outbound_refs=evidence_a.outbound_refs,
            observations=evidence_a.observations,
        )
        stale_reasons = evaluate_outbound_acceptance(stale_evidence)
        assert stale_reasons, "a stale attempt must not produce a positive verdict"
        assert "receipt_belongs_to_foreign_event" in stale_reasons, (
            "a stale attempt receipt from another canonical event must "
            f"refuse the receipt binding; got reasons={stale_reasons}"
        )
        # The stale observation belongs to event A's generation: even when it
        # decrypt-matches its OWN native id, it cannot satisfy event B's body
        # expectation. Either the body or the identity check must refuse.
        assert "observation_body_mismatch" in stale_reasons, (
            "a stale observation from another canonical event must refuse "
            f"the body binding; got reasons={stale_reasons}"
        )
    finally:
        try:
            if rig is not None:
                await _stop_rig(rig)
        finally:
            with contextlib.suppress(Exception):
                await close_nio_client(peer_client)


# ---------------------------------------------------------------------------
# Startup/restart and key-store reuse
# ---------------------------------------------------------------------------


async def test_adapter_restart_on_same_key_store_still_delivers_and_decrypts(
    synapse_e2ee_env: E2EETestEnvironment,
    peer_account: tuple[str, str],
    temp_storage: SQLiteStorage,
    tmp_path: Path,
) -> None:
    """Restart the adapter on the same store; the next routed send decrypts.

    The nio crypto store is durable state: the reopened adapter keeps its
    olm account identity, the ledger keeps the first generation's facts, and
    an independent client decrypts a second post-restart delivery.
    """
    peer_user_id, peer_password = peer_account
    room_id = synapse_e2ee_env.encrypted_room_id
    peer_store = str(tmp_path / "peer-store")
    Path(peer_store).mkdir(parents=True, exist_ok=True)

    peer_client = await _init_peer_client(
        synapse_e2ee_env, peer_user_id, peer_password, peer_store, room_id
    )
    rig: Rig | None = None
    reopened_device_id: str | None = None
    try:
        rig = await _start_rig(synapse_e2ee_env, temp_storage, tmp_path)
        await _await_bot_knows_peer_device(rig, peer_user_id, peer_client.device_id)
        session_client = getattr(rig.adapter._session, "_client", None)
        pre_restart_device_id = getattr(session_client, "device_id", None)
        pre_restart_keys = _olm_identity_keys(session_client)

        body_a = "medre encrypted outbound pre-restart body"
        event_a = await _admit_message(rig, "encrypted-outbound-evt-restart-a", body_a)
        await _await_outbox_terminal_sent(rig, event_a.event_id)
        ref_a = await _outbound_native_ref(rig, event_a.event_id)
        await _await_peer_decrypted_body(peer_client, room_id, body_a)

        # Production stop paths; the retry worker must hold no retained work.
        await _stop_rig(rig)
        rig = None

        # Reopen on the SAME durable storage and key store.
        rig = await _start_rig(synapse_e2ee_env, temp_storage, tmp_path)
        reopened_session_client = getattr(rig.adapter._session, "_client", None)
        reopened_device_id = getattr(reopened_session_client, "device_id", None)
        assert (
            reopened_device_id == pre_restart_device_id
        ), "restart on the same store must preserve the device identity"
        assert (
            _olm_identity_keys(reopened_session_client) == pre_restart_keys
        ), "same device ID masked an Olm account identity replacement"

        body_b = "medre encrypted outbound post-restart body"
        event_b = await _admit_message(rig, "encrypted-outbound-evt-restart-b", body_b)
        outbox_rows_b = await _await_outbox_terminal_sent(rig, event_b.event_id)
        receipts_b = await _attempt_receipts(rig, event_b.event_id)
        ref_b = await _outbound_native_ref(rig, event_b.event_id)
        assert (
            ref_b.native_message_id != ref_a.native_message_id
        ), "the post-restart delivery must be a new native generation"
        observed_b = await _await_peer_decrypted_body(peer_client, room_id, body_b)
        evidence_b = _collect_evidence(
            rig, event_b.event_id, body_b, outbox_rows_b, receipts_b, ref_b, observed_b
        )
        assert evaluate_outbound_acceptance(evidence_b) == [], (
            "post-restart decrypt-and-deliver verdict refused: "
            f"{evaluate_outbound_acceptance(evidence_b)}"
        )

        # The pre-restart generation is still readable durable evidence.
        assert (
            await rig.storage.resolve_native_ref(
                adapter=rig.adapter_id,
                native_channel_id=room_id,
                native_message_id=ref_a.native_message_id,
            )
            == event_a.event_id
        )
    finally:
        try:
            if rig is not None:
                await _stop_rig(rig)
        finally:
            with contextlib.suppress(Exception):
                await close_nio_client(peer_client)


# ---------------------------------------------------------------------------
# Opt-in strict verification; unverified peer refuses
# ---------------------------------------------------------------------------


async def test_strict_verification_opt_in_refuses_unverified_then_delivers(
    synapse_e2ee_env: E2EETestEnvironment,
    peer_account: tuple[str, str],
    temp_storage: SQLiteStorage,
    tmp_path: Path,
) -> None:
    """Strict is opt-in: unverified peers refuse, verified peers decrypt.

    The refusal happens BEFORE any native send, so no receipt and no
    observation can exist for the refused attempt: an unverified peer cannot
    yield a positive verdict. The strict gate demands the COMPLETE recipient
    view (every known device of every room member), so this scenario runs in
    a dedicated encrypted room whose only members are the bot and the peer,
    and local verification covers every known device of both accounts.
    """
    peer_user_id, peer_password = peer_account
    peer_store = str(tmp_path / "peer-store")
    Path(peer_store).mkdir(parents=True, exist_ok=True)

    # Dedicated encrypted room: members are exactly the fixture bot and the
    # independent peer. The fixture room also contains the shared test user,
    # whose login-only devices never upload device keys and therefore can
    # never satisfy a strict complete view.
    room_id = _http_json(
        f"{synapse_e2ee_env.base_url}/_matrix/client/v3/createRoom",
        {"name": "MEDRE encrypted outbound strict room", "preset": "public_chat"},
        token=synapse_e2ee_env.bot_access_token,
        method="POST",
    )["room_id"]
    _http_json(
        f"{synapse_e2ee_env.base_url}/_matrix/client/v3/rooms/{room_id}"
        "/state/m.room.encryption",
        {"algorithm": "m.megolm.v1.aes-sha2"},
        token=synapse_e2ee_env.bot_access_token,
        method="PUT",
    )

    peer_client = await _init_peer_client(
        synapse_e2ee_env, peer_user_id, peer_password, peer_store, room_id
    )
    rig: Rig | None = None
    try:
        rig = await _start_rig(
            synapse_e2ee_env,
            temp_storage,
            tmp_path,
            target_room_id=room_id,
            verification_policy="strict",
        )
        await _await_bot_knows_peer_device(rig, peer_user_id, peer_client.device_id)

        # The unverified first attempt must refuse before sending.
        body_a = "medre encrypted outbound strict refused body"
        event_a = await _admit_message(rig, "encrypted-outbound-evt-strict-a", body_a)

        async def _any_outbox_row() -> bool:
            return bool(await _outbox_rows(rig, event_a.event_id))

        assert await wait_until(
            _any_outbox_row, timeout=_DELIVERY_TIMEOUT, interval=0.25
        ), "the refused attempt should still create its durable obligation"

        async def _strict_refusal_observed() -> bool:
            rows = await _outbox_rows(rig, event_a.event_id)
            refusals = rig.adapter._peer_device_trust_strict_refusals
            return (
                bool(rows)
                and refusals > 0
                and all(
                    row.attempt_number >= 1
                    and row.active_attempt is None
                    and row.status in ("retry_wait", "dead_lettered")
                    for row in rows
                )
            )

        assert await wait_until(
            _strict_refusal_observed, timeout=_DELIVERY_TIMEOUT, interval=0.25
        ), "strict gate never completed a refused dispatch attempt"
        assert not await rig.storage.list_native_refs_for_event(
            event_a.event_id
        ), "refused event reached native send path"
        refused_rows = await _outbox_rows(rig, event_a.event_id)
        assert all(row.status != "sent" for row in refused_rows)

        # Complete local verification: every known device of every room
        # member account (the bot's own sibling devices and every peer
        # account device the store learned, including earlier-session ones).
        session = rig.adapter._session
        client = getattr(session, "_client", None)
        device_store = getattr(client, "device_store", None)
        assert device_store is not None, "strict bot has no device store"
        verified_any = False
        for user_id in (synapse_e2ee_env.bot_user_id, peer_user_id):
            for device in list(device_store.active_user_devices(user_id)):
                assert client.verify_device(device) is True, (
                    f"local verification failed for {user_id} device "
                    f"{getattr(device, 'id', '?')}"
                )
                verified_any = True
        assert verified_any, "the strict bot's store held no devices to verify"

        body_b = "medre encrypted outbound strict verified body"
        event_b = await _admit_message(rig, "encrypted-outbound-evt-strict-b", body_b)
        outbox_rows = await _await_outbox_terminal_sent(rig, event_b.event_id)
        receipts = await _attempt_receipts(rig, event_b.event_id)
        native_ref = await _outbound_native_ref(rig, event_b.event_id)
        observed = await _await_peer_decrypted_body(peer_client, room_id, body_b)
        evidence = _collect_evidence(
            rig, event_b.event_id, body_b, outbox_rows, receipts, native_ref, observed
        )
        assert evaluate_outbound_acceptance(evidence) == [], (
            "verified strict delivery must satisfy the required verdict: "
            f"{evaluate_outbound_acceptance(evidence)}"
        )
    finally:
        try:
            if rig is not None:
                # Unsents remain durable, but no live dispatch may survive.
                await _stop_rig(rig)
        finally:
            with contextlib.suppress(Exception):
                await close_nio_client(peer_client)
