"""Pure host-side checks before a plugin decision may be committed.

Capability declaration is not authorization. Calling this validator cannot
execute a plugin, admit events, or modify state; the execution host must
validate every returned decision before attempting durable admission.
"""

from __future__ import annotations

from medre.core.contracts.plugins.capabilities import PluginCapability
from medre.core.contracts.plugins.decisions import (
    Deny,
    Derive,
    Supersede,
    _validate_stage_outcome,
)
from medre.core.contracts.plugins.instances import (
    PluginInstanceConfig,
    effective_capabilities,
    missing_required_grants,
)
from medre.core.contracts.plugins.spec import PluginSpec
from medre.core.contracts.plugins.stages import PluginStage


class PluginCapabilityError(PermissionError):
    """An instance attempted an operation outside its effective authority."""


def validate_plugin_decision(
    spec: PluginSpec,
    instance: PluginInstanceConfig,
    stage: PluginStage,
    event_kind: str,
    decision: object,
) -> None:
    """Reject invalid stage results, missing grants, and undeclared kind flows."""
    if not isinstance(stage, PluginStage):
        raise TypeError("stage must be a PluginStage")
    capabilities = effective_capabilities(spec, instance)
    if (
        not instance.enabled
        or missing_required_grants(spec, instance)
        or PluginCapability.READ_EVENTS not in capabilities
    ):
        raise PluginCapabilityError(
            "plugin instance is disabled or missing required grants"
        )
    if stage not in spec.stages:
        raise ValueError("plugin instance did not subscribe to this stage")
    _validate_stage_outcome(stage, decision)
    if spec.input_kinds and event_kind not in spec.input_kinds:
        raise ValueError("plugin input kind is outside its declaration")
    if isinstance(decision, (Derive, Supersede)):
        if PluginCapability.EMIT_EVENTS not in capabilities:
            raise PluginCapabilityError("plugin derivation requires emit_events")
        proposals = (
            decision.events if isinstance(decision, Derive) else (decision.replacement,)
        )
        if any(proposal.kind not in spec.output_kinds for proposal in proposals):
            raise ValueError("plugin output kind is outside its declaration")
    if isinstance(decision, (Deny, Supersede)) or stage is PluginStage.EVENT_POLICY:
        if PluginCapability.SUPPRESS_EVENTS not in capabilities:
            raise PluginCapabilityError(
                "policy or replacement requires suppress_events"
            )
