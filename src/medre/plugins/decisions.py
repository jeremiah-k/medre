"""Typed stage decisions returned by plugin implementations.

Plugins never construct :class:`~medre.core.events.canonical.CanonicalEvent`
values and never return bare lists of events. Each stage receives a typed
decision vocabulary that keeps MEDRE responsible for event identity, lineage,
validation, admission, routing, and delivery:

* ``Pass`` — the original event continues unchanged.
* ``Derive`` — propose additional derived events; the original event also
  continues (additive derivation).
* ``Supersede`` — propose a replacement; the original event remains stored
  and immutable but stops routing downstream (transform stage only).
* ``Allow`` / ``Deny`` — policy verdicts (event-policy stage only). A denial
  is a policy suppression with an explicit reason, distinct from an event
  matching zero routes.

Derived events are *proposals*: MEDRE constructs the canonical event from a
proposal, stamps ``parent_event_id`` and ``lineage``, and admits it through
the same invariants as any other event.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias, get_args

from medre.core.contracts.json_values import freeze_json_mapping
from medre.core.events.kinds import KNOWN_KINDS
from medre.plugins.stages import PluginStage

__all__ = [
    "Allow",
    "Deny",
    "Derive",
    "DerivedEvent",
    "EnrichOutcome",
    "Pass",
    "PLUGIN_OUTPUT_KINDS",
    "PolicyOutcome",
    "Supersede",
    "TransformOutcome",
]


PLUGIN_OUTPUT_KINDS: frozenset[str] = frozenset(
    kind for kind in KNOWN_KINDS if not kind.startswith(("delivery.", "system."))
)
"""Registered content kinds plugins may propose; core evidence stays core-owned."""


@dataclass(frozen=True, slots=True)
class DerivedEvent:
    """Proposed content for one derived canonical event.

    ``kind`` must be a registered event kind permitted for plugin output; plugin-specific events use the
    single ``plugin.custom`` kind with a sub-kind in the payload. MEDRE
    constructs the canonical event from this proposal and owns its identity,
    lineage, timestamps, admission, and routing.
    """

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in PLUGIN_OUTPUT_KINDS:
            raise ValueError(
                f"DerivedEvent.kind must be a registered event kind permitted for plugin output; got {self.kind!r}"
            )
        object.__setattr__(
            self,
            "payload",
            freeze_json_mapping(self.payload, label="DerivedEvent.payload"),
        )


@dataclass(frozen=True, slots=True)
class Pass:
    """The original event continues unchanged; no events are derived."""

    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.note, str):
            raise TypeError("decision note must be a string")


@dataclass(frozen=True, slots=True)
class Derive:
    """Propose additional derived events; the original event also continues."""

    events: tuple[DerivedEvent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        if not self.events:
            raise ValueError("Derive requires at least one event; use Pass() instead")
        for event in self.events:
            if not isinstance(event, DerivedEvent):
                raise TypeError(
                    "Derive.events entries must be DerivedEvent proposals; got "
                    f"{type(event).__name__}"
                )


@dataclass(frozen=True, slots=True)
class Supersede:
    """Replace the original event for downstream routing (transform only).

    The original event remains stored and immutable; routing and delivery see
    the replacement, which MEDRE constructs with lineage back to the original.
    """

    replacement: DerivedEvent

    def __post_init__(self) -> None:
        if not isinstance(self.replacement, DerivedEvent):
            raise TypeError(
                "Supersede.replacement must be a DerivedEvent proposal; got "
                f"{type(self.replacement).__name__}"
            )


@dataclass(frozen=True, slots=True)
class Allow:
    """Policy verdict: the event may continue toward routing."""

    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.note, str):
            raise TypeError("decision note must be a string")


@dataclass(frozen=True, slots=True)
class Deny:
    """Policy verdict: suppress delivery of the event under granted authority.

    A denial is recorded as a policy suppression with its reason and is never
    represented as an ordinary zero-route match.
    """

    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Deny.reason must be a non-empty string")


EnrichOutcome: TypeAlias = Pass | Derive
TransformOutcome: TypeAlias = Pass | Derive | Supersede
PolicyOutcome: TypeAlias = Allow | Deny


def _validate_stage_outcome(stage: PluginStage, decision: object) -> None:
    """Use the public outcome unions as the single stage-result authority."""
    if not isinstance(stage, PluginStage):
        raise TypeError("stage must be a PluginStage")
    outcome = {
        PluginStage.ENRICH: EnrichOutcome,
        PluginStage.TRANSFORM: TransformOutcome,
        PluginStage.EVENT_POLICY: PolicyOutcome,
    }[stage]
    if type(decision) not in get_args(outcome):
        raise TypeError("decision is not permitted for this plugin stage")
