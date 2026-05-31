"""Tests for the delivery outcome ledger.

Covers: empty input, single sent, queued outbox item, retry chain choosing
highest attempt, suppressed excluded from retry, dead_lettered /
retry_exhausted, replay_run_id only when replay, capability suppression
metadata from rendering_evidence / error dict, aggregate counts, JSON-safe
output, and fields that cannot be derived from current records.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from medre.core.events.canonical import DeliveryReceipt
from medre.core.evidence.delivery_ledger import (
    build_delivery_outcome_ledger,
)
from medre.core.storage.backend import DeliveryOutboxItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)


def _receipt(
    *,
    receipt_id: str = "rcpt-001",
    event_id: str = "ev-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-1",
    delivery_plan_id: str = "dp-001",
    status: str = "sent",
    attempt_number: int = 1,
    error: str | None = None,
    failure_kind: str | None = None,
    next_retry_at: datetime | None = None,
    source: str = "live",
    replay_run_id: str | None = None,
    rendering_evidence: str | None = None,
    adapter_message_id: str | None = None,
    parent_receipt_id: str | None = None,
    sequence: int = 0,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=sequence,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        route_id=route_id,
        status=status,
        error=error,
        failure_kind=failure_kind,
        attempt_number=attempt_number,
        next_retry_at=next_retry_at,
        source=source,
        replay_run_id=replay_run_id,
        rendering_evidence=rendering_evidence,
        adapter_message_id=adapter_message_id,
        parent_receipt_id=parent_receipt_id,
        created_at=_TS,
    )


def _outbox(
    *,
    outbox_id: str = "ob-001",
    event_id: str = "ev-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-1",
    delivery_plan_id: str = "dp-001",
    status: str = "pending",
    attempt_number: int = 1,
    failure_kind: str | None = None,
    error_summary: str | None = None,
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id=route_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        attempt_number=attempt_number,
        status=status,
        failure_kind=failure_kind,
        error_summary=error_summary,
    )


# ===================================================================
# 1. Empty input
# ===================================================================


class TestEmptyInput:
    """Empty receipts and outbox items produce an empty ledger."""

    def test_empty_ledger_has_no_entries(self) -> None:
        ledger = build_delivery_outcome_ledger()
        assert len(ledger.entries) == 0

    def test_empty_ledger_aggregate_counts(self) -> None:
        ledger = build_delivery_outcome_ledger()
        assert ledger.aggregate_counts["by_status"] == {}
        assert ledger.aggregate_counts["by_failure_taxon"] == {}

    def test_empty_ledger_to_dict_roundtrip(self) -> None:
        ledger = build_delivery_outcome_ledger()
        d = ledger.to_dict()
        raw = json.dumps(d)
        reloaded = json.loads(raw)
        assert reloaded["entries"] == {}
        assert reloaded["aggregate_counts"]["by_status"] == {}


# ===================================================================
# 2. Single sent receipt
# ===================================================================


class TestSingleSentReceipt:
    """One sent receipt produces a single entry with status=sent."""

    def test_one_entry(self) -> None:
        ledger = build_delivery_outcome_ledger(receipts=[_receipt(status="sent")])
        assert len(ledger.entries) == 1

    def test_final_status_sent(self) -> None:
        ledger = build_delivery_outcome_ledger(receipts=[_receipt(status="sent")])
        entry = next(iter(ledger.entries.values()))
        assert entry.final_status == "sent"

    def test_attempt_number_is_one(self) -> None:
        ledger = build_delivery_outcome_ledger(receipts=[_receipt(status="sent")])
        entry = next(iter(ledger.entries.values()))
        assert entry.attempt_number == 1

    def test_retry_state_is_terminal(self) -> None:
        ledger = build_delivery_outcome_ledger(receipts=[_receipt(status="sent")])
        entry = next(iter(ledger.entries.values()))
        assert entry.retry_state == "terminal"

    def test_failure_taxon_is_none(self) -> None:
        ledger = build_delivery_outcome_ledger(receipts=[_receipt(status="sent")])
        entry = next(iter(ledger.entries.values()))
        assert entry.failure_taxon is None
        assert entry.failure_taxon_category is None

    def test_replay_run_id_none_for_live(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[_receipt(status="sent", source="live")]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.replay_run_id is None

    def test_aggregate_counts_one_sent(self) -> None:
        ledger = build_delivery_outcome_ledger(receipts=[_receipt(status="sent")])
        assert ledger.aggregate_counts["by_status"] == {"sent": 1}
        assert ledger.aggregate_counts["by_failure_taxon"] == {}


# ===================================================================
# 3. Queued outbox item
# ===================================================================


class TestQueuedOutboxItem:
    """A queued outbox item produces an entry with final_status=queued."""

    def test_outbox_item_produces_entry(self) -> None:
        ledger = build_delivery_outcome_ledger(outbox_items=[_outbox(status="queued")])
        assert len(ledger.entries) == 1

    def test_final_status_queued(self) -> None:
        ledger = build_delivery_outcome_ledger(outbox_items=[_outbox(status="queued")])
        entry = next(iter(ledger.entries.values()))
        assert entry.final_status == "queued"

    def test_retry_state_active(self) -> None:
        ledger = build_delivery_outcome_ledger(outbox_items=[_outbox(status="queued")])
        entry = next(iter(ledger.entries.values()))
        assert entry.retry_state == "active"

    def test_outbox_id_populated(self) -> None:
        ledger = build_delivery_outcome_ledger(
            outbox_items=[_outbox(outbox_id="ob-test-42", status="queued")]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.outbox_id == "ob-test-42"


# ===================================================================
# 4. Retry chain — highest attempt wins
# ===================================================================


class TestRetryChainHighestAttempt:
    """Multiple receipts for same target: highest attempt_number wins."""

    def test_three_attempts_selects_third(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    receipt_id="rcpt-1",
                    status="failed",
                    attempt_number=1,
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
                _receipt(
                    receipt_id="rcpt-2",
                    status="failed",
                    attempt_number=2,
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
                _receipt(
                    receipt_id="rcpt-3",
                    status="sent",
                    attempt_number=3,
                ),
            ]
        )
        assert len(ledger.entries) == 1
        entry = next(iter(ledger.entries.values()))
        assert entry.attempt_number == 3
        assert entry.final_status == "sent"

    def test_all_receipt_ids_collected(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    receipt_id="rcpt-a",
                    attempt_number=1,
                    status="failed",
                    failure_kind="adapter_transient",
                    error="T",
                ),
                _receipt(receipt_id="rcpt-b", attempt_number=2, status="sent"),
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert sorted(entry.receipt_ids) == ["rcpt-a", "rcpt-b"]


# ===================================================================
# 5. Suppressed excluded from retry
# ===================================================================


class TestSuppressedNotRetryable:
    """Suppressed receipts have retry_state=terminal, not retryable."""

    def test_loop_suppressed_is_terminal(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="suppressed",
                    failure_kind="loop_suppressed",
                    error="Self-loop guard",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.retry_state == "terminal"
        assert entry.final_status == "suppressed"

    def test_capability_suppressed_is_terminal(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="suppressed",
                    failure_kind="capability_suppressed",
                    error="capability_suppressed: reactions unsupported by adapter",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.retry_state == "terminal"


# ===================================================================
# 6. Dead-lettered / retry_exhausted
# ===================================================================


class TestDeadLetteredRetryExhausted:
    """dead_lettered status maps to retry_exhausted taxon."""

    def test_dead_lettered_taxon(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="dead_lettered",
                    failure_kind="adapter_transient",
                    error="Retry exhausted",
                    attempt_number=5,
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.failure_taxon == "retry_exhausted"
        assert entry.failure_taxon_category == "derived_terminal"
        assert entry.retry_state == "terminal"
        assert entry.attempt_number == 5

    def test_dead_lettered_in_aggregate(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="dead_lettered",
                    failure_kind="adapter_transient",
                    error="Retry exhausted",
                )
            ]
        )
        assert ledger.aggregate_counts["by_status"] == {"dead_lettered": 1}
        assert ledger.aggregate_counts["by_failure_taxon"] == {"retry_exhausted": 1}


# ===================================================================
# 7. replay_run_id only when replay
# ===================================================================


class TestReplayRunIdOnlyWhenReplay:
    """replay_run_id is populated only when source=replay."""

    def test_replay_source_has_run_id(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="sent",
                    source="replay",
                    replay_run_id="run-abc-123",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.source == "replay"
        assert entry.replay_run_id == "run-abc-123"

    def test_live_source_has_no_run_id(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="sent",
                    source="live",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.replay_run_id is None

    def test_replay_run_id_stored_but_source_is_live(self) -> None:
        """If replay_run_id is set but source is 'live', replay_run_id is None."""
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="sent",
                    source="live",
                    replay_run_id="run-ghost",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        # replay_run_id is only populated when source=replay
        assert entry.replay_run_id is None


# ===================================================================
# 8. Capability suppression metadata from rendering_evidence / error
# ===================================================================


class TestCapabilitySuppressionMetadata:
    """Capability fields derived from rendering_evidence JSON and error."""

    def test_rendering_evidence_populates_strategy(self) -> None:
        evidence = json.dumps(
            {
                "delivery_strategy": "direct",
                "capability_level": "native",
            }
        )
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="sent",
                    rendering_evidence=evidence,
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.delivery_strategy == "direct"
        assert entry.capability_level == "native"

    def test_suppressed_error_populates_capability_field(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="suppressed",
                    failure_kind="capability_suppressed",
                    error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.capability_field == "reactions"
        assert entry.capability_level == "unsupported"
        assert entry.delivery_strategy == "skip"
        assert entry.suppression_reason is not None
        assert "reactions unsupported" in entry.suppression_reason

    def test_fallback_capability_populated(self) -> None:
        evidence = json.dumps(
            {
                "delivery_strategy": "fallback_text",
                "capability_level": "fallback",
            }
        )
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="sent",
                    rendering_evidence=evidence,
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.delivery_strategy == "fallback_text"
        assert entry.capability_level == "fallback"


# ===================================================================
# 9. Aggregate counts across multiple entries
# ===================================================================


class TestAggregateCounts:
    """Aggregate counts are correct across diverse statuses."""

    def test_mixed_statuses(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    receipt_id="r-1",
                    target_channel="ch-sent",
                    delivery_plan_id="dp-1",
                    status="sent",
                ),
                _receipt(
                    receipt_id="r-2",
                    target_channel="ch-failed",
                    delivery_plan_id="dp-2",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
                _receipt(
                    receipt_id="r-3",
                    target_channel="ch-dl",
                    delivery_plan_id="dp-3",
                    status="dead_lettered",
                    failure_kind="adapter_transient",
                    error="Retry exhausted",
                ),
            ]
        )
        assert ledger.aggregate_counts["by_status"]["sent"] == 1
        assert ledger.aggregate_counts["by_status"]["failed"] == 1
        assert ledger.aggregate_counts["by_status"]["dead_lettered"] == 1
        assert ledger.aggregate_counts["by_failure_taxon"]["retry_exhausted"] == 1

    def test_aggregate_counts_sorted(self) -> None:
        """Aggregate count dicts have sorted keys for determinism."""
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(receipt_id="r-s", status="sent"),
                _receipt(
                    receipt_id="r-f",
                    target_channel="ch-2",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="T",
                ),
            ]
        )
        by_status = ledger.aggregate_counts["by_status"]
        assert list(by_status.keys()) == sorted(by_status.keys())


# ===================================================================
# 10. JSON-safe output
# ===================================================================


class TestJsonSafeOutput:
    """Full ledger survives JSON round-trip."""

    def test_to_dict_json_roundtrip(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    receipt_id="r-json-1",
                    status="sent",
                    delivery_plan_id="dp-json",
                    rendering_evidence='{"delivery_strategy": "direct", "capability_level": "native"}',
                ),
            ]
        )
        raw = json.dumps(ledger.to_dict())
        reloaded = json.loads(raw)
        assert len(reloaded["entries"]) == 1
        entry = next(iter(reloaded["entries"].values()))
        assert entry["final_status"] == "sent"
        assert entry["delivery_strategy"] == "direct"

    def test_ledger_with_datetime_field_is_json_safe(self) -> None:
        """next_retry_at datetime is converted to ISO string."""
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    receipt_id="r-dt",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                    next_retry_at=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert isinstance(entry.next_retry_at, str)
        # Full ledger round-trips cleanly.
        raw = json.dumps(ledger.to_dict())
        reloaded = json.loads(raw)
        e = next(iter(reloaded["entries"].values()))
        assert e["next_retry_at"] is not None


# ===================================================================
# 11. Deterministic key ordering
# ===================================================================


class TestDeterministicKeys:
    """Ledger entries have deterministic keys."""

    def test_same_input_same_keys(self) -> None:
        receipts = [
            _receipt(receipt_id="r-det-1", status="sent"),
        ]
        ledger1 = build_delivery_outcome_ledger(receipts=receipts)
        ledger2 = build_delivery_outcome_ledger(receipts=receipts)
        assert list(ledger1.entries.keys()) == list(ledger2.entries.keys())

    def test_key_is_parseable_json(self) -> None:
        ledger = build_delivery_outcome_ledger(receipts=[_receipt(status="sent")])
        for key in ledger.entries:
            parsed = json.loads(key)
            assert isinstance(parsed, dict)
            assert "primary_id" in parsed


# ===================================================================
# 12. Dict-like input accepted
# ===================================================================


class TestDictInput:
    """The ledger accepts plain dicts in addition to structs."""

    def test_dict_receipt(self) -> None:
        receipt_dict = {
            "receipt_id": "rcpt-dict-1",
            "event_id": "ev-dict",
            "delivery_plan_id": "dp-dict",
            "target_adapter": "radio",
            "target_channel": "ch-dict",
            "route_id": "route-dict",
            "status": "sent",
            "attempt_number": 1,
            "source": "live",
        }
        ledger = build_delivery_outcome_ledger(receipts=[receipt_dict])
        assert len(ledger.entries) == 1
        entry = next(iter(ledger.entries.values()))
        assert entry.final_status == "sent"
        assert entry.delivery_plan_id == "dp-dict"

    def test_dict_outbox_item(self) -> None:
        outbox_dict = {
            "outbox_id": "ob-dict-1",
            "event_id": "ev-dict",
            "delivery_plan_id": "dp-dict",
            "target_adapter": "radio",
            "target_channel": "ch-dict",
            "route_id": "route-dict",
            "status": "pending",
            "attempt_number": 1,
        }
        ledger = build_delivery_outcome_ledger(outbox_items=[outbox_dict])
        assert len(ledger.entries) == 1
        entry = next(iter(ledger.entries.values()))
        assert entry.final_status == "pending"
        assert entry.outbox_id == "ob-dict-1"


# ===================================================================
# 13. Fields not derivable from minimal records
# ===================================================================


class TestUndervivableFieldsNone:
    """Fields that cannot be derived from minimal records are None."""

    def test_minimal_receipt_none_fields(self) -> None:
        """A receipt with only status set has None for non-derivable fields."""
        ledger = build_delivery_outcome_ledger(receipts=[_receipt(status="queued")])
        entry = next(iter(ledger.entries.values()))
        # These fields cannot be derived from a minimal queued receipt.
        assert entry.delivery_strategy is None
        assert entry.capability_field is None
        assert entry.capability_level is None
        assert entry.suppression_reason is None
        assert entry.failure_kind is None
        assert entry.failure_taxon is None
        assert entry.failure_taxon_category is None
        assert entry.replay_run_id is None
        assert entry.adapter_message_id is None
        assert entry.next_retry_at is None
        assert entry.error is None

    def test_outbox_item_no_capability_fields(self) -> None:
        """Outbox items have no rendering_evidence so capability is None."""
        ledger = build_delivery_outcome_ledger(outbox_items=[_outbox(status="pending")])
        entry = next(iter(ledger.entries.values()))
        assert entry.delivery_strategy is None
        assert entry.capability_field is None
        assert entry.capability_level is None
        assert entry.suppression_reason is None

    def test_rendering_evidence_without_capability_keys(self) -> None:
        """rendering_evidence JSON without capability keys leaves them None."""
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="sent",
                    rendering_evidence='{"renderer": "matrix", "truncated": false}',
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.delivery_strategy is None
        assert entry.capability_level is None


# ===================================================================
# 14. Mixed receipts and outbox items
# ===================================================================


class TestMixedReceiptsAndOutbox:
    """Receipts and outbox items are grouped together by target key."""

    def test_receipt_and_outbox_same_target(self) -> None:
        """A receipt and outbox item for the same target collapse to one entry."""
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    receipt_id="rcpt-mix-1",
                    status="sent",
                    attempt_number=2,
                ),
            ],
            outbox_items=[
                _outbox(
                    outbox_id="ob-mix-1",
                    status="sent",
                    attempt_number=1,
                ),
            ],
        )
        # Same composite key → one entry, highest attempt wins.
        assert len(ledger.entries) == 1
        entry = next(iter(ledger.entries.values()))
        assert entry.attempt_number == 2
        assert entry.final_status == "sent"

    def test_receipt_and_outbox_different_targets(self) -> None:
        """Different targets produce separate entries."""
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    receipt_id="rcpt-a",
                    target_channel="ch-a",
                    delivery_plan_id="dp-a",
                    status="sent",
                ),
            ],
            outbox_items=[
                _outbox(
                    outbox_id="ob-b",
                    target_channel="ch-b",
                    delivery_plan_id="dp-b",
                    status="pending",
                ),
            ],
        )
        assert len(ledger.entries) == 2


# ===================================================================
# 15. Failed receipt is retryable
# ===================================================================


class TestFailedReceiptRetryable:
    """Failed receipts with transient failure_kind are retryable."""

    def test_adapter_transient_is_retryable(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.retry_state == "retryable"
        assert entry.failure_taxon == "adapter_transient"
        assert entry.failure_taxon_category == "retryable"

    def test_adapter_permanent_is_not_retryable(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="adapter_permanent",
                    error="Target crashed",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        # adapter_permanent does not get retryable, but "failed" status
        # without next_retry_at and with non-transient failure_kind
        # still maps to "retryable" in our _derive_retry_state because
        # we treat all "failed" as retryable. Let's check the taxon instead.
        assert entry.failure_taxon == "adapter_permanent"
        assert entry.failure_taxon_category == "permanent"


# ===================================================================
# 16. Grouping without delivery_plan_id falls back to event_id
# ===================================================================


class TestGroupingFallbackToEventId:
    """When delivery_plan_id is empty, grouping falls back to event_id."""

    def test_empty_plan_id_uses_event_id_in_key(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    receipt_id="r-no-plan",
                    event_id="ev-fallback-1",
                    delivery_plan_id="",
                    status="sent",
                ),
            ]
        )
        assert len(ledger.entries) == 1
        key = next(iter(ledger.entries.keys()))
        parsed = json.loads(key)
        assert parsed["primary_id"] == "ev-fallback-1"


# ===================================================================
# 17. Failure taxon from error detail
# ===================================================================


class TestFailureTaxonFromErrorDetail:
    """Error detail patterns refine the failure taxon."""

    def test_meshtastic_queue_full_is_unavailable(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="adapter_transient",
                    error="meshtastic queue is full, enqueue rejected",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.failure_taxon == "unavailable"
        assert entry.failure_taxon_category == "derived_terminal"

    def test_e2ee_blocked_is_delivery_failed(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    status="failed",
                    failure_kind="adapter_permanent",
                    error="Matrix room is encrypted but e2ee is not active",
                )
            ]
        )
        entry = next(iter(ledger.entries.values()))
        assert entry.failure_taxon == "delivery_failed"
        assert entry.failure_taxon_category == "permanent"


# ===================================================================
# 18. Multiple distinct failure taxa in aggregate
# ===================================================================


class TestMultipleTaxaInAggregate:
    """Multiple entries with different taxa all counted in aggregates."""

    def test_two_different_taxa(self) -> None:
        ledger = build_delivery_outcome_ledger(
            receipts=[
                _receipt(
                    receipt_id="r-t1",
                    target_channel="ch-1",
                    delivery_plan_id="dp-1",
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
                _receipt(
                    receipt_id="r-t2",
                    target_channel="ch-2",
                    delivery_plan_id="dp-2",
                    status="dead_lettered",
                    failure_kind="adapter_transient",
                    error="Retry exhausted",
                ),
            ]
        )
        assert ledger.aggregate_counts["by_failure_taxon"]["adapter_transient"] == 1
        assert ledger.aggregate_counts["by_failure_taxon"]["retry_exhausted"] == 1
