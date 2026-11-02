"""Audited exact-target decisions acknowledge without dispatching or mutating.

The decision authority records an operator resolve/resume acknowledgement
over one observed exact target under the same BEGIN IMMEDIATE discipline as
the exact unstarted cancellation. The outbox row is never modified, nothing
is staged, and every refusal leaves storage exactly as observed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.observability.exact_target import (
    ExactTargetSelection,
    preview_exact_target,
)
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite._operator_decision import (
    EXACT_TARGET_DECISION_ACTIONS,
    RESUME_SCHEDULED_ACTION,
    RESOLVE_UNCERTAIN_ACTION,
    ExactTargetDecision,
    OperatorDecisionRefused,
)


async def _seed(storage, event, channel, *, outbox_id):
    await storage.append(event)
    await storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event.event_id,
            delivery_plan_id="plan",
            target_adapter="matrix",
            target_channel=channel,
            route_id="route",
            status="pending",
        )
    )
    result = await storage.get_outbox_item(outbox_id)
    assert result is not None
    return result


def _decision(
    item,
    *,
    decision_id="decision-1",
    action=RESUME_SCHEDULED_ACTION,
    channel=None,
    generation=None,
    status=None,
    updated_at=None,
):
    return ExactTargetDecision(
        decision_id=decision_id,
        actor_id="operator-test",
        action=action,
        identity=DeliveryIdentity(
            item.event_id,
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel if channel is None else channel,
        ),
        outbox_id=item.outbox_id,
        expected_generation=item.attempt_number if generation is None else generation,
        expected_status=item.status if status is None else status,
        expected_updated_at=(
            item.updated_at.isoformat()
            if (updated_at is None and hasattr(item.updated_at, "isoformat"))
            else str(item.updated_at if updated_at is None else updated_at)
        ),
    )


async def _audit(storage, event_id):
    return await storage.list_operator_decisions(event_id)


async def test_resume_scheduled_decision_records_audit_without_touching_the_row(
    temp_storage, sample_event
):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-decide")
    requested = _decision(item)
    assert await temp_storage.record_exact_target_decision(requested) is True
    assert await temp_storage.record_exact_target_decision(requested) is False

    current = await temp_storage.get_outbox_item(item.outbox_id)
    assert current.status == "pending"
    assert current.updated_at == item.updated_at
    assert current.active_attempt is None
    assert current.next_attempt_at is None

    evidence = await _audit(temp_storage, sample_event.event_id)
    assert len(evidence) == 1
    assert evidence[0]["action"] == "resume_scheduled"
    assert evidence[0]["actor_id"] == "operator-test"
    assert evidence[0]["outbox_id"] == item.outbox_id
    assert evidence[0]["observed_generation"] == item.attempt_number
    assert evidence[0]["observed_status"] == "pending"
    assert evidence[0]["observed_updated_at"] == (
        item.updated_at.isoformat()
        if hasattr(item.updated_at, "isoformat")
        else str(item.updated_at)
    )
    assert "message" not in str(evidence)

    # The acknowledgement settles no content owner: the row keeps its hold.
    deps = await temp_storage.list_mandatory_content_dependencies(
        sample_event.event_id
    )
    state = {(kind, owner): released for kind, owner, _, released in deps}
    assert state.get(("outbox", item.outbox_id), False) is False


async def test_committed_decision_leaves_the_normal_resumption_fences_in_charge(
    temp_storage, sample_event
):
    """The decision never fences dispatch: the worker's own claim resumes."""
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-resume")
    assert await temp_storage.record_exact_target_decision(_decision(item)) is True
    claimed = await temp_storage.claim_due_outbox_items(
        now="2035-01-01T00:00:00", worker_id="retry", limit=1
    )
    assert [row.outbox_id for row in claimed] == [item.outbox_id]
    current = await temp_storage.get_outbox_item(item.outbox_id)
    assert current.status == "in_progress"


async def test_resolve_uncertain_decision_records_distinct_intent(
    temp_storage, sample_event
):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-resolve")
    resume = _decision(item, decision_id="resume-1")
    resolve = _decision(
        item,
        decision_id="resolve-1",
        action=RESOLVE_UNCERTAIN_ACTION,
    )
    assert await temp_storage.record_exact_target_decision(resume) is True
    assert await temp_storage.record_exact_target_decision(resolve) is True
    assert await temp_storage.record_exact_target_decision(resume) is False
    assert await temp_storage.record_exact_target_decision(resolve) is False
    evidence = await _audit(temp_storage, sample_event.event_id)
    assert sorted(row["action"] for row in evidence) == [
        "resolve_uncertain",
        "resume_scheduled",
    ]
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "pending"


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda d: replace(d, actor_id="someone-else"), id="actor"),
        pytest.param(
            lambda d: replace(
                d,
                identity=DeliveryIdentity(
                    d.identity.event_id,
                    d.identity.delivery_plan_id,
                    d.identity.target_adapter,
                    "!elsewhere",
                ),
            ),
            id="channel",
        ),
        pytest.param(lambda d: replace(d, outbox_id="obox-other"), id="outbox"),
        pytest.param(
            lambda d: replace(d, expected_generation=d.expected_generation + 1),
            id="generation",
        ),
        pytest.param(
            lambda d: replace(d, expected_updated_at="2000-01-01T00:00:00+00:00"),
            id="update-version",
        ),
        pytest.param(
            lambda d: replace(d, expected_status="retry_wait"), id="status"
        ),
        pytest.param(
            lambda d: replace(d, action=RESOLVE_UNCERTAIN_ACTION), id="action"
        ),
    ],
)
async def test_decision_id_reuse_with_any_changed_parameter_fails_closed(
    temp_storage, sample_event, mutation
):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-reuse")
    committed = _decision(item)
    assert await temp_storage.record_exact_target_decision(committed) is True
    with pytest.raises(OperatorDecisionRefused, match="already binds"):
        await temp_storage.record_exact_target_decision(mutation(committed))
    assert len(await _audit(temp_storage, sample_event.event_id)) == 1


@pytest.mark.parametrize("action", sorted(EXACT_TARGET_DECISION_ACTIONS))
async def test_claimed_row_refuses_the_decision(temp_storage, sample_event, action):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-claimed")
    await temp_storage.claim_due_outbox_items(
        now="2026-12-01T00:00:00", worker_id="retry", limit=1
    )
    with pytest.raises(
        OperatorDecisionRefused, match="not decidable by this authority"
    ):
        await temp_storage.record_exact_target_decision(
            _decision(item, action=action)
        )
    current = await temp_storage.get_outbox_item(item.outbox_id)
    assert current.status == "in_progress"
    assert await _audit(temp_storage, sample_event.event_id) == []


@pytest.mark.parametrize(
    ("status", "pattern"),
    [
        ("queued", "not decidable by this authority"),
        ("abandoned", "not decidable by this authority"),
        ("sent", "Completed or cancelled delivery work"),
        ("cancelled", "Completed or cancelled delivery work"),
        # The outbox status vocabulary persists terminal failure only as
        # dead_lettered; the refusal predicate also names failed to mirror
        # the observation contract's terminal-failure vocabulary.
        ("dead_lettered", "requires a separate send-capable authority"),
    ],
)
async def test_state_family_refusals_name_the_state_without_audit(
    temp_storage, sample_event, status, pattern
):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-state")
    await temp_storage._write(
        "UPDATE delivery_outbox SET status=? WHERE outbox_id=?",
        (status, item.outbox_id),
    )
    observed = await temp_storage.get_outbox_item(item.outbox_id)
    with pytest.raises(OperatorDecisionRefused, match=pattern):
        await temp_storage.record_exact_target_decision(_decision(observed))
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == status
    assert await _audit(temp_storage, sample_event.event_id) == []


@pytest.mark.parametrize(
    "statement",
    [
        # active_attempt cannot coexist with pending by DDL (a reservation
        # requires in_progress), so the reserved case is covered by the
        # claim test and the reserved competing sibling above.
        "UPDATE delivery_outbox SET worker_id='owner' WHERE outbox_id=?",
        "UPDATE delivery_outbox SET locked_at=updated_at WHERE outbox_id=?",
        "UPDATE delivery_outbox SET lease_until=updated_at WHERE outbox_id=?",
    ],
)
async def test_pending_row_with_reservation_or_claim_refuses_the_decision(
    temp_storage, sample_event, statement
):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-owned")
    await temp_storage._write(statement, (item.outbox_id,))
    with pytest.raises(
        OperatorDecisionRefused, match="not decidable by this authority"
    ):
        await temp_storage.record_exact_target_decision(_decision(item))
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "pending"
    assert await _audit(temp_storage, sample_event.event_id) == []


@pytest.mark.parametrize("action", sorted(EXACT_TARGET_DECISION_ACTIONS))
async def test_stale_observation_refuses_without_audit(
    temp_storage, sample_event, action
):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-stale")
    with pytest.raises(OperatorDecisionRefused, match="Stale observed state"):
        await temp_storage.record_exact_target_decision(
            _decision(item, action=action, generation=item.attempt_number + 3)
        )
    with pytest.raises(OperatorDecisionRefused, match="Stale observed state"):
        await temp_storage.record_exact_target_decision(
            _decision(
                item,
                action=action,
                updated_at="2000-01-01T00:00:00+00:00",
            )
        )
    # The state family is keyed on the revalidated row, not the expectation:
    # a fabricated failed expectation over a pending row is merely stale.
    with pytest.raises(OperatorDecisionRefused, match="Stale observed state"):
        await temp_storage.record_exact_target_decision(
            _decision(item, action=action, status="failed")
        )
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "pending"
    assert await _audit(temp_storage, sample_event.event_id) == []


@pytest.mark.parametrize("action", sorted(EXACT_TARGET_DECISION_ACTIONS))
async def test_identity_and_competing_generation_refuse_without_audit(
    temp_storage, sample_event, action
):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-fenced")
    with pytest.raises(OperatorDecisionRefused, match="no longer exists"):
        await temp_storage.record_exact_target_decision(
            _decision(item, action=action, channel="!other")
        )
    # A reserved sibling with a lower finalized counter still supersedes the
    # selected generation, because the reserved active_attempt competes.
    await temp_storage._write(
        "INSERT INTO delivery_outbox (outbox_id,event_id,delivery_plan_id,"
        "target_adapter,target_channel,attempt_number,active_attempt,status,"
        "created_at,updated_at,worker_id) VALUES (?,?,?,?,?,0,1,'in_progress',?,?,?)",
        (
            "reserved",
            sample_event.event_id,
            "plan",
            "matrix",
            "!room",
            item.created_at,
            item.updated_at,
            "owner",
        ),
    )
    with pytest.raises(OperatorDecisionRefused, match="competing"):
        await temp_storage.record_exact_target_decision(
            _decision(item, action=action)
        )
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "pending"
    assert await _audit(temp_storage, sample_event.event_id) == []


async def test_retry_wait_row_is_decidable_with_a_fresh_observation(
    temp_storage, sample_event
):
    item = await _seed(temp_storage, sample_event, "!retry", outbox_id="obox-defer")
    await temp_storage.claim_due_outbox_items(
        now="2026-12-01T00:00:00", worker_id="retry", limit=1
    )
    assert await temp_storage.mark_outbox_retry_wait(
        item.outbox_id, "2030-01-01T00:00:00", expected_worker_id="retry"
    )
    deferred = await temp_storage.get_outbox_item(item.outbox_id)
    assert deferred.status == "retry_wait"
    assert deferred.active_attempt is None
    # The pre-retry observation is superseded by the transition.
    with pytest.raises(OperatorDecisionRefused, match="Stale observed state"):
        await temp_storage.record_exact_target_decision(_decision(item))
    assert await temp_storage.record_exact_target_decision(_decision(deferred)) is True
    current = await temp_storage.get_outbox_item(item.outbox_id)
    assert current.status == "retry_wait"
    assert current.next_attempt_at == deferred.next_attempt_at
    evidence = await _audit(temp_storage, sample_event.event_id)
    assert [row["observed_status"] for row in evidence] == ["retry_wait"]


async def test_row_moving_after_observation_refuses_the_decision(
    temp_storage, sample_event
):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-move")
    requested = _decision(item)
    await temp_storage.claim_due_outbox_items(
        now="2026-12-01T00:00:00", worker_id="retry", limit=1
    )
    with pytest.raises(
        OperatorDecisionRefused, match="not decidable by this authority"
    ):
        await temp_storage.record_exact_target_decision(requested)
    assert await _audit(temp_storage, sample_event.event_id) == []


async def test_decision_survives_restart_as_idempotent_replay(tmp_path, sample_event):
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = str(tmp_path / "operational.sqlite")
    first = SQLiteStorage(path)
    await first.initialize()
    item = await _seed(first, sample_event, "!room", outbox_id="obox-reopen")
    requested = _decision(item)
    assert await first.record_exact_target_decision(requested) is True
    await first.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert await reopened.record_exact_target_decision(requested) is False
        current = await reopened.get_outbox_item(item.outbox_id)
        assert current.status == "pending"
        assert current.updated_at == item.updated_at
        assert len(await _audit(reopened, sample_event.event_id)) == 1
    finally:
        await reopened.close()


async def test_audit_failure_rolls_back_the_decision(temp_storage, sample_event):
    from medre.core.storage.backend import StorageError

    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-audit")
    await temp_storage._write(
        "CREATE TRIGGER refuse_operator_audit BEFORE INSERT ON operator_delivery_decisions "
        "BEGIN SELECT RAISE(ABORT, 'audit_unavailable'); END"
    )
    with pytest.raises(StorageError, match="audit_unavailable"):
        await temp_storage.record_exact_target_decision(_decision(item))
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "pending"
    assert await _audit(temp_storage, sample_event.event_id) == []


async def test_old_single_action_audit_shape_is_refused_without_migration(tmp_path):
    import sqlite3
    from contextlib import closing

    from medre.core.storage.backend import PreReleaseSchemaConstraintMismatchError
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = str(tmp_path / "old-action-shape.sqlite")
    original = SQLiteStorage(path)
    await original.initialize()
    await original.close()
    with closing(sqlite3.connect(path)) as db:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='operator_delivery_decisions'"
        ).fetchone()[0]
        sql = sql.replace(
            "CHECK (action IN ('cancel_unstarted', 'resolve_uncertain',"
            " 'resume_scheduled')),",
            "CHECK (action = 'cancel_unstarted'),",
        )
        db.execute("DROP TABLE operator_delivery_decisions")
        db.execute(sql)
        db.commit()
    reopened = SQLiteStorage(path)
    try:
        with pytest.raises(PreReleaseSchemaConstraintMismatchError):
            await reopened.initialize()
    finally:
        await reopened.close()
    with closing(sqlite3.connect(path)) as db:
        definition = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='operator_delivery_decisions'"
        ).fetchone()[0]
    assert "'resolve_uncertain'" not in definition


async def test_observation_evidence_never_authorizes_the_decision(
    temp_storage, sample_event
):
    """A genuine preview is evidence only; the transaction revalidates."""
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-observe")
    preview = await preview_exact_target(
        temp_storage,
        ExactTargetSelection(sample_event.event_id, "plan", "matrix", "!room"),
    )
    assert preview.execution_authorized is False
    assert preview.cancel_eligible is True
    requested = ExactTargetDecision(
        decision_id="observed-decision",
        actor_id="operator-test",
        action=RESUME_SCHEDULED_ACTION,
        identity=DeliveryIdentity(sample_event.event_id, "plan", "matrix", "!room"),
        outbox_id=preview.outbox_id,
        expected_generation=preview.observed_generation or 0,
        expected_status=preview.status or "",
        expected_updated_at=preview.updated_at or "",
    )
    # Move the row after the observation: the fingerprint-era expectation
    # no longer holds, so the decision is refused despite the genuine
    # preview, and execution stays unauthorized on the observation side.
    await temp_storage.claim_due_outbox_items(
        now="2026-12-01T00:00:00", worker_id="retry", limit=1
    )
    with pytest.raises(
        OperatorDecisionRefused, match="not decidable by this authority"
    ):
        await temp_storage.record_exact_target_decision(requested)
    assert await _audit(temp_storage, sample_event.event_id) == []
    assert preview.execution_authorized is False


async def test_recorded_row_conforms_to_the_decision_schema(
    temp_storage, sample_event
):
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="obox-schema")
    committed = await temp_storage.record_exact_target_decision(
        _decision(item, action=RESOLVE_UNCERTAIN_ACTION)
    )
    assert committed is True
    evidence = await _audit(temp_storage, sample_event.event_id)
    assert len(evidence) == 1
    schema_path = (
        Path(__file__).parents[1] / "docs/schemas/exact-target-decision.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(evidence[0], schema)
    assert schema["properties"]["action"]["enum"] == sorted(
        EXACT_TARGET_DECISION_ACTIONS
    )


@pytest.mark.parametrize(
    "kwargs, pattern",
    [
        ({"action": "cancel_unstarted"}, "frozen exact-target decision"),
        ({"action": ""}, "frozen exact-target decision"),
        ({"decision_id": ""}, "non-empty bounded string"),
        ({"actor_id": " "}, "non-empty bounded string"),
        ({"expected_generation": -1}, "nonnegative"),
        ({"expected_generation": "1"}, "nonnegative"),
        ({"expected_status": ""}, "non-empty bounded string"),
        ({"expected_updated_at": ""}, "non-empty bounded string"),
    ],
)
def test_malformed_decision_input_raises_before_storage(kwargs, pattern):
    base = {
        "decision_id": "decision-1",
        "actor_id": "operator-test",
        "action": RESUME_SCHEDULED_ACTION,
        "identity": DeliveryIdentity("e", "plan", "matrix", "!room"),
        "outbox_id": "obox",
        "expected_generation": 0,
        "expected_status": "pending",
        "expected_updated_at": "2026-01-01T00:00:00+00:00",
    }
    with pytest.raises(ValueError, match=pattern):
        ExactTargetDecision(**{**base, **kwargs})
