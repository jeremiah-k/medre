"""Read-only per-adapter work-status projection over the delivery outbox.

This module defines the operator work-status view: which destination has
outstanding delivery work, why, how old it is, what is known about native
acceptance, and which exact inspection applies next. It is a projection of
durable storage facts, never a liveness, connectivity, or recipient-delivery
claim, and it performs no I/O of its own.

Authority and scope rules:

* one *current generation* per ``(event_id, delivery_plan_id,
  target_adapter, target_channel)`` delivery identity is selected by the
  shared current-delivery authority rule
  (:mod:`medre.core.delivery_authority`); retained non-current generations
  are history and never count as outstanding work;
* the current generations partition into a closed category vocabulary whose
  counts are non-overlapping and sum exactly to the current-generation
  total whenever they are summed;
* ``terminal_outcome`` counts local completions, exhaustions and
  cancellations; a ``sent`` state proves local acceptance only, never that
  an independent recipient observed a delivery;
* ``uncertain_outcome`` counts ``abandoned`` outcomes whose native
  acceptance was never settled; retained terminal history is reported as a
  separate dimension so it cannot masquerade as outstanding work;
* a ``reserved_active`` row proves a dispatch owner reserved or claimed the
  work and nothing more: an offline database query cannot establish owner
  liveness, adapter connectivity, or whether a native send occurred.
  Uncertainty flags surface the recorded lease state and whether the
  reserved generation carries any attempt evidence at all;
* ``unknown is not zero``: missing facts are labeled as limits, not folded
  into healthy-looking zero counters. Aggregated results state the scope
  they cover and remain partial by construction.

The storage layer computes the same category rule in SQL
(:mod:`medre.core.storage.sqlite._work_status`) and is checked against the
pure functions here by conformance tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal

__all__ = [
    "OUTSTANDING_CATEGORIES",
    "WORK_STATUS_CATEGORIES",
    "AdapterWorkStatus",
    "WorkStatusOldest",
    "WorkStatusReport",
    "outstanding_age_origin",
    "work_status_category",
]

#: Closed category vocabulary for current delivery generations. Every
#: current generation maps to exactly one category; the counts are
#: non-overlapping and sum to the current-generation total. The vocabulary
#: is frozen across the pure classifier, the SQL projection, and the
#: published JSON schema.
WorkStatusCategory = Literal[
    "unstarted_eligible",
    "retry_scheduled",
    "reserved_active",
    "native_queue",
    "uncertain_outcome",
    "terminal_outcome",
    "unrecognized",
]

WORK_STATUS_CATEGORIES: tuple[WorkStatusCategory, ...] = (
    "unstarted_eligible",
    "retry_scheduled",
    "reserved_active",
    "native_queue",
    "uncertain_outcome",
    "terminal_outcome",
    "unrecognized",
)

#: Categories whose generations are still outstanding obligations: a
#: worker, queue, or schedule may still act on them. ``uncertain_outcome``
#: and ``terminal_outcome`` generations are settled locally and are never
#: outstanding.
OUTSTANDING_CATEGORIES: frozenset[str] = frozenset(
    {
        "unstarted_eligible",
        "retry_scheduled",
        "reserved_active",
        "native_queue",
    }
)

#: Fail-closed category for a persisted status outside the stored
#: vocabulary. The storage CHECK constraint prevents it in practice; the
#: accounting invariant still holds when it appears.
_UNRECOGNIZED: Literal["unrecognized"] = "unrecognized"

_TERMINAL_OUTCOME_STATUSES = frozenset({"sent", "dead_lettered", "cancelled"})
_UNSTARTED_STATUSES = frozenset({"pending", "retry_wait"})

#: Closed origin labels for the oldest-outstanding age anchor. The anchor
#: is never a single column blindly: a scheduled retry's relevant age runs
#: from its due time, not its admission, and a claimed generation's runs
#: from its claim, not its creation.
WorkStatusAgeOrigin = Literal["admission", "retry_due", "claim", "queue_admission"]

#: Fixed definition text shipped with every report so offline consumers can
#: interpret the projection without this module.
WORK_STATUS_DEFINITION = (
    "One current outbox generation per delivery identity "
    "(event, delivery plan, target adapter, target channel) is selected by "
    "the shared current-delivery authority rule and classified into one "
    "closed category. Category counts are non-overlapping and sum to the "
    "current-generation total. Retained non-current generations and receipt "
    "history are reported separately and never count as outstanding work. "
    "sent records local acceptance only; recipient delivery is never "
    "claimed. A reserved or claimed generation proves ownership, not owner "
    "liveness, adapter connectivity, or that a native send occurred."
)


def timestamp_microseconds(value: str | None) -> int | None:
    """Represent an offset-aware ISO timestamp exactly, or report it unknown.

    Integer microseconds avoid floating-point precision loss at the retry
    and lease equality boundaries. No timezone is inferred for legacy rows.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            return None
        elapsed = parsed.astimezone(timezone.utc) - datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
    except (ValueError, OverflowError):
        return None
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def work_status_category(
    status: str,
    *,
    next_attempt_at: str | None,
    sampled_at: str,
) -> WorkStatusCategory:
    """Classify one current outbox generation into the closed vocabulary.

    Pure function and the conformance oracle for the SQL ``CASE`` in the
    storage projection: both compare offset-aware instants at microsecond
    precision. A known future retry stays scheduled; other retry_wait rows
    enter the unstarted bucket. This is a state summary, not a claim check;
    an unusable deadline is not evidence of dispatch eligibility.
    """
    if status == "in_progress":
        return "reserved_active"
    if status == "queued":
        return "native_queue"
    if status == "abandoned":
        return "uncertain_outcome"
    if status in _TERMINAL_OUTCOME_STATUSES:
        return "terminal_outcome"
    if status == "retry_wait":
        due = timestamp_microseconds(next_attempt_at)
        sample = timestamp_microseconds(sampled_at)
        if due is not None and sample is not None and due > sample:
            return "retry_scheduled"
    if status in _UNSTARTED_STATUSES:
        return "unstarted_eligible"
    return _UNRECOGNIZED


def outstanding_age_origin(
    status: str,
    *,
    next_attempt_at: str | None,
    created_at: str | None,
    locked_at: str | None,
    updated_at: str | None,
) -> tuple[WorkStatusAgeOrigin, str | None]:
    """Return the precise age ``(origin, anchor)`` for one outstanding row.

    The anchor is the timestamp the row's waiting age is measured from, or
    ``None`` when the row carries no usable timestamp. Origin labels state
    which fact the anchor is, so an admission age is never presented as a
    retry-due age or the reverse.
    """
    if status == "retry_wait" and next_attempt_at:
        return "retry_due", next_attempt_at
    if status == "in_progress" and locked_at:
        return "claim", locked_at
    if status == "queued":
        return "queue_admission", updated_at or created_at
    return "admission", created_at or updated_at


def _age_seconds(anchor: str | None, sampled_at: str) -> int | None:
    """Whole seconds between *anchor* and *sampled_at*, or ``None``.

    Negative values mean the anchor is in the future (a not-yet-due
    retry). Parsing failures return ``None`` instead of inventing an age.
    """
    anchor_us = timestamp_microseconds(anchor)
    sampled_us = timestamp_microseconds(sampled_at)
    if anchor_us is None or sampled_us is None:
        return None
    return (sampled_us - anchor_us) // 1_000_000


@dataclass(frozen=True, slots=True)
class WorkStatusUncertainty:
    """Recorded-uncertainty flags for one adapter's current generations.

    These are separate dimensions, never part of the category partition.
    Each flag is an observation about recorded facts; none establishes or
    excludes a live native send.
    """

    #: Reserved generations with an active attempt but no matching attempt
    #: receipt for their delivery identity: native send is unknown.
    reserved_without_generation_receipt: int = 0
    #: Reserved generations whose recorded lease is missing or expired at
    #: the sampled time: recorded-owner liveness is unknown. This is not
    #: a claimability check.
    lease_missing_or_expired: int = 0
    #: Queued generations whose last update is older than the storage
    #: reclaim grace: adapter-local queue admission may be stale.
    stale_queue_reclaimable: int = 0

    def to_dict(self) -> dict[str, int]:
        """JSON-safe flag mapping."""
        return {
            "reserved_without_generation_receipt": (
                self.reserved_without_generation_receipt
            ),
            "lease_missing_or_expired": self.lease_missing_or_expired,
            "stale_queue_reclaimable": self.stale_queue_reclaimable,
        }


@dataclass(frozen=True, slots=True)
class WorkStatusOldest:
    """The longest-waiting outstanding generation for one adapter."""

    event_id: str
    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None
    outbox_id: str
    category: str
    status: str
    attempt_number: int
    active_attempt: int | None
    #: Which fact the age is measured from (admission, retry_due, claim,
    #: queue_admission) — never inferred.
    origin: str
    #: The anchor timestamp itself, as persisted.
    anchor: str | None
    #: Whole seconds from the anchor to the sampled time; negative when the
    #: anchor is in the future. ``None`` when no usable anchor exists.
    age_seconds: int | None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe record."""
        return {
            "event_id": self.event_id,
            "delivery_plan_id": self.delivery_plan_id,
            "target_adapter": self.target_adapter,
            "target_channel": self.target_channel,
            "outbox_id": self.outbox_id,
            "category": self.category,
            "status": self.status,
            "attempt_number": self.attempt_number,
            "active_attempt": self.active_attempt,
            "origin": self.origin,
            "anchor": self.anchor,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True, slots=True)
class AdapterWorkStatus:
    """Per-adapter work summary over current delivery generations."""

    target_adapter: str
    current_generations: int
    categories: Mapping[str, int]
    uncertainty: WorkStatusUncertainty
    #: The longest-waiting outstanding generation, or ``None`` when the
    #: adapter holds no outstanding work.
    oldest_outstanding: WorkStatusOldest | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "categories", MappingProxyType(dict(self.categories)))

    @property
    def outstanding(self) -> int:
        """Generations a worker, queue, or schedule may still act on."""
        return sum(
            count
            for key, count in self.categories.items()
            if key in OUTSTANDING_CATEGORIES
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe record including the derived outstanding total."""
        return {
            "target_adapter": self.target_adapter,
            "current_generations": self.current_generations,
            "outstanding": self.outstanding,
            "categories": dict(self.categories),
            "uncertainty": self.uncertainty.to_dict(),
            "oldest_outstanding": (
                self.oldest_outstanding.to_dict()
                if self.oldest_outstanding is not None
                else None
            ),
        }


def _work_status_limits() -> dict[str, str]:
    """Return the fixed honesty limits shipped with every report."""
    return {
        "connectivity": (
            "unknown: this view reads local durable storage only and cannot "
            "probe adapter connectivity"
        ),
        "owner_liveness": (
            "unknown: claims and leases are recorded facts, not proof that a "
            "recorded owner still runs"
        ),
        "native_send": (
            "unknown for reserved generations without attempt evidence: an "
            "in_progress row alone establishes neither a live native send "
            "nor a held uncertain outcome"
        ),
        "recipient_delivery": (
            "unknown: local acceptance and completion are local facts and "
            "are never independent recipient observation"
        ),
        "scope": (
            "partial: the projection covers the delivery outbox and receipt "
            "history in one database read snapshot; sampled_at is the comparison "
            "clock, not a historical database query. Categories and grace flags "
            "do not establish worker claimability"
        ),
    }


@dataclass(frozen=True, slots=True)
class WorkStatusReport:
    """The complete read-only work-status projection document."""

    sampled_at: str
    adapter_filter: str | None
    totals: Mapping[str, int]
    adapters: tuple[AdapterWorkStatus, ...]
    #: Recorded uncertainty summed across the reported adapters.
    uncertainty: WorkStatusUncertainty
    #: Raw stored outbox status counts over every retained row, including
    #: superseded generations. A separate dimension, never a category sum.
    raw_outbox_states: Mapping[str, int]
    retained_history: Mapping[str, Any]
    limits: Mapping[str, str] = field(default_factory=_work_status_limits)

    def __post_init__(self) -> None:
        history = dict(self.retained_history)
        history["receipts_by_status"] = MappingProxyType(
            dict(history.get("receipts_by_status", {}))
        )
        object.__setattr__(self, "retained_history", MappingProxyType(history))
        for name in ("totals", "raw_outbox_states", "limits"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe report document matching the published schema."""
        return {
            "scope": "work_status",
            "schema_version": 1,
            "sampled_at": self.sampled_at,
            "adapter_filter": self.adapter_filter,
            "definition": WORK_STATUS_DEFINITION,
            "totals": dict(self.totals),
            "adapters": [adapter.to_dict() for adapter in self.adapters],
            "uncertainty": self.uncertainty.to_dict(),
            "raw_outbox_states": dict(self.raw_outbox_states),
            "retained_history": {
                **self.retained_history,
                "receipts_by_status": dict(self.retained_history["receipts_by_status"]),
            },
            "limits": dict(self.limits),
        }


def assemble_work_status_report(
    *,
    sampled_at: str,
    adapter_filter: str | None,
    adapter_rows: list[dict[str, Any]],
    oldest_rows: list[dict[str, Any]],
    raw_status_rows: list[dict[str, Any]],
    current_generations_total: int,
    receipts_by_status: dict[str, int],
) -> WorkStatusReport:
    """Assemble and validate the report from bounded storage aggregates.

    ``adapter_rows`` carry one row per adapter of category counts and
    uncertainty flags; ``oldest_rows`` carry at most one oldest outstanding
    generation per adapter; ``raw_status_rows`` carry the all-retained-row
    status counts. The assembly fails closed with :class:`ValueError`
    rather than presenting inconsistent accounting.
    """
    if adapter_filter == "":
        raise ValueError("adapter filter must not be empty")
    adapters: list[AdapterWorkStatus] = []
    oldest_by_adapter = {str(row["target_adapter"]): row for row in oldest_rows}
    if len(oldest_by_adapter) != len(oldest_rows):
        raise ValueError("duplicate oldest-outstanding adapter")
    seen: set[str] = set()
    for row in adapter_rows:
        adapter = str(row["target_adapter"])
        if adapter in seen or (
            adapter_filter is not None and adapter != adapter_filter
        ):
            raise ValueError("duplicate or out-of-scope work-status adapter")
        seen.add(adapter)
        categories: dict[str, int] = {
            category: int(row.get(category, 0) or 0)
            for category in WORK_STATUS_CATEGORIES
        }
        generations = int(row.get("current_generations", 0) or 0)
        if sum(categories.values()) != generations:
            raise ValueError(
                f"work-status accounting invariant violated for {adapter!r}: "
                f"category sum {sum(categories.values())} != "
                f"current generations {generations}"
            )
        oldest_row = oldest_by_adapter.pop(adapter, None)
        outstanding = sum(categories[key] for key in OUTSTANDING_CATEGORIES)
        if bool(outstanding) != (oldest_row is not None):
            raise ValueError(
                f"oldest-outstanding presence contradicts counts for {adapter!r}"
            )
        oldest = None
        if oldest_row is not None:
            if oldest_row[
                "category"
            ] not in OUTSTANDING_CATEGORIES or not categories.get(
                oldest_row["category"]
            ):
                raise ValueError(
                    f"oldest-outstanding row for {adapter!r} is not "
                    f"outstanding: {oldest_row['category']!r}"
                )
            oldest = WorkStatusOldest(
                event_id=str(oldest_row["event_id"]),
                delivery_plan_id=str(oldest_row["delivery_plan_id"]),
                target_adapter=adapter,
                target_channel=oldest_row["target_channel"],
                outbox_id=str(oldest_row["outbox_id"]),
                category=str(oldest_row["category"]),
                status=str(oldest_row["status"]),
                attempt_number=int(oldest_row["attempt_number"] or 0),
                active_attempt=oldest_row["active_attempt"],
                origin=str(oldest_row["age_origin"]),
                anchor=oldest_row["age_anchor"],
                age_seconds=_age_seconds(oldest_row["age_anchor"], sampled_at),
            )
        adapters.append(
            AdapterWorkStatus(
                target_adapter=adapter,
                current_generations=generations,
                categories=categories,
                uncertainty=WorkStatusUncertainty(
                    reserved_without_generation_receipt=int(
                        row.get("reserved_without_generation_receipt", 0) or 0
                    ),
                    lease_missing_or_expired=int(
                        row.get("lease_missing_or_expired", 0) or 0
                    ),
                    stale_queue_reclaimable=int(
                        row.get("stale_queue_reclaimable", 0) or 0
                    ),
                ),
                oldest_outstanding=oldest,
            )
        )

    if oldest_by_adapter:
        raise ValueError("oldest-outstanding row has no matching adapter")
    totals = _report_totals(adapters, current_generations_total, adapter_filter)
    raw_outbox_states = {
        str(row["status"]): int(row["rows"]) for row in raw_status_rows
    }
    outbox_rows_total = sum(raw_outbox_states.values())
    current_reported = totals["current_generations"]
    if current_reported != current_generations_total:
        raise ValueError(
            "work-status accounting invariant violated: per-adapter current "
            f"generation sum {current_reported} != storage total "
            f"{current_generations_total}"
        )
    if outbox_rows_total < current_reported:
        raise ValueError(
            "work-status accounting invariant violated: retained rows "
            f"{outbox_rows_total} < current generations {current_reported}"
        )
    uncertainty = WorkStatusUncertainty(
        reserved_without_generation_receipt=sum(
            adapter.uncertainty.reserved_without_generation_receipt
            for adapter in adapters
        ),
        lease_missing_or_expired=sum(
            adapter.uncertainty.lease_missing_or_expired for adapter in adapters
        ),
        stale_queue_reclaimable=sum(
            adapter.uncertainty.stale_queue_reclaimable for adapter in adapters
        ),
    )
    return WorkStatusReport(
        sampled_at=sampled_at,
        adapter_filter=adapter_filter,
        totals=totals,
        adapters=tuple(adapters),
        uncertainty=uncertainty,
        raw_outbox_states=raw_outbox_states,
        retained_history={
            "outbox_rows_total": outbox_rows_total,
            "superseded_generations": outbox_rows_total - current_reported,
            "receipts_total": sum(receipts_by_status.values()),
            "receipts_by_status": dict(sorted(receipts_by_status.items())),
        },
    )


def _report_totals(
    adapters: list[AdapterWorkStatus],
    current_generations_total: int,
    adapter_filter: str | None,
) -> dict[str, int]:
    """Sum the category partition and the derived outstanding total.

    The storage aggregates are scoped to ``adapter_filter`` when one is
    set, so the per-adapter sum must match the storage total in both the
    filtered and unfiltered scopes.
    """
    del adapter_filter
    totals: dict[str, int] = {
        category: sum(adapter.categories.get(category, 0) for adapter in adapters)
        for category in WORK_STATUS_CATEGORIES
    }
    totals["current_generations"] = sum(
        adapter.current_generations for adapter in adapters
    )
    totals["outstanding"] = sum(adapter.outstanding for adapter in adapters)
    if totals["current_generations"] != current_generations_total:
        raise ValueError(
            "work-status accounting invariant violated: per-adapter current "
            f"generation sum {totals['current_generations']} != storage total "
            f"{current_generations_total}"
        )
    return totals
