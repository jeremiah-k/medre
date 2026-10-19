# Plugin System

This page is the normative specification for MEDRE's plugin subsystem: the
contract, discovery, capability authorization, typed execution decisions,
derived-event admission, durable execution records, failure semantics, state
authority, and distribution of plugins.

## 1. Scope and Relationship to Adapters

Adapters and plugins share registration machinery (`medre.registration`), declarative SDK-free
discovery, quarantine semantics, and operator-owned distribution, but they
carry separate contracts with separate typed registries:

- Adapters own the closed transport boundary: lifecycle, delivery hand-off,
  native identity, and optional transport SDKs
  (see [adapter-runtime.md](adapter-runtime.md)).
- Plugins own the canonical boundary: they observe canonical events, propose
  derived events, and evaluate scoped policy. A plugin MUST NOT construct or
  emit transport-native payloads, and MUST NOT construct
  `CanonicalEvent` values itself.

Plugins communicate exclusively through canonical events and runtime APIs.
All output flows through the event pipeline as host-constructed canonical
events.

## 2. Status

| Area                            | Status                                                   |
| ------------------------------- | -------------------------------------------------------- |
| Contract types and vocabularies | Implemented (`src/medre/plugins/`)                       |
| Type registry and discovery     | Implemented (`src/medre/plugins/registry.py`)            |
| Reference plugin                | Implemented (`src/medre/plugins/example.py`)             |
| Pipeline execution of plugins   | Planned — not implemented                                |
| Derived-event admission         | Planned — not implemented                                |
| Durable execution records       | Planned — not implemented                                |
| Instance configuration and CLI  | Planned — not implemented                                |
| Decision authorization checks   | Implemented; pure host validator, runtime wiring planned |

The extension stages remain reserved
(see [architecture.md](architecture.md) §2). Until the execution runtime
lands, plugin integration provides contracts, discovery, and host-side
decision validation, not an executed pipeline behavior. Operators and plugin authors MUST NOT assume
that subscribing a plugin to a stage has any runtime effect.

## 3. Plugin Types and Configured Instances

A plugin **type** is a separately distributed Python package registering
declarative metadata through an entry point. A **configured instance** is an
operator-defined configuration of that type. One type MAY back multiple
instances with different configuration, grants, and identity.

Each configured instance independently owns:

- Its configuration (validated against the type's config model)
- Its capability grants
- Its namespaced state
- Its execution ordering position
- Its diagnostics and failure state
- Its provenance and decision identity in durable records

Discovery identifies types. Runtime configuration identifies instances.
Type-level failure (quarantine) affects every instance of the type;
instance-level failure affects only that instance.

## 4. Discovery and the Type Registry

Plugin packages register zero-argument factories through Python entry points
in the `medre.plugins` group. Each factory MUST return a `PluginSpec` whose
`plugin_id` equals the entry-point name and follows the shared registration
token grammar (`^[a-z][a-z0-9_-]*$`).

Discovery runs once at import time and stores declarative metadata only.
The host loads declaration factories but does not resolve implementation
`SymbolRef` targets. Providers MUST keep their declaration factories and
imported modules free of SDK and implementation imports. Installing a plugin
package requires a process restart. The registry never downloads packages.

Discovery failure semantics mirror the adapter registry:

- A provider that raises on load, returns a non-`PluginSpec`, registers a
  mismatched `plugin_id`, or fails validation MUST be retained as a
  `PluginDiscoveryFailure` with a sanitized reason rather than silently
  skipped.
- Conflicting declarations of one plugin type (duplicate entry-point names)
  MUST quarantine every declaring provider; discovery MUST NOT choose a
  winner by installation order.
- Healthy, unrelated providers remain usable.

## 5. PluginSpec Declarations

| Field               | Requirement                                                                 |
| ------------------- | --------------------------------------------------------------------------- |
| `plugin_id`         | Registration token; equals the entry-point name                             |
| `version`           | Non-empty implementation version; recorded in durable decisions             |
| `capabilities`      | Non-empty set of declared requirements (§6)                                 |
| `stages`            | Non-empty set of stage subscriptions: `enrich`, `transform`, `event_policy` |
| `plugin`            | Lazy `SymbolRef` to a zero-arg factory returning the implementation         |
| `transform_inputs`  | Input event kinds; required iff subscribed to `transform`                   |
| `transform_outputs` | Output event kinds; required iff subscribed to `transform`                  |
| `config`            | Optional lazy `SymbolRef` to the type's typed config model                  |
| `logger_levels`     | Optional dependency logger level requests (adapter rules apply)             |

Validation rules:

- Every declared capability MUST be a `PluginCapability` member that this
  host can grant (§6); declaring a reserved capability quarantines the type.
- Every plugin MUST declare `read_events`: every stage subscription
  processes canonical events.
- Transform kind declarations MUST name registered event kinds from the
  closed registry (see [event-model.md](event-model.md) §5), and MUST be
  present exactly when the type subscribes to the transform stage.
- Plugin output kinds MUST exclude `delivery.*` and `system.*`: plugins
  cannot manufacture core delivery, audit, or lifecycle evidence. Such events
  may be observed as inputs.
- Declaration containers MUST be immutable snapshots. Registry validation
  MUST validate lazy references structurally without importing their targets.
- Logger declarations MUST NOT target `root`, `medre`, or any `medre.*`
  logger, MUST NOT repeat a logger, and MUST name valid levels.

A transform MUST declare its input and output kinds so route planning and
replay can reason about kind flow without loading implementations.

## 6. Capabilities: Declaration and Authorization

Capability declaration is a request, not an authorization. The capabilities
a plugin type declares state what its implementations require. The
capabilities a configured instance receives are the operator's grants. The
runtime grants only the intersection of:

1. What the plugin type declares,
2. What the operator granted to the configured instance, and
3. What this MEDRE host supports (`HOST_GRANTABLE_CAPABILITIES`).

| Capability         | Service                                                  |
| ------------------ | -------------------------------------------------------- |
| `read_events`      | Observe canonical events delivered to a subscribed stage |
| `emit_events`      | Propose derived events                                   |
| `suppress_events`  | Authorize policy verdicts and superseding replacements   |
| `read_routes`      | Read the compiled route view                             |
| `modify_routes`    | Reserved — not grantable (see below)                     |
| `read_identity`    | Read reconciled canonical actor identity                 |
| `read_storage`     | Read canonical events and delivery evidence              |
| `access_telemetry` | Read diagnostic telemetry                                |

Authorization rules:

- A declared capability the instance does not grant is a configuration
  readiness diagnostic: the instance cannot run until the operator grants
  it or disables the instance. Loading MUST surface missing grants
  explicitly; it MUST NOT run an instance silently under-granted.
- A grant the type never declares is an operator configuration diagnostic.
- `modify_routes` stays reserved. Routing authority remains with the
  compiled operator configuration (see
  [routing-delivery.md](routing-delivery.md)); a type declaring
  `modify_routes` is quarantined at discovery rather than silently
  under-granted. A future administrative proposal mechanism, if introduced,
  will be separately authorized and separately specified.

Attempting to use a service that requires an ungranted capability raises a
runtime error. Policy stage subscriptions MUST declare `suppress_events`.
`Derive` and `Supersede` require an effective `emit_events` grant; `Supersede`
also requires `suppress_events`. `Allow` and `Deny` both require the policy
grant: observing events alone cannot gate delivery. The host MUST call
`validate_plugin_decision` before persisting a returned decision.

Grant calculations MUST reject an instance belonging to a
different plugin type. Instance grants and configuration MUST be immutable
snapshots; strings such as `"false"` MUST NOT enable an instance.

## 7. Execution Contract

Plugins subscribe to the reserved extension stages between `STORE` and
`ROUTE` (see [architecture.md](architecture.md) §2). Stage execution order
is fixed: `ENRICH` → `TRANSFORM` → `EVENT_POLICY`. Within a stage,
subscribed instances run in a deterministic order defined by the execution
runtime.

Each stage hands the plugin the current canonical event and receives a
typed decision. Plugins never return bare lists of events and never edit an
event in place.

| Decision    | Stages            | Meaning                                                               |
| ----------- | ----------------- | --------------------------------------------------------------------- |
| `Pass`      | enrich, transform | The original event continues unchanged; nothing is derived            |
| `Derive`    | enrich, transform | Propose additional derived events; **the original event also routes** |
| `Supersede` | transform only    | The replacement routes downstream; the original stops routing         |
| `Allow`     | event_policy only | The event may continue toward routing                                 |
| `Deny`      | event_policy only | Suppress delivery under granted authority, with an explicit reason    |

The original-event routing rule is normative and explicit:

- `Derive` is additive. The original event continues through the remaining
  stages and routing, and each derived event also continues. Deriving MUST
  NOT suppress or replace the original.
- `Supersede` is the only replacement operation. The original event remains
  stored and immutable; downstream stages, route matching, and delivery see
  the replacement, which carries lineage back to the original.
- `Deny` is the only suppression operation. A policy denial is recorded as
  a policy suppression with its reason and MUST NOT be represented as an
  ordinary zero-route match; a stored event matching zero routes is a
  different outcome from a denied event (see
  [routing-delivery.md](routing-delivery.md) §4).
- Stage authority is exclusive: an enrichment plugin cannot supersede or
  deny; a policy plugin cannot derive. A plugin holds only the decision
  vocabulary of the stages it subscribes to.

MEDRE retains responsibility for event identity, lineage, validation,
persistence, admission, routing, and delivery. A `Deny` reason is evidence,
not an error message for operators to parse programmatically.

## 8. Derived-Event Admission

Derived events are proposals (`DerivedEvent`: a registered event kind plus a
JSON-safe, deeply frozen payload). The host constructs the canonical event
from a proposal and MUST admit it through the same invariants as any other
event; derivation confers no bypass of admission, deduplication, storage,
relation, or provenance handling.

- The host assigns `event_id`, sets `parent_event_id` to the source event,
  appends to `lineage`, and stamps timestamps. A plugin MUST NOT set its own
  lineage.
- Kinds come from the closed event-kind registry. Plugin-specific events use
  the single `plugin.custom` kind with a sub-kind in the payload; plugins
  MUST NOT invent `plugin.custom.*` top-level kinds (see
  [event-model.md](event-model.md) §5).
- Derived events feed forward: they MUST NOT
  re-enter an earlier execution position. The execution cursor is the pair
  `(stage, instance position)`: children and replacements continue with the
  remaining instances of the producing stage, then later stages. They MUST
  NOT skip downstream required transforms or policy gates, and MUST NOT
  invoke their producing instance at the same stage again. Lineage depth is
  capped; exceeding the cap is
  recorded as evidence, not silently truncated.
- Each instance's emission is bounded per execution; the entire source-event
  expansion also has a host-owned budget. Per-call limits alone do not bound
  multiplicative fan-out. Exceeding a budget is evidenced before admission.
- A proposal does not inherit a source native message reference: that would
  collide with ingress deduplication. Host-stamped source context and lineage
  retain attribution without promoting source trust or claiming a native
  delivery. Derived events MUST NOT be reinjected as transport ingress.
- Derived-event admission is idempotent under recovery: deterministic
  proposal identity ensures a repeated execution of a committed decision
  does not duplicate events (§9).

## 9. Durable Execution Records and Recovery

Every plugin execution produces a durable decision record containing at
least: the source event identity, the plugin type and configured instance,
the plugin implementation version, a digest of the effective configuration,
the stage, execution-plan digest, input state revision, buffered-state digest,
attempt number, the decision with produced event identities, and the execution
outcome (including failure classification and reason when applicable).

Transaction boundary: persisting the decision, admitting any derived
events, and updating namespaced plugin state MUST commit atomically. A crash
between execution and commit leaves no partial derivation.

- Normal recovery reuses committed decisions rather than re-running plugin
  implementations; an uncommitted decision is re-executed.
- Explicit replay under a changed plugin version or configuration is a
  separate operation with defined semantics; it derives new events with new
  lineage and never rewrites committed decisions or their events (see
  [storage.md](storage.md) §14 on replay).
- Deterministic registration order does not by itself make plugins
  deterministic: plugins that read clocks, call external services, or keep
  mutable state MUST be serialized per instance, and their decisions are
  authoritative only because they are durably recorded.
- Recovery identity MUST NOT include version or configuration: changing either
  cannot silently bypass a committed decision. A changed execution plan needs
  an explicit replay scope, never an automatic re-evaluation during restart.
- Atomic storage does not make external side effects exactly-once. An
  uncommitted invocation may run again; external calls require a stable host
  execution key and plugin-specific idempotency, or must be read-only.

## 10. Failure Taxonomy and Stage Consequences

Failures are classified; classification decides consequence. A temporary
failure MUST NOT permanently quarantine a plugin. Operator-disabled and
discovery-quarantined states describe readiness, not invented invocations.

| Class                | Examples                                   |
| -------------------- | ------------------------------------------ |
| Transient failure    | Timeout, unavailable dependency, exception |
| Contract violation   | Invalid decision type, unregistered kind   |
| Capability violation | Use of an ungranted service                |
| Operator-disabled    | `enabled: false` on the instance           |
| Quarantined          | Discovery failure or repeated violations   |

Stage consequences follow the authority of each stage:

- Optional enrichment MAY fail open with explicit evidence: the original
  event continues underived. Derivation is additive, so its absence is
  honest, not corrupt.
- A required transformation failure defers processing of the event under an
  explicit defer/retry policy; the event MUST NOT silently route around a
  failed required transform.
- Event-policy failures fail closed within the applicable scope: without a
  verdict there is no consent, so the event is stored without delivery, the
  failure is evidenced, and the work state remains recoverable. A failed
  policy evaluation MUST NOT be represented as a denial or as a zero-route
  match; the three outcomes are distinct.
- Contract and capability violations quarantine the instance (or the type,
  at discovery) with durable evidence.
- The host MUST bound admitted output and use cooperative asynchronous
  deadlines and cancellation. Trusted in-process code can block the event
  loop or ignore cancellation; this contract cannot promise preemption or
  isolation from such behavior. A deadline breach is a recoverable execution
  failure, not evidence that an external effect was cancelled.

## 11. State and Storage Authority

MEDRE retains a single canonical storage authority. Plugins access namespaced
state and permitted reads only through core-owned APIs:

- Operational plugin state (checkpoints, caches) is namespaced per
  `(plugin_id, instance_id, scope)` and isolated; a plugin MUST NOT read or write
  state belonging to another plugin.
- The schema-reserved `plugin_state` table (see [storage.md](storage.md)
  §4.6) is reserved for namespaced operational state. Durable decision records
  are immutable execution evidence, not operational state; their storage
  schema must be introduced under the existing authority and schema guards.
- Plugins MUST NOT receive raw SQLite connections or unrestricted
  `StorageBackend` access. `read_storage` grants scoped read APIs over
  canonical events and delivery evidence, nothing else.

## 12. Security Boundary

Plugins are trusted in-process code. Capability gating is an API-level
restriction that keeps authority explicit and auditable; it is not a
security sandbox. MEDRE does not treat an installed plugin's declared
capabilities as authorization (§6), and no-secret-leakage guarantees apply
to plugin decisions, diagnostics, and state exactly as they apply to
adapters (see [security-privacy.md](security-privacy.md)).

Out-of-process plugin isolation is not part of this contract; if untrusted
plugins become a requirement, isolation will be specified separately rather
than excluded.

## 13. Packaging and Distribution

Plugins are Python distributions installed by the operator through the same
uv-owned tool environment as adapters (see
[package-management.md](package-management.md)). Static discovery, lazy
implementation loading, and restart-required package changes apply
identically. The runtime MUST NOT download packages, hot-load plugins, or
load plugin implementation references at discovery time. The SDK-free
declaration factory does execute at discovery; the laziness guarantee applies
to implementations, not arbitrary installed Python code. The source catalog
and CLI surface for plugins arrive with the operator surface and reuse the
adapter machinery rather than forking it.

## 14. Reference Plugin

`text_stats` (`src/medre/plugins/example.py`) is the canonical first-party
reference implementation and conformance fixture: it subscribes to the
enrichment stage, derives word and character counts for `message.created`
and `message.text` events carrying text as a
`plugin.custom` event with a `text_stats` sub-kind in the payload, and
requires only `read_events` and `emit_events`. It declares no configuration
and no external dependency. MEDRE registers no providers itself, so the
reference plugin is deliberately not exposed through an entry point.
