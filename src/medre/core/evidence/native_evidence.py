"""Typed native-evidence observation taxonomy and proof contract.

Every native-evidence observation projects into one scoped, immutable
envelope.  The envelope binds a complete delivery identity and the exact
outbox attempt the evidence is about to a bounded provider claim, so
consumers can reason about native evidence without re-deriving lineage
from receipt timing or provider payloads.

Design constraints
------------------
* **Pure and in-process** — no storage writes, no adapter requirements,
  no mutating endpoints, no clocks (``observed_at`` is a required
  parameter), and no serialized JSON projection.
* **Wraps, never replaces** — the envelope projects existing
  :class:`~medre.core.contracts.delivery.PostHandoffObservation`,
  :class:`~medre.core.contracts.delivery.DeferredHandoffCompleted`, and
  :class:`~medre.core.contracts.delivery.DeferredHandoffFailed` facts.
  Their semantics are unchanged; nothing here competes with the delivery
  feedback pipeline or the delivery-authority resolver.
* **Core ledger authority for projections** — the attempt identity
  (event, plan, adapter, channel, outbox, attempt number) comes from the
  core-issued :class:`~medre.core.events.delivery.DeliveryAttemptProvenance`
  of the wrapped fact.  Direct envelope construction and explicit
  ``not_observed`` records instead consume caller-supplied identity fields;
  neither path proves ledger binding.  A provider fact is authoritative
  only for the provider-specific claim it can actually establish, and
  independent recipient corroboration never changes a core generation.
* **Identifier fields** — projections omit error text and metadata and
  carry only the selected receipt/native-reference identifier fields.
  Identifier values are copied as supplied, not redacted or authenticated;
  callers remain responsible for keeping content and credentials out of them.
* **No execution authorization** — nothing in this module grants or
  records permission to send.  ``unresolved`` never becomes permission to
  resend, and absence of evidence never proves rejection: the explicit
  ``not_observed`` result folds into ``unresolved`` for remediation
  purposes while staying distinct as an observation fact.

Public symbols
--------------
* :class:`NativeEvidenceObservation` — the frozen envelope.
* :func:`native_evidence_from_post_handoff` — project one post-handoff
  transport observation.
* :func:`native_evidence_from_deferred_completion` — project one
  completed deferred hand-off.
* :func:`native_evidence_from_deferred_failure` — project one deferred
  hand-off that terminated before transport hand-off.
* :func:`native_evidence_not_observed` — record that no provider fact is
  available to the caller for one stated attempt identity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, get_args

from medre.core.contracts.delivery import (
    DeferredHandoffCompleted,
    DeferredHandoffFailed,
    PostHandoffObservation,
)
from medre.core.delivery_authority import DeliveryIdentity
from medre.core.events.delivery import DeliveryAttemptProvenance

__all__ = [
    "NATIVE_EVIDENCE_RESULT_VALUES",
    "NATIVE_EVIDENCE_SCOPE_VALUES",
    "NATIVE_EVIDENCE_TIER_VALUES",
    "NativeEvidenceObservation",
    "NativeEvidenceResult",
    "NativeEvidenceScope",
    "NativeEvidenceTier",
    "native_evidence_from_deferred_completion",
    "native_evidence_from_deferred_failure",
    "native_evidence_from_post_handoff",
    "native_evidence_not_observed",
]


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

NativeEvidenceResult = Literal[
    "accepted",
    "definitively_not_submitted",
    "unresolved",
    "not_observed",
]
"""Bounded result of one native-evidence observation.

``accepted`` means the provider-specific claim that the send stood as a
provider submission was established.  ``definitively_not_submitted``
means the observation establishes the send never became a standing
provider submission.  ``unresolved`` means the observation does not
establish either; it never becomes permission to resend.  ``not_observed``
records that the constructing caller has no provider observation; it stays
distinct as a fact and resolves to ``unresolved`` (absence of evidence
never proves rejection).
"""

NATIVE_EVIDENCE_RESULT_VALUES: frozenset[str] = frozenset(
    get_args(NativeEvidenceResult)
)

NativeEvidenceTier = Literal[
    "fake_pipeline",
    "sdk_contract",
    "local_integration",
    "docker",
    "live",
    "hardware",
]
"""Observation tier, reusing the campaign's established tier vocabulary.

The values and their meaning follow the tier map in
``docs/dev/native-evidence-campaign.md`` and the transport realism ladder
in ``docs/spec/appendices/transport-realism.md``.  A lower tier never
stands in for a higher one.
"""

NATIVE_EVIDENCE_TIER_VALUES: frozenset[str] = frozenset(get_args(NativeEvidenceTier))

NativeEvidenceScope = Literal[
    "native_handoff",
    "provider_acceptance",
    "recipient_observation",
]
"""Closed scope of the claim family an observation belongs to.

The scope names the claim family, not its polarity: an observation of a
provider refusal is still ``provider_acceptance``-scoped evidence whose
result is negative.  ``native_handoff`` covers facts about the transport
hand-off boundary itself; ``recipient_observation`` covers facts
established by an independent recipient side.
"""

NATIVE_EVIDENCE_SCOPE_VALUES: frozenset[str] = frozenset(get_args(NativeEvidenceScope))

#: Scopes that can legitimately carry an ``accepted`` result.  A
#: hand-off-boundary fact alone never establishes acceptance.
_ACCEPTED_SCOPES: frozenset[str] = frozenset(
    {"provider_acceptance", "recipient_observation"}
)

#: Scopes that can legitimately carry a ``definitively_not_submitted``
#: result.  A recipient-side fact cannot establish non-submission.
_DEFINITIVELY_NOT_SUBMITTED_SCOPES: frozenset[str] = frozenset(
    {"provider_acceptance", "native_handoff"}
)

#: The synthetic pipeline and SDK-contract tiers cannot establish a
#: provider-side outcome or an independently observed recipient outcome.
#: A confirmed pre-handoff failure may still describe the core seam at
#: these tiers; it is not a provider-side negative observation.
_TIERS_WITHOUT_NATIVE_ACCEPTANCE_PROOF: frozenset[str] = frozenset(
    {"fake_pipeline", "sdk_contract"}
)

#: Domain tag inside the fingerprint statement so a durable fingerprint
#: is never confused with a fingerprint from another vocabulary.
_FINGERPRINT_DOMAIN = "medre.native_evidence_observation.v1"

#: Result projection for post-handoff transport observation states.  The
#: mapping is deliberately conservative: ``rejected``, ``failed`` and
#: ``cancelled`` are all post-handoff outcomes and cannot establish that
#: the provider never accepted the submission.
_POST_HANDOFF_RESULTS: dict[str, NativeEvidenceResult] = {
    "delivered": "accepted",
    "rejected": "unresolved",
    "failed": "unresolved",
    "cancelled": "unresolved",
}


# ---------------------------------------------------------------------------
# Validation helpers (fail closed)
# ---------------------------------------------------------------------------


def _require_str(
    value: object,
    *,
    field_name: str,
    allow_none: bool = False,
    forbid_whitespace: bool = False,
) -> None:
    """Fail closed unless *value* is an acceptable string field value."""
    if value is None and allow_none:
        return
    if not isinstance(value, str):
        raise TypeError(
            f"{field_name} must be a string"
            + (" or None" if allow_none else "")
            + f"; got {type(value).__name__}"
        )
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if forbid_whitespace and any(char.isspace() for char in value):
        raise ValueError(f"{field_name} must not contain whitespace")


def _validate_closed_token(
    value: object,
    *,
    allowed: frozenset[str],
    field_name: str,
) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(
            f"unknown {field_name} {value!r}; expected one of {sorted(allowed)}"
        )


def _validate_observed_at(value: object) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"observed_at must be a datetime; got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "observed_at must be timezone-aware; a naive timestamp is not "
            "an observation fact"
        )


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def _fingerprint_statement(
    *,
    event_id: str,
    delivery_plan_id: str,
    target_adapter: str,
    target_channel: str | None,
    outbox_id: str,
    attempt_number: int,
    observed_row_updated_at: str | None,
    provider: str,
    provider_repository: str | None,
    compatibility_pin: str,
    observation_source: str,
    tier: str,
    scope: str,
    result: str,
    receipt_id: str | None,
    native_message_id: str | None,
    evidence_reference: str | None,
) -> dict[str, object]:
    """Build the canonical fact statement a fingerprint is computed over.

    The observation timestamp is deliberately excluded: the fingerprint
    identifies the observation content, so the same provider fact recorded
    twice — including across a persistence/restart boundary — yields the
    same fingerprint and duplicates stay detectable.
    """
    return {
        "fingerprint_domain": _FINGERPRINT_DOMAIN,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "outbox_id": outbox_id,
        "attempt_number": attempt_number,
        "observed_row_updated_at": observed_row_updated_at,
        "provider": provider,
        "provider_repository": provider_repository,
        "compatibility_pin": compatibility_pin,
        "observation_source": observation_source,
        "tier": tier,
        "scope": scope,
        "result": result,
        "receipt_id": receipt_id,
        "native_message_id": native_message_id,
        "evidence_reference": evidence_reference,
    }


def _fingerprint(statement: dict[str, object]) -> str:
    """Hash one fact statement into a stable 64-hex fingerprint."""
    digest = hashlib.sha256(
        json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("derived fingerprint is not 64 lowercase hex characters")
    return digest


@dataclass(frozen=True, slots=True)
class NativeEvidenceObservation:
    """One scoped native-evidence observation.

    The envelope binds a stated attempt identity to a bounded provider
    claim.  Identity, outbox, and generation fields are consumed as stated
    by the caller; the projection functions take them
    exclusively from the core-issued provenance of the wrapped fact, so a
    provider payload can never override those fields independently. Field
    validation is structural: it does not query the ledger, authenticate the
    provenance issuer, verify a compatibility pin, or attest the stated tier.

    The envelope is a record, not a permit: it never authorizes a send,
    a resend, or any lifecycle transition, and it carries no field that
    could be read as execution authorization.
    """

    # Complete attempt identity; projections take this from core provenance,
    # while direct and absence-record construction consumes caller fields.
    event_id: str
    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None
    # Exact outbox attempt the evidence is about.
    outbox_id: str
    attempt_number: int
    observed_row_updated_at: str | None
    # Provider identity, provenance, and the compatibility pin in force.
    provider: str
    provider_repository: str | None
    compatibility_pin: str
    # Observation context.
    observation_source: str
    tier: NativeEvidenceTier
    scope: NativeEvidenceScope
    observed_at: datetime
    # Bounded result and identifier-only attachments.
    result: NativeEvidenceResult
    receipt_id: str | None = None
    native_message_id: str | None = None
    # Grounding: a persisted evidence reference, or the durable
    # fingerprint derived below.
    evidence_reference: str | None = None
    # Derived values.
    identity: DeliveryIdentity = field(init=False, repr=False, compare=False)
    evidence_fingerprint: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("event_id", "delivery_plan_id", "target_adapter"):
            _require_str(getattr(self, name), field_name=name)
        if self.target_channel is not None and not isinstance(self.target_channel, str):
            raise TypeError(
                "target_channel must be a string or None; got "
                f"{type(self.target_channel).__name__}"
            )
        if self.target_channel is not None and not self.target_channel.strip():
            raise ValueError(
                "target_channel must be a nonblank channel string, or None "
                "for the deliberate no-channel identity; a blank string "
                "is not a channel decision"
            )
        _require_str(self.outbox_id, field_name="outbox_id")
        if type(self.attempt_number) is not int or self.attempt_number < 0:
            raise ValueError(
                "attempt_number must be the raw persisted attempt counter "
                "(integer >= 0, including zero for never-dispatched work)"
            )
        _require_str(
            self.observed_row_updated_at,
            field_name="observed_row_updated_at",
            allow_none=True,
        )
        _require_str(self.provider, field_name="provider", forbid_whitespace=True)
        _require_str(
            self.provider_repository,
            field_name="provider_repository",
            allow_none=True,
        )
        _require_str(
            self.compatibility_pin,
            field_name="compatibility_pin",
            forbid_whitespace=True,
        )
        _require_str(self.observation_source, field_name="observation_source")
        _validate_closed_token(
            self.tier, allowed=NATIVE_EVIDENCE_TIER_VALUES, field_name="tier"
        )
        _validate_closed_token(
            self.scope, allowed=NATIVE_EVIDENCE_SCOPE_VALUES, field_name="scope"
        )
        _validate_closed_token(
            self.result,
            allowed=NATIVE_EVIDENCE_RESULT_VALUES,
            field_name="result",
        )
        _validate_observed_at(self.observed_at)
        for name in ("receipt_id", "native_message_id", "evidence_reference"):
            _require_str(getattr(self, name), field_name=name, allow_none=True)

        if self.result == "not_observed":
            for name in ("receipt_id", "native_message_id", "evidence_reference"):
                if getattr(self, name) is not None:
                    raise ValueError(
                        "result='not_observed' records the absence of a "
                        f"provider observation and cannot carry {name}"
                    )
        if self.result == "accepted" and self.scope not in _ACCEPTED_SCOPES:
            raise ValueError(
                "result='accepted' requires a provider_acceptance or "
                f"recipient_observation scope; got {self.scope!r}: a "
                "hand-off-boundary fact alone never establishes acceptance"
            )
        if (
            self.result == "accepted"
            and self.tier in _TIERS_WITHOUT_NATIVE_ACCEPTANCE_PROOF
        ):
            raise ValueError(
                f"result='accepted' cannot be established at tier {self.tier!r}: "
                "synthetic and SDK-contract facts cannot prove provider "
                "acceptance or an independent recipient observation"
            )
        if (
            self.result == "definitively_not_submitted"
            and self.scope not in _DEFINITIVELY_NOT_SUBMITTED_SCOPES
        ):
            raise ValueError(
                "result='definitively_not_submitted' requires a "
                "provider_acceptance or native_handoff scope; got "
                f"{self.scope!r}: a recipient-side fact cannot establish "
                "non-submission"
            )
        if (
            self.result == "definitively_not_submitted"
            and self.scope == "provider_acceptance"
            and self.tier in _TIERS_WITHOUT_NATIVE_ACCEPTANCE_PROOF
        ):
            raise ValueError(
                "result='definitively_not_submitted' at "
                "provider_acceptance scope cannot be established at tier "
                f"{self.tier!r}: a lower tier does not prove a provider refusal"
            )

        object.__setattr__(
            self,
            "identity",
            DeliveryIdentity(
                self.event_id,
                self.delivery_plan_id,
                self.target_adapter,
                self.target_channel,
            ),
        )
        object.__setattr__(
            self,
            "evidence_fingerprint",
            _fingerprint(
                _fingerprint_statement(
                    event_id=self.event_id,
                    delivery_plan_id=self.delivery_plan_id,
                    target_adapter=self.target_adapter,
                    target_channel=self.identity.target_channel,
                    outbox_id=self.outbox_id,
                    attempt_number=self.attempt_number,
                    observed_row_updated_at=self.observed_row_updated_at,
                    provider=self.provider,
                    provider_repository=self.provider_repository,
                    compatibility_pin=self.compatibility_pin,
                    observation_source=self.observation_source,
                    tier=self.tier,
                    scope=self.scope,
                    result=self.result,
                    receipt_id=self.receipt_id,
                    native_message_id=self.native_message_id,
                    evidence_reference=self.evidence_reference,
                )
            ),
        )

    @property
    def resolution(
        self,
    ) -> Literal["accepted", "definitively_not_submitted", "unresolved"]:
        """Fold the observation into the three-token remediation vocabulary.

        ``not_observed`` resolves to ``unresolved``: the absence of a
        provider observation never proves rejection, so remediation must
        treat an unobserved attempt exactly like any other unresolved
        one.  The raw :attr:`result` keeps the distinction.
        """
        result = self.result
        if result == "not_observed":
            return "unresolved"
        return result

    def is_stale_for_generation(self, current_attempt_number: int) -> bool:
        """Report whether the observation predates *current_attempt_number*.

        The comparison uses the raw generation the evidence is bound to;
        staleness is an observation for remediation reasoning, never a
        permission, and it never changes the envelope's result.
        """
        if type(current_attempt_number) is not int or current_attempt_number < 0:
            raise ValueError(
                "current_attempt_number must be a raw persisted attempt "
                "counter (integer >= 0)"
            )
        return self.attempt_number < current_attempt_number

    def shares_provider_reference(self, other: NativeEvidenceObservation) -> bool:
        """Report whether two observations cite the same provider-native id.

        Provider identifiers are scoped to the observation that reported
        them and are never treated as globally unique.  The same native
        message identifier observed under a different delivery identity is
        a correlation-reuse signal, not the same observation; the two
        envelopes remain distinct and their fingerprints differ. This helper
        compares the provider label and native identifier only; it does not
        establish equality across accounts, profiles, repositories, or pins.
        """
        if not isinstance(other, NativeEvidenceObservation):
            raise TypeError(
                "other must be a NativeEvidenceObservation; got "
                f"{type(other).__name__}"
            )
        if self.provider != other.provider:
            return False
        return (
            self.native_message_id is not None
            and other.native_message_id is not None
            and self.native_message_id == other.native_message_id
        )


# ---------------------------------------------------------------------------
# Pure projection constructors
# ---------------------------------------------------------------------------


def _project_from_provenance(
    provenance: DeliveryAttemptProvenance,
    *,
    scope: NativeEvidenceScope,
    result: NativeEvidenceResult,
    native_message_id: str | None,
    tier: NativeEvidenceTier,
    provider: str,
    provider_repository: str | None,
    compatibility_pin: str,
    observation_source: str,
    observed_at: datetime,
    observed_row_updated_at: str | None,
    evidence_reference: str | None,
    receipt_id: str | None,
) -> NativeEvidenceObservation:
    """Build the envelope from a core-issued attempt provenance.

    Identity components, outbox, and generation come exclusively from the
    wrapped fact's core-issued provenance; callers cannot override them,
    so a provider payload can never re-state the attempt identity.
    """
    if not isinstance(provenance, DeliveryAttemptProvenance):
        raise TypeError(
            "the wrapped fact must carry a DeliveryAttemptProvenance; got "
            f"{type(provenance).__name__}"
        )
    if not isinstance(provenance.outbox_id, str) or not provenance.outbox_id:
        raise ValueError(
            "the wrapped fact must carry a core-issued outbox_id; got "
            f"{provenance.outbox_id!r}"
        )
    if type(provenance.attempt_number) is not int or provenance.attempt_number < 1:
        raise ValueError(
            "the wrapped fact must carry a core-issued attempt_number >= 1; "
            f"got {provenance.attempt_number!r}"
        )
    return NativeEvidenceObservation(
        event_id=provenance.event_id,
        delivery_plan_id=provenance.delivery_plan_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        outbox_id=provenance.outbox_id,
        attempt_number=provenance.attempt_number,
        observed_row_updated_at=observed_row_updated_at,
        provider=provider,
        provider_repository=provider_repository,
        compatibility_pin=compatibility_pin,
        observation_source=observation_source,
        tier=tier,
        scope=scope,
        observed_at=observed_at,
        result=result,
        receipt_id=receipt_id,
        native_message_id=native_message_id,
        evidence_reference=evidence_reference,
    )


def native_evidence_from_post_handoff(
    observation: PostHandoffObservation,
    *,
    tier: NativeEvidenceTier,
    provider: str,
    compatibility_pin: str,
    observation_source: str,
    observed_at: datetime,
    provider_repository: str | None = None,
    observed_row_updated_at: str | None = None,
    evidence_reference: str | None = None,
    receipt_id: str | None = None,
) -> NativeEvidenceObservation:
    """Project one post-handoff transport observation into the envelope.

    The post-handoff fact is provider-side transport evidence, so the
    scope is ``provider_acceptance``.  The result mapping produces the
    typed result without inventing facts: ``delivered`` establishes
    acceptance at a qualifying evidence tier.  A subsequent ``rejected``,
    ``failed`` or ``cancelled`` state cannot prove that the already-handed-off
    submission was never accepted, so all three remain ``unresolved``.
    Only the native message
    identifier is carried; the observation's error text and metadata are
    never projected.
    """
    if not isinstance(observation, PostHandoffObservation):
        raise TypeError(
            "observation must be a PostHandoffObservation; got "
            f"{type(observation).__name__}"
        )
    result = _POST_HANDOFF_RESULTS.get(observation.state)
    if result is None:
        raise ValueError(
            f"unknown post-handoff observation state {observation.state!r}"
        )
    return _project_from_provenance(
        observation.attempt_provenance,
        scope="provider_acceptance",
        result=result,
        native_message_id=observation.native_message_id,
        tier=tier,
        provider=provider,
        provider_repository=provider_repository,
        compatibility_pin=compatibility_pin,
        observation_source=observation_source,
        observed_at=observed_at,
        observed_row_updated_at=observed_row_updated_at,
        evidence_reference=evidence_reference,
        receipt_id=receipt_id,
    )


def native_evidence_from_deferred_completion(
    observation: DeferredHandoffCompleted,
    *,
    tier: NativeEvidenceTier,
    provider: str,
    compatibility_pin: str,
    observation_source: str,
    observed_at: datetime,
    provider_repository: str | None = None,
    observed_row_updated_at: str | None = None,
    evidence_reference: str | None = None,
    receipt_id: str | None = None,
) -> NativeEvidenceObservation:
    """Project one completed deferred hand-off into the envelope.

    The fact establishes that the attempt reached the transport hand-off
    boundary, so the scope is ``native_handoff`` and the result stays
    ``unresolved``: reaching the boundary — including under the strongest
    adapter-reported confirmation level — is not, by itself, an
    independently established provider acceptance.  The hand-off's native
    message identifier is carried when present.
    """
    if not isinstance(observation, DeferredHandoffCompleted):
        raise TypeError(
            "observation must be a DeferredHandoffCompleted; got "
            f"{type(observation).__name__}"
        )
    return _project_from_provenance(
        observation.attempt_provenance,
        scope="native_handoff",
        result="unresolved",
        native_message_id=observation.handoff.native_message_id,
        tier=tier,
        provider=provider,
        provider_repository=provider_repository,
        compatibility_pin=compatibility_pin,
        observation_source=observation_source,
        observed_at=observed_at,
        observed_row_updated_at=observed_row_updated_at,
        evidence_reference=evidence_reference,
        receipt_id=receipt_id,
    )


def native_evidence_from_deferred_failure(
    observation: DeferredHandoffFailed,
    *,
    tier: NativeEvidenceTier,
    provider: str,
    compatibility_pin: str,
    observation_source: str,
    observed_at: datetime,
    provider_repository: str | None = None,
    observed_row_updated_at: str | None = None,
    evidence_reference: str | None = None,
    receipt_id: str | None = None,
) -> NativeEvidenceObservation:
    """Project one failed deferred hand-off into the envelope.

    The wrapped fact's own contract establishes that the attempt
    terminated before transport hand-off for every outcome, so nothing
    was submitted and the result is ``definitively_not_submitted`` at
    ``native_handoff`` scope.  No native reference exists to carry.
    """
    if not isinstance(observation, DeferredHandoffFailed):
        raise TypeError(
            "observation must be a DeferredHandoffFailed; got "
            f"{type(observation).__name__}"
        )
    return _project_from_provenance(
        observation.attempt_provenance,
        scope="native_handoff",
        result="definitively_not_submitted",
        native_message_id=None,
        tier=tier,
        provider=provider,
        provider_repository=provider_repository,
        compatibility_pin=compatibility_pin,
        observation_source=observation_source,
        observed_at=observed_at,
        observed_row_updated_at=observed_row_updated_at,
        evidence_reference=evidence_reference,
        receipt_id=receipt_id,
    )


def native_evidence_not_observed(
    *,
    event_id: str,
    delivery_plan_id: str,
    target_adapter: str,
    target_channel: str | None,
    outbox_id: str,
    attempt_number: int,
    observed_row_updated_at: str | None,
    provider: str,
    compatibility_pin: str,
    observation_source: str,
    tier: NativeEvidenceTier,
    scope: NativeEvidenceScope,
    observed_at: datetime,
    provider_repository: str | None = None,
) -> NativeEvidenceObservation:
    """Record the explicit absence of a provider observation.

    The ``not_observed`` result records the absence of a provider fact
    available to the caller, not a provider-wide negative search.  It
    grounds itself in the fingerprint over that statement and carries
    no identifiers, because none were observed.  It resolves to
    ``unresolved`` for remediation purposes and never proves rejection.
    """
    return NativeEvidenceObservation(
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        outbox_id=outbox_id,
        attempt_number=attempt_number,
        observed_row_updated_at=observed_row_updated_at,
        provider=provider,
        provider_repository=provider_repository,
        compatibility_pin=compatibility_pin,
        observation_source=observation_source,
        tier=tier,
        scope=scope,
        observed_at=observed_at,
        result="not_observed",
        receipt_id=None,
        native_message_id=None,
        evidence_reference=None,
    )
