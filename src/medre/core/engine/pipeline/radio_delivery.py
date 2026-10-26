"""Synchronous radio handoffs with durable, per-packet native-call boundaries.

The adapter owns serialization, pacing and SDK use. Core owns the immutable
wire plan, claim fencing, local handoff evidence and whole-plan aggregation.
No native call is atomic with SQLite; uncertainty blocks automatic resubmission.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import msgspec

from medre.core.contracts.adapter import AdapterPermanentError
from medre.core.contracts.delivery import AdapterHandoffResult
from medre.core.rendering.renderer import RenderingResult
from medre.core.storage.backend import StorageBackend
from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.radio_handoff import (
    RadioLocalHandoff,
    plain_channel_payload_fingerprint,
)

_logger = logging.getLogger(__name__)

_CONFIRMATION_ORDER = (
    "unknown",
    "local_queue",
    "local_transport",
    "remote_service",
    "end_to_end",
)


def _handoff_reference(
    result: AdapterHandoffResult, ordinal: int, payload_fingerprint: str | None = None
) -> str:
    """Retain native scope and confirmation without inventing a native packet ID.

    Channel transports may positively accept a local send without assigning a
    native ID. Such records explicitly retain null and the part's local ordinal;
    they are not materialized as destination-native relation targets.
    """
    return RadioLocalHandoff(
        ordinal=ordinal,
        native_message_id=result.native_message_id,
        native_channel_id=result.native_channel_id,
        confirmation_level=result.confirmation_level,
        payload_fingerprint=payload_fingerprint,
    ).encode()


def _continuation_payload_fingerprint(result: RenderingResult) -> str | None:
    """Record exact plain-channel semantics; unsupported native extras stay held.

    The wire plan retains text but not reply IDs or adapter-specific commands.
    Only a canonical numeric channel with no additional native fields is safe
    to reconstruct from the retained text plan after an interruption.
    """
    if set(result.payload) != {"text", "channel_index"}:
        return None
    channel = result.payload["channel_index"]
    if type(channel) is not int or channel < 0 or str(channel) != result.target_channel:
        return None
    return plain_channel_payload_fingerprint(channel)


@dataclass
class _Submission:
    """One-use grant state whose lifetime cannot leak into the next loop iteration."""

    storage: StorageBackend
    checkpoint: StoredRadioAttempt
    worker_id: str | None
    ordinal: int
    deadline: datetime | None = None
    entered: bool = False
    submitting: StoredRadioAttempt | None = None

    async def begin(self) -> None:
        """Persist one native-call boundary after pacing, refusing reuse or claim loss."""
        if self.entered:
            raise AdapterPermanentError(
                "radio native submission grant cannot be reused"
            )
        self.entered = True
        if self.deadline is not None and datetime.now(timezone.utc) > self.deadline:
            raise AdapterPermanentError(
                "radio delivery deadline exceeded before native submission"
            )
        self.submitting = await self.storage.begin_radio_part(
            self.checkpoint, self.worker_id, self.ordinal
        )
        if self.submitting is None:
            raise AdapterPermanentError(
                "radio claim expired or checkpoint changed before send"
            )


async def deliver_radio_plan(
    storage: StorageBackend,
    result: RenderingResult,
    worker_id: str | None,
    deliver: Callable[[RenderingResult], Awaitable[AdapterHandoffResult]],
    *,
    deadline: datetime | None = None,
    resume_from: StoredRadioAttempt | None = None,
) -> AdapterHandoffResult:
    """Bind once, grant each native call, and aggregate only a complete local plan.

    The grant callback must run exactly once inside the adapter's send owner,
    immediately before a single native invocation. Adapters must disable internal
    retries for that invocation. A failed or cancelled native call retains unknown
    progress; no accepted or uncertain prefix is replayed by a new generation.

    ``resume_from`` is an explicit caller-selected continuation, never inferred
    by automatic retry or accepted from a renderer. Storage independently checks
    the exact retained predecessor and a newer current claim. Only plain channel
    text is eligible; reply or other native payload semantics are not persisted
    by the text plan and therefore cannot be reconstructed safely here.
    """
    plan = result.radio_plan
    provenance = result.attempt_provenance
    if plan is None or provenance is None:
        raise AdapterPermanentError("radio plans require an exact durable attempt")
    payload_fingerprint = _continuation_payload_fingerprint(result)
    if resume_from is None:
        checkpoint = await storage.bind_radio_attempt(provenance, worker_id, plan)
    else:
        try:
            eligible = payload_fingerprint is not None and all(
                RadioLocalHandoff.from_part(
                    part, resume_from.provenance
                ).payload_fingerprint
                == payload_fingerprint
                for part in resume_from.progress.parts
                if part.state == "accepted"
            )
        except (ValueError, msgspec.DecodeError) as exc:
            raise AdapterPermanentError(
                "radio continuation evidence is invalid"
            ) from exc
        if not eligible:
            raise AdapterPermanentError(
                "radio continuation requires plain channel text without native extras"
            )
        checkpoint = await storage.resume_radio_attempt(
            provenance, worker_id, plan, resume_from
        )
    if checkpoint is None:
        raise AdapterPermanentError(
            "radio plan is already bound or its claim is not owned"
        )
    # Use storage's exact plan; a caller cannot substitute a changed wire tail.
    plan = checkpoint.progress.plan
    confirmations = [
        RadioLocalHandoff.from_part(part, checkpoint.provenance).handoff()
        for part in checkpoint.progress.parts
        if part.state == "accepted"
    ]
    for part, progress in zip(plan.parts, checkpoint.progress.parts, strict=True):
        if progress.state == "accepted":
            continue
        grant = _Submission(storage, checkpoint, worker_id, part.ordinal, deadline)
        packet = replace(
            result,
            payload={**result.payload, "text": part.wire_text},
            radio_plan=None,
            before_native_send=grant.begin,
        )
        try:
            handoff = await deliver(packet)
            if grant.submitting is None:
                raise AdapterPermanentError(
                    "radio adapter skipped its native submission grant"
                )
            if (
                not isinstance(handoff, AdapterHandoffResult)
                or handoff.disposition != "transport_handoff"
            ):
                raise AdapterPermanentError(
                    "radio parts require synchronous handoff evidence"
                )
            accepted = await storage.observe_radio_part(
                grant.submitting,
                worker_id,
                part.ordinal,
                handoff_id=_handoff_reference(
                    handoff, part.ordinal, payload_fingerprint
                ),
            )
            if accepted is None:
                raise AdapterPermanentError(
                    "radio claim changed before handoff evidence committed"
                )
            checkpoint = accepted
            confirmations.append(handoff)
        except BaseException as exc:
            if grant.submitting is not None:
                # This write may lose its claim too. The retained submitting
                # record is still fail-closed and becomes unknown on recovery.
                try:
                    marked = await storage.observe_radio_part(
                        grant.submitting, worker_id, part.ordinal
                    )
                    if marked is None:
                        _logger.warning(
                            "Radio uncertainty checkpoint not committed; retained submission blocks replay: "
                            "outbox_id=%s attempt=%d part=%d",
                            provenance.outbox_id,
                            provenance.attempt_number,
                            part.ordinal,
                        )
                except Exception:
                    _logger.warning(
                        "Radio uncertainty checkpoint failed; retained submission blocks replay: "
                        "outbox_id=%s attempt=%d part=%d",
                        provenance.outbox_id,
                        provenance.attempt_number,
                        part.ordinal,
                        exc_info=True,
                    )
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            if grant.submitting is not None or any(
                p.state != "unsent" for p in checkpoint.progress.parts
            ):
                raise AdapterPermanentError(
                    "radio handoff incomplete; retained native progress requires inspection"
                ) from exc
            raise
    if not checkpoint.progress.handoff_complete:
        raise AdapterPermanentError("radio handoff did not cover the whole plan")
    if len(confirmations) == 1:
        return confirmations[0]
    weakest = min(
        confirmations,
        key=lambda item: _CONFIRMATION_ORDER.index(item.confirmation_level),
    )
    return AdapterHandoffResult(
        confirmation_level=weakest.confirmation_level,
        note="All radio parts locally accepted; no whole-message native relation target",
        metadata={
            "radio": {
                "schema_version": 1,
                "plan_fingerprint": plan.fingerprint,
                "part_count": len(plan.parts),
                "handoff_complete": True,
            }
        },
    )
