"""Read-only evidence collector backed by :class:`~medre.core.storage.backend.StorageBackend`.

The :class:`EvidenceCollector` reads stored data (events, receipts, native
refs, outbox items) and assembles a deterministic, JSON-safe
:class:`~medre.core.evidence.bundle.EvidenceBundle` for a single event.
It **never** writes to storage or mutates runtime state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Protocol, cast

import msgspec

from medre.core.diagnostics.convergence.orphans import build_orphan_report
from medre.core.diagnostics.convergence.recovery_convergence import (
    build_recovery_convergence_findings,
)
from medre.core.diagnostics.convergence.summary import build_convergence_summary
from medre.core.events import CanonicalEvent
from medre.core.evidence.bundle import (
    BUNDLE_SCHEMA_VERSION,
    EvidenceBundle,
    ReceiptSummary,
)
from medre.core.evidence.delivery_ledger import build_delivery_outcome_ledger
from medre.core.evidence.retry_outbox import (
    RetryOutboxItemSummary,
    RetryOutboxSummary,
    build_retry_outbox_summary,
)
from medre.core.evidence.tiers import infer_evidence_tier
from medre.core.recovery.builder import (
    build_recovery_summary,
    build_startup_recovery_ledger,
)

# ---------------------------------------------------------------------------
# Minimal storage protocol for the collector
# ---------------------------------------------------------------------------


class _EvidenceStorage(Protocol):
    """Minimal storage interface the collector requires.

    Tests can supply any object satisfying these methods without
    implementing the full backend.

    ``list_outbox_items_for_event`` is intentionally omitted from this
    protocol because the collector probes for it via ``getattr`` /
    ``callable`` - backends that predate the outbox method are
    supported without raising ``AttributeError``.
    """

    async def get(self, event_id: str) -> Any: ...
    async def list_receipts_for_event(self, event_id: str) -> list[Any]: ...
    async def list_native_refs_for_event(self, event_id: str) -> list[Any]: ...


# ---------------------------------------------------------------------------
# Rendering evidence parsing
# ---------------------------------------------------------------------------


def _parse_rendering_evidence(
    raw: str | None,
    *,
    receipt_id: str,
    event_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse ``rendering_evidence`` defensively.

    Returns ``(parsed_dict_or_None, warning_or_None)``.

    * ``None`` raw -> ``(None, None)`` - no warning.
    * Valid JSON object -> ``(dict, None)``.
    * Valid non-object JSON -> ``(None, warning)`` - schema expects object.
    * Invalid JSON -> ``(None, warning)`` - with receipt/event context,
      raw evidence not echoed except for length.
    """
    if raw is None:
        return None, None

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        length = len(raw) if isinstance(raw, str) else 0
        return None, (
            f"Invalid rendering_evidence JSON on receipt {receipt_id} "
            f"(event {event_id}): parse error (raw length={length})"
        )

    if isinstance(parsed, dict):
        return parsed, None

    # Valid JSON but not an object.
    return None, (
        f"Non-object rendering_evidence on receipt {receipt_id} "
        f"(event {event_id}): expected JSON object, got {type(parsed).__name__}"
    )


# ---------------------------------------------------------------------------
# Event summary
# ---------------------------------------------------------------------------


def _summarize_event(event: CanonicalEvent) -> dict[str, Any]:
    """Build a JSON-safe summary dict from a :class:`CanonicalEvent`.

    Avoids embedding full payloads.  Summarises key fields only.
    """
    raw = msgspec.json.encode(event)
    full: dict[str, Any] = msgspec.json.decode(raw)
    # Strip large fields; keep summary metadata.
    summary: dict[str, Any] = {
        "event_id": full.get("event_id", ""),
        "event_kind": full.get("event_kind", ""),
        "schema_version": full.get("schema_version"),
        "timestamp": full.get("timestamp"),
        "source_adapter": full.get("source_adapter", ""),
        "source_transport_id": full.get("source_transport_id", ""),
        "source_channel_id": full.get("source_channel_id"),
        "parent_event_id": full.get("parent_event_id"),
        "depth": full.get("depth", 0),
        "trace_id": full.get("trace_id"),
        "relation_count": len(full.get("relations") or []),
        "relation_types": sorted(
            {
                r.get("relation_type")
                for r in (full.get("relations") or [])
                if r.get("relation_type") is not None
            }
        ),
        "payload_keys": sorted((full.get("payload") or {}).keys()),
        "has_source_native_ref": full.get("source_native_ref") is not None,
    }
    return summary


def _summarize_receipt(receipt: Any, warnings: list[str]) -> ReceiptSummary:
    """Build a :class:`ReceiptSummary` from a :class:`DeliveryReceipt`.

    Parses ``rendering_evidence`` defensively, appending warnings for
    invalid JSON.
    """
    parsed_evidence, warn = _parse_rendering_evidence(
        receipt.rendering_evidence,
        receipt_id=receipt.receipt_id,
        event_id=receipt.event_id,
    )
    if warn is not None:
        warnings.append(warn)

    return ReceiptSummary(
        receipt_id=receipt.receipt_id,
        sequence=receipt.sequence,
        target_adapter=receipt.target_adapter,
        target_channel=receipt.target_channel,
        route_id=receipt.route_id,
        delivery_plan_id=receipt.delivery_plan_id,
        status=receipt.status,
        attempt_number=receipt.attempt_number,
        source=receipt.source,
        replay_run_id=receipt.replay_run_id,
        failure_kind=receipt.failure_kind,
        error=receipt.error,
        rendering_evidence=parsed_evidence,
        created_at=(
            receipt.created_at.isoformat()
            if isinstance(receipt.created_at, datetime)
            else str(receipt.created_at)
        ),
    )


def _summarize_native_ref(ref: Any) -> dict[str, Any]:
    """Build a JSON-safe summary dict from a :class:`NativeMessageRef`."""
    return {
        "id": ref.id,
        "adapter": ref.adapter,
        "native_channel_id": ref.native_channel_id,
        "native_message_id": ref.native_message_id,
        "direction": ref.direction,
        "created_at": (
            ref.created_at.isoformat()
            if isinstance(ref.created_at, datetime)
            else str(ref.created_at)
        ),
    }


def _to_json_safe_timestamp(value: Any) -> Any:
    """Normalize a timestamp value to a JSON-safe string or ``None``.

    Expected input types:

    * :class:`datetime` - converted via ``.isoformat()``.
    * :class:`str` (ISO 8601) - passed through via ``str()``.
    * ``None`` - returned as-is.

    This codebase does **not** use raw integer (epoch) timestamps for
    created_at / updated_at fields, so integer handling is not required.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _summarize_outbox_item(item: Any) -> dict[str, Any]:
    """Build a JSON-safe summary dict from a :class:`DeliveryOutboxItem`."""
    return {
        "outbox_id": item.outbox_id,
        "route_id": item.route_id,
        "delivery_plan_id": item.delivery_plan_id,
        "target_adapter": item.target_adapter,
        "target_channel": item.target_channel,
        "attempt_number": item.attempt_number,
        "status": item.status,
        "failure_kind": item.failure_kind,
        "error_summary": item.error_summary,
        "created_at": _to_json_safe_timestamp(item.created_at),
        "updated_at": _to_json_safe_timestamp(item.updated_at),
    }


def _retry_outbox_item_to_dict(item: RetryOutboxItemSummary) -> dict[str, Any]:
    """Convert a :class:`RetryOutboxItemSummary` to a JSON-safe dict."""
    return {
        "outbox_id": item.outbox_id,
        "delivery_plan_id": item.delivery_plan_id,
        "event_id": item.event_id,
        "route_id": item.route_id,
        "target_adapter": item.target_adapter,
        "target_channel": item.target_channel,
        "status": item.status,
        "retry_state": item.retry_state,
        "attempt_number": item.attempt_number,
        "next_attempt_at": item.next_attempt_at,
        "next_retry_at": item.next_retry_at,
        "failure_kind": item.failure_kind,
        "failure_taxon": item.failure_taxon,
        "failure_category": item.failure_category,
        "failure_kind_detail": item.failure_kind_detail,
        "parent_receipt_id": item.parent_receipt_id,
        "receipt_id": item.receipt_id,
        "reason_pending": item.reason_pending,
    }


def _retry_outbox_summary_to_dict(summary: RetryOutboxSummary) -> dict[str, Any]:
    """Convert a :class:`RetryOutboxSummary` to a JSON-safe dict."""
    return {
        "counts": dict(sorted(summary.counts.items())),
        "items": [_retry_outbox_item_to_dict(item) for item in summary.items],
        "retry_worker": summary.retry_worker,
    }


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class EvidenceCollector:
    """Read-only collector that assembles an :class:`EvidenceBundle`.

    Parameters
    ----------
    storage:
        Any object satisfying the
        :class:`~medre.core.storage.backend.StorageBackend` protocol
        (or at minimum the :class:`_EvidenceStorage` subset).
    now_fn:
        Injectable clock for deterministic testing.
    """

    def __init__(
        self,
        storage: _EvidenceStorage,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        # Stable per-collector recovery run ID for deterministic evidence bundles.
        # Generated once at construction so repeated collect_for_event calls
        # produce identical JSON when given the same clock and storage.
        import uuid as _uuid

        self._recovery_run_id: str = _uuid.uuid4().hex

    async def collect_for_event(self, event_id: str) -> EvidenceBundle:
        """Collect a deterministic evidence bundle for one event.

        Reads event, receipts, native refs, and outbox items from storage
        (all read-only).  Assembles a frozen bundle with deterministic
        ordering.  Missing event produces a warning but not a crash.
        Missing related records produce empty collections, not errors.

        Parameters
        ----------
        event_id:
            The canonical event ID to collect evidence for.

        Returns
        -------
        EvidenceBundle
            Frozen, JSON-safe evidence bundle.
        """
        warnings: list[str] = []

        # -- Event summary -------------------------------------------------
        event = await self._storage.get(event_id)
        event_summary: dict[str, Any] | None = None
        if event is not None:
            event_summary = _summarize_event(event)
        else:
            warnings.append(f"Event {event_id!r} not found in storage")

        # -- Receipts (ordered by sequence) --------------------------------
        receipts = await self._storage.list_receipts_for_event(event_id)
        # Defensive: ensure order by sequence.
        receipts = sorted(receipts, key=lambda r: r.sequence)
        receipt_summaries = tuple(_summarize_receipt(r, warnings) for r in receipts)

        # -- Native refs (ordered by created_at, id) -----------------------
        native_refs = await self._storage.list_native_refs_for_event(event_id)
        # Defensive: ensure order by created_at, id even if storage sorts.
        native_refs = sorted(native_refs, key=lambda r: (r.created_at, r.id))
        native_ref_summaries = tuple(_summarize_native_ref(r) for r in native_refs)

        # -- Outbox items (ordered by created_at, outbox_id) ---------------
        outbox_items: list[Any] = []
        list_outbox = getattr(self._storage, "list_outbox_items_for_event", None)
        if callable(list_outbox):
            outbox_items = await cast(
                Coroutine[Any, Any, list[Any]], list_outbox(event_id)
            )
        else:
            # Backward-compat: storage backends predating list_outbox_items_for_event.
            warnings.append(
                "list_outbox_items_for_event not available on storage backend"
            )
        # Defensive: ensure order by created_at, outbox_id even if storage sorts.
        outbox_items = sorted(
            outbox_items, key=lambda i: (str(i.created_at or ""), i.outbox_id)
        )
        outbox_summaries = tuple(_summarize_outbox_item(i) for i in outbox_items)

        # -- Replay run IDs (sorted) ---------------------------------------
        replay_run_ids = sorted({r.replay_run_id for r in receipts if r.replay_run_id})

        # -- Sources seen (sorted) -----------------------------------------
        sources_seen = sorted({r.source for r in receipts})

        # -- Empty data check ----------------------------------------------
        if event is None and not receipts and not native_refs and not outbox_items:
            warnings.append(
                f"No event, receipts, native refs, or outbox items found for "
                f"event {event_id!r}"
            )

        # -- Tier inference (conservative) ----------------------------------
        source_adapter_name: str | None = None
        if event_summary is not None:
            source_adapter_name = event_summary.get("source_adapter")

        evidence_tier = infer_evidence_tier(
            sources_seen=tuple(sources_seen),
            source_adapter=source_adapter_name,
        )

        # -- Delivery outcome ledger (pure, from receipts + outbox) ----------
        delivery_outcome_ledger = build_delivery_outcome_ledger(
            receipts=receipts,
            outbox_items=outbox_items,
        ).to_dict()

        # -- Retry/outbox accountability summary (pure) ----------------------
        retry_outbox_summary_obj = build_retry_outbox_summary(
            receipts=receipts,
            outbox_items=outbox_items,
        )
        retry_outbox_dict = _retry_outbox_summary_to_dict(retry_outbox_summary_obj)

        # -- Convergence diagnostics (pure, from receipts + outbox) -----------
        # Convergence diagnostics are pure functions over already-loaded snapshots.
        # Failures surface during pre-release intentionally — no silent degradation.
        convergence_summary_obj = build_convergence_summary(
            receipts=receipts,
            outbox_items=outbox_items,
        )
        convergence_dict = convergence_summary_obj.to_dict()

        # -- Orphan / invalid-lineage report (pure, from receipts + outbox) ----
        # Collector does not have an event catalogue, so known_event_ids
        # is not passed — the orphaned_outbox check is silently skipped.
        orphan_report_obj = build_orphan_report(
            receipts=receipts,
            outbox_items=outbox_items,
        )
        orphan_report_dict = orphan_report_obj.to_dict()

        # Cross-populate orphan count from the authoritative orphan_report.
        # Safe to mutate: convergence_dict is a fresh dict from to_dict(),
        # not the frozen dataclass itself.
        convergence_dict["orphan_count"] = orphan_report_obj.total_findings

        # -- Recovery evidence (pure, from outbox snapshots) -----------------
        # Build per-event recovery ledger and summary from outbox items
        # already loaded.  Without BootSummary the startup_timestamp is
        # unavailable; recovery source defaults to RETRY_WORKER_RECOVERY.
        # Per-event snapshot — uses stable per-collector recovery run ID
        # generated once at construction for deterministic bundles.
        _recovery_now = self._now_fn
        recovery_ledger_obj = build_startup_recovery_ledger(
            outbox_items=outbox_items,
            startup_timestamp=None,
            recovery_run_id=self._recovery_run_id,
            now_fn=lambda: _recovery_now().isoformat(),
        )
        recovery_summary_obj = build_recovery_summary(recovery_ledger_obj)

        # -- Merge recovery convergence findings into orphan report ----------
        recovery_findings = build_recovery_convergence_findings(
            outbox_items=outbox_items,
            receipts=receipts,
            recovery_ledger=recovery_ledger_obj,
        )
        # Merge recovery findings into the orphan report dict.
        existing_findings = orphan_report_dict.get("findings", [])
        if isinstance(existing_findings, list):
            all_findings = list(existing_findings) + [
                f.to_dict() for f in recovery_findings
            ]
            all_findings.sort(key=lambda f: (f.get("kind", ""), f.get("record_id", "")))
            orphan_report_dict["findings"] = all_findings
            orphan_report_dict["total_findings"] = len(all_findings)
            # Recompute severity counts — always preserve the stable shape
            # including "safe": 0 so consumers see a consistent JSON schema.
            sev_counts: dict[str, int] = {"safe": 0, "degraded": 0, "inconsistent": 0}
            for f in all_findings:
                sev = f.get("severity", "degraded")
                sev_counts[sev] = sev_counts.get(sev, 0) + 1
            orphan_report_dict["severity_counts"] = sev_counts
            # Recompute worst_severity consistently.
            if sev_counts.get("inconsistent", 0) > 0:
                orphan_report_dict["worst_severity"] = "inconsistent"
            elif sev_counts.get("degraded", 0) > 0:
                orphan_report_dict["worst_severity"] = "degraded"
            else:
                orphan_report_dict["worst_severity"] = "safe"
            # Update orphan count in convergence dict.
            convergence_dict["orphan_count"] = len(all_findings)

        return EvidenceBundle(
            schema_version=BUNDLE_SCHEMA_VERSION,
            event_id=event_id,
            event_summary=event_summary,
            delivery_receipts=receipt_summaries,
            native_refs=native_ref_summaries,
            outbox_items=outbox_summaries,
            replay_run_ids=tuple(replay_run_ids),
            sources_seen=tuple(sources_seen),
            warnings=tuple(warnings),
            generated_at=self._now_fn().isoformat(),
            evidence_tier=evidence_tier,
            delivery_outcome_ledger=delivery_outcome_ledger,
            retry_outbox_summary=retry_outbox_dict,
            convergence_summary=convergence_dict,
            orphan_report=orphan_report_dict,
            recovery_summary=recovery_summary_obj.to_dict(),
            recovery_ledger=recovery_ledger_obj.to_dict(),
        )
