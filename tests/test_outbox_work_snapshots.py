# medre-test-tier: compatibility
"""Guards for the frozen outbox work snapshot contract.

The frozen accepted work context for one outbox generation lives in the
privately owned ``delivery_outbox_snapshots`` table, not in
``delivery_outbox.metadata``. These tests pin, on real SQLite storage:

- the snapshot is written atomically with its outbox row and keyed by the
  exact outbox identity (outbox_id plus the committed creation generation);
- new rows carry empty ``delivery_outbox.metadata`` while historical rows
  keep their inline bytes as the frozen context;
- retirement happens only inside guarded terminal settlement (``sent``,
  ``cancelled``, ``dead_lettered``, ``abandoned`` and the receipt-backed
  finalizers), never for ``pending``/``in_progress``/``retry_wait``/
  ``queued``, and a lost guard or rolled-back transaction preserves the
  snapshot;
- reconstruction consumes the typed owner-scoped read and reproduces the
  pre-change wire payloads exactly, failing closed on missing, corrupted,
  or unknown-version snapshots;
- an unretired snapshot blocks release assessment; a retired one blocks
  nothing; legacy inline metadata keeps blocking through the existing
  outbox-family content probe.

See ``docs/spec/accepted-context-ownership.md`` sections 3.2, 3.3, 4.2,
and 4.3.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import msgspec
import pytest

from medre.core.contracts.delivery import DeferredHandoffFailed
from medre.core.delivery_authority import DeliveryIdentity
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.engine.pipeline.receipt_factory import (
    build_delivery_receipt,
)
from medre.core.engine.pipeline.retry_plan import (
    reconstruct_retry_delivery_plan,
)
from medre.core.events import DeliveryAttemptProvenance, EventMetadata
from medre.core.events.canonical import NativeMessageRef
from medre.core.events.metadata import RoutingMetadata
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
    delivery_target_identity,
)
from medre.core.routing.models import (
    Route,
    RouteDestination,
    RouteSource,
    RouteTarget,
)
from medre.core.storage.backend import (
    OUTBOX_DISPATCH_SNAPSHOT_VERSION,
    DeferredHandoffFinalization,
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
    OutboxSnapshotReadError,
    StorageError,
    TerminalOutboxFinalization,
)
from medre.core.storage.classification import (
    STORAGE_COLUMN_CLASSES,
    ColumnClass,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import admit_event

_NOW = "2026-10-31T00:00:00+00:00"

# docs/spec/accepted-context-ownership.md section 4.3: the staged snapshot
# document members, and section 4.2: the reduced direct-snapshot key sets.
STAGED_SNAPSHOT_KEYS = frozenset({"route", "plan", "routing"})
DESTINATION_DIRECT_SNAPSHOT_KEYS = frozenset(
    {
        "destination_kind",
        "destination_hash",
        "destination_name",
        "destination_metadata",
        "capability_level",
        "delivery_strategy",
        "capability_field",
        "capability_reason",
        "deadline",
    }
)
CHANNEL_DIRECT_SNAPSHOT_KEYS = frozenset(
    {
        "capability_level",
        "delivery_strategy",
        "capability_field",
        "capability_reason",
        "deadline",
    }
)


def _routed_text_event(event_id: str) -> object:
    return msgspec.structs.replace(
        make_storage_event(event_id=event_id, payload={"body": "secret"}),
        metadata=EventMetadata(routing=RoutingMetadata(matched_routes=("r1",))),
    )


def _destination_target() -> RouteTarget:
    return RouteTarget(
        adapter="fake_presentation",
        channel=None,
        destination=RouteDestination(
            kind="mesh",
            destination_hash="hash-1",
            destination_name="Room One",
            metadata={"room_id": "!room:example.org"},
        ),
    )


def _channel_target() -> RouteTarget:
    return RouteTarget(adapter="fake_presentation", channel="destination")


def _route_and_plan(event_id: str, target: RouteTarget) -> tuple[Route, DeliveryPlan]:
    route = Route(
        id="r1",
        source=RouteSource(
            adapter="fake_transport",
            event_kinds=("message.created",),
            channel="ch-0",
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-1",
        event_id=event_id,
        route_id=route.id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
        capability_level="native",
        capability_field="mesh.native",
        capability_reason="adapter advertises native send",
        deadline=None,
        target_identity=delivery_target_identity(target),
        retry_policy=RetryPolicy(max_attempts=4, backoff_base=3.0, jitter=True),
    )
    return route, plan


async def _admit(store: SQLiteStorage, event_id: str) -> object:
    event = _routed_text_event(event_id)
    await store.admit_ingress(event, None, "live")
    return event


async def _stage_pending(
    store: SQLiteStorage, event: object, target: RouteTarget
) -> str:
    route, plan = _route_and_plan(event.event_id, target)
    manager = OutboxManager(store, DeliveryLifecycleService())
    context = await manager.create_for_delivery(
        event, route, plan, target, target.adapter, pending=True
    )
    assert context.outbox_id is not None
    return context.outbox_id


async def _create_direct(
    store: SQLiteStorage, event: object, target: RouteTarget
) -> str:
    route, plan = _route_and_plan(event.event_id, target)
    manager = OutboxManager(store, DeliveryLifecycleService())
    context = await manager.create_for_delivery(
        event, route, plan, target, target.adapter, pending=False
    )
    assert context.outbox_id is not None
    return context.outbox_id


async def _read_snapshot(store: SQLiteStorage, outbox_id: str) -> object | None:
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    return await store.get_outbox_dispatch_context(
        outbox_id,
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
    )


async def _snapshot_count(store: SQLiteStorage) -> int:
    row = await store._read_one("SELECT COUNT(*) AS n FROM delivery_outbox_snapshots")
    assert row is not None
    return int(row["n"])


async def _claim(store: SQLiteStorage, outbox_id: str, worker: str = "delivery"):
    claimed = await store.claim_due_outbox_items(_NOW, worker)
    assert [item.outbox_id for item in claimed] == [outbox_id]
    return claimed[0]


def _item_identity(item: DeliveryOutboxItem) -> dict[str, object]:
    return {
        "event_id": item.event_id,
        "delivery_plan_id": item.delivery_plan_id,
        "target_adapter": item.target_adapter,
        "target_channel": item.target_channel,
    }


# ---------------------------------------------------------------------------
# Persisted shape: empty metadata, keyed snapshots for both staging shapes
# ---------------------------------------------------------------------------


async def test_new_rows_carry_empty_metadata_and_keyed_snapshots(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Staged and direct rows persist no inline context bytes at all."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-shape")
    staged_id = await _stage_pending(store, event, _destination_target())
    direct_id = await _create_direct(store, event, _destination_target())

    for outbox_id in (staged_id, direct_id):
        item = await store.get_outbox_item(outbox_id)
        assert item is not None
        assert item.metadata == {}
        metadata_only = await store.get_outbox_item(outbox_id, metadata_only=True)
        assert metadata_only is not None
        assert metadata_only.metadata == {}

    staged = await _read_snapshot(store, staged_id)
    assert staged is not None
    assert staged.snapshot_version == OUTBOX_DISPATCH_SNAPSHOT_VERSION
    assert staged.kind == "staged"
    assert staged.attempt_number == 0
    assert set(staged.context) == STAGED_SNAPSHOT_KEYS

    direct = await _read_snapshot(store, direct_id)
    assert direct is not None
    assert direct.kind == "direct"
    assert direct.attempt_number == 1
    assert set(direct.context) == DESTINATION_DIRECT_SNAPSHOT_KEYS
    assert direct.context["destination_name"] == "Room One"
    assert direct.context["destination_metadata"] == {"room_id": "!room:example.org"}


async def test_channel_direct_snapshot_carries_the_reduced_key_set(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Channel-addressed direct rows omit the destination_* snapshot keys."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-channel-direct")
    direct_id = await _create_direct(store, event, _channel_target())
    snapshot = await _read_snapshot(store, direct_id)
    assert snapshot is not None
    assert snapshot.kind == "direct"
    assert set(snapshot.context) == CHANNEL_DIRECT_SNAPSHOT_KEYS


def test_snapshot_table_classification_is_declared() -> None:
    """The registry classifies every snapshot column exactly once."""
    columns = STORAGE_COLUMN_CLASSES["delivery_outbox_snapshots"]
    assert columns["outbox_id"] is ColumnClass.IMMUTABLE_IDENTITY
    assert columns["attempt_number"] is ColumnClass.IMMUTABLE_IDENTITY
    assert columns["snapshot_version"] is ColumnClass.IMMUTABLE_IDENTITY
    assert columns["snapshot_kind"] is ColumnClass.IMMUTABLE_IDENTITY
    assert columns["created_at"] is ColumnClass.IMMUTABLE_IDENTITY
    assert columns["context_json"] is ColumnClass.INDEPENDENTLY_OWNED_CONTENT


# ---------------------------------------------------------------------------
# Retirement semantics
# ---------------------------------------------------------------------------


async def test_nonterminal_transitions_never_retire_the_snapshot(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """pending, in_progress, retry_wait, and queued preserve the snapshot."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-nonterminal")
    outbox_id = await _stage_pending(store, event, _channel_target())

    item = await _claim(store, outbox_id, worker="worker-a")
    assert item.status == "in_progress"
    snapshot = await _read_snapshot(store, outbox_id)
    assert snapshot is not None

    assert await store.mark_outbox_retry_wait(
        outbox_id,
        next_attempt_at="2026-10-31T01:00:00+00:00",
        failure_kind="adapter_transient",
    )
    assert await _read_snapshot(store, outbox_id) is not None

    reclaimed = await store.claim_due_outbox_items(
        "2026-10-31T02:00:00+00:00", "worker-b"
    )
    assert [row.outbox_id for row in reclaimed] == [outbox_id]
    assert await store.mark_outbox_queued(outbox_id)
    assert await _read_snapshot(store, outbox_id) is not None
    assert await _snapshot_count(store) == 1


async def test_stale_terminal_callback_preserves_the_snapshot(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """A lost guarded CAS leaves the snapshot in place."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-stale")
    outbox_id = await _stage_pending(store, event, _channel_target())
    await _claim(store, outbox_id, worker="worker-a")

    assert not await store.mark_outbox_sent(
        outbox_id, expected_worker_id="other-worker"
    )
    assert not await store.mark_outbox_cancelled(
        outbox_id, expected_worker_id="other-worker"
    )
    assert await _read_snapshot(store, outbox_id) is not None
    assert await _snapshot_count(store) == 1


async def test_queued_to_sent_guarded_transition_retires_the_snapshot(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """The deferred queued->sent path retires exactly on the terminal change."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-queued-sent")
    outbox_id = await _stage_pending(store, event, _channel_target())
    await _claim(store, outbox_id)
    assert await store.mark_outbox_queued(outbox_id)
    assert await _read_snapshot(store, outbox_id) is not None

    assert await store.mark_outbox_sent(outbox_id)
    assert await _read_snapshot(store, outbox_id) is None
    assert await _snapshot_count(store) == 0


@pytest.mark.parametrize(
    ("settle",),
    [
        ("cancelled",),
        ("abandoned",),
        ("dead_lettered",),
        ("sent",),
    ],
)
async def test_each_terminal_status_retires_the_snapshot(
    outbox_temp_storage: SQLiteStorage, settle: str
) -> None:
    """Every guarded terminal transition retires the row's own snapshot."""
    store = outbox_temp_storage
    event = await _admit(store, f"evt-terminal-{settle}")
    outbox_id = await _stage_pending(store, event, _channel_target())
    await _claim(store, outbox_id)
    assert await _read_snapshot(store, outbox_id) is not None

    if settle == "sent":
        assert await store.mark_outbox_sent(outbox_id)
    else:
        method = getattr(store, f"mark_outbox_{settle}")
        assert await method(outbox_id)
    assert await _read_snapshot(store, outbox_id) is None


async def test_receipt_backed_terminal_finalizer_retires_atomically(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """The receipt-backed finalizer retires in the same transaction."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-finalizer")
    outbox_id = await _create_direct(store, event, _channel_target())
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    receipt = build_delivery_receipt(
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        route_id=item.route_id,
        status="dead_lettered",
        receipt_kind="lifecycle",
        outbox_id=outbox_id,
        attempt_number=item.attempt_number,
    )
    command = TerminalOutboxFinalization(lifecycle_receipt=receipt)
    assert await store.finalize_outbox_terminal(command) is True
    assert await _read_snapshot(store, outbox_id) is None
    assert await _snapshot_count(store) == 0


async def test_failed_receipt_insert_rolls_back_retirement(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """A terminal finalization that lost its guard preserves the snapshot."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-rollback")
    outbox_id = await _create_direct(store, event, _channel_target())
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    receipt = build_delivery_receipt(
        event_id="evt-rollback",
        delivery_plan_id="other-plan",
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        route_id=item.route_id,
        status="dead_lettered",
        receipt_kind="lifecycle",
        outbox_id=outbox_id,
        attempt_number=item.attempt_number,
    )
    command = TerminalOutboxFinalization(lifecycle_receipt=receipt)
    assert await store.finalize_outbox_terminal(command) is False
    assert await _read_snapshot(store, outbox_id) is not None
    assert await _snapshot_count(store) == 1


async def test_native_identity_conflict_rolls_back_retirement(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """The deferred hand-off finalizer aborts before retirement on conflict."""
    store = outbox_temp_storage
    await admit_event(store, "evt-native-owner")
    await store.store_native_ref(
        NativeMessageRef(
            id="nref-native-owner",
            event_id="evt-native-owner",
            adapter="fake_presentation",
            native_channel_id="destination",
            native_message_id="native-conflict",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
    )
    event = await _admit(store, "evt-native-candidate")
    outbox_id = await _create_direct(store, event, _channel_target())
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    receipt = build_delivery_receipt(
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        route_id=item.route_id,
        status="sent",
        adapter_message_id="native-conflict",
        outbox_id=outbox_id,
        attempt_number=item.attempt_number,
    )
    candidate_ref = NativeMessageRef(
        id="nref-native-candidate",
        event_id=item.event_id,
        adapter=item.target_adapter,
        native_channel_id=item.target_channel,
        native_message_id="native-conflict",
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
    )
    with pytest.raises(StorageError, match="different canonical event"):
        await store.finalize_deferred_handoff(
            DeferredHandoffFinalization(native_ref=candidate_ref, receipt=receipt)
        )
    assert await _read_snapshot(store, outbox_id) is not None
    assert await _snapshot_count(store) == 1


async def test_unknown_native_handoff_preserves_the_snapshot(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Deferred feedback naming no eligible row changes and retires nothing."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-unknown-handoff")
    outbox_id = await _stage_pending(store, event, _channel_target())

    manager = OutboxManager(store, DeliveryLifecycleService())
    await manager.record_deferred_failure(
        DeferredHandoffFailed(
            attempt_provenance=DeliveryAttemptProvenance(
                event_id=event.event_id,
                delivery_plan_id="plan-1",
                target_adapter="fake_presentation",
                target_channel="destination",
                outbox_id="obox-does-not-exist",
                attempt_number=1,
                source="live",
            ),
            outcome="exhausted",
        )
    )
    assert await _read_snapshot(store, outbox_id) is not None
    assert await _snapshot_count(store) == 1


# ---------------------------------------------------------------------------
# Sibling independence and generations
# ---------------------------------------------------------------------------


async def test_two_target_siblings_own_independent_snapshots(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Retiring one target's snapshot never touches a sibling target's."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-siblings")
    first = await _stage_pending(store, event, _channel_target())
    second = await _stage_pending(store, event, _destination_target())

    assert await _snapshot_count(store) == 2
    assert await _read_snapshot(store, first) is not None
    assert await _read_snapshot(store, second) is not None

    assert await store.mark_outbox_cancelled(first)
    assert await _read_snapshot(store, first) is None
    assert await _read_snapshot(store, second) is not None
    assessment = await store.assess_event_content_release("evt-siblings")
    assert "unretired_outbox_snapshots" in assessment["blockers"]
    assert "active_outbox" in assessment["blockers"]


async def test_generations_own_separate_snapshots(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """A replay generation snapshots itself; older generations stay intact."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-generations")
    target = _channel_target()
    route, plan = _route_and_plan(event.event_id, target)
    first = DeliveryOutboxItem(
        outbox_id="obox-gen-1",
        event_id=event.event_id,
        route_id=route.id,
        delivery_plan_id=plan.plan_id,
        target_adapter=target.adapter,
        target_channel=target.channel,
        attempt_number=1,
        status="in_progress",
        metadata={},
    )
    await store.create_outbox_item(
        first,
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="direct",
            context={
                "capability_level": "native",
                "delivery_strategy": "direct",
                "capability_field": "mesh.native",
                "capability_reason": "adapter advertises native send",
                "deadline": None,
            },
        ),
    )
    replay_item = DeliveryOutboxItem(
        outbox_id="obox-gen-2",
        event_id=event.event_id,
        route_id=route.id,
        delivery_plan_id=plan.plan_id,
        target_adapter=target.adapter,
        target_channel=target.channel,
        attempt_number=99,
        status="in_progress",
        dispatch_source="replay",
        replay_run_id="run-1",
        metadata={},
    )
    created = await store.create_outbox_item(
        replay_item,
        allocate_new_generation=True,
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="direct",
            context={
                "capability_level": "native",
                "delivery_strategy": "direct",
                "capability_field": "mesh.native",
                "capability_reason": "adapter advertises native send",
                "deadline": None,
            },
        ),
    )
    assert created.attempt_number == 2
    assert created.replay_run_id == "run-1"

    first_snapshot = await _read_snapshot(store, "obox-gen-1")
    second_snapshot = await _read_snapshot(store, "obox-gen-2")
    assert first_snapshot is not None and second_snapshot is not None
    assert first_snapshot.attempt_number == 1
    assert second_snapshot.attempt_number == 2
    assert await _snapshot_count(store) == 2

    assert await store.mark_outbox_sent("obox-gen-2")
    assert await _read_snapshot(store, "obox-gen-2") is None
    assert await _read_snapshot(store, "obox-gen-1") is not None


async def test_attempt_takeover_reuses_the_frozen_snapshot(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """A second worker's takeover reconstructs the same frozen context."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-takeover")
    outbox_id = await _stage_pending(store, event, _channel_target())

    first = await _claim(store, outbox_id, worker="worker-a")
    snapshot_before = await _read_snapshot(store, outbox_id)
    assert snapshot_before is not None

    # The first worker's lease expires; a second worker takes over the claim.
    taken = await store.claim_due_outbox_items("2026-10-31T02:00:00+00:00", "worker-b")
    assert [row.outbox_id for row in taken] == [outbox_id]
    snapshot_after = await _read_snapshot(store, outbox_id)
    assert snapshot_after == snapshot_before

    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    reconstructed = reconstruct_retry_delivery_plan(
        item=item,
        previous_receipt=None,
        default_max_attempts=3,
        dispatch_snapshot=snapshot_after,
    )
    assert reconstructed.plan.plan_id == first.delivery_plan_id
    assert reconstructed.plan.capability_level == "native"


# ---------------------------------------------------------------------------
# Fail-closed reads
# ---------------------------------------------------------------------------


async def test_corrupted_snapshot_fails_closed(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """A malformed frozen document raises instead of guessing."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-corrupt")
    outbox_id = await _create_direct(store, event, _channel_target())
    # A JSON scalar is not the frozen mapping shape: the read fails closed
    # instead of returning a partial context.
    await store._write(
        "UPDATE delivery_outbox_snapshots SET context_json='42' " "WHERE outbox_id=?",
        (outbox_id,),
    )
    with pytest.raises(StorageError, match="malformed"):
        await _read_snapshot(store, outbox_id)

    await store._write(
        "UPDATE delivery_outbox_snapshots SET context_json='[1,2,3]' "
        "WHERE outbox_id=?",
        (outbox_id,),
    )
    with pytest.raises(StorageError, match="malformed"):
        await _read_snapshot(store, outbox_id)


async def _write_foreign_shape_row(
    store: SQLiteStorage,
    outbox_id: str,
    *,
    column: str,
    value: str,
) -> None:
    """Persist a snapshot row with a shape value the DDL CHECK would refuse.

    The fresh-database DDL pins the current shape, so a row carrying a future
    version or unknown kind can only exist as foreign state. Recreating the
    table without the shape CHECK stands in for that newer writer; the typed
    read must still fail closed on it.
    """
    db = store._require_db()
    db.execute("DROP TABLE delivery_outbox_snapshots")
    db.execute(
        "CREATE TABLE delivery_outbox_snapshots ("
        " outbox_id TEXT NOT NULL REFERENCES delivery_outbox(outbox_id),"
        " attempt_number INTEGER NOT NULL,"
        " snapshot_version INTEGER NOT NULL,"
        " snapshot_kind TEXT NOT NULL,"
        " context_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " PRIMARY KEY (outbox_id, attempt_number))"
    )
    db.commit()
    row = await store._read_one(
        "SELECT event_id, delivery_plan_id, target_adapter, target_channel,"
        " created_at FROM delivery_outbox WHERE outbox_id=?",
        (outbox_id,),
    )
    assert row is not None
    await store._write(
        "INSERT INTO delivery_outbox_snapshots"
        " (outbox_id, attempt_number, snapshot_version, snapshot_kind,"
        "  context_json, created_at)"
        " VALUES (?, 1, 1, 'direct', '{}', ?)"
        " ON CONFLICT(outbox_id, attempt_number) DO UPDATE SET"
        f" {column}=excluded.{column}",  # nosec B608 - closed in-test column literal
        (outbox_id, row["created_at"]),
    )
    await store._write(
        f"UPDATE delivery_outbox_snapshots SET {column}=? WHERE outbox_id=?",  # nosec B608 - closed in-test column literal
        (value, outbox_id),
    )


async def test_unknown_snapshot_version_fails_closed(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """An unrecognized shape version never reaches reconstruction."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-version")
    outbox_id = await _create_direct(store, event, _channel_target())
    await _write_foreign_shape_row(
        store, outbox_id, column="snapshot_version", value="2"
    )
    with pytest.raises(StorageError, match="unknown shape version"):
        await _read_snapshot(store, outbox_id)


async def test_unknown_snapshot_kind_fails_closed(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """An unrecognized shape discriminator never reaches reconstruction."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-kind")
    outbox_id = await _create_direct(store, event, _channel_target())
    await _write_foreign_shape_row(
        store, outbox_id, column="snapshot_kind", value="legacy"
    )
    with pytest.raises(StorageError, match="unknown snapshot kind"):
        await _read_snapshot(store, outbox_id)


async def test_identity_bound_read_never_returns_a_sibling_context(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """The typed read binds to the exact outbox identity, not just the ID."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-identity")
    outbox_id = await _create_direct(store, event, _channel_target())
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    wrong = await store.get_outbox_dispatch_context(
        outbox_id,
        event_id="evt-other",
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
    )
    assert wrong is None
    wrong = await store.get_outbox_dispatch_context(
        outbox_id,
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel="not-the-channel",
    )
    assert wrong is None
    right = await store.get_outbox_dispatch_context(
        outbox_id,
        **_item_identity(item),  # type: ignore[arg-type]
    )
    assert right is not None


# ---------------------------------------------------------------------------
# Release assessment
# ---------------------------------------------------------------------------


async def test_replay_generation_snapshot_blocks_release_until_settled(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Replay rows own no content dependency, so the snapshot probe is the gate."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-replay-release")
    target = _channel_target()
    route, plan = _route_and_plan(event.event_id, target)
    replay_item = DeliveryOutboxItem(
        outbox_id="obox-replay-release",
        event_id=event.event_id,
        route_id=route.id,
        delivery_plan_id=plan.plan_id,
        target_adapter=target.adapter,
        target_channel=target.channel,
        attempt_number=99,
        status="in_progress",
        dispatch_source="replay",
        replay_run_id="run-release",
        metadata={},
    )
    await store.create_outbox_item(
        replay_item,
        allocate_new_generation=True,
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="direct",
            context={
                "capability_level": "native",
                "delivery_strategy": "direct",
                "capability_field": "mesh.native",
                "capability_reason": "adapter advertises native send",
                "deadline": None,
            },
        ),
    )
    # Complete ingress and release every mandatory owner: no unsettled owner
    # remains for the replay generation, yet its unretired snapshot blocks.
    claims = await store.claim_ingress_work(worker_id="worker")
    assert [claim.event_id for claim in claims] == [event.event_id]
    assert await store.complete_ingress_work(event.event_id, worker_id="worker")
    assessment = await store.assess_event_content_release(event.event_id)
    assert "unsettled_mandatory_owners" not in assessment["blockers"]
    assert "unretired_outbox_snapshots" in assessment["blockers"]
    surfaces = {
        copy["surface"]: copy["classification"] for copy in assessment["copies"]
    }
    assert surfaces["unretired_outbox_snapshots"] == "authority_or_state"

    await _claim(store, "obox-replay-release")
    assert await store.mark_outbox_sent("obox-replay-release")
    settled = await store.assess_event_content_release(event.event_id)
    assert "unretired_outbox_snapshots" not in settled["blockers"]
    # The admission slice carries the routing namespace as typed facts, so
    # the settled event's canonical envelope no longer blocks either.
    assert settled["blockers"] == []


async def test_no_snapshot_bytes_leak_through_metadata_reads(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Metadata-only projections never return snapshot values."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-no-leak")
    target = _destination_target()
    outbox_id = await _stage_pending(store, event, target)

    full = await store.get_outbox_item(outbox_id)
    assert full is not None and full.metadata == {}
    assert (
        set(full.metadata or {})
        & {
            "route",
            "plan",
            "routing",
            "dispatch_context",
            "destination_name",
            "capability_reason",
        }
        == set()
    )
    metadata_only = await store.get_outbox_item(outbox_id, metadata_only=True)
    assert metadata_only is not None and metadata_only.metadata == {}
    for row in await store.list_outbox_items_for_delivery(
        DeliveryIdentity(event.event_id, "plan-1", "fake_presentation", "destination")
    ):
        assert row.metadata == {}
    raw = await store._read_one(
        "SELECT metadata FROM delivery_outbox WHERE outbox_id=?", (outbox_id,)
    )
    assert raw is not None
    assert msgspec.json.decode(raw["metadata"]) == {}


async def test_snapshot_storage_read_failure_is_not_a_corrupt_snapshot(
    tmp_path, monkeypatch
):
    storage = SQLiteStorage(str(tmp_path / "snapshot-read.sqlite"))
    await storage.initialize()
    try:
        monkeypatch.setattr(
            storage,
            "_read_one",
            AsyncMock(
                side_effect=StorageError("Database read failed: database is locked")
            ),
        )
        with pytest.raises(OutboxSnapshotReadError, match="Could not read"):
            await storage.get_outbox_dispatch_context(
                "outbox-id",
                event_id="event-id",
                delivery_plan_id="plan-id",
                target_adapter="adapter-id",
                target_channel=None,
            )
    finally:
        await storage.close()
