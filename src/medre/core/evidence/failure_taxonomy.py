"""Canonical runtime evidence failure taxonomy.

Provides a stable vocabulary of failure and suppression categories for
evidence outputs, together with pure classification functions that derive
taxon, detail, and retryability from receipt-level data.

Design goals
------------
* **Consistent with existing behaviour**: :func:`derive_failure_kind_detail`
  and :func:`compute_retryable` reproduce the logic in
  :mod:`medre.runtime.reporting` so that downstream consumers get identical
  results without importing the runtime layer.
* **JSON-safe values**: every :class:`FailureTaxon` member is a plain
  string — ``taxon.value`` serialises directly.
* **No I/O, no state mutation**: all public functions are pure.

Public symbols
--------------
* :class:`FailureTaxon` — canonical failure/suppression category enum.
* :func:`derive_failure_kind_detail` — enrich a failure kind from error text.
* :func:`compute_retryable` — determine retryability from receipt fields.
* :func:`resolve_taxon` — full classification from receipt data to taxon.
* :func:`taxon_category` — coarse bucket (retryable / permanent / …).
* :data:`FAILURE_KIND_TO_TAXON` — direct mapping for known failure kinds.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

__all__ = [
    "FailureTaxon",
    "FAILURE_KIND_TO_TAXON",
    "derive_failure_kind_detail",
    "compute_retryable",
    "resolve_taxon",
    "taxon_category",
]


# ---------------------------------------------------------------------------
# Failure taxon — canonical category vocabulary
# ---------------------------------------------------------------------------


class FailureTaxon(Enum):
    """Canonical failure and suppression categories for evidence outputs.

    Members are grouped by origin:

    * **Pipeline infrastructure** — failures that occur inside the
      medre pipeline before any adapter interaction.
    * **Adapter** — failures originating from the transport adapter
      layer (transient or permanent).
    * **Suppression** — delivery intentionally not attempted due to
      a guard, policy, or capability decision.
    * **Operational** — pipeline-level conditions (capacity, shutdown,
      deadline) that prevent delivery from proceeding.
    * **Derived** — higher-level categories synthesised from detail
      strings or receipt status.  These do **not** correspond to a
      single :class:`~medre.core.planning.delivery_plan.DeliveryFailureKind`
      value but are derived from the combination of ``failure_kind``,
      ``error``, and ``status``.
    """

    # -- Pipeline infrastructure -------------------------------------------

    PLANNER_FAILURE = "planner_failure"
    RENDERER_FAILURE = "renderer_failure"
    ADAPTER_MISSING = "adapter_missing"

    # -- Adapter -----------------------------------------------------------

    ADAPTER_TRANSIENT = "adapter_transient"
    ADAPTER_PERMANENT = "adapter_permanent"

    # -- Suppression -------------------------------------------------------

    CAPABILITY_SUPPRESSED = "capability_suppressed"
    LOOP_SUPPRESSED = "loop_suppressed"
    POLICY_SUPPRESSED = "policy_suppressed"

    # -- Operational -------------------------------------------------------

    CAPACITY_REJECTION = "capacity_rejection"
    SHUTDOWN_REJECTION = "shutdown_rejection"
    DEADLINE_EXCEEDED = "deadline_exceeded"

    # -- Derived (from detail / status enrichment) -------------------------

    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    AUTH_FAILED = "auth_failed"
    CONNECTION_FAILED = "connection_failed"
    ROUTE_DISABLED = "route_disabled"
    ROUTE_LISTEN_ONLY = "route_listen_only"
    DELIVERY_FAILED = "delivery_failed"
    RETRY_EXHAUSTED = "retry_exhausted"
    CANCELLED = "cancelled"
    SHUTDOWN_PENDING = "shutdown_pending"
    NOT_EXECUTED = "not_executed"


# ---------------------------------------------------------------------------
# Direct mapping: failure_kind string → FailureTaxon
# ---------------------------------------------------------------------------

FAILURE_KIND_TO_TAXON: dict[str, FailureTaxon] = {
    "planner_failure": FailureTaxon.PLANNER_FAILURE,
    "renderer_failure": FailureTaxon.RENDERER_FAILURE,
    "adapter_transient": FailureTaxon.ADAPTER_TRANSIENT,
    "adapter_permanent": FailureTaxon.ADAPTER_PERMANENT,
    "adapter_missing": FailureTaxon.ADAPTER_MISSING,
    "deadline_exceeded": FailureTaxon.DEADLINE_EXCEEDED,
    "capacity_rejection": FailureTaxon.CAPACITY_REJECTION,
    "shutdown_rejection": FailureTaxon.SHUTDOWN_REJECTION,
    "loop_suppressed": FailureTaxon.LOOP_SUPPRESSED,
    "policy_suppressed": FailureTaxon.POLICY_SUPPRESSED,
    "capability_suppressed": FailureTaxon.CAPABILITY_SUPPRESSED,
}
"""Identity mapping for the 11 ``DeliveryFailureKind`` values.

Every key is a ``DeliveryFailureKind.value`` string; every value is the
corresponding :class:`FailureTaxon` member with the same string value.
"""


# ---------------------------------------------------------------------------
# Detail derivation (mirrors reporting._derive_failure_kind_detail)
# ---------------------------------------------------------------------------


def derive_failure_kind_detail(
    failure_kind: str | None,
    error: str | None,
) -> str | None:
    """Derive a conservative *failure_kind_detail* from error context.

    This is a pure re-implementation of
    :func:`medre.runtime.reporting._derive_failure_kind_detail` so that
    evidence-layer consumers can classify receipts without importing the
    runtime package.

    Patterns
    --------
    * ``"shutdown_drain_timeout"`` — in-flight deliveries abandoned during
      pipeline shutdown drain.
    * ``"policy_suppressed"`` — route policy denial (from error text or
      failure kind).
    * ``"e2ee_blocked"`` — Matrix E2EE decryption / blocking errors.
    * ``"meshtastic_queue_rejected"`` — queue-full or enqueue-rejected.
    * ``"meshtastic_outbound_suppressed"`` — listen-only mode suppression.
    * ``"meshtastic_queue_drain_cancelled"`` — queue drain cancelled during
      shutdown.
    * Otherwise — the original ``failure_kind`` is returned as-is.
    """
    if not failure_kind:
        return None
    err = (error or "").lower()
    # Shutdown drain-timeout abandonment.
    if "shutdown_drain_timeout" in err:
        return "shutdown_drain_timeout"
    # Route-policy denial.
    if "route policy denied" in err:
        return "policy_suppressed"
    if failure_kind == "policy_suppressed":
        return "policy_suppressed"
    # E2EE / encrypted blocking (Matrix adapters).
    if any(
        s in err
        for s in (
            "e2ee",
            "megolm",
            "olm session",
            "unable to decrypt",
            "crypto is not active",
            "matrix room is encrypted",
            "room is encrypted but e2ee",
        )
    ):
        return "e2ee_blocked"
    # Meshtastic queue-full / rejection.
    if ("queue" in err and "full" in err) or "enqueue rejected" in err:
        return "meshtastic_queue_rejected"
    # Meshtastic outbound gate suppression — listen_only mode.
    if "outbound suppressed" in err and "listen_only" in err:
        return "meshtastic_outbound_suppressed"
    # Meshtastic queue drain cancelled.
    if "queue drain cancelled" in err or "queue abandoned" in err:
        return "meshtastic_queue_drain_cancelled"
    # Default: preserve the original failure_kind.
    return failure_kind


# ---------------------------------------------------------------------------
# Retryable computation (mirrors reporting._compute_retryable)
# ---------------------------------------------------------------------------


def compute_retryable(
    failure_kind: str | None,
    status: str,
    next_retry_at: datetime | None,
) -> bool:
    """Determine whether a receipt represents a retryable delivery state.

    This is a pure re-implementation of
    :func:`medre.runtime.reporting._compute_retryable`.

    Rules (evaluated in order; first match wins):

    * ``status == "dead_lettered"`` → ``False`` (terminal).
    * ``status == "suppressed"`` → ``False`` (terminal).
    * ``next_retry_at is not None`` → ``True`` (scheduled retry).
    * ``status == "failed"`` and ``failure_kind == "adapter_transient"``
      → ``True``.
    * Everything else → ``False``.
    """
    if status == "dead_lettered":
        return False
    if status == "suppressed":
        return False
    if next_retry_at is not None:
        return True
    if status == "failed" and failure_kind == "adapter_transient":
        return True
    return False


# ---------------------------------------------------------------------------
# Full taxon resolution
# ---------------------------------------------------------------------------


# Mapping from detail strings to refined FailureTaxon members.
_DETAIL_TO_TAXON: dict[str, FailureTaxon] = {
    "shutdown_drain_timeout": FailureTaxon.SHUTDOWN_PENDING,
    "meshtastic_outbound_suppressed": FailureTaxon.ROUTE_LISTEN_ONLY,
    "meshtastic_queue_drain_cancelled": FailureTaxon.CANCELLED,
    "e2ee_blocked": FailureTaxon.DELIVERY_FAILED,
    "meshtastic_queue_rejected": FailureTaxon.UNAVAILABLE,
}


def resolve_taxon(
    failure_kind: str | None,
    error: str | None,
    status: str | None = None,
) -> FailureTaxon | None:
    """Classify receipt data into a canonical :class:`FailureTaxon`.

    Resolution order:

    1. If ``failure_kind`` is ``None`` and ``status`` is a success-like
       value, return ``None`` (no failure).
    2. If ``status == "dead_lettered"``, return
       :attr:`FailureTaxon.RETRY_EXHAUSTED`.
    3. Derive ``failure_kind_detail`` from ``failure_kind`` and ``error``.
       If the detail maps to a refined taxon, return it.
    4. If ``failure_kind`` is a known :data:`FAILURE_KIND_TO_TAXON` key,
       return the mapped taxon.
    5. If ``status == "suppressed"``, return
       :attr:`FailureTaxon.NOT_EXECUTED`.
    6. Return ``None`` for unknown inputs.
    """
    if not failure_kind:
        if status in ("sent", "queued", "success"):
            return None
        # Suppressed without a failure_kind — still a suppression.
        if status == "suppressed":
            return FailureTaxon.NOT_EXECUTED
        # dead_lettered without failure_kind — treat as retry exhausted.
        if status == "dead_lettered":
            return FailureTaxon.RETRY_EXHAUSTED
        return None

    # Dead-lettered status overrides: retries were exhausted.
    if status == "dead_lettered":
        return FailureTaxon.RETRY_EXHAUSTED

    # Derive detail and check for refined taxa.
    detail = derive_failure_kind_detail(failure_kind, error)
    if detail and detail in _DETAIL_TO_TAXON:
        return _DETAIL_TO_TAXON[detail]

    # Direct mapping from failure_kind string.
    if failure_kind in FAILURE_KIND_TO_TAXON:
        return FAILURE_KIND_TO_TAXON[failure_kind]

    # Suppressed status without a recognised failure_kind.
    if status == "suppressed":
        return FailureTaxon.NOT_EXECUTED

    return None


# ---------------------------------------------------------------------------
# Coarse categorisation
# ---------------------------------------------------------------------------

_RETRYABLE_TAXA: frozenset[FailureTaxon] = frozenset({FailureTaxon.ADAPTER_TRANSIENT})
_PERMANENT_TAXA: frozenset[FailureTaxon] = frozenset(
    {
        FailureTaxon.PLANNER_FAILURE,
        FailureTaxon.RENDERER_FAILURE,
        FailureTaxon.ADAPTER_MISSING,
        FailureTaxon.ADAPTER_PERMANENT,
        FailureTaxon.LOOP_SUPPRESSED,
        FailureTaxon.POLICY_SUPPRESSED,
        FailureTaxon.CAPABILITY_SUPPRESSED,
        FailureTaxon.DELIVERY_FAILED,
        FailureTaxon.AUTH_FAILED,
        FailureTaxon.NOT_CONFIGURED,
        FailureTaxon.ROUTE_DISABLED,
        FailureTaxon.ROUTE_LISTEN_ONLY,
    }
)
_OPERATIONAL_TAXA: frozenset[FailureTaxon] = frozenset(
    {
        FailureTaxon.CAPACITY_REJECTION,
        FailureTaxon.SHUTDOWN_REJECTION,
        FailureTaxon.DEADLINE_EXCEEDED,
        FailureTaxon.SHUTDOWN_PENDING,
    }
)
_DERIVED_TERMINAL_TAXA: frozenset[FailureTaxon] = frozenset(
    {
        FailureTaxon.RETRY_EXHAUSTED,
        FailureTaxon.CANCELLED,
        FailureTaxon.NOT_EXECUTED,
        FailureTaxon.UNAVAILABLE,
        FailureTaxon.CONNECTION_FAILED,
    }
)


def taxon_category(taxon: FailureTaxon | None) -> str:
    """Map a :class:`FailureTaxon` to a coarse category string.

    Returns one of: ``"retryable"``, ``"permanent"``, ``"operational"``,
    ``"derived_terminal"``, ``"unknown"``.
    """
    if taxon is None:
        return "unknown"
    if taxon in _RETRYABLE_TAXA:
        return "retryable"
    if taxon in _PERMANENT_TAXA:
        return "permanent"
    if taxon in _OPERATIONAL_TAXA:
        return "operational"
    if taxon in _DERIVED_TERMINAL_TAXA:
        return "derived_terminal"
    return "unknown"
