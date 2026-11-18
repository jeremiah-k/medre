"""Work-status projection tests: categories, accounting, honesty labels.

These pin the projection vocabulary and its invariants before any display
field exists. The SQL projection in the storage layer is checked against
the pure classifier here by conformance tests in the storage suite.
"""

from __future__ import annotations

import pytest

from medre.core.engine.pipeline.delivery_state import OUTBOX_STATUSES
from medre.core.observability.work_status import (
    OUTSTANDING_CATEGORIES,
    WORK_STATUS_CATEGORIES,
    WORK_STATUS_DEFINITION,
    AdapterWorkStatus,
    WorkStatusUncertainty,
    assemble_work_status_report,
    outstanding_age_origin,
    work_status_category,
)

_SAMPLED = "2026-01-02T00:00:00+00:00"
_PAST = "2026-01-01T00:00:00+00:00"
_FUTURE = "2026-01-03T00:00:00+00:00"


@pytest.mark.parametrize(
    ("status", "next_attempt_at", "expected"),
    [
        ("pending", None, "unstarted_eligible"),
        ("pending", _PAST, "unstarted_eligible"),
        ("retry_wait", _PAST, "unstarted_eligible"),
        ("retry_wait", None, "unstarted_eligible"),
        ("retry_wait", _FUTURE, "retry_scheduled"),
        ("retry_wait", _SAMPLED, "unstarted_eligible"),
        ("in_progress", None, "reserved_active"),
        ("queued", None, "native_queue"),
        ("abandoned", None, "uncertain_outcome"),
        ("sent", None, "terminal_outcome"),
        ("dead_lettered", None, "terminal_outcome"),
        ("cancelled", None, "terminal_outcome"),
    ],
)
def test_category_classification_table(
    status: str, next_attempt_at: str | None, expected: str
) -> None:
    assert (
        work_status_category(status, next_attempt_at=next_attempt_at, sampled_at=_SAMPLED)
        == expected
    )


def test_every_stored_outbox_status_classifies() -> None:
    """The stored status vocabulary never reaches the fail-closed bucket."""
    for status in OUTBOX_STATUSES:
        if status == "retry_wait":
            continue
        assert work_status_category(
            status, next_attempt_at=None, sampled_at=_SAMPLED
        ) != "unrecognized"
    assert work_status_category(
        "retry_wait", next_attempt_at=_PAST, sampled_at=_SAMPLED
    ) != "unrecognized"
    assert work_status_category(
        "retry_wait", next_attempt_at=_FUTURE, sampled_at=_SAMPLED
    ) != "unrecognized"


def test_category_partition_is_closed_and_outstanding_subset_is_defined() -> None:
    assert set(WORK_STATUS_CATEGORIES) == {
        "unstarted_eligible",
        "retry_scheduled",
        "reserved_active",
        "native_queue",
        "uncertain_outcome",
        "terminal_outcome",
        "unrecognized",
    }
    assert OUTSTANDING_CATEGORIES == {
        "unstarted_eligible",
        "retry_scheduled",
        "reserved_active",
        "native_queue",
    }


@pytest.mark.parametrize(
    ("status", "expected_origin", "expected_anchor"),
    [
        ("pending", "admission", "created"),
        ("retry_wait", "retry_due", _PAST),
        ("queued", "queue_admission", "updated"),
    ],
)
def test_age_origin_names_the_exact_fact(
    status: str, expected_origin: str, expected_anchor: str
) -> None:
    origin, anchor = outstanding_age_origin(
        status,
        next_attempt_at=_PAST,
        created_at="created",
        locked_at="locked",
        updated_at="updated",
    )
    assert (origin, anchor) == (expected_origin, expected_anchor)


def test_claim_origin_prefers_the_recorded_claim() -> None:
    origin, anchor = outstanding_age_origin(
        "in_progress",
        next_attempt_at=None,
        created_at="created",
        locked_at="locked",
        updated_at="updated",
    )
    assert (origin, anchor) == ("claim", "locked")


def test_claim_origin_falls_back_to_admission_without_a_claim() -> None:
    origin, anchor = outstanding_age_origin(
        "in_progress",
        next_attempt_at=None,
        created_at="created",
        locked_at=None,
        updated_at="updated",
    )
    assert (origin, anchor) == ("admission", "created")


def test_assembly_sums_the_partition_and_flags_uncertainty() -> None:
    report = assemble_work_status_report(
        sampled_at=_SAMPLED,
        adapter_filter=None,
        adapter_rows=[
            {
                "target_adapter": "held",
                "current_generations": 3,
                "unstarted_eligible": 1,
                "reserved_active": 1,
                "terminal_outcome": 1,
                "reserved_without_generation_receipt": 1,
                "lease_missing_or_expired": 0,
                "stale_queue_reclaimable": 0,
            },
            {
                "target_adapter": "lane",
                "current_generations": 1,
                "retry_scheduled": 1,
                "stale_queue_reclaimable": 1,
            },
        ],
        oldest_rows=[
            {
                "target_adapter": "held",
                "event_id": "event-h",
                "delivery_plan_id": "plan-h",
                "target_channel": "out",
                "outbox_id": "ob-h",
                "category": "reserved_active",
                "status": "in_progress",
                "attempt_number": 0,
                "active_attempt": 1,
                "age_origin": "claim",
                "age_anchor": _PAST,
            }
        ],
        raw_status_rows=[
            {"status": "sent", "rows": 2},
            {"status": "in_progress", "rows": 1},
            {"status": "pending", "rows": 1},
        ],
        current_generations_total=4,
        receipts_by_status={"sent": 2},
    )
    document = report.to_dict()
    assert document["scope"] == "work_status"
    assert document["schema_version"] == 1
    assert document["definition"] == WORK_STATUS_DEFINITION
    assert document["totals"]["current_generations"] == 4
    assert document["totals"]["outstanding"] == 3
    assert document["totals"]["terminal_outcome"] == 1
    assert document["uncertainty"]["reserved_without_generation_receipt"] == 1
    assert document["uncertainty"]["stale_queue_reclaimable"] == 1
    held = next(a for a in document["adapters"] if a["target_adapter"] == "held")
    assert held["outstanding"] == 2
    assert held["oldest_outstanding"]["origin"] == "claim"
    assert held["oldest_outstanding"]["age_seconds"] == 86400
    assert document["raw_outbox_states"] == {
        "in_progress": 1,
        "pending": 1,
        "sent": 2,
    }
    assert document["retained_history"]["outbox_rows_total"] == 4
    assert document["retained_history"]["superseded_generations"] == 0
    assert document["retained_history"]["receipts_by_status"] == {"sent": 2}
    assert set(document["limits"]) == {
        "connectivity",
        "owner_liveness",
        "native_send",
        "recipient_delivery",
        "scope",
    }
    assert document["limits"]["recipient_delivery"].startswith("unknown")
    assert document["limits"]["connectivity"].startswith("unknown")
    assert document["limits"]["owner_liveness"].startswith("unknown")


def test_future_retry_anchor_reports_a_negative_age() -> None:
    report = assemble_work_status_report(
        sampled_at=_SAMPLED,
        adapter_filter=None,
        adapter_rows=[
            {
                "target_adapter": "lane",
                "current_generations": 1,
                "retry_scheduled": 1,
            }
        ],
        oldest_rows=[
            {
                "target_adapter": "lane",
                "event_id": "event-r",
                "delivery_plan_id": "plan-r",
                "target_channel": None,
                "outbox_id": "ob-r",
                "category": "retry_scheduled",
                "status": "retry_wait",
                "attempt_number": 1,
                "active_attempt": None,
                "age_origin": "retry_due",
                "age_anchor": _FUTURE,
            }
        ],
        raw_status_rows=[{"status": "retry_wait", "rows": 1}],
        current_generations_total=1,
        receipts_by_status={},
    )
    oldest = report.adapters[0].oldest_outstanding
    assert oldest is not None
    assert oldest.age_seconds == -86400
    assert oldest.origin == "retry_due"


def test_missing_anchor_age_is_none_not_zero() -> None:
    report = assemble_work_status_report(
        sampled_at=_SAMPLED,
        adapter_filter=None,
        adapter_rows=[
            {
                "target_adapter": "lane",
                "current_generations": 1,
                "unstarted_eligible": 1,
            }
        ],
        oldest_rows=[
            {
                "target_adapter": "lane",
                "event_id": "event-n",
                "delivery_plan_id": "plan-n",
                "target_channel": None,
                "outbox_id": "ob-n",
                "category": "unstarted_eligible",
                "status": "pending",
                "attempt_number": 0,
                "active_attempt": None,
                "age_origin": "admission",
                "age_anchor": None,
            }
        ],
        raw_status_rows=[{"status": "pending", "rows": 1}],
        current_generations_total=1,
        receipts_by_status={},
    )
    oldest = report.adapters[0].oldest_outstanding
    assert oldest is not None
    assert oldest.age_seconds is None


def test_assembly_rejects_a_broken_category_partition() -> None:
    with pytest.raises(ValueError, match="accounting invariant"):
        assemble_work_status_report(
            sampled_at=_SAMPLED,
            adapter_filter=None,
            adapter_rows=[
                {
                    "target_adapter": "a",
                    "current_generations": 5,
                    "unstarted_eligible": 1,
                }
            ],
            oldest_rows=[],
            raw_status_rows=[],
            current_generations_total=5,
            receipts_by_status={},
        )


def test_assembly_rejects_cross_statement_total_mismatch() -> None:
    with pytest.raises(ValueError, match="accounting invariant"):
        assemble_work_status_report(
            sampled_at=_SAMPLED,
            adapter_filter=None,
            adapter_rows=[
                {
                    "target_adapter": "a",
                    "current_generations": 2,
                    "unstarted_eligible": 2,
                }
            ],
            oldest_rows=[],
            raw_status_rows=[],
            current_generations_total=3,
            receipts_by_status={},
        )


def test_assembly_rejects_retained_history_smaller_than_current() -> None:
    with pytest.raises(ValueError, match="accounting invariant"):
        assemble_work_status_report(
            sampled_at=_SAMPLED,
            adapter_filter=None,
            adapter_rows=[
                {
                    "target_adapter": "a",
                    "current_generations": 2,
                    "unstarted_eligible": 2,
                }
            ],
            oldest_rows=[],
            raw_status_rows=[{"status": "pending", "rows": 1}],
            current_generations_total=2,
            receipts_by_status={},
        )


def test_assembly_rejects_a_terminal_oldest_row() -> None:
    with pytest.raises(ValueError, match="not outstanding"):
        assemble_work_status_report(
            sampled_at=_SAMPLED,
            adapter_filter=None,
            adapter_rows=[
                {
                    "target_adapter": "a",
                    "current_generations": 1,
                    "terminal_outcome": 1,
                }
            ],
            oldest_rows=[
                {
                    "target_adapter": "a",
                    "event_id": "e",
                    "delivery_plan_id": "p",
                    "target_channel": None,
                    "outbox_id": "o",
                    "category": "terminal_outcome",
                    "status": "sent",
                    "attempt_number": 1,
                    "active_attempt": None,
                    "age_origin": "admission",
                    "age_anchor": _PAST,
                }
            ],
            raw_status_rows=[{"status": "sent", "rows": 1}],
            current_generations_total=1,
            receipts_by_status={},
        )


def test_adapter_filter_scope_is_stated_not_zero_filled() -> None:
    report = assemble_work_status_report(
        sampled_at=_SAMPLED,
        adapter_filter="only",
        adapter_rows=[
            {
                "target_adapter": "only",
                "current_generations": 1,
                "unstarted_eligible": 1,
            }
        ],
        oldest_rows=[],
        raw_status_rows=[{"status": "pending", "rows": 1}],
        current_generations_total=1,
        receipts_by_status={},
    )
    assert report.adapter_filter == "only"
    document = report.to_dict()
    assert document["adapter_filter"] == "only"
    assert [a["target_adapter"] for a in document["adapters"]] == ["only"]


def test_uncertainty_defaults_are_zero_flags_not_unknown() -> None:
    uncertainty = WorkStatusUncertainty()
    assert uncertainty.to_dict() == {
        "reserved_without_generation_receipt": 0,
        "lease_missing_or_expired": 0,
        "stale_queue_reclaimable": 0,
    }


def test_adapter_outstanding_excludes_settled_categories() -> None:
    adapter = AdapterWorkStatus(
        target_adapter="a",
        current_generations=6,
        categories={
            "unstarted_eligible": 1,
            "retry_scheduled": 2,
            "reserved_active": 1,
            "native_queue": 1,
            "uncertain_outcome": 1,
            "terminal_outcome": 0,
            "unrecognized": 0,
        },
        uncertainty=WorkStatusUncertainty(),
        oldest_outstanding=None,
    )
    assert adapter.outstanding == 5
