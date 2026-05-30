"""Frozen, JSON-safe evidence bundle model and supporting types.

The :class:`EvidenceBundle` is a read-only, deterministic snapshot of all
stored evidence for a single event.  It **must not** mutate storage or
runtime state.

Deterministic ordering guarantees
----------------------------------
* ``delivery_receipts`` ordered by ``sequence`` (append order).
* ``native_refs`` ordered by ``created_at``, then ``id``.
* ``outbox_items`` ordered by ``created_at``, then ``outbox_id``.
* ``replay_run_ids`` sorted lexicographically.
* ``warnings`` preserved in deterministic insertion order.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import msgspec

# ---------------------------------------------------------------------------
# Bundle schema version
# ---------------------------------------------------------------------------

BUNDLE_SCHEMA_VERSION: int = 1
"""Evidence bundle schema version.  Frozen at 1 during pre-release."""

# ---------------------------------------------------------------------------
# Receipt summary
# ---------------------------------------------------------------------------


class ReceiptSummary(msgspec.Struct, frozen=True):
    """Deterministic summary of a :class:`~medre.core.events.DeliveryReceipt`.

    Designed for inclusion in an :class:`EvidenceBundle` without embedding
    the full receipt or large payloads.
    """

    receipt_id: str = ""
    sequence: int = 0
    target_adapter: str = ""
    target_channel: str | None = None
    route_id: str = ""
    delivery_plan_id: str = ""
    status: str = ""
    attempt_number: int = 1
    source: str = "live"
    replay_run_id: str | None = None
    failure_kind: str | None = None
    error: str | None = None
    rendering_evidence: dict[str, Any] | None = None
    created_at: str = ""


# ---------------------------------------------------------------------------
# Evidence bundle
# ---------------------------------------------------------------------------


class EvidenceBundle(msgspec.Struct, frozen=True):
    """Frozen, JSON-safe evidence bundle for a single event.

    Aggregates event summary, delivery receipt summaries, parsed rendering
    evidence, native refs, outbox state, replay/source context, diagnostics,
    and a generated timestamp - without mutating runtime state.

    Attributes
    ----------
    schema_version:
        Bundle schema version (currently ``1``).
    event_id:
        The canonical event ID this bundle covers.
    event_summary:
        Summary dict of the canonical event (or ``None`` if event missing).
    delivery_receipts:
        Ordered list of receipt summaries (by ``sequence``).
    native_refs:
        Ordered list of native ref summary dicts (by ``created_at``, ``id``).
    outbox_items:
        Ordered list of outbox item summary dicts (by ``created_at``,
        ``outbox_id``).
    replay_run_ids:
        Sorted list of distinct ``replay_run_id`` values seen on receipts.
    sources_seen:
        Sorted list of distinct ``source`` values seen on receipts.
    warnings:
        Deterministic list of warning strings collected during assembly.
    generated_at:
        ISO 8601 timestamp when this bundle was created.
    """

    schema_version: int = BUNDLE_SCHEMA_VERSION
    event_id: str = ""
    event_summary: dict[str, Any] | None = None
    delivery_receipts: tuple[ReceiptSummary, ...] = ()
    native_refs: tuple[dict[str, Any], ...] = ()
    outbox_items: tuple[dict[str, Any], ...] = ()
    replay_run_ids: tuple[str, ...] = ()
    sources_seen: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation.

        All nested containers are plain ``dict`` / ``list`` / ``str`` /
        ``int`` / ``float`` / ``bool`` / ``None`` - ``json.dumps()`` will
        succeed without a custom encoder.
        """
        return _bundle_to_dict(self)

    def to_json(self, *, sort_keys: bool = True, indent: int | None = None) -> str:
        """Return a deterministic JSON string.

        Parameters
        ----------
        sort_keys:
            Sort dict keys for deterministic output (default ``True``).
        indent:
            Optional indentation for pretty-printing.
        """
        return json.dumps(self.to_dict(), sort_keys=sort_keys, indent=indent)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _receipt_summary_to_dict(rs: ReceiptSummary) -> dict[str, Any]:
    """Convert a ReceiptSummary to a JSON-safe dict."""
    return msgspec.structs.asdict(rs)


def _bundle_to_dict(bundle: EvidenceBundle) -> dict[str, Any]:
    """Convert an EvidenceBundle to a fully JSON-safe dict."""
    return {
        "schema_version": bundle.schema_version,
        "event_id": bundle.event_id,
        "event_summary": copy.deepcopy(bundle.event_summary),
        "delivery_receipts": [
            _receipt_summary_to_dict(r) for r in bundle.delivery_receipts
        ],
        "native_refs": [copy.deepcopy(r) for r in bundle.native_refs],
        "outbox_items": [copy.deepcopy(i) for i in bundle.outbox_items],
        "replay_run_ids": list(bundle.replay_run_ids),
        "sources_seen": list(bundle.sources_seen),
        "warnings": list(bundle.warnings),
        "generated_at": bundle.generated_at,
    }
