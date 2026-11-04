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
            "historical",
            "attempt_superseded",
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
