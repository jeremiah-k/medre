"""Public plugin-author imports for the kernel records contract."""

from medre.core.contracts.plugins.records import (
    MAX_PLUGIN_DEPTH,
    MAX_PLUGIN_EMISSIONS,
    MAX_PLUGIN_RECORD_BYTES,
    MAX_PLUGIN_STATE_BYTES,
    PLUGIN_RECORD_SCHEMA_VERSION,
    PluginDecision,
    PluginExecutionKey,
    PluginExecutionRecord,
    PluginFailure,
    PluginStateSnapshot,
    canonical_json,
    configuration_digest,
    decision_data,
    state_digest,
    validate_scope,
)

__all__ = [
    "PLUGIN_RECORD_SCHEMA_VERSION",
    "MAX_PLUGIN_EMISSIONS",
    "MAX_PLUGIN_STATE_BYTES",
    "MAX_PLUGIN_RECORD_BYTES",
    "MAX_PLUGIN_DEPTH",
    "canonical_json",
    "validate_scope",
    "configuration_digest",
    "PluginExecutionKey",
    "PluginFailure",
    "PluginDecision",
    "decision_data",
    "PluginExecutionRecord",
    "PluginStateSnapshot",
    "state_digest",
]
