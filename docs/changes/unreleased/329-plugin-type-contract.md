# Declarative plugin type contract, registry, and reference plugin

MEDRE plugins are canonical-boundary extensions beside adapters, sharing the
registration machinery while carrying a separate contract. The dormant
scaffolding in medre.plugins is replaced by the definitive type contract:
the PluginSpec declaration with lazy SymbolRef references, the capability
vocabulary separating declaration from operator authorization (effective
grants are the intersection of declared, granted, and host-supported
capabilities; modify_routes stays reserved and quarantines any type
declaring it), stage subscriptions limited to the reserved enrich, transform,
and event-policy stages with declared transform input/output kinds from the
closed event-kind registry, typed stage decisions (pass, derive, supersede,
allow, deny with required reason), configured instances carrying per-instance
grants and configuration, the runtime context with stage protocols, and a
first-party reference plugin deriving plugin.custom text statistics as the
conformance fixture.

Plugin types register through the medre.plugins entry-point group and are
discovered into an immutable registry with adapter-identical quarantine
semantics: broken, mismatched, conflicting, or reserved-capability providers
are retained as discovery failures, never silently skipped, with healthy
providers unaffected. Discovery stores declarative metadata only and never
imports plugin implementations. SymbolRef and the registration token grammar
move to medre.registration as the shared primitive for adapter and plugin
declarations; existing adapter import paths are unchanged.

Pipeline execution of plugins, pipeline derived-event admission, and the
operator configuration and packaging surface remain planned; the extension stages stay reserved and the scope guard now bans the
plugin execution runtime and any wiring of medre.plugins into runtime
assembly, configuration, or the CLI until those land. The superseded
list-returning Plugin protocol, validate_plugin_payload, and
PluginBoundaryError scaffolding symbols are removed under the prerelease
contract policy. The normative contract is specified in docs/spec/plugins.md.

Plugin declarations, instance configuration, and decision proposals retain immutable
snapshots. Grant checks reject mismatched type/instance identities, lazy references
are checked without loading implementations, and plugins cannot propose core-owned
delivery or system evidence. The execution contract specifies a forward-only
instance cursor, host-wide expansion bounds, and cooperative cancellation limits.

Separate `suppress_events` authorization from observing and deriving content.
Policy verdicts and superseding replacements require the suppression grant;
transform kind flows and stage-specific decisions have a pure host validator.
