"""Focused delivery-gate tests for dynamic mutation binding suppression.

Proves the delivery integration (contract §3) through the real pipeline:

* a ``message.edited`` / ``message.deleted`` event whose destination
  relation fact is NOT ``bound_owned`` is SUPPRESSED before rendering and
  adapter dispatch — zero adapter calls, no fallback ordinary message, no
  sent receipt and no outbound native ref;
* the suppression evidence carries the stable dynamic reason code
  ``relation_target_not_bindable:<status>:<reason>`` (distinct from static
  ``capability_suppressed:`` capability reasons);
* a ``bound_owned`` mutation delivers normally;
* retry re-binds at execution time with the same authority and fails
  closed when the target has become ambiguous — retries never retarget;
* replay re-render (``render_replay_event``) re-binds and produces no
  rendering result for an unbound mutation context.

All adapters are the generic synthetic presentation fake — no SDK imports,
no platform conditionals.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent
from medre.core.events.canonical import DeliveryReceipt, EventRelation, NativeMessageRef
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Synthetic world (generic; no platform specifics)
# ---------------------------------------------------------------------------

DEST_ADAPTER = "presentation-dest"
DEST_CHANNEL = "dest-room-1"

#: Identity facts of the original author.  ``ORIGIN_ACTOR`` matches the
#: ``source_transport_id`` default of :func:`make_event` so the mutation
#: event built through that helper carries pairwise-equal identity facts.
ORIGIN_ADAPTER = "origin-feed"
ORIGIN_ACTOR = "node-1"

ORIGINAL_ID = "orig-evt-1"
MUTATION_ID = "mut-evt-1"


def _original_event() -> CanonicalEvent:
    """The stored original, authored by ORIGIN_* in DEST_CHANNEL."""
    return make_event(
        event_id=ORIGINAL_ID,
        event_kind="message.text",
        source_adapter=ORIGIN_ADAPTER,
        source_channel_id=DEST_CHANNEL,
        payload={"text": "original body"},
    )


def _native_ref(
    msg_id: str,
    *,
    direction: str = "outbound",
    channel: str | None = DEST_CHANNEL,
    event_id: str = ORIGINAL_ID,
) -> NativeMessageRef:
    return NativeMessageRef(
        id=f"nref-{event_id}-{channel}-{msg_id}-{direction}",
        event_id=event_id,
        adapter=DEST_ADAPTER,
        native_channel_id=channel,
        native_message_id=msg_id,
        native_thread_id=None,
        native_relation_id=None,
        direction=direction,  # type: ignore[arg-type]
    )


def _mutation_event(
    relation_type: str,
    *,
    event_kind: str = "message.edited",
) -> CanonicalEvent:
    """A mutation event carrying the same identity facts as the original."""
    relation = EventRelation(
        relation_type=relation_type,  # type: ignore[arg-type]
        target_event_id=ORIGINAL_ID,
        target_native_ref=None,
        key=None,
        fallback_text=None,
    )
    return make_event(
        event_id=MUTATION_ID,
        event_kind=event_kind,
        source_adapter=ORIGIN_ADAPTER,
        source_channel_id=DEST_CHANNEL,
        payload={"text": "mutated body"},
        relations=(relation,),
    )


def _route() -> Route:
    return Route(
        id="mutation-gate-route",
        source=RouteSource(
            adapter=ORIGIN_ADAPTER,
            event_kinds=("message.edited", "message.deleted"),
            channel=None,
        ),
        targets=[RouteTarget(adapter=DEST_ADAPTER, channel=DEST_CHANNEL)],
    )


def _plan(event: CanonicalEvent) -> DeliveryPlan:
    return DeliveryPlan(
        plan_id="plan:mutation-gate",
        event_id=event.event_id,
        target=RouteTarget(adapter=DEST_ADAPTER, channel=DEST_CHANNEL),
        primary_strategy=DeliveryStrategy(method="direct"),
    )


def _native_mutation_adapter() -> FakePresentationAdapter:
    adapter = FakePresentationAdapter(adapter_id=DEST_ADAPTER)
    # Generic presentation fake with native edit/delete support.
    adapter._capabilities = AdapterCapabilities(
        text=True,
        edits="native",
        deletes="native",
    )
    return adapter


def _make_runner(
    storage: SQLiteStorage,
    adapter: FakePresentationAdapter,
) -> PipelineRunner:
    config = make_pipeline_config_for_pipeline(
        storage=storage,
        router=Router(routes=[_route()]),
        adapters={DEST_ADAPTER: adapter},
    )
    return PipelineRunner(config)


async def _seed_original_and_refs(
    storage: SQLiteStorage,
    refs: list[NativeMessageRef],
) -> None:
    await storage.append(_original_event())
    for ref in refs:
        await storage.store_native_ref(ref)


# ---------------------------------------------------------------------------
# Suppression gate
# ---------------------------------------------------------------------------


class TestMutationSuppressionGate:
    async def test_ambiguous_target_suppresses_with_reason_and_no_adapter_call(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Two DISTINCT outbound copies in the destination ⇒ suppressed."""
        await _seed_original_and_refs(
            temp_storage,
            [_native_ref("out-msg-1"), _native_ref("out-msg-2")],
        )

        adapter = _native_mutation_adapter()
        runner = _make_runner(temp_storage, adapter)
        await runner.start()
        try:
            event = _mutation_event("edit")
            await temp_storage.append(event)

            receipt = await runner.deliver_to_target(event, _route(), _plan(event))

            assert receipt.status == "suppressed"
            assert receipt.receipt_kind == "lifecycle"
            assert receipt.failure_kind == "capability_suppressed"
            assert receipt.error is not None
            assert receipt.error.startswith("relation_target_not_bindable:ambiguous")
            # Zero adapter calls — no native mutation AND no fallback
            # ordinary message.
            assert adapter.delivered_payloads == []
            # No outbound native ref recorded for the mutation.
            refs = await temp_storage.list_native_refs_for_event(MUTATION_ID)
            assert refs == []
            # No sent attempt receipt — the only receipt is the suppressed
            # lifecycle evidence.
            receipts = await temp_storage.list_receipts_for_event(MUTATION_ID)
            assert [r.status for r in receipts] == ["suppressed"]
        finally:
            await runner.stop()

    async def test_inbound_only_target_suppresses(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await _seed_original_and_refs(
            temp_storage, [_native_ref("in-msg-1", direction="inbound")]
        )

        adapter = _native_mutation_adapter()
        runner = _make_runner(temp_storage, adapter)
        await runner.start()
        try:
            event = _mutation_event("edit")
            await temp_storage.append(event)

            receipt = await runner.deliver_to_target(event, _route(), _plan(event))

            assert receipt.status == "suppressed"
            assert receipt.error is not None
            assert receipt.error == (
                "relation_target_not_bindable:" "not_authorized:inbound_only_match"
            )
            assert adapter.delivered_payloads == []
        finally:
            await runner.stop()

    async def test_target_in_other_context_suppresses(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Target stored but zero refs in THIS destination ⇒ out_of_scope."""
        await _seed_original_and_refs(
            temp_storage, [_native_ref("out-msg-1", channel="other-room")]
        )

        adapter = _native_mutation_adapter()
        runner = _make_runner(temp_storage, adapter)
        await runner.start()
        try:
            event = _mutation_event("edit")
            await temp_storage.append(event)

            receipt = await runner.deliver_to_target(event, _route(), _plan(event))

            assert receipt.status == "suppressed"
            assert receipt.error is not None
            assert receipt.error.startswith("relation_target_not_bindable:out_of_scope")
            assert adapter.delivered_payloads == []
        finally:
            await runner.stop()

    async def test_missing_original_suppresses(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """Target canonical event not stored ⇒ unresolved, fail closed."""
        adapter = _native_mutation_adapter()
        runner = _make_runner(temp_storage, adapter)
        await runner.start()
        try:
            # The original is NOT stored. SQLite foreign keys make a truly
            # dangling native ref impossible, so "missing original" means no
            # original row and therefore no refs at all.
            event = _mutation_event("delete", event_kind="message.deleted")
            await temp_storage.append(event)

            receipt = await runner.deliver_to_target(event, _route(), _plan(event))

            assert receipt.status == "suppressed"
            assert receipt.error is not None
            assert receipt.error.startswith(
                "relation_target_not_bindable:unresolved_target"
            )
            assert adapter.delivered_payloads == []
        finally:
            await runner.stop()

    async def test_non_mutation_event_not_gated(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """A plain message with a reply relation is never mutation-gated."""
        await _seed_original_and_refs(temp_storage, [_native_ref("out-msg-1")])

        adapter = _native_mutation_adapter()
        runner = _make_runner(temp_storage, adapter)
        await runner.start()
        try:
            relation = EventRelation(
                relation_type="reply",
                target_event_id=ORIGINAL_ID,
                target_native_ref=None,
                key=None,
                fallback_text="original body",
            )
            event = make_event(
                event_id="plain-evt-1",
                event_kind="message.text",
                source_adapter=ORIGIN_ADAPTER,
                source_channel_id=DEST_CHANNEL,
                payload={"text": "hello"},
                relations=(relation,),
            )
            await temp_storage.append(event)

            receipt = await runner.deliver_to_target(event, _route(), _plan(event))

            # Referential delivery is not suppressed by the mutation gate.
            assert receipt.status == "sent"
            assert len(adapter.delivered_payloads) == 1
        finally:
            await runner.stop()


class TestBoundOwnedMutationDelivers:
    async def test_bound_owned_mutation_reaches_adapter(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Proven authorship + exactly one OUTBOUND copy ⇒ delivered."""
        await _seed_original_and_refs(temp_storage, [_native_ref("out-msg-1")])

        adapter = _native_mutation_adapter()
        runner = _make_runner(temp_storage, adapter)
        await runner.start()
        try:
            event = _mutation_event("edit")
            await temp_storage.append(event)

            receipt = await runner.deliver_to_target(event, _route(), _plan(event))

            assert receipt.status == "sent"
            assert len(adapter.delivered_payloads) == 1
            refs = await temp_storage.list_native_refs_for_event(MUTATION_ID)
            assert len(refs) == 1
            assert refs[0].direction == "outbound"
        finally:
            await runner.stop()


class TestRetryRebindsFailClosed:
    async def test_retry_after_target_becomes_ambiguous_suppresses(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A previously-owned target that became ambiguous suppresses."""
        await _seed_original_and_refs(temp_storage, [_native_ref("out-msg-1")])

        adapter = _native_mutation_adapter()
        runner = _make_runner(temp_storage, adapter)
        await runner.start()
        try:
            event = _mutation_event("edit")
            await temp_storage.append(event)

            first = await runner.deliver_to_target(event, _route(), _plan(event))
            assert first.status == "sent"
            assert len(adapter.delivered_payloads) == 1

            # A second DISTINCT outbound copy appears in the destination.
            await temp_storage.store_native_ref(_native_ref("out-msg-2"))

            retry = await runner.deliver_to_target(
                event,
                _route(),
                _plan(event),
                previous_receipt=first,
                source="retry",
            )

            # Re-bound at execution time: now ambiguous ⇒ suppressed.
            assert retry.status == "suppressed"
            assert retry.attempt_number == 2
            assert retry.parent_receipt_id == first.receipt_id
            assert retry.error is not None
            assert retry.error.startswith("relation_target_not_bindable:ambiguous")
            # No new adapter call — the retry never re-sent or retargeted.
            assert len(adapter.delivered_payloads) == 1
        finally:
            await runner.stop()


class TestReplayRerenderFailsClosed:
    async def _persist_render_context(
        self,
        storage: SQLiteStorage,
        event: CanonicalEvent,
    ) -> None:
        """Persist minimal rendering evidence so render_replay_event has a
        historical destination context to re-bind against."""
        evidence = json.dumps(
            {
                "delivery_strategy": "direct",
                "capability_level": "native",
                "target_platform": "fake_presentation",
                "max_text_chars": 500,
                "max_text_bytes": 1500,
                "source_origin_label": "Origin",
            }
        )
        await storage.append_receipt(
            DeliveryReceipt(
                receipt_id="rcpt-replay-ctx-1",
                event_id=event.event_id,
                delivery_plan_id="plan:mutation-gate",
                target_adapter=DEST_ADAPTER,
                target_channel=DEST_CHANNEL,
                route_id="mutation-gate-route",
                status="sent",
                attempt_number=1,
                rendering_evidence=evidence,
                created_at=datetime.now(timezone.utc),
            )
        )

    async def test_ambiguous_replay_rerender_produces_no_result(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await _seed_original_and_refs(
            temp_storage,
            [_native_ref("out-msg-1"), _native_ref("out-msg-2")],
        )

        adapter = _native_mutation_adapter()
        runner = _make_runner(temp_storage, adapter)
        await runner.start()
        try:
            event = _mutation_event("edit")
            await temp_storage.append(event)
            await self._persist_render_context(temp_storage, event)

            rendered = await runner.render_replay_event(event)

            assert rendered == []
        finally:
            await runner.stop()

    async def test_bound_owned_replay_rerender_still_renders(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await _seed_original_and_refs(temp_storage, [_native_ref("out-msg-1")])

        adapter = _native_mutation_adapter()
        runner = _make_runner(temp_storage, adapter)
        await runner.start()
        try:
            event = _mutation_event("edit")
            await temp_storage.append(event)
            await self._persist_render_context(temp_storage, event)

            rendered = await runner.render_replay_event(event)

            assert len(rendered) == 1
        finally:
            await runner.stop()
