"""Exact native-evidence ledger binding is read-only and fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.core.evidence.native_evidence import NativeEvidenceObservation
from medre.core.evidence.native_ledger_binding import (
    assess_native_evidence_ledger_binding,
)
from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage


def _observation(**changes: object) -> NativeEvidenceObservation:
    data = dict(
        event_id="evt",
        delivery_plan_id="plan",
        target_adapter="matrix-ci",
        target_channel="!room:local",
        outbox_id="outbox",
        attempt_number=2,
        observed_row_updated_at="t2",
        provider="matrix",
        provider_repository="jeremiah-k/medre-adapter-matrix",
        compatibility_pin="a" * 40,
        observation_source="synapse_second_client",
        tier="docker",
        scope="provider_acceptance",
        result="accepted",
        observed_at=datetime.now(UTC),
        receipt_id="receipt-2",
        native_message_id="$native",
        evidence_reference=None,
    )
    data.update(changes)
    return NativeEvidenceObservation(**data)


def _storage(**row_changes: object) -> SimpleNamespace:
    row = dict(
        outbox_id="outbox",
        event_id="evt",
        delivery_plan_id="plan",
        target_adapter="matrix-ci",
        target_channel="!room:local",
        attempt_number=2,
        active_attempt=None,
        updated_at="t2",
    )
    row.update(row_changes)
    receipt = SimpleNamespace(
        outbox_id="outbox",
        event_id="evt",
        delivery_plan_id="plan",
        target_adapter="matrix-ci",
        target_channel="!room:local",
        attempt_number=2,
        receipt_kind="attempt",
        receipt_id="receipt-2",
        adapter_message_id="$native",
    )
    item = SimpleNamespace(**row)
    return SimpleNamespace(
        get_outbox_item=AsyncMock(return_value=item),
        list_outbox_items_for_delivery=AsyncMock(return_value=[item]),
        list_receipts_for_outbox=AsyncMock(return_value=[receipt]),
        list_receipts_for_delivery=AsyncMock(return_value=[receipt]),
    )


@pytest.mark.parametrize(
    ("changes", "row", "expected", "reason"),
    [
        ({}, {}, "current", "exact"),
        ({"event_id": "other"}, {}, "unbound", "identity_mismatch"),
        ({"attempt_number": 3}, {}, "unbound", "attempt_unrecognized"),
        ({"receipt_id": "other"}, {}, "unbound", "receipt_mismatch"),
        ({"native_message_id": "$other"}, {}, "unbound", "native_reference_mismatch"),
        (
            {
                "attempt_number": 1,
                "result": "unresolved",
                "receipt_id": None,
                "native_message_id": None,
            },
            {},
            "unbound",
            "attempt_unrecognized",
        ),
        ({"observed_row_updated_at": "old"}, {}, "historical", "row_changed"),
    ],
)
async def test_native_binding_compares_exact_identity_and_attempt(
    changes: dict[str, object], row: dict[str, object], expected: str, reason: str
) -> None:
    storage = _storage(**row)
    result = await assess_native_evidence_ledger_binding(
        storage, _observation(**changes)
    )
    assert (result.state, result.reason) == (expected, reason)
    storage.get_outbox_item.assert_awaited_once_with("outbox", metadata_only=True)


async def test_missing_row_and_receipt_never_bind() -> None:
    storage = _storage()
    storage.get_outbox_item.return_value = None
    result = await assess_native_evidence_ledger_binding(storage, _observation())
    assert (result.state, result.reason) == ("unbound", "row_missing")
    storage.list_receipts_for_outbox.assert_not_awaited()
    storage = _storage()
    storage.list_receipts_for_outbox.return_value = []
    result = await assess_native_evidence_ledger_binding(storage, _observation())
    assert (result.state, result.reason) == ("unbound", "receipt_missing")


async def test_storage_errors_propagate_without_downgrading_to_an_outcome() -> None:
    storage = _storage()
    storage.get_outbox_item.side_effect = OSError("storage read unavailable")
    with pytest.raises(OSError, match="storage read unavailable"):
        await assess_native_evidence_ledger_binding(storage, _observation())


async def test_absence_statement_can_bind_an_identity_but_never_proves_a_send() -> None:
    storage = _storage()
    absence = _observation(
        result="not_observed",
        receipt_id=None,
        native_message_id=None,
        evidence_reference=None,
    )
    result = await assess_native_evidence_ledger_binding(storage, absence)
    assert result.state == "current"
    assert absence.resolution == "unresolved"
    storage.list_receipts_for_outbox.assert_not_awaited()


async def test_positive_claim_without_a_correlation_reference_is_unbound() -> None:
    storage = _storage()
    observation = _observation(receipt_id=None, native_message_id=None)
    result = await assess_native_evidence_ledger_binding(storage, observation)
    assert (result.state, result.reason) == ("unbound", "receipt_missing")
    storage.list_receipts_for_outbox.assert_not_awaited()


async def test_newer_replay_row_makes_older_receipt_historical() -> None:
    storage = _storage()
    storage.list_outbox_items_for_delivery.return_value.append(
        SimpleNamespace(
            outbox_id="replay-generation", attempt_number=3, active_attempt=None
        )
    )
    result = await assess_native_evidence_ledger_binding(storage, _observation())
    assert (result.state, result.reason) == ("historical", "attempt_superseded")
    storage.list_outbox_items_for_delivery.assert_awaited_once_with(
        _observation().identity
    )


async def test_competing_equal_generation_never_classifies_current() -> None:
    storage = _storage()
    storage.list_outbox_items_for_delivery.return_value.append(
        SimpleNamespace(
            outbox_id="competing-row", attempt_number=2, active_attempt=None
        )
    )
    result = await assess_native_evidence_ledger_binding(storage, _observation())
    assert (result.state, result.reason) == ("unbound", "generation_ambiguous")


async def test_missing_row_from_identity_history_is_not_current() -> None:
    storage = _storage()
    storage.list_outbox_items_for_delivery.return_value = []
    result = await assess_native_evidence_ledger_binding(storage, _observation())
    assert (result.state, result.reason) == ("unbound", "row_missing")


async def test_conflicting_projections_of_one_row_refuse_current() -> None:
    storage = _storage()
    storage.list_outbox_items_for_delivery.return_value = [
        SimpleNamespace(
            outbox_id="outbox", attempt_number=2, active_attempt=None, updated_at="t3"
        )
    ]
    result = await assess_native_evidence_ledger_binding(storage, _observation())
    assert (result.state, result.reason) == ("unbound", "generation_ambiguous")


async def test_newer_generation_wins_over_equal_competing_generation() -> None:
    storage = _storage()
    storage.list_outbox_items_for_delivery.return_value.extend(
        [
            SimpleNamespace(outbox_id="equal", attempt_number=2, active_attempt=None),
            SimpleNamespace(outbox_id="newer", attempt_number=3, active_attempt=None),
        ]
    )
    result = await assess_native_evidence_ledger_binding(storage, _observation())
    assert (result.state, result.reason) == ("historical", "attempt_superseded")


async def test_history_backend_errors_propagate() -> None:
    storage = _storage()
    storage.list_outbox_items_for_delivery.side_effect = OSError("history unavailable")
    with pytest.raises(OSError, match="history unavailable"):
        await assess_native_evidence_ledger_binding(storage, _observation())


async def test_lower_attempt_with_matching_durable_receipt_is_historical() -> None:
    storage = _storage()
    previous = SimpleNamespace(
        outbox_id="outbox",
        event_id="evt",
        delivery_plan_id="plan",
        target_adapter="matrix-ci",
        target_channel="!room:local",
        attempt_number=1,
        receipt_kind="attempt",
        receipt_id="receipt-1",
        adapter_message_id="$earlier",
    )
    storage.list_receipts_for_outbox.return_value = [previous]
    observation = _observation(
        attempt_number=1,
        result="unresolved",
        receipt_id=None,
        native_message_id=None,
    )
    result = await assess_native_evidence_ledger_binding(storage, observation)
    assert (result.state, result.reason) == ("historical", "attempt_superseded")


async def test_lower_attempt_without_proof_cannot_be_historical() -> None:
    storage = _storage()
    storage.list_receipts_for_outbox.return_value = []
    observation = _observation(
        attempt_number=0,
        result="not_observed",
        receipt_id=None,
        native_message_id=None,
    )
    result = await assess_native_evidence_ledger_binding(storage, observation)
    assert (result.state, result.reason) == ("unbound", "attempt_unrecognized")


@pytest.mark.parametrize("newer_row", [False, True])
@pytest.mark.parametrize("has_receipt", [False, True])
@pytest.mark.parametrize("result", ["not_observed", "unresolved"])
async def test_superseded_attempt_requires_receipt_for_every_generation_path(
    newer_row: bool, has_receipt: bool, result: str
) -> None:
    storage = _storage(active_attempt=None if newer_row else 3)
    if newer_row:
        storage.list_outbox_items_for_delivery.return_value.append(
            SimpleNamespace(
                outbox_id="replay-outbox", attempt_number=3, active_attempt=None
            )
        )
    if not has_receipt:
        storage.list_receipts_for_outbox.return_value = []
    observation = _observation(result=result, receipt_id=None, native_message_id=None)
    binding = await assess_native_evidence_ledger_binding(storage, observation)
    expected = (
        ("historical", "attempt_superseded")
        if has_receipt
        else ("unbound", "attempt_unrecognized")
    )
    assert (binding.state, binding.reason) == expected
    storage.list_receipts_for_outbox.assert_awaited_once_with("outbox")


@pytest.mark.parametrize("has_receipt", [False, True])
async def test_reserved_sqlite_attempt_does_not_prove_the_finalized_counter(
    temp_storage: SQLiteStorage, sample_event: CanonicalEvent, has_receipt: bool
) -> None:
    await temp_storage.append(sample_event)
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="outbox",
            event_id=sample_event.event_id,
            delivery_plan_id="plan",
            target_adapter="matrix-ci",
            target_channel="!room:local",
            route_id="route",
            attempt_number=2,
            status="in_progress",
            worker_id="worker",
        )
    )
    if has_receipt:
        await temp_storage.append_receipt(
            DeliveryReceipt(
                receipt_id="receipt-2",
                event_id=sample_event.event_id,
                delivery_plan_id="plan",
                target_adapter="matrix-ci",
                target_channel="!room:local",
                route_id="route",
                attempt_number=2,
                status="sent",
                outbox_id="outbox",
                created_at=datetime.now(UTC),
            )
        )
    assert await temp_storage.reserve_outbox_attempt("outbox", "worker", 2) == 3
    binding = await assess_native_evidence_ledger_binding(
        temp_storage,
        _observation(
            event_id=sample_event.event_id,
            result="not_observed",
            receipt_id=None,
            native_message_id=None,
            observed_row_updated_at=None,
        ),
    )
    expected = (
        ("historical", "attempt_superseded")
        if has_receipt
        else ("unbound", "attempt_unrecognized")
    )
    assert (binding.state, binding.reason) == expected


@pytest.mark.parametrize("same_row", [False, True])
@pytest.mark.parametrize("omit_native_reference", [False, True])
async def test_accepted_native_reference_reused_by_prior_attempt_is_unbound(
    same_row: bool, omit_native_reference: bool
) -> None:
    storage = _storage()
    prior = SimpleNamespace(
        outbox_id="outbox" if same_row else "older-outbox",
        event_id="evt",
        delivery_plan_id="plan",
        target_adapter="matrix-ci",
        target_channel="!room:local",
        attempt_number=1,
        receipt_kind="attempt",
        receipt_id="receipt-1",
        adapter_message_id="$native",
    )
    if not same_row:
        storage.list_outbox_items_for_delivery.return_value.append(
            SimpleNamespace(
                outbox_id="older-outbox", attempt_number=1, active_attempt=None
            )
        )
    storage.list_receipts_for_delivery.return_value = [
        prior,
        *storage.list_receipts_for_outbox.return_value,
    ]
    observation = _observation(
        native_message_id=None if omit_native_reference else "$native"
    )
    result = await assess_native_evidence_ledger_binding(storage, observation)
    assert (result.state, result.reason) == ("unbound", "native_reference_reused")
    storage.list_receipts_for_delivery.assert_awaited_once_with(observation.identity)


async def test_later_reuse_does_not_erase_earlier_historical_binding() -> None:
    storage = _storage(attempt_number=1)
    older = SimpleNamespace(
        outbox_id="outbox",
        event_id="evt",
        delivery_plan_id="plan",
        target_adapter="matrix-ci",
        target_channel="!room:local",
        attempt_number=1,
        receipt_kind="attempt",
        receipt_id="receipt-1",
        adapter_message_id="$native",
    )
    storage.list_receipts_for_outbox.return_value = [older]
    storage.list_receipts_for_delivery.return_value = [
        older,
        SimpleNamespace(
            event_id="evt",
            delivery_plan_id="plan",
            target_adapter="matrix-ci",
            target_channel="!room:local",
            attempt_number=2,
            receipt_kind="attempt",
            receipt_id="receipt-2",
            adapter_message_id="$native",
        ),
    ]
    storage.list_outbox_items_for_delivery.return_value.append(
        SimpleNamespace(
            outbox_id="replay-outbox", attempt_number=2, active_attempt=None
        )
    )
    observation = _observation(attempt_number=1, receipt_id="receipt-1")
    result = await assess_native_evidence_ledger_binding(storage, observation)
    assert (result.state, result.reason) == ("historical", "attempt_superseded")


async def test_reused_reference_ledger_read_error_propagates() -> None:
    storage = _storage()
    storage.list_receipts_for_delivery.side_effect = OSError(
        "receipt ledger unavailable"
    )
    with pytest.raises(OSError, match="receipt ledger unavailable"):
        await assess_native_evidence_ledger_binding(storage, _observation())


async def test_receipt_from_another_outbox_does_not_bind() -> None:
    storage = _storage()
    receipt = storage.list_receipts_for_outbox.return_value[0]
    receipt.outbox_id = "different-outbox"
    result = await assess_native_evidence_ledger_binding(storage, _observation())
    assert (result.state, result.reason) == ("unbound", "receipt_missing")


async def test_native_reference_from_different_identity_does_not_collide() -> None:
    storage = _storage()
    storage.list_receipts_for_delivery.return_value.insert(
        0,
        SimpleNamespace(
            event_id="evt",
            delivery_plan_id="plan",
            target_adapter="matrix-ci",
            target_channel="!other:local",
            attempt_number=1,
            receipt_kind="attempt",
            receipt_id="different-channel",
            adapter_message_id="$native",
        ),
    )
    result = await assess_native_evidence_ledger_binding(storage, _observation())
    assert (result.state, result.reason) == ("current", "exact")


async def test_accepted_receipt_only_still_checks_native_reference_reuse() -> None:
    storage = _storage()
    storage.list_receipts_for_delivery.return_value.insert(
        0,
        SimpleNamespace(
            event_id="evt",
            delivery_plan_id="plan",
            target_adapter="matrix-ci",
            target_channel="!room:local",
            attempt_number=1,
            receipt_kind="attempt",
            receipt_id="earlier",
            adapter_message_id="$native",
        ),
    )
    observation = _observation(native_message_id=None)
    result = await assess_native_evidence_ledger_binding(storage, observation)
    assert (result.state, result.reason) == ("unbound", "native_reference_reused")
