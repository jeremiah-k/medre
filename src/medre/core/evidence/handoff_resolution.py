"""Uncertain-native-handoff remediation: resolve-by-evidence decisions.

Gap G02 remediation. Recovery previously classified an uncertain native
handoff (crash mid-send, lost adapter acknowledgement, process death between
the grant fence and receipt persistence) and accounted for it conservatively
without ever determining the actual outcome. This module is the bounded,
read-only evidence pass that decides what the retained facts establish, so
claim reconciliation can settle the obligation instead of only consuming the
attempt ambiguously.

Contract
--------
``resolve_uncertain_handoff`` is pure: it consumes evidence the system
already holds — the attempt's retained radio checkpoint (if any) and the
outbox's append-only delivery observations — and returns one verdict of a
closed vocabulary:

* ``delivered_with_evidence`` — a fenced radio checkpoint shows the whole
  plan locally handed off, or adapter-reported observations prove
  provider-side acceptance of the whole message at ``remote_service`` or
  ``end_to_end`` confirmation. The verdict claims evidence only; the
  settlement commits through the existing guarded outbox transition and
  never fabricates a receipt.
* ``definitively_not_submitted`` — the radio grant fence is wholly empty:
  the plan was bound but no packet-start grant ever committed, so no native
  call was authorized. Normal retry semantics follow with an evidence-backed
  diagnostic.
* ``unresolved`` — everything else, including every case where evidence is
  absent, conflicting, or unreadable. Absence of evidence never proves
  rejection; conflicting reports prevent settlement on that pass. The
  attempt is consumed conservatively and its diagnostic enables a bounded
  pre-dispatch recheck before a newer attempt dispatches. Normal retry may
  still dispatch when that recheck cannot resolve the outcome.

Honesty boundaries
------------------
* The decision never mutates state. :class:`EvidenceBackedHandoffResolver`
  only reads; the caller settles through existing guarded transitions.
* Radio attempts are judged by their per-part checkpoint discipline. Packet
  observations are correlated exactly like the read-only packet projector,
  and packet-specific admission requires a retained original acceptance.
  Ordinary observation storage can contain uncorrelated claims, which are
  unreadable to this resolver. An uncertain radio part is never upgraded
  from an observation here, although whole-message acceptance can settle
  the obligation without changing its parts. The adapter-side capability to
  attest an uncertain packet is a named follow-up, not an assumption here.
* Message-level observations are trusted only with a complete, exact
  delivery identity on the reclaimed attempt; packet-scoped facts are
  ignored when no checkpoint exists to correlate them. An observation
  whose packet metadata is invalid is unreadable evidence: it is never
  classified as whole-message evidence and never correlated as a packet
  fact, so it can neither prove nor refute — fail closed to ``unresolved``.
* A checkpoint is validated against the reclaimed row's dispatch
  provenance as well as its delivery identity: a checkpoint bound under a
  different replay run, or one whose source lineage disagrees with the row,
  never settles anything.
* Whole-message negative reports are evidence too: a failure observation
  for the exact attempt keeps a fully accepted checkpoint held instead of
  letting conflicting evidence settle ``delivered``.
* Dispatch provenance constrains the verdict: a checkpoint from a foreign
  attempt source or replay run remains unresolved even if its parts were
  accepted. It never grants send authority and settlement never rewrites
  ``dispatch_source``/``replay_run_id``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from medre.core.delivery_authority import (
    DeliveryIdentity,
    dispatch_source_matches_attempt,
)
from medre.core.events import DeliveryObservation
from medre.core.rendering.radio_text import MAX_RADIO_TEXT_PARTS
from medre.core.storage.backend import DeliveryOutboxItem, StorageError
from medre.core.storage.radio import RadioAttemptMetadata, StoredRadioAttempt
from medre.core.storage.radio_handoff import RadioLocalHandoff

__all__ = [
    "HANDOFF_NOT_SUBMITTED_DETAIL",
    "UNCERTAIN_NATIVE_HANDOFF_DETAIL",
    "EvidenceBackedHandoffResolver",
    "HandoffResolutionVerdict",
    "UncertainHandoffResolution",
    "UncertainNativeHandoffResolver",
    "resolve_uncertain_handoff",
]

HandoffResolutionVerdict = Literal[
    "delivered_with_evidence",
    "definitively_not_submitted",
    "unresolved",
]
"""Closed settlement vocabulary for one uncertain native handoff."""

#: Operator-visible ``failure_kind_detail`` for an attempt whose native
#: handoff outcome stayed undetermined after the bounded evidence pass.
UNCERTAIN_NATIVE_HANDOFF_DETAIL = "uncertain_native_handoff"

#: Operator-visible ``failure_kind_detail`` for an attempt whose retained
#: grant fence proves no native submission was ever authorized.
HANDOFF_NOT_SUBMITTED_DETAIL = "handoff_not_submitted"

#: Confirmation levels that prove provider-side acceptance. Local queue and
#: transport echoes are MEDRE-local facts and never settle a send.
_CONFIRMATION_LEVELS_PROVING_ACCEPTANCE = frozenset({"remote_service", "end_to_end"})

#: Post-handoff observation states that report a transport-side negative.
_NEGATIVE_OBSERVATION_STATES = frozenset({"failed", "rejected", "cancelled"})

_UNCERTAIN_PART_STATES = frozenset({"submitting", "unknown"})


@dataclass(frozen=True, slots=True)
class UncertainHandoffResolution:
    """One bounded, read-only resolution decision over retained evidence.

    ``uncertain`` marks that the pass actually found unresolved native
    handoff state (an interrupted radio part, negative transport reports, or
    unreadable evidence) rather than simply finding nothing. Part counts are
    populated when the radio checkpoint can be classified. The observation
    count fences settlement or the next reservation against appended evidence.
    """

    verdict: HandoffResolutionVerdict
    attempt_number: int
    uncertain: bool
    reasons: tuple[str, ...]
    has_checkpoint: bool
    accepted_parts: int | None = None
    uncertain_parts: int | None = None
    unsent_parts: int | None = None
    observation_count: int | None = None

    def summary(self) -> str:
        """Return a bounded human-readable statement of what was decided."""
        reasons = ", ".join(self.reasons)
        summary = f"attempt {self.attempt_number}: {reasons}"
        if self.accepted_parts is not None:
            summary += (
                f"; {self.accepted_parts} accepted, {self.uncertain_parts} uncertain, "
                f"{self.unsent_parts} unsent packet(s)"
            )
        return summary


def _resolution(
    verdict: HandoffResolutionVerdict,
    attempt_number: int,
    *,
    uncertain: bool,
    reason: str,
    has_checkpoint: bool,
    accepted_parts: int | None = None,
    uncertain_parts: int | None = None,
    unsent_parts: int | None = None,
) -> UncertainHandoffResolution:
    return UncertainHandoffResolution(
        verdict=verdict,
        attempt_number=attempt_number,
        uncertain=uncertain,
        reasons=(reason,),
        has_checkpoint=has_checkpoint,
        accepted_parts=accepted_parts,
        uncertain_parts=uncertain_parts,
        unsent_parts=unsent_parts,
    )


def _matches_identity(
    observation: DeliveryObservation, identity: DeliveryIdentity
) -> bool:
    return (
        observation.event_id == identity.event_id
        and observation.delivery_plan_id == identity.delivery_plan_id
        and observation.target_adapter == identity.target_adapter
        and (observation.target_channel or None) == identity.target_channel
    )


_OBSERVATION_SCOPE_MESSAGE = "message"
_OBSERVATION_SCOPE_PACKET = "packet"
_OBSERVATION_SCOPE_UNREADABLE = "unreadable"


def _observation_scope(observation: DeliveryObservation) -> str:
    """Classify one observation's evidence scope without trusting its payload.

    An observation whose metadata blob is not a readable mapping, or whose
    ``radio_packet`` claim is not a well-formed mapping, is unreadable
    evidence: it is never classified as whole-message evidence and never
    correlated as a packet fact, so it can neither prove nor refute.
    """
    metadata = observation.metadata
    if not isinstance(metadata, Mapping):
        return _OBSERVATION_SCOPE_UNREADABLE
    if "radio_packet" not in metadata:
        return _OBSERVATION_SCOPE_MESSAGE
    return (
        _OBSERVATION_SCOPE_PACKET
        if isinstance(metadata["radio_packet"], Mapping)
        else _OBSERVATION_SCOPE_UNREADABLE
    )


def _packet_ordinal(observation: DeliveryObservation) -> int | None:
    """Read a packet ordinal only for an exact, structurally valid claim."""
    payload = observation.metadata.get("radio_packet")
    if not isinstance(payload, Mapping):
        return None
    ordinal = payload.get("ordinal")
    origin_attempt = payload.get("origin_attempt_number")
    if (
        type(ordinal) is not int
        or not 1 <= ordinal <= MAX_RADIO_TEXT_PARTS
        or type(origin_attempt) is not int
        or origin_attempt != observation.attempt_number
        or payload.get("origin_outbox_id") != observation.outbox_id
    ):
        return None
    return ordinal


def _exact_observations(
    observations: Sequence[DeliveryObservation],
    *,
    outbox_id: str,
    attempt_number: int,
) -> list[DeliveryObservation]:
    return [
        observation
        for observation in observations
        if observation.outbox_id == outbox_id
        and observation.attempt_number == attempt_number
    ]


def _proves_provider_acceptance(observation: DeliveryObservation) -> bool:
    return (
        observation.state == "delivered"
        and observation.confirmation_level in _CONFIRMATION_LEVELS_PROVING_ACCEPTANCE
    )


def _resolve_message_level(
    *,
    outbox_id: str,
    attempt_number: int,
    identity: DeliveryIdentity,
    observations: Sequence[DeliveryObservation],
) -> UncertainHandoffResolution:
    relevant = _exact_observations(
        observations,
        outbox_id=outbox_id,
        attempt_number=attempt_number,
    )
    message_level = []
    for observation in relevant:
        if not _matches_identity(observation, identity):
            return _resolution(
                "unresolved",
                attempt_number,
                uncertain=True,
                reason="observation_identity_mismatch",
                has_checkpoint=False,
            )
        scope = _observation_scope(observation)
        if scope == _OBSERVATION_SCOPE_MESSAGE:
            message_level.append(observation)
        elif (
            scope == _OBSERVATION_SCOPE_UNREADABLE
            or _packet_ordinal(observation) is None
        ):
            return _resolution(
                "unresolved",
                attempt_number,
                uncertain=True,
                reason="unreadable_packet_evidence",
                has_checkpoint=False,
            )

    positive = any(_proves_provider_acceptance(item) for item in message_level)
    negative = any(item.state in _NEGATIVE_OBSERVATION_STATES for item in message_level)
    if positive and not negative:
        return _resolution(
            "delivered_with_evidence",
            attempt_number,
            uncertain=False,
            reason="adapter_reported_provider_acceptance",
            has_checkpoint=False,
        )
    if negative:
        return _resolution(
            "unresolved",
            attempt_number,
            uncertain=True,
            reason="conflicting_or_negative_observation",
            has_checkpoint=False,
        )
    return _resolution(
        "unresolved",
        attempt_number,
        uncertain=False,
        reason="no_resolution_evidence",
        has_checkpoint=False,
    )


def _resolve_checkpoint(
    *,
    outbox_id: str,
    checkpoint: StoredRadioAttempt | RadioAttemptMetadata,
    attempt_number: int,
    identity: DeliveryIdentity,
    observations: Sequence[DeliveryObservation],
    attempt_source: str | None = None,
    replay_run_id: str | None = None,
) -> UncertainHandoffResolution:
    if isinstance(checkpoint, StoredRadioAttempt):
        provenance = checkpoint.provenance
        parts = checkpoint.progress.parts
    else:
        provenance = checkpoint.provenance
        parts = checkpoint.parts
    part_count = len(parts)
    if part_count == 0:
        return _resolution(
            "unresolved",
            attempt_number,
            uncertain=True,
            reason="unreadable_radio_checkpoint",
            has_checkpoint=True,
        )
    mismatch = (
        provenance.outbox_id != outbox_id
        or provenance.attempt_number != attempt_number
        or provenance.event_id != identity.event_id
        or provenance.delivery_plan_id != identity.delivery_plan_id
        or provenance.target_adapter != identity.target_adapter
        or (provenance.target_channel or None) != identity.target_channel
    )
    if mismatch:
        return _resolution(
            "unresolved",
            attempt_number,
            uncertain=True,
            reason="checkpoint_identity_mismatch",
            has_checkpoint=True,
        )

    if attempt_source is not None or replay_run_id is not None:
        # Provenance coherence with the reclaimed row. The exact lane identity
        # (adapter and channel) is already validated above; a checkpoint bound
        # under a different replay run, or one whose dispatch-source lineage
        # disagrees with the row (live versus replay-lineage), never settles
        # anything.
        coherent_source = attempt_source is None or dispatch_source_matches_attempt(
            attempt_source, provenance.source, attempt_number
        )
        coherent = provenance.replay_run_id == replay_run_id and coherent_source
        if not coherent:
            return _resolution(
                "unresolved",
                attempt_number,
                uncertain=True,
                reason="checkpoint_provenance_incoherent",
                has_checkpoint=True,
            )

    accepted: dict[int, RadioLocalHandoff] = {}
    try:
        for part in parts:
            if part.state == "accepted":
                accepted[part.ordinal] = RadioLocalHandoff.from_part(part, provenance)
    except (TypeError, ValueError):
        return _resolution(
            "unresolved",
            attempt_number,
            uncertain=True,
            reason="unreadable_handoff_reference",
            has_checkpoint=True,
        )
    uncertain = [part for part in parts if part.state in _UNCERTAIN_PART_STATES]
    unsent = [part for part in parts if part.state == "unsent"]

    # Classify the exact attempt's observations. Packet observations are
    # admitted by the packet-specific API only against a retained acceptance.
    # A negative packet report contradicts those facts; no packet report can
    # upgrade an uncertain part. A whole-message failure report
    # for the exact attempt contradicts a fully accepted checkpoint the same
    # way. A packet claim whose metadata is invalid, or whose part reference
    # does not correlate exactly, is unreadable evidence: it neither proves
    # nor refutes and must never be classified as whole-message evidence —
    # fail closed to unresolved.
    negative_packet = False
    negative_message = False
    positive_message = False
    unreadable_packet = False
    for observation in _exact_observations(
        observations,
        outbox_id=outbox_id,
        attempt_number=attempt_number,
    ):
        if not _matches_identity(observation, identity):
            return _resolution(
                "unresolved",
                attempt_number,
                uncertain=True,
                reason="observation_identity_mismatch",
                has_checkpoint=True,
            )
        scope = _observation_scope(observation)
        if scope == _OBSERVATION_SCOPE_MESSAGE:
            negative_message |= observation.state in _NEGATIVE_OBSERVATION_STATES
            positive_message |= _proves_provider_acceptance(observation)
            continue
        if scope == _OBSERVATION_SCOPE_UNREADABLE:
            unreadable_packet = True
            continue
        ordinal = _packet_ordinal(observation)
        handoff = accepted.get(ordinal) if ordinal is not None else None
        if (
            handoff is None
            or handoff.origin is None
            or handoff.origin.outbox_id != outbox_id
            or handoff.origin.attempt_number != attempt_number
            or handoff.native_message_id is None
            or handoff.native_message_id != observation.adapter_message_id
            or handoff.native_channel_id != observation.native_channel_id
        ):
            unreadable_packet = True
        elif observation.state in _NEGATIVE_OBSERVATION_STATES:
            negative_packet = True

    # A whole-message observation can settle the obligation without changing
    # any local packet state. A packet observation alone cannot do so.
    reason: str
    verdict: HandoffResolutionVerdict = "unresolved"
    if unreadable_packet:
        reason = "unreadable_packet_evidence"
    elif negative_packet:
        reason = "conflicting_packet_observation"
    elif negative_message:
        reason = "conflicting_message_observation"
    elif positive_message and unsent:
        # An unsent part has no grant; whole-message acceptance cannot be
        # reconciled with an incomplete grant fence.
        reason = (
            "provider_acceptance_conflicts_with_empty_fence"
            if not accepted and not uncertain
            else "provider_acceptance_conflicts_with_unsent_parts"
        )
    elif positive_message:
        verdict = "delivered_with_evidence"
        reason = "adapter_reported_provider_acceptance"
    elif uncertain:
        reason = "uncertain_native_submission"
    elif len(accepted) == part_count:
        verdict = "delivered_with_evidence"
        reason = "radio_handoff_complete"
    elif not accepted:
        verdict = "definitively_not_submitted"
        reason = "native_grant_fence_empty"
    else:
        reason = "unclassified_radio_progress"
    return _resolution(
        verdict,
        attempt_number,
        uncertain=verdict == "unresolved",
        reason=reason,
        has_checkpoint=True,
        accepted_parts=len(accepted),
        uncertain_parts=len(uncertain),
        unsent_parts=len(unsent),
    )


def resolve_uncertain_handoff(
    *,
    outbox_id: str,
    attempt_number: int,
    identity: DeliveryIdentity,
    checkpoint: StoredRadioAttempt | RadioAttemptMetadata | None,
    observations: Sequence[DeliveryObservation],
    attempt_source: str | None = None,
    replay_run_id: str | None = None,
) -> UncertainHandoffResolution:
    """Decide what retained evidence establishes about one attempt's handoff.

    Pure and fail-closed. The verdict never mutates state, never grants a
    send, and never fabricates evidence; see the module contract for the
    honesty boundaries. *attempt_source* and *replay_run_id* carry the
    reclaimed row's dispatch provenance; when provided, a retained checkpoint
    must be coherent with them or nothing settles.
    """
    if not identity.complete or not outbox_id or attempt_number < 1:
        return _resolution(
            "unresolved",
            attempt_number,
            uncertain=True,
            reason="incomplete_delivery_identity",
            has_checkpoint=checkpoint is not None,
        )
    if checkpoint is None:
        return _resolve_message_level(
            outbox_id=outbox_id,
            attempt_number=attempt_number,
            identity=identity,
            observations=observations,
        )
    return _resolve_checkpoint(
        outbox_id=outbox_id,
        checkpoint=checkpoint,
        attempt_number=attempt_number,
        identity=identity,
        observations=observations,
        attempt_source=attempt_source,
        replay_run_id=replay_run_id,
    )


class UncertainNativeHandoffResolver(Protocol):
    """The evidence-pass surface :meth:`reconcile_retry_claim` consumes."""

    async def resolve(
        self, item: DeliveryOutboxItem, attempt_number: int
    ) -> UncertainHandoffResolution:
        """Resolve one reclaimed attempt from evidence storage already holds."""
        ...


class HandoffEvidenceStore(Protocol):
    """Narrow read surface the evidence-backed resolver needs.

    Backends without both surfaces leave resolution dormant at the runtime
    composition boundary, mirroring the optional deadline-sweep policy.
    """

    async def get_radio_attempt(
        self, outbox_id: str, attempt_number: int
    ) -> StoredRadioAttempt | None: ...

    async def list_delivery_observations_for_attempt(
        self, outbox_id: str, attempt_number: int
    ) -> list[DeliveryObservation]: ...


class EvidenceBackedHandoffResolver:
    """Load one attempt's retained evidence and project the verdict.

    The resolver never writes. The durable row's current dispatch source can
    describe a newer attempt; when rechecking an older checkpoint, source
    coherence belongs to that checkpoint's original reservation and cannot
    be reconstructed from the newer row. Exact identity and replay run still
    constrain its verdict. A storage error while reading the evidence is
    reported as an explicitly uncertain ``unresolved`` result so
    reconciliation keeps its conservative accounting instead of failing the
    reclaim; the operator-visible diagnostic names the unreadable evidence.
    Corrupt or malformed retained rows — an observation payload that fails to
    decode, an unreadable timestamp — are handled the same way: they neither
    prove nor refute, and they never raise out of reconciliation, which would
    strand the claimed reservation.
    """

    def __init__(self, storage: HandoffEvidenceStore) -> None:
        self._storage = storage

    async def resolve(
        self, item: DeliveryOutboxItem, attempt_number: int
    ) -> UncertainHandoffResolution:
        identity = DeliveryIdentity(
            item.event_id,
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel,
        )
        checkpoint = None
        try:
            checkpoint = await self._storage.get_radio_attempt(
                item.outbox_id, attempt_number
            )
            observations = await self._storage.list_delivery_observations_for_attempt(
                item.outbox_id, attempt_number
            )
        except StorageError:
            return _resolution(
                "unresolved",
                attempt_number,
                uncertain=True,
                reason="resolution_evidence_unavailable",
                has_checkpoint=checkpoint is not None,
            )
        except (TypeError, ValueError):
            # Undecodable retained evidence (msgspec validation errors and
            # malformed timestamps are ValueError subclasses). Fail closed to
            # an explicitly uncertain verdict instead of stranding the claim.
            return _resolution(
                "unresolved",
                attempt_number,
                uncertain=True,
                reason="resolution_evidence_unreadable",
                has_checkpoint=checkpoint is not None,
            )
        resolution = resolve_uncertain_handoff(
            outbox_id=item.outbox_id,
            attempt_number=attempt_number,
            identity=identity,
            checkpoint=checkpoint,
            observations=observations,
            attempt_source=(
                item.dispatch_source
                if attempt_number
                >= (item.active_attempt or item.attempt_number)
                else None
            ),
            replay_run_id=item.replay_run_id,
        )
        return replace(resolution, observation_count=len(observations))
