"""Exact-target selection contract and read-only preview classification.

Pins the closed category vocabulary mapping, the explicit None-vs-empty
channel decision, fail-closed selection, raw generation reporting, and the
no-write guarantee of :func:`medre.core.observability.exact_target.
preview_exact_target`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import msgspec
import pytest

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.observability.exact_target import (
    EXACT_TARGET_CATEGORIES,
    EXACT_TARGET_PREVIEW_SCOPE,
    ExactTargetPreview,
    ExactTargetSelection,
    classify_outbox_status,
    preview_exact_target,
)
from medre.core.storage.backend import DeliveryOutboxItem
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import (
    admit_event,
    create_outbox_item_with_parent,
    make_outbox_item,
)

_EVENT = "evt-exact-target"
_PLAN = "plan-1"
_ADAPTER = "fake_presentation"
_CHANNEL = "ch-0"


def _selection(
    event_id: str = _EVENT,
    plan: str = _PLAN,
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
) -> ExactTargetSelection:
    return ExactTargetSelection(
        event_id=event_id,
        delivery_plan_id=plan,
        target_adapter=adapter,
        target_channel=channel,
    )


def _row(**overrides: Any) -> DeliveryOutboxItem:
    return make_outbox_item(
        delivery_plan_id=overrides.pop("delivery_plan_id", _PLAN),
        target_adapter=overrides.pop("target_adapter", _ADAPTER),
        target_channel=overrides.pop("target_channel", _CHANNEL),
        event_id=overrides.pop("event_id", _EVENT),
        **overrides,
    )


def _claim_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _matching_context(plan_id: str = _PLAN) -> dict[str, Any]:
    """A staged frozen document that agrees with a row of the given plan."""
    return {
        "route": {"id": "route-1"},
        "plan": {
            "plan_id": plan_id,
            "event_id": _EVENT,
            "route_id": "route-1",
            "target": {"adapter": _ADAPTER, "channel": _CHANNEL},
        },
        "routing": {},
    }


# ---------------------------------------------------------------------------
# Selection key
# ---------------------------------------------------------------------------


def test_selection_rejects_empty_channel_and_accepts_explicit_none() -> None:
    with pytest.raises(ValueError, match="channel decision"):
        ExactTargetSelection(_EVENT, _PLAN, _ADAPTER, "")
    selection = ExactTargetSelection(_EVENT, _PLAN, _ADAPTER, None)
    assert selection.identity == DeliveryIdentity(_EVENT, _PLAN, _ADAPTER, None)
    assert selection.identity.target_channel is None


def test_selection_preserves_explicit_channel() -> None:
    selection = ExactTargetSelection(_EVENT, _PLAN, _ADAPTER, "!room-a")
    assert selection.identity == DeliveryIdentity(_EVENT, _PLAN, _ADAPTER, "!room-a")


def test_selection_requires_complete_identity() -> None:
    for args in (
        ("", _PLAN, _ADAPTER, None),
        (_EVENT, "", _ADAPTER, None),
        (_EVENT, _PLAN, "", None),
    ):
        with pytest.raises(ValueError, match="requires event, plan and adapter"):
            ExactTargetSelection(*args)


# ---------------------------------------------------------------------------
# Status-to-category mapping (table-driven)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "active_attempt", "failure_kind", "expected"),
    [
        ("pending", None, None, "pending"),
        ("in_progress", None, None, "in_progress"),
        ("pending", 2, None, "in_progress"),
        ("retry_wait", 3, None, "in_progress"),
        ("queued", None, None, "awaiting_feedback"),
        ("retry_wait", None, None, "retry_scheduled"),
        ("sent", None, None, "completed"),
        ("cancelled", None, None, "cancelled"),
        ("abandoned", None, None, "inspect_native"),
        ("dead_lettered", None, "adapter_transient", "preview_replay"),
        ("dead_lettered", None, "retry_exhausted", "preview_replay"),
        ("dead_lettered", None, "adapter_permanent", "needs_operator_action"),
        ("dead_lettered", None, None, "needs_operator_action"),
        ("failed", None, "adapter_transient", "preview_replay"),
        ("failed", None, "renderer_failure", "needs_operator_action"),
        (None, None, None, "ambiguous"),
        ("mystery", None, None, "ambiguous"),
    ],
)
def test_classify_outbox_status_maps_the_frozen_vocabulary(
    status: str | None,
    active_attempt: int | None,
    failure_kind: str | None,
    expected: str,
) -> None:
    assert (
        classify_outbox_status(
            status, active_attempt=active_attempt, failure_kind=failure_kind
        )
        == expected
    )


def test_category_vocabulary_is_closed_and_frozen() -> None:
    assert EXACT_TARGET_CATEGORIES == frozenset(
        {
            "not_found",
            "ambiguous",
            "stale",
            "pending",
            "in_progress",
            "awaiting_feedback",
            "retry_scheduled",
            "completed",
            "cancelled",
            "inspect_native",
            "preview_replay",
            "needs_operator_action",
            "content_unavailable",
            "route_or_plan_changed",
        }
    )
    with pytest.raises(ValueError, match="Unknown exact-target category"):
        ExactTargetPreview(
            event_id=_EVENT,
            delivery_plan_id=_PLAN,
            target_adapter=_ADAPTER,
            target_channel=None,
            category="delivered",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Preview over storage
# ---------------------------------------------------------------------------


async def test_preview_not_found_for_absent_event(temp_storage) -> None:
    report = await preview_exact_target(temp_storage, _selection())
    assert report.category == "not_found"
    assert report.outbox_id is None
    assert report.attempt_number is None
    assert report.active_attempt is None
    assert report.observed_generation is None
    assert report.status is None
    assert report.cancel_eligible is False
    assert report.excluded_sibling_count == 0
    assert report.snapshot_present is False
    assert report.snapshot_kind is None
    assert len(report.observed_fingerprint) == 64


async def test_preview_ambiguous_when_identity_has_no_outbox_row(temp_storage) -> None:
    await admit_event(temp_storage, _EVENT)
    report = await preview_exact_target(temp_storage, _selection())
    assert report.category == "ambiguous"
    assert report.outbox_id is None
    assert report.status is None
    assert report.cancel_eligible is False
    assert report.excluded_sibling_count == 0


async def test_preview_ambiguous_when_live_generations_contest_identity(
    temp_storage,
) -> None:
    await create_outbox_item_with_parent(temp_storage, _row())
    replay = replace(_row(attempt_number=1), dispatch_source="replay")
    second = await temp_storage.create_outbox_item(replay, allocate_new_generation=True)
    assert second.attempt_number == 2
    report = await preview_exact_target(temp_storage, _selection())
    assert report.category == "ambiguous"
    assert report.outbox_id is None
    assert report.excluded_sibling_count == 0


async def test_preview_selects_one_channel_and_counts_excluded_sibling(
    temp_storage,
) -> None:
    first = await create_outbox_item_with_parent(
        temp_storage, _row(target_channel="ch-a")
    )
    await create_outbox_item_with_parent(temp_storage, _row(target_channel="ch-b"))
    report = await preview_exact_target(temp_storage, _selection(channel="ch-a"))
    assert report.category == "pending"
    assert report.outbox_id == first.outbox_id
    assert report.target_channel == "ch-a"
    assert report.excluded_sibling_count == 1
    assert report.cancel_eligible is True
    assert report.attempt_number == 1
    assert report.observed_generation == report.attempt_number
    assert report.active_attempt is None
    assert report.status == "pending"


async def test_preview_none_channel_matches_empty_persisted_channel(
    temp_storage,
) -> None:
    row = await create_outbox_item_with_parent(temp_storage, _row(target_channel=""))
    report = await preview_exact_target(temp_storage, _selection(channel=None))
    assert report.category == "pending"
    assert report.outbox_id == row.outbox_id
    assert report.target_channel is None
    assert report.cancel_eligible is True


async def test_preview_stale_when_expected_generation_is_superseded(
    temp_storage,
) -> None:
    row = await create_outbox_item_with_parent(temp_storage, _row())
    fresh = await preview_exact_target(
        temp_storage, _selection(), expected_generation=row.attempt_number
    )
    assert fresh.category == "pending"
    stale = await preview_exact_target(
        temp_storage, _selection(), expected_generation=0
    )
    assert stale.category == "stale"
    assert stale.attempt_number == row.attempt_number
    assert stale.observed_generation == row.attempt_number
    assert stale.outbox_id == row.outbox_id
    assert stale.cancel_eligible is True


async def test_preview_stale_when_reservation_appears(temp_storage) -> None:
    row = await create_outbox_item_with_parent(temp_storage, _row())
    claimed = await temp_storage.claim_due_outbox_items(_claim_now(), "worker-1")
    assert row.outbox_id in {item.outbox_id for item in claimed}
    assert await temp_storage.reserve_outbox_attempt(row.outbox_id, "worker-1", 1) == 2
    report = await preview_exact_target(
        temp_storage, _selection(), expected_generation=1
    )
    assert report.category == "stale"
    assert report.active_attempt == 2
    assert report.attempt_number == 1
    untargeted = await preview_exact_target(temp_storage, _selection())
    assert untargeted.category == "in_progress"
    assert untargeted.active_attempt == 2
    assert untargeted.cancel_eligible is False


async def test_preview_stale_when_expected_fingerprint_differs(temp_storage) -> None:
    row = await create_outbox_item_with_parent(temp_storage, _row())
    first = await preview_exact_target(temp_storage, _selection())
    claimed = await temp_storage.claim_due_outbox_items(_claim_now(), "worker-1")
    assert row.outbox_id in {item.outbox_id for item in claimed}
    stale = await preview_exact_target(
        temp_storage, _selection(), expected_fingerprint=first.observed_fingerprint
    )
    assert stale.category == "stale"
    current = await preview_exact_target(temp_storage, _selection())
    assert current.category == "in_progress"
    assert current.observed_fingerprint != first.observed_fingerprint


async def test_preview_completed_after_sent_settlement(temp_storage) -> None:
    row = await create_outbox_item_with_parent(temp_storage, _row())
    await temp_storage.claim_due_outbox_items(_claim_now(), "worker-1")
    assert await temp_storage.mark_outbox_sent(row.outbox_id)
    report = await preview_exact_target(temp_storage, _selection())
    assert report.category == "completed"
    assert report.status == "sent"
    assert report.cancel_eligible is False


async def test_preview_dead_lettered_failure_kind_splits_replay_from_operator(
    temp_storage,
) -> None:
    transient = await create_outbox_item_with_parent(
        temp_storage, _row(delivery_plan_id="plan-t")
    )
    permanent = await create_outbox_item_with_parent(
        temp_storage, _row(delivery_plan_id="plan-p")
    )
    unclassified = await create_outbox_item_with_parent(
        temp_storage, _row(delivery_plan_id="plan-u")
    )
    await temp_storage.claim_due_outbox_items(_claim_now(), "worker-1")
    assert await temp_storage.mark_outbox_dead_lettered(
        transient.outbox_id, failure_kind="adapter_transient"
    )
    assert await temp_storage.mark_outbox_dead_lettered(
        permanent.outbox_id, failure_kind="adapter_permanent"
    )
    assert await temp_storage.mark_outbox_dead_lettered(unclassified.outbox_id)
    assert (
        await preview_exact_target(temp_storage, _selection(plan="plan-t"))
    ).category == "preview_replay"
    assert (
        await preview_exact_target(temp_storage, _selection(plan="plan-p"))
    ).category == "needs_operator_action"
    assert (
        await preview_exact_target(temp_storage, _selection(plan="plan-u"))
    ).category == "needs_operator_action"


async def test_preview_content_unavailable_after_source_release(temp_storage) -> None:
    event = msgspec.structs.replace(
        make_storage_event(event_id=_EVENT), payload={"body": "private"}
    )
    await temp_storage.append(event)
    row = await temp_storage.create_outbox_item(_row())
    before = await preview_exact_target(temp_storage, _selection())
    assert before.category == "pending"
    released = await temp_storage._write_rowcount(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind IN ('ingress','append')",
        (
            datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            _EVENT,
        ),
    )
    assert released == 1
    after = await preview_exact_target(temp_storage, _selection())
    assert after.category == "content_unavailable"
    assert after.outbox_id == row.outbox_id
    assert after.status == "pending"
    assert after.cancel_eligible is True


async def test_preview_route_or_plan_changed_on_contradictory_snapshot(
    temp_storage,
) -> None:
    from medre.core.storage.backend import OutboxDispatchSnapshotWrite

    async def create_with_snapshot(plan_id: str, context: dict[str, Any]) -> Any:
        await admit_event(temp_storage, _EVENT)
        return await temp_storage.create_outbox_item(
            _row(delivery_plan_id=plan_id),
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="staged", context=context
            ),
        )

    agreeing = await create_with_snapshot("plan-ok", _matching_context("plan-ok"))
    report = await preview_exact_target(temp_storage, _selection(plan="plan-ok"))
    assert report.category == "pending"
    assert report.snapshot_present is True
    assert report.snapshot_kind == "staged"

    rebound = dict(_matching_context())
    rebound["plan"] = dict(rebound["plan"], plan_id="plan-rebound")
    contradicted = await create_with_snapshot("plan-bad", rebound)
    changed = await preview_exact_target(temp_storage, _selection(plan="plan-bad"))
    assert changed.category == "route_or_plan_changed"
    assert changed.outbox_id == contradicted.outbox_id
    assert changed.snapshot_present is True
    assert changed.snapshot_kind == "staged"
    assert changed.attempt_number == agreeing.attempt_number

    uninterpretable = await create_with_snapshot("plan-vague", {"route": {}})
    vague = await preview_exact_target(temp_storage, _selection(plan="plan-vague"))
    assert vague.category == "ambiguous"
    assert vague.outbox_id == uninterpretable.outbox_id


async def test_preview_reports_snapshot_retirement_for_terminal_row(
    temp_storage,
) -> None:
    from medre.core.storage.backend import OutboxDispatchSnapshotWrite

    await admit_event(temp_storage, _EVENT)
    row = await temp_storage.create_outbox_item(
        _row(),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="staged", context=_matching_context()
        ),
    )
    staged = await preview_exact_target(temp_storage, _selection())
    assert staged.snapshot_present is True
    await temp_storage.claim_due_outbox_items(_claim_now(), "worker-1")
    assert await temp_storage.mark_outbox_sent(row.outbox_id)
    settled = await preview_exact_target(temp_storage, _selection())
    assert settled.category == "completed"
    assert settled.snapshot_present is False
    assert settled.snapshot_kind is None


# ---------------------------------------------------------------------------
# Malformed input refuses before storage
# ---------------------------------------------------------------------------


class _RefusingStorage:
    """Storage double that fails the test if the preview touches it."""

    async def get_metadata(self, event_id: str) -> None:
        raise AssertionError("malformed input must not touch storage")

    async def list_outbox_items_for_event(
        self, event_id: str, *, metadata_only: bool = False
    ) -> list[DeliveryOutboxItem]:
        raise AssertionError("malformed input must not touch storage")

    async def read_event_for_replay(
        self, event_id: str, *, now: datetime | None = None
    ) -> Any:
        raise AssertionError("malformed input must not touch storage")

    async def get_outbox_dispatch_context(
        self,
        outbox_id: str,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
    ) -> None:
        raise AssertionError("malformed input must not touch storage")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expected_generation": -1},
        {"expected_generation": True},
        {"expected_generation": "1"},
        {"expected_fingerprint": ""},
        {"expected_fingerprint": None, "expected_generation": 1.5},
    ],
)
async def test_preview_malformed_expectation_refuses_before_storage(kwargs) -> None:
    with pytest.raises(ValueError):
        await preview_exact_target(_RefusingStorage(), _selection(), **kwargs)
    with pytest.raises(ValueError, match="channel decision"):
        await preview_exact_target(
            _RefusingStorage(),
            ExactTargetSelection(_EVENT, _PLAN, _ADAPTER, ""),
        )


# ---------------------------------------------------------------------------
# Model invariants and serialization
# ---------------------------------------------------------------------------


async def test_preview_fixed_scope_and_execution_authorization(temp_storage) -> None:
    await create_outbox_item_with_parent(temp_storage, _row())
    pending = await preview_exact_target(temp_storage, _selection())
    absent = await preview_exact_target(temp_storage, _selection(event_id="evt-absent"))
    for report in (pending, absent):
        assert report.execution_authorized is False
        assert report.scope == EXACT_TARGET_PREVIEW_SCOPE
        exported = report.to_dict()
        assert exported["execution_authorized"] is False
        assert exported["scope"] == "read_only_current_generation"


async def test_preview_to_dict_is_json_safe_and_complete(temp_storage) -> None:
    await create_outbox_item_with_parent(temp_storage, _row())
    report = await preview_exact_target(temp_storage, _selection())
    exported = report.to_dict()
    assert set(exported) == {
        "scope",
        "execution_authorized",
        "event_id",
        "delivery_plan_id",
        "target_adapter",
        "target_channel",
        "outbox_id",
        "attempt_number",
        "active_attempt",
        "observed_generation",
        "status",
        "updated_at",
        "next_attempt_at",
        "payload_hash",
        "snapshot_present",
        "snapshot_kind",
        "category",
        "excluded_sibling_count",
        "cancel_eligible",
        "observed_fingerprint",
    }
    encoded = json.loads(json.dumps(exported))
    assert encoded["category"] == "pending"
    assert encoded["observed_generation"] == encoded["attempt_number"]
    assert encoded["execution_authorized"] is False


# ---------------------------------------------------------------------------
# Vocabulary freeze parity with the JSON Schema document
# ---------------------------------------------------------------------------


def _preview_schema() -> dict[str, Any]:
    import json

    from pathlib import Path

    directory = Path(__file__).parents[1] / "docs" / "schemas"
    document = directory / "exact-target-preview.schema.json"
    return json.loads(document.read_text(encoding="utf-8"))


def test_schema_category_enum_matches_python_vocabulary() -> None:
    """The closed category vocabulary is identical across Literal and schema."""
    schema = _preview_schema()
    assert set(schema["properties"]["category"]["enum"]) == set(EXACT_TARGET_CATEGORIES)


def test_schema_required_fields_match_model_serialization() -> None:
    """Every schema property is emitted by to_dict, and nothing else."""
    from jsonschema import Draft202012Validator

    schema = _preview_schema()
    report = ExactTargetPreview(
        event_id=_EVENT,
        delivery_plan_id=_PLAN,
        target_adapter=_ADAPTER,
        target_channel=None,
        category="pending",
        outbox_id="obox-1",
        attempt_number=0,
        active_attempt=None,
        observed_fingerprint="a" * 64,
    )
    exported = report.to_dict()
    assert set(schema["required"]) == set(exported)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["scope"] == {"const": EXACT_TARGET_PREVIEW_SCOPE}
    assert schema["properties"]["execution_authorized"] == {"const": False}
    Draft202012Validator(schema).validate(exported)


def test_schema_snapshot_kind_enum_matches_storage_vocabulary() -> None:
    """The snapshot kind enum is the frozen dispatch snapshot vocabulary."""
    from medre.core.storage.backend import OUTBOX_DISPATCH_SNAPSHOT_KINDS

    schema = _preview_schema()
    assert set(schema["properties"]["snapshot_kind"]["enum"]) - {None} == set(
        OUTBOX_DISPATCH_SNAPSHOT_KINDS
    )


# ---------------------------------------------------------------------------
# No-write guarantee
# ---------------------------------------------------------------------------


async def test_preview_never_mutates_storage(temp_storage) -> None:
    event = msgspec.structs.replace(
        make_storage_event(event_id=_EVENT), payload={"body": "retained"}
    )
    await temp_storage.append(event)
    await create_outbox_item_with_parent(temp_storage, _row(target_channel="ch-a"))
    await create_outbox_item_with_parent(temp_storage, _row(target_channel="ch-b"))

    async def observed_state() -> tuple[Any, ...]:
        items = await temp_storage.list_outbox_items_for_event(
            _EVENT, metadata_only=False
        )
        metadata = await temp_storage.get_metadata(_EVENT)
        counts = []
        for table in (
            "delivery_outbox",
            "delivery_outbox_snapshots",
            "delivery_receipts",
            "operator_delivery_decisions",
            "event_content_dependencies",
            "durable_ingress_work",
        ):
            row = await temp_storage._read_one(
                f"SELECT COUNT(*) AS n FROM {table}"  # nosec B608
            )
            counts.append((table, int(row["n"])))
        return (sorted(i.outbox_id for i in items), items, metadata, tuple(counts))

    before = await observed_state()
    first = await preview_exact_target(temp_storage, _selection(channel="ch-a"))
    second = await preview_exact_target(
        temp_storage,
        _selection(channel="ch-a"),
        expected_fingerprint=first.observed_fingerprint,
    )
    assert second.category == "pending"
    assert await observed_state() == before
    again = await preview_exact_target(temp_storage, _selection(channel="ch-a"))
    assert again.observed_fingerprint == first.observed_fingerprint
    assert again.to_dict() == first.to_dict()


# ---------------------------------------------------------------------------
# Fingerprint sensitivity
# ---------------------------------------------------------------------------


async def test_preview_fingerprint_tracks_reservation_and_status(temp_storage) -> None:
    row = await create_outbox_item_with_parent(temp_storage, _row())
    baseline = await preview_exact_target(temp_storage, _selection())
    claimed = await temp_storage.claim_due_outbox_items(_claim_now(), "worker-1")
    assert row.outbox_id in {item.outbox_id for item in claimed}
    claimed_view = await preview_exact_target(temp_storage, _selection())
    assert claimed_view.observed_fingerprint != baseline.observed_fingerprint
    reserved = await temp_storage.reserve_outbox_attempt(row.outbox_id, "worker-1", 1)
    assert reserved == 2
    reserved_view = await preview_exact_target(temp_storage, _selection())
    assert reserved_view.observed_fingerprint != claimed_view.observed_fingerprint
    assert reserved_view.active_attempt == 2
