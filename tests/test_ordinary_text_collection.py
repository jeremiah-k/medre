"""The ordinary routed text vertical is provably collectible end to end.

One normal text event, admitted through the production admission path and
fanned out by the durable ingress worker through the real
``OutboxManager.create_for_delivery`` staging, can finish all target work,
release only its classified canonical body rows through the existing guarded
collector, preserve every immutable identity and recovery fact, and
afterwards answer mediated content reads from the closed
retained/unavailable/denied vocabulary without fabricating text.

The demonstration sequence:

1. admit a normal routed message and stage two sibling targets through the
   real machinery (no manual outbox fixtures);
2. settle target A; the pending sibling B blocks collection and the refusal
   names B's live owner;
3. after a storage restart and a mutated route configuration, B finalizes
   through the real dispatcher from its persisted accepted snapshot — never
   current configuration — via the actual native/receipt transition;
4. delivery facts, native references, attempt lineage, typed routing facts,
   and conversation identity survive collection unchanged; the retired work
   snapshot is unrecoverable as content;
5. preview then explicit collection is idempotent across restarts, reports
   ``already_collected`` afterwards, keeps terminal actions unresendable, and
   every report stays schema-conformant and content-free;
6. opaque native metadata, another event's incoming quote, a shared
   attachment, a radio packet plan, historical inline outbox evidence, and an
   unresolved native handoff each keep blocking independently.

The default policy stays retain; apply is an explicit operator action and
never claims removal from diagnostics, external providers, logs, WAL,
backups, or peers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import msgspec

from medre.cli.storage_commands import _storage_collect
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.pipeline.retry_plan import reconstruct_retry_delivery_plan
from medre.core.events import EventMetadata, EventRelation, NativeRef
from medre.core.events.metadata import NativeMetadata, RoutingMetadata
from medre.core.ingress import DurableIngressWorker
from medre.core.routing import RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem, OutboxDispatchSnapshot
from medre.core.storage.read_contracts import AuthorizedEventRead
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.initial_outbox import _pipeline
from tests.helpers.pipeline import make_event

_BODY = "ordinary-text-body"
_MUTATED_ORIGIN_LABEL = "replacement label"
_MUTATED_CHANNEL = "mutated-channel"
_EVENT_ID = "ordinary-vertical"
_QUOTE = "incoming-sensitive-quote"
_INLINE_TRACE = "INLINE-LEGACY-TRACE"

_SCHEMAS = Path(__file__).resolve().parents[1] / "docs/schemas"


def _text_event(
    event_id: str = _EVENT_ID,
    *,
    metadata: EventMetadata | None = None,
    payload: dict | None = None,
) -> object:
    """A normal message with core routing metadata and a separated body.

    An explicitly empty payload keeps the canonical envelope at ``{}`` with a
    bodyless admission identity; the shared factory's default is bypassed so
    the empty envelope stays empty.
    """
    event = make_event(
        event_id=event_id,
        source_adapter="source",
        payload={"body": _BODY},
        source_native_ref=NativeRef(
            adapter="source",
            native_channel_id="ch-0",
            native_message_id=f"native-{event_id}",
        ),
    )
    routing = (
        metadata.routing
        if metadata is not None and metadata.routing is not None
        else RoutingMetadata(matched_routes=("mapping",))
    )
    carried = EventMetadata() if metadata is None else msgspec.structs.replace(metadata)
    return msgspec.structs.replace(
        event,
        payload={"body": _BODY} if payload is None else payload,
        metadata=msgspec.structs.replace(carried, routing=routing),
    )


async def _admit_and_stage(
    store: SQLiteStorage, event_id: str = _EVENT_ID, **event_kwargs: object
) -> tuple[dict[str, DeliveryOutboxItem], PipelineRunner]:
    """Admit through the real pipeline and stage both targets for delivery.

    The durable ingress worker fans the event out through
    ``DeliveryCoordinator.enqueue_many``, so both outbox rows and their frozen
    work snapshots are written by the production staging path.
    """
    runner, _, _ = _pipeline(store)
    event = _text_event(event_id, **event_kwargs)
    await runner.admit_ingress(event, "live")
    ingress = DurableIngressWorker(storage=store, pipeline=runner, batch_size=1)
    assert await ingress.run_once() == 1
    rows = await store.list_outbox_items_for_event(event_id)
    by_adapter = {row.target_adapter: row for row in rows}
    assert set(by_adapter) == {"radio", "matrix"}
    assert all(row.status == "pending" for row in by_adapter.values())
    assert all(row.attempt_number == 0 for row in by_adapter.values())
    assert all(row.metadata == {} for row in by_adapter.values())
    return by_adapter, runner


async def _settle_first_target(store: SQLiteStorage, rows: dict) -> None:
    """Settle the radio target through its real claim and terminal transition."""
    claimed = await store.claim_due_outbox_items(
        datetime.now(timezone.utc).isoformat(),
        "worker",
        excluded_adapters=("matrix",),
    )
    assert [item.outbox_id for item in claimed] == [rows["radio"].outbox_id]
    assert await store.mark_outbox_sent(rows["radio"].outbox_id)


async def _dispatch_pending_through_real_worker(
    store: SQLiteStorage, runner: PipelineRunner
) -> None:
    """Run one real initial-dispatch batch; no sleeps, no synthetic receipts."""
    dispatcher = RetryWorker(store, runner, None, initial_dispatch=True, enabled=False)
    await dispatcher._process_due(datetime.now(timezone.utc))


async def _run_vertical_to_settled(
    path: str, event_id: str = _EVENT_ID, **event_kwargs: object
) -> SQLiteStorage:
    """Drive the whole vertical to full settlement across a real restart.

    Target A settles through its guarded claim transition; target B settles
    through the real dispatcher after the storage handle is reopened, using
    its persisted accepted snapshot. The returned handle stays open; the
    caller closes it.
    """
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        rows, _ = await _admit_and_stage(store, event_id, **event_kwargs)
        await _settle_first_target(store, rows)
    finally:
        await store.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        runner, _, _ = _pipeline(reopened)
        await _dispatch_pending_through_real_worker(reopened, runner)
        settled = await reopened.list_outbox_items_for_event(event_id)
        assert all(row.status == "sent" for row in settled)
    except BaseException:
        await reopened.close()
        raise
    return reopened


async def _snapshot_of(
    store: SQLiteStorage, item: DeliveryOutboxItem
) -> OutboxDispatchSnapshot:
    snapshot = await store.get_outbox_dispatch_context(
        item.outbox_id,
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
    )
    assert snapshot is not None
    return snapshot


async def _retired_snapshot(store: SQLiteStorage, item: DeliveryOutboxItem) -> None:
    assert (
        await store.get_outbox_dispatch_context(
            item.outbox_id,
            event_id=item.event_id,
            delivery_plan_id=item.delivery_plan_id,
            target_adapter=item.target_adapter,
            target_channel=item.target_channel,
        )
        is None
    )


def _validate_collection_report(report: dict) -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads((_SCHEMAS / "event-content-collection.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)


def _validate_batch_report(report: dict) -> None:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    batch = json.loads((_SCHEMAS / "content-collection-batch.schema.json").read_text())
    event = json.loads((_SCHEMAS / "event-content-collection.schema.json").read_text())
    Draft202012Validator.check_schema(batch)
    Draft202012Validator.check_schema(event)
    registry = Registry().with_resource(event["$id"], Resource.from_contents(event))
    Draft202012Validator(batch, registry=registry).validate(report)


async def _body_rows(store: SQLiteStorage, event_id: str) -> int:
    row = await store._read_one(
        "SELECT COUNT(*) AS n FROM event_body_content WHERE event_id=?", (event_id,)
    )
    return int(row["n"])


# ---------------------------------------------------------------------------
# Steps 1-2: real staging; a pending sibling blocks and names its owner
# ---------------------------------------------------------------------------


async def test_pending_sibling_blocks_collection_and_names_its_live_owner(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "sibling.db"))
    await store.initialize()
    try:
        rows, _ = await _admit_and_stage(store)
        await _settle_first_target(store, rows)
        blocked = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == [
            "active_outbox",
            "unsettled_mandatory_owners",
            "unretired_outbox_snapshots",
        ]
        assert blocked["rows_removed"] == 0
        _validate_collection_report(blocked)
        # The refusal identifies the pending sibling's live owner: one active
        # outbox row (target B) holding one unretired frozen snapshot.
        assessment = await store.assess_event_content_release(_EVENT_ID)
        assert assessment["status"] == "blocked"
        counts = {copy["surface"]: copy["rows"] for copy in assessment["copies"]}
        assert counts["active_outbox"] == 1
        assert counts["unretired_outbox_snapshots"] == 1
        holder = await store.get_outbox_item(rows["matrix"].outbox_id)
        assert holder is not None and holder.status == "pending"
        # Nothing was removed and no collection receipt was written.
        assert await _body_rows(store, _EVENT_ID) == 1
        assert not await store._read_one(
            "SELECT 1 FROM event_content_collection_log WHERE event_id=?",
            (_EVENT_ID,),
        )
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Step 3: restart + mutated route configuration; B finalizes from its
# persisted snapshot through the real native/receipt transition
# ---------------------------------------------------------------------------


async def test_pending_target_finalizes_from_its_frozen_snapshot_after_restart(
    tmp_path,
) -> None:
    path = str(tmp_path / "restart-dispatch.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        rows, _ = await _admit_and_stage(store)
        target_b = rows["matrix"]
        await _settle_first_target(store, rows)
        # The staged snapshot carries the accepted work context, including
        # opaque origin labels that must become unrecoverable after retirement.
        staged = await _snapshot_of(store, target_b)
        assert staged.kind == "staged"
        assert staged.context["route"]["source"]["origin_label"] == "Original mesh"
        accepted = reconstruct_retry_delivery_plan(
            item=target_b,
            previous_receipt=None,
            default_max_attempts=1,
            dispatch_snapshot=staged,
        )
        accepted_route = msgspec.json.encode(accepted.route)
        accepted_plan = msgspec.json.encode(accepted.plan)
    finally:
        await store.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        runner, route, adapters = _pipeline(reopened)
        # The restarted process carries a mutated route configuration that
        # would plan a different obligation; the frozen snapshot wins.
        route.source = RouteSource(
            "replacement-source", ("message.created",), None, _MUTATED_ORIGIN_LABEL
        )
        route.targets = [RouteTarget("matrix", _MUTATED_CHANNEL)]
        stored_b = await reopened.get_outbox_item(target_b.outbox_id)
        assert stored_b is not None and stored_b.status == "pending"
        reconstructed = reconstruct_retry_delivery_plan(
            item=stored_b,
            previous_receipt=None,
            default_max_attempts=1,
            dispatch_snapshot=await _snapshot_of(reopened, stored_b),
        )
        assert msgspec.json.encode(reconstructed.route) == accepted_route
        assert msgspec.json.encode(reconstructed.plan) == accepted_plan
        assert reconstructed.plan.target.channel == "out"
        assert route.targets[0].channel == _MUTATED_CHANNEL

        await _dispatch_pending_through_real_worker(reopened, runner)
        final = await reopened.get_outbox_item(target_b.outbox_id)
        assert final is not None
        assert final.status == "sent" and final.attempt_number == 1
        receipts = await reopened.list_receipts_for_outbox(target_b.outbox_id)
        assert len(receipts) == 1
        assert receipts[0].attempt_number == 1
        assert receipts[0].source == "live"
        assert receipts[0].target_channel == "out"
        assert receipts[0].route_id == "mapping"
        # The real adapter received the delivery exactly once, on the frozen
        # plan's channel, and the settled snapshot retired with the transition.
        assert len(adapters["matrix"].delivered_payloads) == 1
        assert not adapters["radio"].delivered_payloads
        await _retired_snapshot(reopened, final)
        assessment = await reopened.assess_event_content_release(_EVENT_ID)
        assert assessment["blockers"] == []
        assert assessment["core_subset_status"] == "eligible"
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Step 4: immutable facts survive collection; retired context is not content
# ---------------------------------------------------------------------------


async def test_collected_vertical_preserves_facts_and_hides_retired_context(
    tmp_path,
) -> None:
    path = str(tmp_path / "facts.db")
    store = await _run_vertical_to_settled(path)
    try:
        rows = await store.list_outbox_items_for_event(_EVENT_ID)
        receipts = await store.list_receipts_for_outbox(rows[1].outbox_id)
        native_refs = await store.count_native_refs()
        metadata = await store.get_metadata(_EVENT_ID)
        assert metadata is not None
        assert metadata.conversation_id is not None
        matched_routes = await store._read_one(
            "SELECT matched_routes FROM event_routing_facts WHERE event_id=?",
            (_EVENT_ID,),
        )
        assert matched_routes is not None
        raw_metadata = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?", (_EVENT_ID,)
        )
        assert raw_metadata is not None
        dependencies = await store.list_mandatory_content_dependencies(_EVENT_ID)
        assert dependencies
        assert all(released for _, _, _, released in dependencies)

        applied = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert applied["status"] == "collected"
        assert applied["rows_removed"] == 1

        # Immutable delivery identity, attempt lineage, native references,
        # typed routing facts, and conversation identity are unchanged.
        assert await store.list_outbox_items_for_event(_EVENT_ID) == rows
        assert await store.list_receipts_for_outbox(rows[1].outbox_id) == receipts
        assert await store.count_native_refs() == native_refs
        after_routes = await store._read_one(
            "SELECT matched_routes FROM event_routing_facts WHERE event_id=?",
            (_EVENT_ID,),
        )
        assert after_routes is not None
        assert after_routes["matched_routes"] == matched_routes["matched_routes"]
        after_metadata = await store.get_metadata(_EVENT_ID)
        assert after_metadata is not None
        assert after_metadata.conversation_id == metadata.conversation_id
        assert after_metadata.metadata.routing is not None
        assert after_metadata.metadata.routing.matched_routes == ("mapping",)
        assert json.loads(raw_metadata["metadata"]) == {}
        assert (
            await store.list_mandatory_content_dependencies(_EVENT_ID) == dependencies
        )
        # The retired work snapshots are absent, not empty, and the opaque
        # origin label frozen inside them is recoverable as neither content
        # nor metadata.
        for row in rows:
            await _retired_snapshot(store, row)
        composed = json.dumps(msgspec.to_builtins(after_metadata))
        assert "Original mesh" not in composed
        assert _BODY not in composed
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Step 5: preview, explicit apply, idempotence across restart, honest reads
# ---------------------------------------------------------------------------


async def test_vertical_collection_is_idempotent_and_unresendable(tmp_path) -> None:
    path = str(tmp_path / "operator.db")
    store = await _run_vertical_to_settled(path)
    try:
        rows = await store.list_outbox_items_for_event(_EVENT_ID)
        assessment = await store.assess_event_content_release(_EVENT_ID)
        assert assessment["status"] == "unmeasured"
        assert assessment["core_subset_status"] == "eligible"
        assert assessment["classified_core_candidate"] is True
        assert assessment["blockers"] == []
        assert assessment["deletion_authorized"] is False

        preview = await store.collect_event_content(_EVENT_ID)
        assert preview["status"] == "dry_run"
        assert preview["candidate_rows"] == 1
        assert preview["candidate_logical_bytes"] == len(_BODY)
        assert preview["rows_removed"] == 0
        _validate_collection_report(preview)
        assert _BODY not in json.dumps(preview)
        assert await _body_rows(store, _EVENT_ID) == 1

        applied = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert applied["status"] == "collected"
        assert applied["rows_removed"] == 1
        assert applied["logical_bytes_removed"] == len(_BODY)
        _validate_collection_report(applied)
        assert _BODY not in json.dumps(applied)
        assert await _body_rows(store, _EVENT_ID) == 0
    finally:
        await store.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        repeat = await reopened.collect_event_content(_EVENT_ID, dry_run=False)
        assert repeat["status"] == "already_collected"
        assert repeat["rows_removed"] == 0
        _validate_collection_report(repeat)
        assert await _body_rows(reopened, _EVENT_ID) == 0
        rows = await reopened.list_outbox_items_for_event(_EVENT_ID)
        # Mediated reads answer from the closed vocabulary once content is
        # released: authority is gone, so no read fabricates or leaks text.
        for row in rows:
            read = await reopened.read_event_for_outbox(_EVENT_ID, row.outbox_id)
            assert read == AuthorizedEventRead("denied")
        replay = await reopened.read_event_for_replay(_EVENT_ID)
        assert replay == AuthorizedEventRead("denied")
        body = await reopened.read_body_for_consumer(_EVENT_ID, "history")
        assert body.status == "denied" and body.body is None
        # No terminal action becomes resendable after collection.
        for row in rows:
            assert not await reopened.authorize_native_submission(
                _EVENT_ID, row.outbox_id, worker_id="worker", attempt_number=1
            )
            assert not await reopened.mark_outbox_cancelled(row.outbox_id)
    finally:
        await reopened.close()


async def test_cli_batch_reports_the_vertical_labels_and_stays_content_free(
    tmp_path, capsys
) -> None:
    path = str(tmp_path / "cli.db")
    store = await _run_vertical_to_settled(path)
    await store.close()

    await _storage_collect(path)
    retained = json.loads(capsys.readouterr().out)
    _validate_batch_report(retained)
    assert retained["mode"] == "retain"
    assert retained["dry_run"] is True
    assert retained["limit"] <= 100
    assert retained["results"][0]["status"] == "retained_by_policy"

    await _storage_collect(path, mode="relay-only")
    preview = json.loads(capsys.readouterr().out)
    _validate_batch_report(preview)
    assert preview["dry_run"] is True
    assert preview["results"][0]["status"] == "dry_run"
    assert preview["rows_removed"] == 0

    await _storage_collect(path, mode="relay-only", apply=True, yes=True)
    applied = json.loads(capsys.readouterr().out)
    _validate_batch_report(applied)
    assert applied["dry_run"] is False
    assert applied["results"][0]["status"] == "collected"
    assert applied["results"][0]["rows_removed"] == 1

    await _storage_collect(path, mode="relay-only", apply=True, yes=True)
    repeat = json.loads(capsys.readouterr().out)
    _validate_batch_report(repeat)
    assert repeat["results"][0]["status"] == "already_collected"

    for report in (retained, preview, applied, repeat):
        assert report["physical_erasure_claimed"] is False
        assert report["unmeasured"]
        assert _BODY not in json.dumps(report)
        assert "Original mesh" not in json.dumps(report)


async def test_bodyless_routed_event_reads_unavailable_and_collects_empty(
    tmp_path,
) -> None:
    """Known-absent content stays honest on the same real vertical.

    An admitted event without a body never fabricates empty text: its
    authorized outbox body read reports ``unavailable`` while the target is
    live, and collection reports ``empty`` instead of writing a receipt for
    bytes that never existed.
    """
    store = SQLiteStorage(str(tmp_path / "bodyless.db"))
    await store.initialize()
    try:
        rows, runner = await _admit_and_stage(store, "bodyless", payload={})
        target = rows["radio"]
        read = await store.read_body_for_outbox("bodyless", target.outbox_id)
        assert read.status == "unavailable" and read.body is None
        await _dispatch_pending_through_real_worker(store, runner)
        settled = await store.list_outbox_items_for_event("bodyless")
        assert all(row.status == "sent" for row in settled)
        preview = await store.collect_event_content("bodyless")
        assert preview["status"] == "empty"
        assert preview["candidate_rows"] == 0
        applied = await store.collect_event_content("bodyless", dry_run=False)
        assert applied["status"] == "empty"
        assert applied["rows_removed"] == 0
        again = await store.collect_event_content("bodyless", dry_run=False)
        assert again["status"] == "empty"
        for row in settled:
            post = await store.read_event_for_outbox("bodyless", row.outbox_id)
            assert post == AuthorizedEventRead("denied")
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Step 6: each independently introduced copy family keeps blocking
# ---------------------------------------------------------------------------


async def test_opaque_native_metadata_blocks_the_settled_vertical(tmp_path) -> None:
    path = str(tmp_path / "native-opaque.db")
    store = await _run_vertical_to_settled(
        path,
        metadata=EventMetadata(
            routing=RoutingMetadata(matched_routes=("mapping",)),
            native=NativeMetadata(data={"provider_field": "opaque-native-blob"}),
        ),
    )
    try:
        blocked = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert blocked["status"] == "blocked"
        # The opaque namespace blocks the canonical envelope, and the same
        # admission honestly propagated it into the inbound native reference.
        assert "opaque_envelope" in blocked["blockers"]
        assert "native_references" in blocked["blockers"]
        assert "opaque-native-blob" not in json.dumps(blocked)
        assert await _body_rows(store, _EVENT_ID) == 1
    finally:
        await store.close()


async def test_incoming_quote_owned_by_another_event_blocks_collection(
    tmp_path,
) -> None:
    path = str(tmp_path / "incoming-quote.db")
    store = await _run_vertical_to_settled(path)
    try:
        runner, _, _ = _pipeline(store)
        reply = make_event(event_id="later-reply", source_adapter="source")
        relation = EventRelation("reply", _EVENT_ID, None, None, _QUOTE)
        reply = msgspec.structs.replace(reply, relations=(relation,))
        await runner.admit_ingress(reply, "live")
        blocked = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == ["incoming_relation_excerpts"]
        assert _QUOTE not in json.dumps(blocked)
        assert await _body_rows(store, _EVENT_ID) == 1
    finally:
        await store.close()


async def test_shared_attachment_and_radio_plan_surfaces_still_block(tmp_path) -> None:
    """Each introduction alone blocks; the collector never expands its scope."""
    path = str(tmp_path / "attachments-radio.db")
    store = await _run_vertical_to_settled(path)
    try:
        rows = await store.list_outbox_items_for_event(_EVENT_ID)
        # A shared attachment copy blocks through the association; the second
        # referring event is admitted through the real storage append path.
        sibling = make_event(event_id="attachment-sibling", payload={})
        await store.append(sibling)
        shared_ref = "sha256:" + "c" * 64
        await store._write(
            "INSERT INTO attachment_blobs "
            "(content_ref,size_bytes,media_kind,mime_type,created_at,data) "
            "VALUES (?, 3, NULL, NULL, '2026-10-31T00:00:00+00:00', ?)",
            (shared_ref, b"abc"),
        )
        await store._write(
            "INSERT INTO event_attachment_associations "
            "(event_id,content_ref,created_at) VALUES "
            "(?, ?, '2026-10-31T00:00:00+00:00'), "
            "('attachment-sibling', ?, '2026-10-31T00:00:00+00:00')",
            (_EVENT_ID, shared_ref, shared_ref),
        )
        blocked = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == ["attachment_associations"]
        await store._write("DELETE FROM event_attachment_associations")
        await store._write("DELETE FROM attachment_blobs")
        # ...and an immutable radio packet plan blocks under its own policy.
        await store._write(
            "INSERT INTO radio_attempt_progress "
            "(outbox_id,attempt_number,plan_fingerprint,plan_json,provenance_json,"
            "parts_json,revision,created_at,updated_at) "
            "SELECT outbox_id, 1, ?, '{}', '{}', '{}', 0, "
            "'2026-10-31T00:00:00+00:00', '2026-10-31T00:00:00+00:00' "
            "FROM delivery_outbox WHERE outbox_id=?",
            ("a" * 64, rows[0].outbox_id),
        )
        blocked = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == ["radio_plans"]
        await store._write("DELETE FROM radio_attempt_progress")
        applied = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert applied["status"] == "collected"
    finally:
        await store.close()


async def test_historical_inline_outbox_evidence_blocks_even_after_retirement(
    tmp_path,
) -> None:
    """Inline legacy bytes keep the outbox family probe active for the row."""
    path = str(tmp_path / "inline-legacy.db")
    store = await _run_vertical_to_settled(path)
    try:
        rows = await store.list_outbox_items_for_event(_EVENT_ID)
        # The row's frozen snapshot retired with settlement; the introduced
        # inline bytes are the only remaining content surface.
        await _retired_snapshot(store, rows[0])
        await store._write(
            "UPDATE delivery_outbox SET metadata=? WHERE outbox_id=?",
            (json.dumps({"trace": _INLINE_TRACE}), rows[0].outbox_id),
        )
        blocked = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == ["outbox_records"]
        assert _INLINE_TRACE not in json.dumps(blocked)
        assert await _body_rows(store, _EVENT_ID) == 1
        await store._write(
            "UPDATE delivery_outbox SET metadata='{}' WHERE outbox_id=?",
            (rows[0].outbox_id,),
        )
        applied = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert applied["status"] == "collected"
    finally:
        await store.close()


async def test_unresolved_native_handoff_keeps_its_snapshot_and_blocks(
    tmp_path,
) -> None:
    """A queued obligation never retires its snapshot or becomes collectible."""
    store = SQLiteStorage(str(tmp_path / "handoff.db"))
    await store.initialize()
    try:
        rows, _ = await _admit_and_stage(store)
        target_b = rows["matrix"]
        claimed = await store.claim_due_outbox_items(
            datetime.now(timezone.utc).isoformat(),
            "worker",
            excluded_adapters=("radio",),
        )
        assert [item.outbox_id for item in claimed] == [target_b.outbox_id]
        assert await store.mark_outbox_queued(target_b.outbox_id)
        # The unresolved handoff's frozen context is intact and blocking.
        snapshot = await _snapshot_of(store, target_b)
        assert snapshot.kind == "staged"
        blocked = await store.collect_event_content(_EVENT_ID, dry_run=False)
        assert blocked["status"] == "blocked"
        assert "active_outbox" in blocked["blockers"]
        assert "unretired_outbox_snapshots" in blocked["blockers"]
        assert await _body_rows(store, _EVENT_ID) == 1
        held = await store.get_outbox_item(target_b.outbox_id)
        assert held is not None and held.status == "queued"
    finally:
        await store.close()
