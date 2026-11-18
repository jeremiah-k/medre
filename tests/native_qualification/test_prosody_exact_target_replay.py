# medre-test-tier: compatibility
"""Core-authorized exact-target replay observed by a real Prosody service.

The MEDRE side executes ``ReplayEngine`` through ``PipelineRunner`` and the
pinned ``XMPPAdapter`` (Slixmpp 1.17.0) against one disposable Docker Prosody
13.0 server. Two channels (MUC rooms) live on the SAME adapter instance, so a
pass cannot come from adapter-only filtering; each room carries an independent
raw-SDK recipient account that joined with ``maxstanzas=0`` before any send.

Scenario B-0 (positive control): one event is seeded through the normal
runtime — canonical admission via ``storage.append``, routing/planning via
``PipelineRunner.route_event`` — with the sibling target delivered live
through ``deliver_to_targets`` (a genuinely completed sibling) and the
selected target staged as a never-dispatched ``pending`` generation carrying
the runtime-produced dispatch snapshot. Durable storage is then closed and
reopened and the XMPP provider lifetime is retired and restarted before an
authorized ``ReplayRequest(BEST_EFFORT, exact_target_selections=...)`` runs.
The selected recipient must observe EXACTLY ONE nonce-correlated stanza
within a declared window, bound by room address, originating account, the
recorded origin-id, and the room stanza-id the provider returned to the
adapter; the completed excluded sibling must retain its identity with no
matching new stanza in its own window.

Scenario B-1 (negative control), against the same reopened storage and a
fresh provider lifetime: an unbound selection (no persisted generation), a
drifted selection (a channel no plan names), and a superseded observation
(captured before a competing generation exists) must all be refused, stage
no native work, and cause zero new native sends and no sibling changes.
Positive peer controls bracket the refusal window so witness liveness is
proven, and absence is a bounded ``wait_until`` timeout, never a fixed sleep.

Scope: local Docker Prosody, groupchat (MUC) channel shape, and the declared
observation windows only. The provider reports a local TLS stream handoff;
independent recipient reception is observed here through separate recipient
accounts, which is not end-to-end encrypted or external-network proof. No
uncertain native handoff is resolved or reauthorized by this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import msgspec
import pytest

from medre.adapters.xmpp.adapter import XMPPAdapter
from medre.adapters.xmpp.compat import HAS_SLIXMPP
from medre.adapters.xmpp.config import XMPPConfig
from medre.adapters.xmpp.connection import SlixConnection
from medre.adapters.xmpp.fake import FakeXMPPAdapter
from medre.adapters.xmpp.renderer import XMPPRenderer
from medre.adapters.xmpp.session import XMPPSession
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.engine.pipeline.retry_plan import OutboxDispatchContext
from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.types import ReplayMode, ReplayRequest, ReplayResult
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.events.metadata import RoutingMetadata
from medre.core.evidence.native_evidence import NativeEvidenceObservation
from medre.core.evidence.native_ledger_binding import (
    assess_native_evidence_ledger_binding,
)
from medre.core.observability.exact_target import (
    ExactTargetSelection,
    preview_exact_target,
)
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    stable_delivery_plan_id,
)
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from scripts.ci.native_qualification import qualification_rows
from tests.helpers.async_utils import wait_until
from tests.integration.conftest import (
    _RUN_ARTIFACT_DIR,
    ProsodyEnvironment,
    _write_artifact_json,
    _write_run_metadata,
)
from tests.integration.conftest import prosody_env as prosody_env

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not HAS_SLIXMPP, reason="requires pinned Slixmpp 1.17.0"),
]

_NATIVE = "prosody-exact-target"
_MONITOR = "prosody-exact-monitor"
_SOURCE = "prosody-exact-source"
_ROUTE = "prosody-exact-replay"
_NICKNAME = "medre"

# Declared observation windows (seconds). The recipient window bounds how
# long the expected stanza may take to arrive; the quiet window bounds how
# long absence is asserted before the window closes. Neither implies a
# lifetime guarantee beyond the window.
_RECIPIENT_WINDOW = 20.0
_QUIET_WINDOW = 8.0
_JOIN_TIMEOUT = 20.0

logger = logging.getLogger("tests.prosody_exact_target_replay")

_SID = "{urn:xmpp:sid:0}"
_CLIENT = "{jabber:client}"


def _c2s_endpoint() -> tuple[str, int]:
    """The fixture binds the mapped c2s port on loopback."""
    from tests.integration.conftest import _PROSODY_PORT

    return "localhost", _PROSODY_PORT


def _tls_context(env: ProsodyEnvironment) -> ssl.SSLContext:
    """Trust only the fixture's own throwaway CA for the local Prosody."""
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(env.ca_path))
    return context


class _Recipient:
    """One independent raw-SDK recipient account; never touches MEDRE state.

    The recipient is a separate Slixmpp client with its own account. Its
    verdict comes from the Prosody network round trip, never from MEDRE
    reading back its own receipts. Presence items (the rooms are provisioned
    non-anonymous) supply the occupant-to-account mapping used to attribute
    an observed stanza to its originating XMPP account.
    """

    def __init__(self, env: ProsodyEnvironment, localpart: str, resource: str) -> None:
        from slixmpp import ClientXMPP

        self.account = env.jid(localpart)
        self.client = ClientXMPP(f"{self.account}/{resource}", env.password(localpart))
        self.client.ssl_context = _tls_context(env)
        self.client.register_plugin("xep_0030")
        self.client.register_plugin("xep_0045")
        self.stanzas: list[dict[str, Any]] = []
        self.occupants: dict[str, str] = {}
        self.client.add_event_handler("message", self._on_message)

    async def join(self, room: str, nick: str) -> None:
        host, port = _c2s_endpoint()
        await self.client.connect(host, port)
        await asyncio.wait_for(self.client.wait_until("session_start"), _JOIN_TIMEOUT)
        self.client.send_presence()
        # Slixmpp's muc plugin consumes occupant presence after the join, so
        # occupant-to-account attribution listens on the plugin's per-room
        # event rather than the generic stream presence event.
        self.client.add_event_handler(f"muc::{room}::presence", self._on_muc_presence)
        await self.client.plugin["xep_0045"].join_muc_wait(
            room, nick, maxstanzas=0, timeout=_JOIN_TIMEOUT
        )

    async def configure_room(self, room: str) -> None:
        """Submit persistent/open/non-anonymous config as the creating owner."""
        form = await self.client.plugin["xep_0045"].get_room_config(room)
        form["type"] = "submit"
        form["values"] = {
            **form["values"],
            "muc#roomconfig_persistentroom": True,
            "muc#roomconfig_membersonly": False,
            "muc#roomconfig_whois": "anyone",
        }
        await self.client.plugin["xep_0045"].set_room_config(room, form)

    async def _on_message(self, message: Any) -> None:
        xml = message.xml
        if xml.get("type") != "groupchat":
            return
        body = xml.find(f"{_CLIENT}body")
        origin = xml.find(f"{_SID}origin-id")
        self.stanzas.append(
            {
                "arrival": time.monotonic(),
                "from": xml.get("from", ""),
                "room": xml.get("from", "").rpartition("/")[0],
                "body": body.text if body is not None else None,
                "origin_id": origin.get("id") if origin is not None else None,
                "stanza_ids": {
                    entry.get("by"): entry.get("id")
                    for entry in xml.findall(f"{_SID}stanza-id")
                },
                "delayed": xml.find("{urn:xmpp:delay}delay") is not None,
            }
        )
        if len(self.stanzas) > 1000:
            del self.stanzas[:-1000]

    def _on_muc_presence(self, presence: Any) -> None:
        sender = str(presence["from"])
        real_jid = presence["muc"]["item"]["jid"]
        if real_jid:
            self.occupants[sender] = str(real_jid).split("/")[0]

    def matches(
        self,
        body: str,
        room: str,
        *,
        since: float,
    ) -> list[dict[str, Any]]:
        """Groupchat stanzas with exactly this body, room, and arrival window."""
        return [
            record
            for record in self.stanzas
            if record["body"] == body
            and record["room"] == room
            and not record["delayed"]
            and record["arrival"] >= since
        ]

    def originating_account(self, record: dict[str, Any]) -> str | None:
        return self.occupants.get(record["from"])

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.client.disconnect(), 10)
        except Exception:
            logger.warning("Prosody recipient teardown left its client running")
            self.client.abort()


class _RecordingConnection(SlixConnection):
    """Observe native transport writes without changing send semantics."""

    def __init__(self, config: XMPPConfig, receive: Any) -> None:
        super().__init__(config, receive)
        self.sends: list[dict[str, Any]] = []

    async def send(self, payload: Mapping[str, str]) -> str | None:
        writes: list[str] = []
        client_send_raw = self.client.send_raw

        def capture_raw(data: Any) -> None:
            if isinstance(data, bytes):
                writes.append(data.decode("utf-8", "replace"))
            elif isinstance(data, str):
                writes.append(data)
            client_send_raw(data)

        self.client.send_raw = capture_raw
        try:
            outcome = await super().send(payload)
        finally:
            del self.client.send_raw
        origin_id = None
        for raw in writes:
            if "<message" not in raw or "origin-id" not in raw:
                continue
            # Slixmpp serializes with the stream's namespace context, so the
            # standalone fragment can carry unbound prefixes; correlate the
            # recorded outbound write by its XEP-0359 origin id attribute.
            match = re.search(r"<[\w:.-]*origin-id[^>]*\bid=['\"]([^'\"]+)['\"]", raw)
            if match:
                origin_id = match.group(1)
        self.sends.append(
            {
                "to": payload.get("to"),
                "body": payload.get("body"),
                "origin_id": origin_id,
                "native_id": outcome,
            }
        )
        return outcome


class _Case:
    """Assemble, seed, reopen, and replay one disposable Prosody campaign."""

    def __init__(
        self,
        env: ProsodyEnvironment,
        path: Path,
        *,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.env = env
        self.path = path
        self.monkeypatch = monkeypatch
        self.run = uuid.uuid4().hex[:8]
        self.selected_room = env.room_jid("sel-" + self.run)
        self.sibling_room = env.room_jid("sib-" + self.run)
        self.monitor_room = env.room_jid("monitor")
        self.route = Route(
            id=_ROUTE,
            source=RouteSource(
                adapter=_SOURCE, channel=None, event_kinds=("message.created",)
            ),
            targets=[
                RouteTarget(adapter=_NATIVE, channel=self.selected_room),
                RouteTarget(adapter=_NATIVE, channel=self.sibling_room),
                RouteTarget(adapter=_MONITOR, channel=self.monitor_room),
            ],
        )
        self.event_id = ""
        self.body = ""
        self._selected_plan: DeliveryPlan | None = None
        self._sibling_plan: DeliveryPlan | None = None
        self.sibling_outbox_id: str | None = None
        self.staged_outbox_id: str | None = None
        self._storage: SQLiteStorage | None = None
        self._runner: PipelineRunner | None = None
        self._adapter: XMPPAdapter | None = None
        self._monitor: FakeXMPPAdapter | None = None
        self._connections: list[_RecordingConnection] = []
        self._observers: list[_Recipient] = []
        self.observer_a: _Recipient | None = None
        self.observer_b: _Recipient | None = None

    @property
    def storage(self) -> SQLiteStorage:
        assert self._storage is not None, "case storage is not open"
        return self._storage

    @property
    def runner(self) -> PipelineRunner:
        assert self._runner is not None, "case pipeline is not started"
        return self._runner

    @property
    def adapter(self) -> XMPPAdapter:
        assert self._adapter is not None, "case adapter is not started"
        return self._adapter

    @property
    def monitor(self) -> FakeXMPPAdapter:
        assert self._monitor is not None, "case monitor is not started"
        return self._monitor

    @property
    def selected_plan(self) -> DeliveryPlan:
        assert self._selected_plan is not None, "case is not seeded"
        return self._selected_plan

    @property
    def selection(self) -> ExactTargetSelection:
        return ExactTargetSelection(
            event_id=self.event_id,
            delivery_plan_id=self.selected_plan.plan_id,
            target_adapter=_NATIVE,
            target_channel=self.selected_room,
        )

    def selection_for(self, event_id: str, *, channel: str) -> ExactTargetSelection:
        """Build a selection identity for a refusal or unbound case."""
        return ExactTargetSelection(
            event_id=event_id,
            delivery_plan_id=stable_delivery_plan_id(
                event_id,
                RouteTarget(adapter=_NATIVE, channel=self.selected_room),
                route_id=self.route.id,
                target_index=0,
            ),
            target_adapter=_NATIVE,
            target_channel=channel,
        )

    def _native_config(self) -> XMPPConfig:
        return XMPPConfig(
            adapter_id=_NATIVE,
            jid=self.env.jid("medre-bot") + "/bridge",
            password=self.env.password("medre-bot"),
            rooms=(self.selected_room, self.sibling_room),
            nickname=_NICKNAME,
            host="localhost",
            port=_c2s_endpoint()[1],
            relay_prefix="",
        ).validate()

    def _monitor_config(self) -> XMPPConfig:
        return XMPPConfig(
            adapter_id=_MONITOR,
            jid="monitor@" + self.env.domain + "/monitor",
            rooms=(self.monitor_room,),
            nickname=_NICKNAME,
        ).validate()

    def _session_factory(
        self, config: XMPPConfig, receive: Any, **options: Any
    ) -> XMPPSession:
        def connection_factory(*args: Any, **kwargs: Any) -> _RecordingConnection:
            connection = _RecordingConnection(*args, **kwargs)
            self._connections.append(connection)
            return connection

        return XMPPSession(
            config, receive, connection_factory=connection_factory, **options
        )

    def _assemble(
        self, storage: SQLiteStorage
    ) -> tuple[XMPPAdapter, FakeXMPPAdapter, PipelineRunner]:
        native_config = self._native_config()
        adapter = XMPPAdapter(native_config, session_factory=self._session_factory)
        monitor = FakeXMPPAdapter(_MONITOR, self._monitor_config())
        rendering = RenderingPipeline()
        rendering.register(
            XMPPRenderer(configs={_NATIVE: native_config, _MONITOR: monitor.config}),
            priority=50,
        )
        rendering.register(TextRenderer(), priority=100)
        runner = PipelineRunner(
            PipelineConfig(
                storage=storage,
                router=Router(routes=[self.route]),
                fallback_resolver=FallbackResolver(),
                relation_resolver=RelationResolver(storage=storage),
                adapters={_NATIVE: adapter, _MONITOR: monitor},
                rendering_pipeline=rendering,
                runtime_accounting=RuntimeAccounting(),
                event_bus=EventBus(),
            )
        )
        return adapter, monitor, runner

    def _context(self, adapter_id: str, runner: PipelineRunner) -> AdapterContext:
        return AdapterContext(
            adapter_id=adapter_id,
            publish_inbound=runner.handle_ingress,
            report_delivery_feedback=runner._record_delivery_feedback,
            logger=logging.getLogger(f"tests.prosody_exact_target.{adapter_id}"),
            clock=lambda: datetime.now(UTC),
            shutdown_event=asyncio.Event(),
        )

    async def arm(self) -> None:
        """Provision rooms, then join independent recipients before any send."""
        for room in (self.selected_room, self.sibling_room):
            provisioner = _Recipient(self.env, "admin", "provision-" + self.run)
            try:
                await provisioner.join(room, "provisioner")
                await provisioner.configure_room(room)
            finally:
                await provisioner.close()
        for localpart, room, nick, slot in (
            ("observer-a", self.selected_room, "observer-a", "a"),
            ("observer-b", self.sibling_room, "observer-b", "b"),
        ):
            observer = _Recipient(self.env, localpart, "campaign-" + self.run)
            await observer.join(room, nick)
            self._observers.append(observer)
            if slot == "a":
                self.observer_a = observer
            else:
                self.observer_b = observer

    async def start_lifetime(self) -> None:
        """Open storage and start one pipeline/provider lifetime."""
        # The adapter builds its own ssl.create_default_context(); OpenSSL
        # reads SSL_CERT_FILE when default verify paths are loaded.
        self.monkeypatch.setenv("SSL_CERT_FILE", str(self.env.ca_path))
        storage = SQLiteStorage(str(self.path))
        await storage.initialize()
        adapter, monitor, runner = self._assemble(storage)
        await runner.start()
        try:
            await adapter.start(self._context(_NATIVE, runner))
        except BaseException:
            await runner.stop()
            await storage.close()
            raise
        try:
            await monitor.start(self._context(_MONITOR, runner))
        except BaseException:
            try:
                await adapter.stop()
            finally:
                await runner.stop()
                await storage.close()
            raise
        self._storage = storage
        self._runner = runner
        self._adapter = adapter
        self._monitor = monitor

    async def seed(self, *, event_id: str, body: str) -> None:
        """Seed one event through the normal runtime boundary.

        Boundary documentation: the canonical event is admitted through
        ``storage.append``; routing and planning run through the real
        ``PipelineRunner.route_event``; the sibling target is delivered LIVE
        through ``deliver_to_targets(source="live")`` so its outbox
        generation completes with real native evidence; the selected target
        is staged as a never-dispatched ``pending`` generation through
        ``storage.create_outbox_item`` carrying the runtime-produced
        dispatch snapshot. No replay path participates in seeding.
        """
        self.event_id = event_id
        self.body = body
        storage = self.storage
        runner = self.runner
        event = _make_event(event_id, body)
        await storage.append(event)
        _, plan_pairs = await runner.route_event(event)
        assert len(plan_pairs) == 3, "one plan per route target is required"
        for route, plan in plan_pairs:
            assert route.id == self.route.id
            target = (plan.target.adapter, plan.target.channel)
            if target == (_NATIVE, self.selected_room):
                self._selected_plan = plan
            elif target == (_NATIVE, self.sibling_room):
                self._sibling_plan = plan
        assert self._selected_plan is not None
        assert self._sibling_plan is not None

        sibling_outcomes = await runner.deliver_to_targets(
            event, [(self.route, self._sibling_plan)], source="live"
        )
        assert len(sibling_outcomes) == 1
        sibling = sibling_outcomes[0]
        assert sibling.receipt is not None, "sibling live send must produce a receipt"
        assert sibling.receipt.status == "sent"
        assert sibling.receipt.target_channel == self.sibling_room

        sibling_row = await self._find_outbox_row(storage, self._sibling_plan.plan_id)
        assert sibling_row is not None
        self.sibling_outbox_id = sibling_row.outbox_id

        self.staged_outbox_id = "staged-" + self.run
        created = await storage.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id=self.staged_outbox_id,
                event_id=event_id,
                route_id=self.route.id,
                delivery_plan_id=self._selected_plan.plan_id,
                target_adapter=_NATIVE,
                target_channel=self.selected_room,
                status="pending",
                attempt_number=0,
            ),
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="staged",
                context=msgspec.to_builtins(
                    OutboxDispatchContext(
                        route=self.route,
                        plan=self._selected_plan,
                        routing=RoutingMetadata(
                            matched_routes=(self.route.id,),
                            route_trace=(self.route.id,),
                        ),
                    )
                ),
            ),
        )
        assert created.outbox_id == self.staged_outbox_id

    async def _find_outbox_row(
        self, storage: SQLiteStorage, delivery_plan_id: str
    ) -> Any | None:
        rows = await storage.list_outbox_items_for_event(self.event_id)
        for row in rows:
            if row.delivery_plan_id == delivery_plan_id:
                return row
        return None

    async def deliver_live(self, event_id: str, body: str) -> None:
        """Deliver a fresh control event to both XMPP rooms through the runtime."""
        runner = self.runner
        event = _make_event(event_id, body)
        await self.storage.append(event)
        _, plan_pairs = await runner.route_event(event)
        xmpp_pairs = [
            (route, plan)
            for route, plan in plan_pairs
            if plan.target.adapter == _NATIVE
        ]
        assert len(xmpp_pairs) == 2
        outcomes = await runner.deliver_to_targets(event, xmpp_pairs, source="live")
        assert len(outcomes) == 2
        for outcome in outcomes:
            assert outcome.status == "success", outcome.error
            assert outcome.receipt is not None
            assert outcome.receipt.status == "sent"

    async def reopen(self) -> None:
        """Close durable storage, reopen it, and start a fresh provider lifetime."""
        await self._stop_lifetime()
        await self.storage.close()
        self._storage = None
        self._runner = None
        self._adapter = None
        self._monitor = None
        await self.start_lifetime()

    async def _stop_lifetime(self) -> None:
        adapter, monitor, runner = self._adapter, self._monitor, self._runner
        try:
            if adapter is not None:
                await adapter.stop()
        finally:
            try:
                if monitor is not None:
                    await monitor.stop()
            finally:
                if runner is not None:
                    await runner.stop()

    def sends_since_lifetime(self, lifetime: int) -> list[dict[str, Any]]:
        """Native transport writes recorded from the given lifetime onward."""
        connections_before = lifetime - 1
        return [
            send
            for connection in self._connections[connections_before:]
            for send in connection.sends
        ]

    async def replay(self, selection: ExactTargetSelection) -> list[ReplayResult]:
        """Request the scoped replay through the durable exact-target authority."""
        engine = ReplayEngine(storage=self.storage, pipeline=self.runner)
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[selection.event_id],
            run_id="prosody-d1-" + uuid.uuid4().hex[:8],
            exact_target_selections=(selection,),
        )
        return [result async for result in engine.replay(request)]

    async def wait_for_recipient(
        self,
        observer: _Recipient,
        *,
        body: str,
        room: str,
        since: float,
        count: int = 1,
    ) -> list[dict[str, Any]]:
        assert await wait_until(
            lambda: len(observer.matches(body, room, since=since)) >= count,
            timeout=_RECIPIENT_WINDOW,
        ), f"recipient saw {observer.stanzas!r} within {_RECIPIENT_WINDOW}s"
        return observer.matches(body, room, since=since)

    async def assert_quiet(
        self,
        observer: _Recipient,
        *,
        body: str,
        room: str,
        since: float,
    ) -> None:
        """Absence holds only while a bounded wait times out (witness armed)."""
        assert not await wait_until(
            lambda: observer.matches(body, room, since=since),
            timeout=_QUIET_WINDOW,
        ), f"recipient unexpectedly observed {observer.stanzas!r}"

    async def aclose(self) -> None:
        await self._stop_lifetime()
        if self._storage is not None:
            await self._storage.close()
            self._storage = None
        for observer in self._observers:
            await observer.close()


@asynccontextmanager
async def _case(
    env: ProsodyEnvironment,
    path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Case]:
    case = _Case(env, path, monkeypatch=monkeypatch)
    try:
        await case.arm()
        await case.start_lifetime()
        yield case
    finally:
        await case.aclose()


def _single_deliver_result(event_id: str, results: list[ReplayResult]) -> ReplayResult:
    deliver_results = [result for result in results if result.stage == "deliver"]
    assert len(deliver_results) == 1, (
        f"expected exactly one deliver result for {event_id}, got "
        f"{len(deliver_results)}"
    )
    return deliver_results[0]


async def _ledger_observation(
    storage: SQLiteStorage,
    receipt: Any,
    *,
    native_message_id: str,
) -> tuple[NativeEvidenceObservation, Any]:
    """Bind the independent Prosody observation to the durable attempt."""
    manifest = qualification_rows(
        Path(__file__).resolve().parents[2],
        candidate_refs=os.environ.get("MEDRE_CI_CANDIDATE_REFS"),
    )
    observation = NativeEvidenceObservation(
        event_id=receipt.event_id,
        delivery_plan_id=receipt.delivery_plan_id,
        target_adapter=receipt.target_adapter,
        target_channel=receipt.target_channel,
        outbox_id=receipt.outbox_id,
        attempt_number=receipt.attempt_number,
        observed_row_updated_at=None,
        provider="xmpp",
        provider_repository="jeremiah-k/medre-adapter-xmpp",
        compatibility_pin=next(
            row["ref"] for row in manifest if row["transport"] == "xmpp"
        ),
        observation_source="prosody_independent_recipient",
        tier="docker",
        scope="provider_acceptance",
        observed_at=datetime.now(UTC),
        result="accepted",
        receipt_id=receipt.receipt_id,
        native_message_id=native_message_id,
    )
    binding = await assess_native_evidence_ledger_binding(storage, observation)
    return observation, binding


def _make_event(event_id: str, body: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter=_SOURCE,
        source_transport_id="prosody-proof",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": body},
        metadata=EventMetadata(),
    )


@dataclass
class _RefusalProbe:
    selection: ExactTargetSelection
    body: str


async def test_exact_target_replay_reaches_one_prosody_recipient(
    prosody_env: ProsodyEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-0: one authorized replay send, one independent recipient, one sibling.

    The intended recipient must see exactly one nonce-correlated groupchat
    stanza from the originating account in the selected room over the
    declared window; the completed excluded sibling retains its identity
    with no matching new stanza in its own window; the monitor sibling
    never widens; and the provider observation binds to the durable ledger.
    """
    async with _case(
        prosody_env, tmp_path / "medre.sqlite", monkeypatch=monkeypatch
    ) as case:
        assert case.observer_a is not None and case.observer_b is not None
        event_id = "prosody-replay-" + uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        body = f"MEDRE prosody exact-target replay nonce={nonce}"
        await case.seed(event_id=event_id, body=body)

        selection = case.selection
        before = await preview_exact_target(case.storage, selection)
        assert before.category == "pending", before.category
        assert before.excluded_sibling_count == 1
        assert case.staged_outbox_id is not None
        staged = await case.storage.get_outbox_item(case.staged_outbox_id)
        assert staged is not None and staged.status == "pending"
        assert staged.attempt_number == 0 and staged.active_attempt is None
        assert case.sibling_outbox_id is not None
        sibling_before = await case.storage.get_outbox_item(case.sibling_outbox_id)
        assert sibling_before is not None and sibling_before.status == "sent"
        sibling_receipts_before = [
            receipt
            for receipt in await case.storage.list_receipts_for_event(event_id)
            if receipt.target_channel == case.sibling_room
        ]
        assert len(sibling_receipts_before) == 1
        sibling_native_before = sibling_receipts_before[0].adapter_message_id
        assert sibling_native_before, "the live sibling send returned a stanza id"
        # The independent sibling recipient observed that completed delivery.
        sibling_records = await case.wait_for_recipient(
            case.observer_b, body=body, room=case.sibling_room, since=0.0
        )
        assert len(sibling_records) == 1
        assert (
            sibling_records[0]["stanza_ids"].get(case.sibling_room)
            == sibling_native_before
        )

        await case.reopen()

        reopened = await preview_exact_target(case.storage, selection)
        assert reopened.category == "pending"
        assert reopened.observed_fingerprint == before.observed_fingerprint

        window_start = time.monotonic()
        results = await case.replay(selection)
        deliver = _single_deliver_result(event_id, results)
        assert deliver.status == "passed", deliver.error
        assert deliver.output is not None
        assert deliver.output["replay"] is True
        assert "exact_target_refusals" not in deliver.output
        outcomes = deliver.output["adapter_results"]
        assert len(outcomes) == 1, "one exact target must never widen"
        outcome = outcomes[0]
        assert outcome.target_adapter == _NATIVE
        assert outcome.status == "success", outcome.error
        receipt = outcome.receipt
        assert receipt is not None
        # Full identity checks: event, plan, adapter, channel.
        assert receipt.status == "sent"
        assert receipt.source == "replay"
        assert receipt.event_id == event_id
        assert receipt.delivery_plan_id == selection.delivery_plan_id
        assert receipt.target_adapter == _NATIVE
        assert receipt.target_channel == case.selected_room
        assert receipt.attempt_number == 1
        assert (
            receipt.outbox_id != case.staged_outbox_id
        ), "replay allocates a fresh generation over the staged obligation"
        native_id = receipt.adapter_message_id
        assert native_id, "the trusted room must return the provider stanza id"

        observed = await case.wait_for_recipient(
            case.observer_a,
            body=body,
            room=case.selected_room,
            since=window_start,
        )
        assert len(observed) == 1, "exactly one nonce-correlated stanza is allowed"
        record = observed[0]
        assert record["from"] == f"{case.selected_room}/{_NICKNAME}"
        assert (
            record["stanza_ids"].get(case.selected_room) == native_id
        ), "the provider's returned identity must match the recipient stanza-id"
        assert record["origin_id"] is not None
        sends = case.sends_since_lifetime(2)
        assert len(sends) == 1
        assert sends[0]["to"] == case.selected_room
        assert sends[0]["body"] == body
        assert sends[0]["native_id"] == native_id
        assert (
            sends[0]["origin_id"] == record["origin_id"]
        ), "the recorded origin id must equal the stanza the recipient saw"
        originating = case.observer_a.originating_account(record)
        assert originating == case.env.jid("medre-bot"), (
            f"stanza originated from {originating!r}; "
            f"occupants={case.observer_a.occupants!r}"
        )

        observation, binding = await _ledger_observation(
            case.storage, receipt, native_message_id=native_id
        )
        assert (binding.state, binding.reason) == (
            "current",
            "exact",
        ), f"provider fact must name the current attempt, got {binding.state}"
        assert binding.evidence_fingerprint == observation.evidence_fingerprint
        assert observation.resolution == "accepted"

        # Excluded sibling: identity retained, nothing new in its own window.
        await case.assert_quiet(
            case.observer_b, body=body, room=case.sibling_room, since=window_start
        )
        sibling_after = await case.storage.get_outbox_item(case.sibling_outbox_id)
        assert sibling_after is not None
        assert sibling_after.status == "sent"
        assert sibling_after.attempt_number == sibling_before.attempt_number
        sibling_receipts_after = [
            receipt
            for receipt in await case.storage.list_receipts_for_event(event_id)
            if receipt.target_channel == case.sibling_room
        ]
        assert [r.receipt_id for r in sibling_receipts_after] == [
            sibling_receipts_before[0].receipt_id
        ]
        assert len(case.monitor.delivered_payloads) == 0

        final = await preview_exact_target(case.storage, selection)
        assert final.category == "completed"
        # The completed sibling row and the superseded staged generation are
        # both excluded from the selected replay generation.
        assert final.excluded_sibling_count == 2
        staged_after = await case.storage.get_outbox_item(case.staged_outbox_id)
        assert staged_after is not None
        assert staged_after.status == "pending"
        assert staged_after.attempt_number == 0 and staged_after.active_attempt is None

        if _RUN_ARTIFACT_DIR is not None:
            _write_artifact_json(
                "prosody-exact-target-replay.json",
                {
                    "scenario": "prosody_exact_target_positive_replay",
                    "transport": "xmpp",
                    "evidence_level": "docker_independent_recipient",
                    "event_id": event_id,
                    "replay_run_id": receipt.replay_run_id,
                    "attempt_number": receipt.attempt_number,
                    "native_message_id": native_id,
                    "origin_id": record["origin_id"],
                    "room_stanza_id": record["stanza_ids"].get(case.selected_room),
                    "originating_account": originating,
                    "observation_window_seconds": _RECIPIENT_WINDOW,
                    "quiet_window_seconds": _QUIET_WINDOW,
                    "sibling_new_stanzas": 0,
                    "monitor_deliveries": len(case.monitor.delivered_payloads),
                    "native_evidence_fingerprint": observation.evidence_fingerprint,
                },
            )
            _write_run_metadata(
                scenario="prosody_exact_target_replay",
                containers={"prosody": case.env.container_name},
                storage_path=str(case.path),
                extras={"event_id": event_id},
            )


async def test_refused_and_superseded_selections_stay_unsent(
    prosody_env: ProsodyEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-1: unbound, drifted, and superseded selections never stage a send.

    Every refused selection is evaluated against reopened storage with a
    fresh provider lifetime. Positive peer controls bracket the refusal
    window so each witness is proven live; absence is a bounded timeout.
    """
    async with _case(
        prosody_env, tmp_path / "medre.sqlite", monkeypatch=monkeypatch
    ) as case:
        assert case.observer_a is not None and case.observer_b is not None
        event_id = "prosody-refused-" + uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        body = f"MEDRE prosody exact-target refusal nonce={nonce}"
        await case.seed(event_id=event_id, body=body)
        selection = case.selection
        before = await preview_exact_target(case.storage, selection)
        assert before.category == "pending"

        unbound_event = "prosody-unbound-" + uuid.uuid4().hex
        unbound_body = f"MEDRE prosody unbound selection nonce={uuid.uuid4().hex}"
        await case.storage.append(_make_event(unbound_event, unbound_body))
        ghost_room = case.env.room_jid("ghost-" + case.run)
        probes = {
            "unbound": _RefusalProbe(
                case.selection_for(unbound_event, channel=case.selected_room),
                unbound_body,
            ),
            "drifted": _RefusalProbe(
                case.selection_for(event_id, channel=ghost_room), body
            ),
        }

        await case.reopen()
        reopened = await preview_exact_target(case.storage, selection)
        assert reopened.category == "pending"
        assert reopened.observed_fingerprint == before.observed_fingerprint

        # Positive peer control BEFORE the refusal window: a fresh live
        # delivery through the normal runtime reaches both rooms.
        control_before_body = f"MEDRE prosody witness-before nonce={uuid.uuid4().hex}"
        window_start = time.monotonic()
        await case.deliver_live(
            "prosody-witness-before-" + uuid.uuid4().hex, control_before_body
        )
        await case.wait_for_recipient(
            case.observer_a,
            body=control_before_body,
            room=case.selected_room,
            since=window_start,
        )
        await case.wait_for_recipient(
            case.observer_b,
            body=control_before_body,
            room=case.sibling_room,
            since=window_start,
        )
        sibling_snapshot = await case.storage.get_outbox_item(case.sibling_outbox_id)
        assert sibling_snapshot is not None
        sibling_receipt_ids_before = [
            receipt.receipt_id
            for receipt in await case.storage.list_receipts_for_event(event_id)
            if receipt.target_channel == case.sibling_room
        ]
        assert len(sibling_receipt_ids_before) == 1

        deliver_results: dict[str, ReplayResult] = {}
        for name, probe in probes.items():
            results = await case.replay(probe.selection)
            deliver_results[name] = _single_deliver_result(
                probe.selection.event_id, results
            )

        unbound = deliver_results["unbound"]
        assert unbound.status == "skipped"
        assert unbound.error is not None
        assert unbound.error.startswith(
            "exact_target_refused: ambiguous"
        ), unbound.error
        unbound_refusals = unbound.output["exact_target_refusals"]
        assert len(unbound_refusals) == 1
        assert unbound_refusals[0]["category"] == "ambiguous"
        assert unbound_refusals[0]["target_adapter"] == _NATIVE

        drifted = deliver_results["drifted"]
        assert drifted.status == "skipped"
        assert drifted.error is not None
        assert "No delivery plans matched" in drifted.error, drifted.error

        # Superseded observation: the pre-competition preview is stale
        # evidence. A competing generation makes the identity contested; a
        # fresh durable reread must refuse it, expectations or not.
        await case.storage.create_outbox_item(
            DeliveryOutboxItem(
                outbox_id="staged-competing-" + case.run,
                event_id=event_id,
                route_id=case.route.id,
                delivery_plan_id=selection.delivery_plan_id,
                target_adapter=_NATIVE,
                target_channel=case.selected_room,
                status="pending",
                attempt_number=1,
            ),
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="staged",
                context=msgspec.to_builtins(
                    OutboxDispatchContext(
                        route=case.route,
                        plan=case.selected_plan,
                        routing=RoutingMetadata(
                            matched_routes=(case.route.id,),
                            route_trace=(case.route.id,),
                        ),
                    )
                ),
            ),
        )
        superseded = await preview_exact_target(
            case.storage,
            selection,
            expected_fingerprint=before.observed_fingerprint,
            expected_generation=before.observed_generation,
        )
        assert superseded.category == "ambiguous", superseded.category
        fresh = await preview_exact_target(case.storage, selection)
        assert fresh.category == "ambiguous"
        results = await case.replay(selection)
        superseded_deliver = _single_deliver_result(event_id, results)
        assert superseded_deliver.status == "skipped"
        assert superseded_deliver.error is not None
        assert superseded_deliver.error.startswith(
            "exact_target_refused: ambiguous"
        ), superseded_deliver.error

        # Zero new native sends and no staged work for refused identities.
        sends = case.sends_since_lifetime(2)
        assert [send["to"] for send in sends] == [
            case.selected_room,
            case.sibling_room,
        ], sends
        assert {send["body"] for send in sends} == {control_before_body}
        assert not await case.storage.list_receipts_for_event(unbound_event)
        assert await case.storage.list_outbox_items_for_event(unbound_event) == []
        staged_row = await case.storage.get_outbox_item(case.staged_outbox_id)
        assert staged_row is not None and staged_row.status == "pending"
        assert staged_row.attempt_number == 0 and staged_row.active_attempt is None
        sibling_now = await case.storage.get_outbox_item(case.sibling_outbox_id)
        assert sibling_now is not None
        assert sibling_now.status == sibling_snapshot.status
        assert sibling_now.attempt_number == sibling_snapshot.attempt_number
        sibling_receipt_ids_now = [
            receipt.receipt_id
            for receipt in await case.storage.list_receipts_for_event(event_id)
            if receipt.target_channel == case.sibling_room
        ]
        assert sibling_receipt_ids_now == sibling_receipt_ids_before
        assert not any(
            receipt.target_adapter == _NATIVE
            and receipt.target_channel == case.selected_room
            for receipt in await case.storage.list_receipts_for_event(event_id)
        )
        for observer, room in (
            (case.observer_a, case.selected_room),
            (case.observer_b, case.sibling_room),
        ):
            for probe in probes.values():
                await case.assert_quiet(
                    observer, body=probe.body, room=room, since=window_start
                )
        assert len(case.monitor.delivered_payloads) == 0

        # Positive peer control AFTER the refusal window: each witness is
        # still live, so the absence above cannot be an observer outage.
        control_after_body = f"MEDRE prosody witness-after nonce={uuid.uuid4().hex}"
        witness_since = time.monotonic()
        await case.deliver_live(
            "prosody-witness-after-" + uuid.uuid4().hex, control_after_body
        )
        await case.wait_for_recipient(
            case.observer_a,
            body=control_after_body,
            room=case.selected_room,
            since=witness_since,
        )
        await case.wait_for_recipient(
            case.observer_b,
            body=control_after_body,
            room=case.sibling_room,
            since=witness_since,
        )
        for observer, room in (
            (case.observer_a, case.selected_room),
            (case.observer_b, case.sibling_room),
        ):
            for probe in probes.values():
                assert observer.matches(probe.body, room, since=window_start) == []

        if _RUN_ARTIFACT_DIR is not None:
            _write_artifact_json(
                "prosody-exact-target-refusals.json",
                {
                    "scenario": "prosody_exact_target_refusal_probes",
                    "transport": "xmpp",
                    "evidence_level": "docker_refusal_with_live_witness",
                    "refusals": {
                        name: {
                            "status": deliver_results[name].status,
                            "error": (deliver_results[name].error or "")[:120],
                        }
                        for name in ("unbound", "drifted")
                    },
                    "superseded_refusal": {
                        "status": superseded_deliver.status,
                        "error": (superseded_deliver.error or "")[:120],
                    },
                    "new_native_sends_during_window": 0,
                    "witness_events": 2,
                    "observation_window_seconds": _QUIET_WINDOW,
                },
            )
            _write_run_metadata(
                scenario="prosody_exact_target_replay",
                containers={"prosody": case.env.container_name},
                storage_path=str(case.path),
                extras={"event_id": event_id},
            )
