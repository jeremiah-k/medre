"""Routed events become collectable only after every target settles.

The headline guarantee: one admitted event fanned out to two destinations
blocks collection while any target lifecycle is live, names that live owner
in the refusal, and collects through the ordinary guarded path once every
target reaches a terminal state. Retained durable evidence — delivery
receipts, observations, outbox records, native references, ingress work and
the separated diagnostics — survives collection, and a family blocks again
the moment its registry content-potential columns carry a value.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.core.events import DeliveryObservation, DeliveryReceipt, NativeMessageRef
from medre.core.storage.backend import DeliveryOutboxItem, StorageError
from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

_NOW = "2026-10-31T00:00:00+00:00"
_TARGET_A_ADAPTER = "fake_presentation"
_TARGET_B_ADAPTER = "relay_presentation"
_BODY = "private-routed-body"


def _event(event_id="routed"):
    return msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": _BODY}, metadata={}
    )


def _inbound_ref(event_id, message_id="native-1"):
    return NativeMessageRef(
        id=f"nref-{event_id}",
        event_id=event_id,
        adapter="fake_transport",
        native_channel_id="ch-0",
        native_message_id=message_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
    )


def _target(event_id, name):
    return DeliveryOutboxItem(
        outbox_id=f"obox-{event_id}-{name}",
        event_id=event_id,
        route_id="route-1",
        delivery_plan_id=f"plan-{name}",
        target_adapter=(_TARGET_A_ADAPTER if name == "a" else _TARGET_B_ADAPTER),
        target_channel=name,
    )


def _receipt(event_id, item, *, status="sent"):
    return DeliveryReceipt(
        receipt_id=f"rcpt-{item.outbox_id}",
        event_id=event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        route_id=item.route_id,
        status=status,
        receipt_kind="attempt",
        attempt_number=1,
        outbox_id=item.outbox_id,
        created_at=datetime.now(timezone.utc),
    )


def _observation(event_id, item):
    return DeliveryObservation(
        observation_id=f"obs-{item.outbox_id}",
        event_id=event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        outbox_id=item.outbox_id,
        attempt_number=1,
        state="delivered",
        confirmation_level="remote_service",
    )


async def _admit_two_targets(store, event_id="routed"):
    """Admit through durable ingress and fan out to two live target generations."""
    await store.admit_ingress(
        _event(event_id), _inbound_ref(event_id, f"native-{event_id}"), "live"
    )
    target_a = _target(event_id, "a")
    target_b = _target(event_id, "b")
    await store.create_outbox_item(target_a)
    await store.create_outbox_item(target_b)
    assert len(await store.claim_ingress_work(worker_id="worker", limit=1)) == 1
    assert await store.complete_ingress_work(event_id, worker_id="worker")
    return target_a, target_b


async def _claim_only(store, item, *, other_adapter, now=_NOW):
    claimed = await store.claim_due_outbox_items(
        now=now, worker_id="worker", excluded_adapters=(other_adapter,)
    )
    assert [claimed.outbox_id for claimed in claimed] == [item.outbox_id]


async def _settle_target_sent(store, event_id, item, *, other_adapter):
    await _claim_only(store, item, other_adapter=other_adapter)
    await store.append_receipt(_receipt(event_id, item))
    assert await store.mark_outbox_sent(
        item.outbox_id, receipt_id=f"rcpt-{item.outbox_id}"
    )
    await store.append_delivery_observation(_observation(event_id, item))


async def _settle_target_dead_lettered(store, item, *, diagnostic):
    await _claim_only(store, item, other_adapter=_TARGET_A_ADAPTER)
    assert await store.mark_outbox_dead_lettered(
        item.outbox_id,
        failure_kind="adapter_permanent",
        error_summary=diagnostic,
    )


async def _count(store, sql):
    row = await store._read_one(sql)
    return int(row["n"])


# ---------------------------------------------------------------------------
# Headline: one event, two destinations
# ---------------------------------------------------------------------------


async def test_pending_second_target_blocks_collection_naming_the_live_owner(tmp_path):
    store = SQLiteStorage(str(tmp_path / "gate.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["status"] == "blocked"
        assert "active_outbox" in blocked["blockers"]
        assert "unsettled_mandatory_owners" in blocked["blockers"]
        row = await store._read_one(
            "SELECT body FROM event_body_content WHERE event_id='routed'"
        )
        assert row["body"] == _BODY
        assert not await store._read_one(
            "SELECT 1 FROM event_content_collection_log WHERE event_id='routed'"
        )
        preview = await store.collect_event_content("routed")
        assert preview["status"] == "blocked"
        assert preview["rows_removed"] == 0
        assert (
            await _count(
                store,
                "SELECT COUNT(*) AS n FROM event_body_content WHERE event_id='routed'",
            )
            == 1
        )
    finally:
        await store.close()


@pytest.mark.parametrize("terminal", ["sent", "dead_lettered"])
async def test_collection_succeeds_after_second_target_reaches_terminal_state(
    tmp_path, terminal
):
    store = SQLiteStorage(str(tmp_path / f"terminal-{terminal}.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        if terminal == "sent":
            await _claim_only(store, target_b, other_adapter=_TARGET_A_ADAPTER)
            await store.append_receipt(_receipt("routed", target_b))
            assert await store.mark_outbox_sent(
                target_b.outbox_id, receipt_id=f"rcpt-{target_b.outbox_id}"
            )
        else:
            await _settle_target_dead_lettered(
                store, target_b, diagnostic="relay unreachable: no route to peer"
            )
        preview = await store.collect_event_content("routed")
        assert preview["status"] == "dry_run"
        assert preview["candidate_rows"] == 1
        assert json.dumps(preview).find(_BODY) == -1
        applied = await store.collect_event_content("routed", dry_run=False)
        assert applied["status"] == "collected"
        assert applied["rows_removed"] == 1
        assert applied["blockers"] == []
        assert (
            await _count(
                store,
                "SELECT COUNT(*) AS n FROM event_body_content WHERE event_id='routed'",
            )
            == 0
        )
        log = await store._read_one(
            "SELECT policy, rows_removed FROM event_content_collection_log "
            "WHERE event_id='routed'"
        )
        assert log["policy"] == "relay-only"
        assert int(log["rows_removed"]) == 1
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "already_collected"
    finally:
        await store.close()


async def test_collected_routed_event_keeps_metadata_delivery_and_native_facts(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "evidence.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _settle_target_dead_lettered(
            store, target_b, diagnostic="relay unreachable: no route to peer"
        )
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
        metadata = await store.get_metadata("routed")
        assert metadata is not None and metadata.event_id == "routed"
        assert (
            await _count(
                store,
                "SELECT COUNT(*) AS n FROM delivery_receipts WHERE event_id='routed'",
            )
            == 1
        )
        assert (
            await _count(
                store,
                "SELECT COUNT(*) AS n FROM delivery_observations WHERE event_id='routed'",
            )
            == 1
        )
        assert (
            await _count(
                store,
                "SELECT COUNT(*) AS n FROM delivery_outbox WHERE event_id='routed'",
            )
            == 2
        )
        assert (
            await _count(
                store,
                "SELECT COUNT(*) AS n FROM native_message_refs WHERE event_id='routed'",
            )
            == 1
        )
        diagnostics = await store._read_all(
            "SELECT family, field FROM delivery_evidence_diagnostics "
            "WHERE family='outbox' AND row_id=?",
            (target_b.outbox_id,),
        )
        assert [str(row["field"]) for row in diagnostics] == ["error_summary"]
        assert (
            await store.read_event_for_outbox("routed", target_a.outbox_id)
        ).status == "denied"
        assert (
            await store.read_event_for_outbox("routed", target_b.outbox_id)
        ).status == "denied"
    finally:
        await store.close()


async def test_collected_routed_event_duplicate_ingress_still_dedupes(tmp_path):
    store = SQLiteStorage(str(tmp_path / "dedupe.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _settle_target_dead_lettered(
            store, target_b, diagnostic="relay unreachable: no route to peer"
        )
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
        duplicate = await store.admit_ingress(
            _event("routed"), _inbound_ref("routed", "native-routed"), "live"
        )
        assert duplicate.created is False
        assert duplicate.event_id == "routed"
        assert await _count(store, "SELECT COUNT(*) AS n FROM canonical_events") == 1
        assert (
            await _count(store, "SELECT COUNT(*) AS n FROM durable_ingress_work") == 1
        )
        assert await _count(store, "SELECT COUNT(*) AS n FROM event_body_content") == 0
    finally:
        await store.close()


async def test_replay_and_recovery_inspection_stay_consistent_after_collection(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "inspection.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _settle_target_dead_lettered(
            store, target_b, diagnostic="relay unreachable: no route to peer"
        )
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
        assert (await store.read_event_for_replay("routed")).status == "denied"
        assert (
            await store.read_event_for_replay("routed", now=datetime.now(timezone.utc))
        ).status == "denied"
        dependencies = await store.list_mandatory_content_dependencies("routed")
        assert dependencies
        assert all(released for _, _, _, released in dependencies)
        preview = await store.inspect_event_content_dependencies("routed")
        # The advisory preview stays deliberately more conservative than the
        # collector: a dead-lettered target remains a possible hold candidate.
        assert preview["delivery_hold_candidate"] is True
        assert {"status": "sent", "count": 1} in preview["outbox"]
        assert {"status": "dead_lettered", "count": 1} in preview["outbox"]
        assert preview["content_copies"]["receipts"] == 1
        assert preview["content_copies"]["observations"] == 1
        assessment = await store.assess_event_content_release("routed")
        assert assessment["status"] == "unmeasured"
        assert assessment["blockers"] == []
        copies = {copy["surface"]: copy for copy in assessment["copies"]}
        assert copies["retained_diagnostics"]["rows"] == 1
        assert copies["retained_diagnostics"]["classification"] == "retained_evidence"
        assert copies["outbox_records"]["rows"] == 2
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Adversarial: live states and content values keep blocking
# ---------------------------------------------------------------------------


async def test_uncertain_native_attempt_blocks_collection_until_terminal(tmp_path):
    store = SQLiteStorage(str(tmp_path / "uncertain.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _claim_only(
            store,
            target_b,
            other_adapter=_TARGET_A_ADAPTER,
            now=datetime.now(timezone.utc).isoformat(),
        )
        reserved = await store.reserve_outbox_attempt(
            target_b.outbox_id, "worker", from_attempt=1
        )
        assert reserved == 2
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["status"] == "blocked"
        assert "active_outbox" in blocked["blockers"]
        # An uncertain native handoff cannot be released as if it never happened.
        await store.release_outbox_claim(target_b.outbox_id, worker_id="worker")
        held = await store.get_outbox_item(target_b.outbox_id)
        assert held is not None
        assert held.status == "in_progress"
        assert held.active_attempt == reserved
        assert await store.mark_outbox_dead_lettered(
            target_b.outbox_id, failure_kind="adapter_permanent"
        )
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
    finally:
        await store.close()


async def test_retry_wait_target_blocks_collection_and_refusal_never_mutates(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "retry-wait.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _claim_only(store, target_b, other_adapter=_TARGET_A_ADAPTER)
        assert await store.mark_outbox_retry_wait(
            target_b.outbox_id, next_attempt_at="2026-10-31T01:00:00+00:00"
        )
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["status"] == "blocked"
        assert "active_outbox" in blocked["blockers"]
        assert (
            await _count(
                store,
                "SELECT COUNT(*) AS n FROM event_body_content WHERE event_id='routed'",
            )
            == 1
        )
        assert not await store._read_one(
            "SELECT 1 FROM event_content_collection_log WHERE event_id='routed'"
        )
        assert await store.mark_outbox_dead_lettered(
            target_b.outbox_id, failure_kind="adapter_permanent"
        )
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
    finally:
        await store.close()


async def test_unrevoked_optional_grant_blocks_collection_until_revoked(tmp_path):
    store = SQLiteStorage(str(tmp_path / "grant.db"))
    await store.initialize()
    try:
        await store.admit_ingress(_event(), _inbound_ref("routed"), "live")
        # A grant can only be issued while the source hold is live; it then
        # survives source settlement until its explicit expiry or revocation.
        now = datetime.now(timezone.utc)
        policy = AvailabilityPolicy(
            hard_ceiling=now + timedelta(hours=2),
            max_optional_hold=timedelta(hours=1),
            allowed_optional_consumers=frozenset({"history"}),
        )
        await store.issue_optional_body_hold(
            "routed",
            ContentHold("history", "body", now + timedelta(minutes=10), issued_at=now),
            policy,
            now=now,
        )
        target_a = _target("routed", "a")
        target_b = _target("routed", "b")
        await store.create_outbox_item(target_a)
        await store.create_outbox_item(target_b)
        assert len(await store.claim_ingress_work(worker_id="worker", limit=1)) == 1
        assert await store.complete_ingress_work("routed", worker_id="worker")
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _settle_target_dead_lettered(
            store, target_b, diagnostic="relay unreachable: no route to peer"
        )
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["status"] == "blocked"
        assert "unrevoked_optional_grants" in blocked["blockers"]
        assert await store.revoke_optional_body_hold("routed", "history", now=now)
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
    finally:
        await store.close()


async def test_stale_worker_completion_cannot_settle_or_unblock(tmp_path):
    store = SQLiteStorage(str(tmp_path / "stale.db"))
    await store.initialize()
    try:
        await store.admit_ingress(_event(), _inbound_ref("routed"), "live")
        target_a = _target("routed", "a")
        target_b = _target("routed", "b")
        await store.create_outbox_item(target_a)
        await store.create_outbox_item(target_b)
        assert len(await store.claim_ingress_work(worker_id="worker", limit=1)) == 1
        assert not await store.complete_ingress_work("routed", worker_id="stale")
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["status"] == "blocked"
        assert "active_ingress" in blocked["blockers"]
        assert "unsettled_mandatory_owners" in blocked["blockers"]
        assert await store.complete_ingress_work("routed", worker_id="worker")
        assert not await store.complete_ingress_work("routed", worker_id="worker")
    finally:
        await store.close()


async def test_clock_rollback_after_collection_cannot_resurrect_collected_body(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "rollback.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _settle_target_dead_lettered(
            store, target_b, diagnostic="relay unreachable: no route to peer"
        )
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
        await store._write(
            "INSERT INTO event_body_content(event_id,field,body,created_at) "
            "VALUES ('routed','body','RECREATED','2026-10-30T00:00:00+00:00')"
        )
        with pytest.raises(StorageError, match="unexpectedly recreated"):
            await store.collect_event_content("routed", dry_run=False)
        with pytest.raises(StorageError, match="unexpectedly recreated"):
            await store.collect_event_content("routed")
        assert (
            await _count(
                store, "SELECT COUNT(*) AS n FROM event_content_collection_log"
            )
            == 1
        )
        log = await store._read_one(
            "SELECT rows_removed FROM event_content_collection_log "
            "WHERE event_id='routed'"
        )
        assert int(log["rows_removed"]) == 1
    finally:
        await store.close()


async def test_concurrent_collect_and_terminal_settlement_serialize(tmp_path):
    import asyncio

    path = str(tmp_path / "serialize.db")
    first = SQLiteStorage(path)
    await first.initialize()
    try:
        target_a, target_b = await _admit_two_targets(first)
        await _settle_target_sent(
            first, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
    finally:
        await first.close()
    second = SQLiteStorage(path)
    await second.initialize()
    third = SQLiteStorage(path)
    await third.initialize()
    try:
        collection, cancellation = await asyncio.gather(
            second.collect_event_content("routed", dry_run=False),
            third.mark_outbox_cancelled(target_b.outbox_id),
        )
        assert cancellation is True
        assert collection["status"] in {"blocked", "collected"}
        body_rows = await _count(
            second,
            "SELECT COUNT(*) AS n FROM event_body_content WHERE event_id='routed'",
        )
        receipts = await _count(
            second, "SELECT COUNT(*) AS n FROM event_content_collection_log"
        )
        if collection["status"] == "collected":
            assert body_rows == 0
            assert receipts == 1
        else:
            assert "active_outbox" in collection["blockers"]
            assert body_rows == 1
            assert receipts == 0
        dependencies = await second.list_mandatory_content_dependencies("routed")
        assert all(released for _, _, _, released in dependencies)
    finally:
        await second.close()
        await third.close()


async def test_late_observation_after_collection_is_append_only(tmp_path):
    store = SQLiteStorage(str(tmp_path / "late-observation.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _settle_target_dead_lettered(
            store, target_b, diagnostic="relay unreachable: no route to peer"
        )
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
        late = DeliveryObservation(
            observation_id="obs-late",
            event_id="routed",
            delivery_plan_id=target_a.delivery_plan_id,
            target_adapter=target_a.target_adapter,
            target_channel=target_a.target_channel,
            outbox_id=target_a.outbox_id,
            attempt_number=1,
            state="delivered",
            confirmation_level="end_to_end",
        )
        assert await store.append_delivery_observation(late) is True
        assert await _count(store, "SELECT COUNT(*) AS n FROM event_body_content") == 0
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "already_collected"
    finally:
        await store.close()


async def test_collected_routed_event_survives_restart_with_evidence_intact(tmp_path):
    path = str(tmp_path / "restart.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _settle_target_dead_lettered(
            store, target_b, diagnostic="relay unreachable: no route to peer"
        )
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
    finally:
        await store.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert (await reopened.collect_event_content("routed"))[
            "status"
        ] == "already_collected"
        assert (
            await _count(
                reopened,
                "SELECT COUNT(*) AS n FROM delivery_evidence_diagnostics",
            )
            == 1
        )
        inventory = await reopened.inspect_content_inventory()
        surfaces = {surface["table"]: surface for surface in inventory["surfaces"]}
        assert surfaces["delivery_evidence_diagnostics"]["rows"] == 1
        assert surfaces["event_body_content"]["rows"] == 0
    finally:
        await reopened.close()


async def test_content_bearing_evidence_values_keep_blocking_column_aware(
    tmp_path,
):
    """Each family's refusal names exactly the family holding a value."""
    store = SQLiteStorage(str(tmp_path / "historical.db"))
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store)
        await _settle_target_sent(
            store, "routed", target_a, other_adapter=_TARGET_B_ADAPTER
        )
        await _settle_target_dead_lettered(
            store, target_b, diagnostic="relay unreachable: no route to peer"
        )
        # Historical inline receipt diagnostics keep blocking until cleared.
        await store._write(
            "UPDATE delivery_receipts SET error='HISTORICAL-SEND-ERROR' "
            "WHERE event_id='routed'"
        )
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == ["delivery_receipts"]
        assert "HISTORICAL-SEND-ERROR" not in json.dumps(blocked)
        await store._write(
            "UPDATE delivery_receipts SET error=NULL WHERE event_id='routed'"
        )
        # Non-empty provider metadata on the outbox row keeps blocking.
        await store._write(
            'UPDATE delivery_outbox SET metadata=\'{"trace":"META-TRACE"}\' '
            "WHERE outbox_id=?",
            (target_b.outbox_id,),
        )
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["blockers"] == ["outbox_records"]
        await store._write(
            "UPDATE delivery_outbox SET metadata='{}' WHERE outbox_id=?",
            (target_b.outbox_id,),
        )
        # Non-empty native-reference metadata keeps blocking.
        await store._write(
            'UPDATE native_message_refs SET metadata=\'{"peer":"NATIVE-PEER"}\' '
            "WHERE event_id='routed'"
        )
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["blockers"] == ["native_references"]
        await store._write(
            "UPDATE native_message_refs SET metadata='{}' WHERE event_id='routed'"
        )
        # Historical inline ingress and observation diagnostics keep blocking.
        await store._write(
            "UPDATE durable_ingress_work SET last_error='HISTORICAL-INGRESS-ERROR' "
            "WHERE event_id='routed'"
        )
        await store._write(
            "UPDATE delivery_observations SET error='HISTORICAL-OBSERVATION-ERROR' "
            "WHERE event_id='routed'"
        )
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == [
            "delivery_observations",
            "ingress_work",
        ]
        assert "HISTORICAL-INGRESS-ERROR" not in json.dumps(blocked)
        await store._write(
            "UPDATE durable_ingress_work SET last_error=NULL WHERE event_id='routed'"
        )
        await store._write(
            "UPDATE delivery_observations SET error=NULL WHERE event_id='routed'"
        )
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
    finally:
        await store.close()


@pytest.mark.parametrize("value", ["{}", ""])
@pytest.mark.parametrize(
    "table,column,blocker",
    [
        ("delivery_receipts", "error", "delivery_receipts"),
        ("delivery_receipts", "capability_reason", "delivery_receipts"),
        ("delivery_receipts", "rendering_evidence", "delivery_receipts"),
        ("delivery_observations", "error", "delivery_observations"),
        ("delivery_outbox", "failure_kind_detail", "outbox_records"),
        ("delivery_outbox", "error_summary", "outbox_records"),
        ("durable_ingress_work", "last_error", "ingress_work"),
    ],
)
async def test_plain_text_diagnostics_never_use_the_empty_json_sentinel(
    temp_storage, table, column, blocker, value
):
    a, b = await _admit_two_targets(temp_storage)
    await _settle_target_sent(
        temp_storage, "routed", a, other_adapter=_TARGET_B_ADAPTER
    )
    await _settle_target_dead_lettered(temp_storage, b, diagnostic="retained evidence")
    # Identifiers are a closed test parameter set; text remains bound.
    await temp_storage._write(
        f"UPDATE {table} SET {column}=? WHERE event_id=?",  # nosec B608 - closed test parameter identifiers; values are bound
        (value, "routed"),
    )
    preview = await temp_storage.assess_event_content_release("routed")
    assert preview["blockers"] == [blocker]
    result = await temp_storage.collect_event_content("routed", dry_run=False)
    assert result["status"] == "blocked"
    assert result["blockers"] == [blocker]
    assert result["rows_removed"] == 0


async def test_release_preview_refuses_body_row_with_absent_admission_identity(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "integrity-preview.db"))
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="identity-conflict"), payload={}, metadata={}
        )
        await store.append(event)
        await store._write(
            "UPDATE event_content_dependencies SET released_at='2026-10-31' "
            "WHERE event_id='identity-conflict'"
        )
        await store._write(
            "INSERT INTO event_body_content(event_id,field,body,created_at) "
            "VALUES ('identity-conflict','body','UNEXPLAINED','2026-10-31')"
        )
        preview = await store.assess_event_content_release("identity-conflict")
        assert "inconsistent_admission_identity" in preview["blockers"]
        assert preview["core_subset_status"] == "blocked"
        with pytest.raises(StorageError, match="admission recorded no body"):
            await store.collect_event_content("identity-conflict", dry_run=False)
    finally:
        await store.close()


async def test_release_preview_refuses_missing_uncollected_admission_body(tmp_path):
    store = SQLiteStorage(str(tmp_path / "missing-body-preview.db"))
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="body-missing"),
            payload={"body": "EXPECTED"},
            metadata={},
        )
        await store.append(event)
        await store._write(
            "UPDATE event_content_dependencies SET released_at='2026-10-31' "
            "WHERE event_id='body-missing'"
        )
        await store._write(
            "DELETE FROM event_body_content WHERE event_id='body-missing'"
        )
        report = await store.assess_event_content_release("body-missing")
        assert "inconsistent_admission_identity" in report["blockers"]
        assert report["core_subset_status"] == "blocked"
    finally:
        await store.close()
