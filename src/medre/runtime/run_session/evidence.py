"""Evidence collection for run-session.

Provides helpers to create session events, select source adapters,
poll for delivery receipts, and collect native refs for the evidence
report.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from medre.core.events.canonical import CanonicalEvent, NativeMessageRef
from medre.core.events.kinds import EventKind
from medre.runtime.app import MedreApp

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RECEIPT_POLL_TIMEOUT: float = 3.0
"""Seconds to wait for delivery receipts after event injection."""

_RECEIPT_POLL_INTERVAL: float = 0.1
"""Seconds between receipt polling attempts."""

# ---------------------------------------------------------------------------
# Event creation
# ---------------------------------------------------------------------------


def _make_session_event(
    adapter: Any,
    text: str,
) -> CanonicalEvent:
    """Create a canonical event with both 'body' and 'text' payload keys.

    Bridges the gap between FakeMatrixAdapter.make_event (stores under
    ``"body"``) and TextRenderer (reads ``payload["text"]``) so rendered
    output is non-empty and inspectable.
    """
    base = adapter.make_event(text=text, event_kind=EventKind.MESSAGE_TEXT)
    merged = dict(base.payload)
    merged["text"] = text
    return CanonicalEvent(
        event_id=base.event_id,
        event_kind=base.event_kind,
        schema_version=base.schema_version,
        timestamp=base.timestamp,
        source_adapter=base.source_adapter,
        source_transport_id=base.source_transport_id,
        source_channel_id=base.source_channel_id,
        parent_event_id=base.parent_event_id,
        lineage=base.lineage,
        relations=base.relations,
        payload=merged,
        metadata=base.metadata,
        source_native_ref=base.source_native_ref,
    )


# ---------------------------------------------------------------------------
# Adapter selection
# ---------------------------------------------------------------------------


def _pick_source_adapter(app: MedreApp) -> tuple[str, Any]:
    """Select a deterministic source adapter for session injection.

    Prefers a Matrix-platform adapter since fake-bridge-smoke.toml routes
    originate from ``fake_matrix``.
    """
    for aid in sorted(app.adapters.keys()):
        adapter = app.adapters[aid]
        if getattr(adapter, "platform", None) == "matrix":
            return aid, adapter
    aid = sorted(app.adapters.keys())[0]
    return aid, app.adapters[aid]


# ---------------------------------------------------------------------------
# Receipt polling
# ---------------------------------------------------------------------------


async def _poll_for_receipts(
    storage: Any,
    event_id: str,
    timeout: float = _RECEIPT_POLL_TIMEOUT,
    interval: float = _RECEIPT_POLL_INTERVAL,
) -> list[Any]:
    """Poll storage for delivery receipts until found or timeout.

    Returns the receipt list (may be empty on timeout).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            receipts = await storage.list_receipts_for_event(event_id)
            if receipts:
                return receipts
        except Exception:
            pass
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            break
        await asyncio.sleep(min(interval, deadline - now))

    # Final attempt.
    try:
        return await storage.list_receipts_for_event(event_id)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Native ref collection
# ---------------------------------------------------------------------------


async def _collect_native_refs(
    app: MedreApp,
    outcomes: list[Any],
    event_id: str,
    errors: list[str],
) -> list[dict[str, str]]:
    """Resolve native refs for each successful delivery outcome.

    Derives evidence from actual stored receipts rather than fabricating
    platform-specific IDs.  Looks up ``NativeMessageRef`` records persisted
    by the pipeline when adapters return an ``AdapterDeliveryResult`` with
    ``native_message_id`` set.
    """
    refs: list[dict[str, str]] = []
    storage = app.storage
    if storage is None:
        return refs

    # Retrieve actual native refs stored by the pipeline for this event.
    native_ref_records: list[NativeMessageRef] = []
    try:
        native_ref_records = await storage.list_native_refs_for_event(event_id)
    except (AttributeError, TypeError):
        # Storage backend may not implement list_native_refs_for_event in
        # all test mocks; native refs will be empty but not fatal.
        _logger.debug(
            "Storage does not support list_native_refs_for_event; "
            "native refs will be omitted from the report",
        )
    except Exception as exc:
        errors.append(f"Native ref lookup error: {exc}")
        return refs

    for nref in native_ref_records:
        # Only include outbound refs for adapters that have successful outcomes.
        if nref.direction != "outbound":
            continue
        has_success = any(
            o.status == "success" and o.target_adapter == nref.adapter
            for o in outcomes
        )
        if not has_success:
            continue
        # Verify via resolve_native_ref.
        try:
            resolved = await storage.resolve_native_ref(
                nref.adapter,
                nref.native_channel_id,
                nref.native_message_id,
            )
        except Exception as exc:
            errors.append(
                f"resolve_native_ref failed for adapter={nref.adapter}: {exc}"
            )
            continue
        refs.append({
            "adapter": nref.adapter,
            "channel": nref.native_channel_id or "",
            "native_id": nref.native_message_id,
            "resolves_to": resolved or nref.event_id,
        })

    return refs
