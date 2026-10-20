"""Host-owned execution evidence and operational state snapshots.

Recovery identities deliberately exclude plugin versions and configuration.
Committed work cannot be re-evaluated merely by updating a package or config;
explicit re-evaluation requires a separately named replay scope.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from medre.core.contracts.json_values import freeze_json_mapping
from medre.core.contracts.plugins.decisions import (
    Allow,
    Deny,
    Derive,
    DerivedEvent,
    Pass,
    Supersede,
    _validate_stage_outcome,
)
from medre.core.contracts.plugins.instances import (
    PluginInstanceConfig,
    effective_capabilities,
)
from medre.core.contracts.plugins.spec import PluginSpec
from medre.core.contracts.plugins.stages import PluginStage
from medre.core.observability.sanitization import sanitize_error
from medre.registration import valid_registration_name

PLUGIN_RECORD_SCHEMA_VERSION = 1
MAX_PLUGIN_EMISSIONS = 32
MAX_PLUGIN_STATE_BYTES = 65536
MAX_PLUGIN_RECORD_BYTES = 262144
MAX_PLUGIN_DEPTH = 16


def canonical_json(value: object) -> str:
    """Deterministic finite JSON encoding for state, evidence, and digests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_scope(scope: str) -> None:
    if (
        not isinstance(scope, str)
        or scope != scope.strip()
        or len(scope) > 256
        or not scope.isprintable()
        or (
            scope != "live"
            and not (
                scope.startswith("replay:")
                and scope[7:]
                and scope[7:] == scope[7:].strip()
            )
        )
    ):
        raise ValueError("plugin scope must be 'live' or 'replay:<run-id>'")


def configuration_digest(spec: PluginSpec, instance: PluginInstanceConfig) -> str:
    """Digest effective declared configuration without persisting its plaintext.

    The host must supply validated effective config, including model defaults
    and environment overrides, before using this digest for execution.
    """
    capabilities = effective_capabilities(spec, instance)
    data = {
        "plugin_id": instance.plugin_id,
        "instance_id": instance.instance_id,
        "enabled": instance.enabled,
        "config": instance.config,
        "grants": sorted(cap.value for cap in capabilities),
        "stages": sorted(stage.value for stage in spec.stages),
        "input_kinds": sorted(spec.input_kinds),
        "output_kinds": sorted(spec.output_kinds),
    }
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PluginExecutionKey:
    """Logical invocation identity, independent of attempts and package changes."""

    source_event_id: str
    plugin_id: str
    instance_id: str
    stage: PluginStage
    scope: str = "live"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_event_id, str)
            or not self.source_event_id.strip()
        ):
            raise ValueError("source event identity must be non-empty")
        if not valid_registration_name(self.plugin_id) or not valid_registration_name(
            self.instance_id
        ):
            raise ValueError(
                "plugin and instance identities must be registration tokens"
            )
        if not isinstance(self.stage, PluginStage):
            raise TypeError("stage must be a PluginStage")
        validate_scope(self.scope)

    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.source_event_id,
            self.plugin_id,
            self.instance_id,
            self.stage.value,
            self.scope,
        )

    @property
    def idempotency_key(self) -> str:
        """Stable invocation identity for plugin-owned idempotent external calls."""
        return str(
            uuid5(
                NAMESPACE_URL,
                "medre:plugin:invocation:" + canonical_json(self.identity()),
            )
        )

    def derived_event_id(self, ordinal: int) -> str:
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise ValueError("proposal ordinal must be a non-negative integer")
        return str(
            uuid5(
                NAMESPACE_URL,
                "medre:plugin:derived:" + canonical_json((*self.identity(), ordinal)),
            )
        )


class PluginFailure(StrEnum):
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    CONTRACT = "contract_violation"
    CAPABILITY = "capability_violation"
    QUARANTINED = "quarantined"


PluginDecision = Pass | Derive | Supersede | Allow | Deny


def decision_data(decision: PluginDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    if isinstance(decision, (Pass, Allow)):
        return {
            "kind": "pass" if isinstance(decision, Pass) else "allow",
            "note": decision.note,
        }
    if isinstance(decision, Deny):
        return {"kind": "deny", "reason": decision.reason}
    proposals = (
        decision.events if isinstance(decision, Derive) else (decision.replacement,)
    )
    return {
        "kind": "derive" if isinstance(decision, Derive) else "supersede",
        "events": [
            {"kind": proposal.kind, "payload": proposal.payload}
            for proposal in proposals
        ],
    }


def _decision_from_data(data: dict[str, Any] | None) -> PluginDecision | None:
    if data is None:
        return None
    kind = data["kind"]
    if kind == "pass":
        return Pass(note=data["note"])
    if kind == "allow":
        return Allow(note=data["note"])
    if kind == "deny":
        return Deny(reason=data["reason"])
    if kind not in {"derive", "supersede"}:
        raise ValueError("unknown plugin decision kind")
    proposals = tuple(DerivedEvent(**entry) for entry in data["events"])
    if kind == "derive":
        return Derive(proposals)
    if len(proposals) != 1:
        raise ValueError("supersede requires exactly one proposal")
    return Supersede(proposals[0])


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginExecutionRecord:
    """One immutable host decision, including recoverable invocation failures.

    A quarantined disposition records a host skip; it does not claim an
    implementation was invoked.

    An enrichment failure may carry Pass to record an explicit fail-open
    disposition. A transform/policy failure cannot invent an output/verdict.
    Transient failures without a disposition may be retried as a later attempt.
    """

    key: PluginExecutionKey
    schema_version: int = PLUGIN_RECORD_SCHEMA_VERSION
    attempt: int
    plugin_version: str
    config_digest: str
    plan_digest: str
    recorded_at: datetime
    state_revision: int = 0
    state_digest: str | None = None
    decision: PluginDecision | None = None
    failure: PluginFailure | None = None
    failure_reason: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PLUGIN_RECORD_SCHEMA_VERSION
        ):
            raise ValueError("unsupported plugin record schema version")
        if not isinstance(self.key, PluginExecutionKey):
            raise TypeError("key must be a PluginExecutionKey")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ValueError("attempt must be a positive integer")
        if not isinstance(self.plugin_version, str) or not self.plugin_version.strip():
            raise ValueError("plugin version must be non-empty")
        if not isinstance(self.config_digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.config_digest
        ):
            raise ValueError("config digest must be a SHA-256 hex digest")
        if not isinstance(self.plan_digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.plan_digest
        ):
            raise ValueError("plan digest must be a SHA-256 hex digest")
        if (
            not isinstance(self.recorded_at, datetime)
            or self.recorded_at.utcoffset() is None
        ):
            raise ValueError("recorded_at must be timezone-aware")
        if (
            isinstance(self.state_revision, bool)
            or not isinstance(self.state_revision, int)
            or self.state_revision < 0
        ):
            raise ValueError("state_revision must be a non-negative integer")
        if self.state_digest is not None and (
            not isinstance(self.state_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", self.state_digest)
        ):
            raise ValueError("state digest must be a SHA-256 hex digest")
        if self.failure is not None and self.state_digest is not None:
            raise ValueError("failed executions cannot update plugin state")
        if self.decision is not None:
            _validate_stage_outcome(self.key.stage, self.decision)
        if not isinstance(self.failure_reason, str):
            raise TypeError("failure reason must be a string")
        if self.failure is None:
            if self.decision is None or self.failure_reason:
                raise ValueError(
                    "successful executions require a decision and no failure reason"
                )
        else:
            if not isinstance(self.failure, PluginFailure):
                raise TypeError("failure must be a PluginFailure")
            if (
                not isinstance(self.failure_reason, str)
                or not self.failure_reason.strip()
            ):
                raise ValueError("failure requires a non-empty reason")
            if self.decision is not None and not (
                self.key.stage is PluginStage.ENRICH and type(self.decision) is Pass
            ):
                raise ValueError(
                    "failed executions cannot invent a decision outside enrichment fail-open"
                )
            object.__setattr__(
                self, "failure_reason", sanitize_error(self.failure_reason)
            )
        if len(self.proposals) > MAX_PLUGIN_EMISSIONS:
            raise ValueError("plugin execution exceeds the proposal limit")
        if len(canonical_json(self.to_data()).encode()) > MAX_PLUGIN_RECORD_BYTES:
            raise ValueError("plugin execution exceeds the record size limit")

    @property
    def proposals(self) -> tuple[DerivedEvent, ...]:
        if isinstance(self.decision, Derive):
            return self.decision.events
        if isinstance(self.decision, Supersede):
            return (self.decision.replacement,)
        return ()

    @property
    def produced_event_ids(self) -> tuple[str, ...]:
        return tuple(
            self.key.derived_event_id(index) for index in range(len(self.proposals))
        )

    @property
    def record_id(self) -> str:
        return str(
            uuid5(
                NAMESPACE_URL,
                "medre:plugin:execution:"
                + canonical_json((*self.key.identity(), self.attempt)),
            )
        )

    @property
    def settled(self) -> bool:
        return self.decision is not None or self.quarantines_instance

    @property
    def quarantines_instance(self) -> bool:
        return self.failure in {
            PluginFailure.CONTRACT,
            PluginFailure.CAPABILITY,
            PluginFailure.QUARANTINED,
        }

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_event_id": self.key.source_event_id,
            "plugin_id": self.key.plugin_id,
            "instance_id": self.key.instance_id,
            "stage": self.key.stage.value,
            "scope": self.key.scope,
            "attempt": self.attempt,
            "plugin_version": self.plugin_version,
            "config_digest": self.config_digest,
            "plan_digest": self.plan_digest,
            "recorded_at": self.recorded_at.isoformat(),
            "state_revision": self.state_revision,
            "state_digest": self.state_digest,
            "decision": decision_data(self.decision),
            "failure": self.failure.value if self.failure else None,
            "failure_reason": self.failure_reason,
            "produced_event_ids": self.produced_event_ids,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PluginExecutionRecord:
        record = cls(
            schema_version=data["schema_version"],
            key=PluginExecutionKey(
                data["source_event_id"],
                data["plugin_id"],
                data["instance_id"],
                PluginStage(data["stage"]),
                data["scope"],
            ),
            attempt=data["attempt"],
            plugin_version=data["plugin_version"],
            config_digest=data["config_digest"],
            plan_digest=data["plan_digest"],
            state_revision=data["state_revision"],
            state_digest=data["state_digest"],
            recorded_at=datetime.fromisoformat(data["recorded_at"]),
            decision=_decision_from_data(data["decision"]),
            failure=(
                PluginFailure(data["failure"]) if data["failure"] is not None else None
            ),
            failure_reason=data["failure_reason"],
        )
        # Unknown fields and altered deterministic identities are corruption,
        # not forward-compatible evidence to silently reinterpret.
        if canonical_json(record.to_data()) != canonical_json(data):
            raise ValueError(
                "plugin execution record does not match the closed record contract"
            )
        return record


@dataclass(frozen=True, slots=True)
class PluginStateSnapshot:
    revision: int
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError("state revision must be a non-negative integer")
        values = freeze_json_mapping(self.values, label="plugin state")
        if len(canonical_json(values).encode()) > MAX_PLUGIN_STATE_BYTES:
            raise ValueError("plugin state exceeds the size limit")
        object.__setattr__(self, "values", values)


def state_digest(values: Mapping[str, Any]) -> str:
    """Fingerprint a buffered bounded state write for immutable execution evidence."""
    snapshot = PluginStateSnapshot(0, values)
    return hashlib.sha256(canonical_json(snapshot.values).encode()).hexdigest()
