"""Closed adapter-to-core outbound delivery contracts.

This module owns the process-local boundary between transport adapters and the
core delivery engine.  Adapters report facts; core remains authoritative for
receipt, outbox, retry, and observation lifecycle semantics.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, TypeAlias, get_args

import msgspec
from msgspec.structs import force_setattr

from medre.core.events.delivery import (
    DELIVERY_CONFIRMATION_LEVEL_VALUES,
    DELIVERY_OBSERVATION_STATE_VALUES,
    DeliveryAttemptProvenance,
    DeliveryConfirmationLevel,
    DeliveryObservationState,
)
from medre.core.events.metadata import FrozenDict

AdapterHandoffDisposition = Literal["transport_handoff", "deferred"]
"""How far a successful synchronous ``deliver()`` call progressed."""

ADAPTER_HANDOFF_DISPOSITION_VALUES: frozenset[str] = frozenset(
    get_args(AdapterHandoffDisposition)
)

DeferredFailureOutcome = Literal[
    "exhausted",
    "permanent_failed",
    "cancelled",
    "abandoned",
]
"""Terminal result of work accepted for deferred hand-off."""

DEFERRED_FAILURE_OUTCOME_VALUES: frozenset[str] = frozenset(
    get_args(DeferredFailureOutcome)
)


def _freeze_json_safe_metadata(
    metadata: Mapping[str, object],
    *,
    owner: str,
) -> FrozenDict:
    """Validate and deeply freeze JSON-safe adapter metadata."""

    def _to_builtins(value: object) -> object:
        if isinstance(value, MappingProxyType):
            value = dict(value)
        if isinstance(value, Mapping):
            normalized_mapping: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(
                        f"{owner}.metadata keys must be strings; got {type(key).__name__}"
                    )
                normalized_mapping[key] = _to_builtins(item)
            return normalized_mapping
        if isinstance(value, (list, tuple)):
            return [_to_builtins(item) for item in value]
        return value

    normalized = _to_builtins(metadata)
    if not isinstance(normalized, dict):
        raise TypeError(f"{owner}.metadata must be a mapping")
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{owner}.metadata must contain only JSON-safe values") from exc
    return FrozenDict(normalized)


def _validate_literal_string(
    value: object,
    *,
    allowed: frozenset[str],
    field_name: str,
) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(
            f"unknown {field_name} {value!r}; expected one of {sorted(allowed)}"
        )
    return value


def _validate_optional_native_id(value: str | None, *, field_name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{field_name} must be a non-empty string or None")


class AdapterHandoffResult(msgspec.Struct, frozen=True, kw_only=True):
    """Fact returned by an adapter after a successful synchronous hand-off.

    ``transport_handoff`` means the adapter reached its external transport
    boundary during ``deliver()``.  ``deferred`` means the adapter accepted
    work locally and will report the later transport result through
    :class:`DeliveryFeedback`.

    This type deliberately does not claim recipient delivery.  Evidence
    strength is expressed independently by ``confirmation_level``.
    """

    disposition: AdapterHandoffDisposition = "transport_handoff"
    native_message_id: str | None = None
    native_channel_id: str | None = None
    native_thread_id: str | None = None
    native_relation_id: str | None = None
    confirmation_level: DeliveryConfirmationLevel = "unknown"
    note: str = ""
    metadata: dict[str, object] = msgspec.field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_literal_string(
            self.disposition,
            allowed=ADAPTER_HANDOFF_DISPOSITION_VALUES,
            field_name="adapter handoff disposition",
        )
        _validate_literal_string(
            self.confirmation_level,
            allowed=DELIVERY_CONFIRMATION_LEVEL_VALUES,
            field_name="confirmation_level",
        )
        for name in (
            "native_message_id",
            "native_channel_id",
            "native_thread_id",
            "native_relation_id",
        ):
            _validate_optional_native_id(getattr(self, name), field_name=name)
        if not isinstance(self.note, str):
            raise TypeError("AdapterHandoffResult.note must be a string")
        if self.disposition == "deferred" and self.native_message_id is not None:
            raise ValueError(
                "deferred hand-off cannot claim a native_message_id before "
                "transport hand-off completes"
            )
        force_setattr(
            self,
            "metadata",
            _freeze_json_safe_metadata(self.metadata, owner="AdapterHandoffResult"),
        )


class DeferredHandoffCompleted(
    msgspec.Struct,
    frozen=True,
    kw_only=True,
    tag="deferred_handoff_completed",
    tag_field="kind",
):
    """A previously deferred attempt reached its transport hand-off boundary."""

    attempt_provenance: DeliveryAttemptProvenance
    handoff: AdapterHandoffResult

    def __post_init__(self) -> None:
        if self.handoff.disposition != "transport_handoff":
            raise ValueError(
                "DeferredHandoffCompleted.handoff must use "
                "disposition='transport_handoff'"
            )


class DeferredHandoffFailed(
    msgspec.Struct,
    frozen=True,
    kw_only=True,
    tag="deferred_handoff_failed",
    tag_field="kind",
):
    """A previously deferred attempt terminated before transport hand-off."""

    attempt_provenance: DeliveryAttemptProvenance
    outcome: DeferredFailureOutcome
    native_channel_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _validate_literal_string(
            self.outcome,
            allowed=DEFERRED_FAILURE_OUTCOME_VALUES,
            field_name="deferred failure outcome",
        )
        _validate_optional_native_id(
            self.native_channel_id, field_name="native_channel_id"
        )
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("DeferredHandoffFailed.error must be a string or None")


class PostHandoffObservation(
    msgspec.Struct,
    frozen=True,
    kw_only=True,
    tag="post_handoff_observation",
    tag_field="kind",
):
    """Append-only transport evidence emitted after successful hand-off."""

    attempt_provenance: DeliveryAttemptProvenance
    state: DeliveryObservationState
    native_channel_id: str | None = None
    native_message_id: str | None = None
    confirmation_level: DeliveryConfirmationLevel = "unknown"
    error: str | None = None
    metadata: dict[str, object] = msgspec.field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_literal_string(
            self.state,
            allowed=DELIVERY_OBSERVATION_STATE_VALUES,
            field_name="delivery observation state",
        )
        _validate_literal_string(
            self.confirmation_level,
            allowed=DELIVERY_CONFIRMATION_LEVEL_VALUES,
            field_name="confirmation_level",
        )
        _validate_optional_native_id(
            self.native_channel_id, field_name="native_channel_id"
        )
        _validate_optional_native_id(
            self.native_message_id, field_name="native_message_id"
        )
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("PostHandoffObservation.error must be a string or None")
        force_setattr(
            self,
            "metadata",
            _freeze_json_safe_metadata(self.metadata, owner="PostHandoffObservation"),
        )


DeliveryFeedback: TypeAlias = (
    DeferredHandoffCompleted | DeferredHandoffFailed | PostHandoffObservation
)
"""Closed union of asynchronous adapter facts reported back to core."""


__all__ = [
    "ADAPTER_HANDOFF_DISPOSITION_VALUES",
    "DEFERRED_FAILURE_OUTCOME_VALUES",
    "AdapterHandoffDisposition",
    "AdapterHandoffResult",
    "DeferredFailureOutcome",
    "DeferredHandoffCompleted",
    "DeferredHandoffFailed",
    "DeliveryFeedback",
    "PostHandoffObservation",
]
