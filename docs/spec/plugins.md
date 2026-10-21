# Plugin System

This page is the normative specification for MEDRE's plugin subsystem: the
contract, discovery, capability authorization, typed execution decisions,
derived-event admission, durable execution records, failure semantics, state
authority, and distribution of plugins.

## 1. Scope and Relationship to Adapters

Adapters and plugins share registration primitives (`medre.registration`), declarative SDK-free
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

| Area                            | Status                                                     |
| ------------------------------- | ---------------------------------------------------------- |
| Contract types and vocabularies | Implemented (`core/contracts/plugins/`; public re-exports) |
| Type registry and discovery     | Implemented (`src/medre/plugins/registry.py`)              |
| Reference plugin                | Implemented (`src/medre/plugins/example.py`)               |
| Pipeline execution of plugins   | Implemented for explicit programmatic assembly             |
| Derived-event admission         | Implemented; atomic storage and forward continuation       |
| Durable execution records       | Implemented; SQLite atomic decision/event/state commit     |
| Instance configuration and CLI  | Planned — not implemented                                  |
| Decision authorization checks   | Implemented and invoked by the core executor               |

The core executor is wired through `PipelineConfig.plugin_executor` into inline
and durable pipeline processing. Assembly owns startup and shutdown; it MUST
start the executor before accepting work and drain ingress before stopping it.
`runtime.plugins.build_plugin_executor` validates effective configuration and
validates all configured instances before loading any enabled implementation.
Factories defer resource acquisition to `initialize`, where lifecycle cleanup
is host-owned. Installing a package or declaring an entry point
does not activate plugins; runtime YAML and CLI activation remain unimplemented.

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

| Field           | Requirement                                                        |
| --------------- | ------------------------------------------------------------------ |
| `plugin_id`     | Registration token; equals the entry-point name                    |
| `version`       | Non-empty implementation version; recorded in durable decisions    |
| `capabilities`  | Non-empty set of declared requirements (§6)                        |
| `stages`        | Non-empty subscribed stages: `enrich`, `transform`, `event_policy` |
| `plugin`        | Lazy `SymbolRef` to a zero-arg implementation factory              |
| `input_kinds`   | Registered event kinds to receive; empty means all kinds for       |
|                 | enrichment/policy, but transforms MUST select explicit inputs      |
| `output_kinds`  | Registered event kinds the plugin may derive; required iff         |
|                 | declaring `emit_events`, and never permitted without that grant    |
| `config`        | Optional lazy `SymbolRef` to a typed config model                  |
| `logger_levels` | Optional dependency logger levels (adapter restrictions apply)     |

Validation rules:

- Every declared capability MUST be supported by this host. Declaring a
  reserved capability quarantines the type rather than ignoring its request.
- Every plugin MUST declare `read_events`. Transforms MUST declare
  `emit_events` and at least one `input_kinds` entry.
- Every plugin that declares `emit_events` MUST declare at least one output
  kind. A read-only plugin MUST NOT declare output kinds.
- `input_kinds` applies identically to every subscribed stage. An empty
  selection means all registered kinds for enrichment and policy; a filtered
  event MUST NOT invoke the plugin or record a synthetic policy decision.
- Every proposed derived event MUST have a kind in the plugin's `output_kinds`,
  including proposals emitted at **enrichment** stages. Stages do not confer
  an implicit permission to emit arbitrary kinds. `output_kinds` MUST exclude
  `delivery.*` and `system.*` to keep core evidence core-owned.
- Input/output declarations MUST name registered kinds in the closed event-kind
  registry ([event-model.md](event-model.md) §5). Stage and kind filtering
  MUST be enforced by the host, not only documented for plugin authors.
- Declaration containers MUST be immutable snapshots; lazy references MUST
  be validated structurally without importing their targets.
- Logger declarations MUST NOT target `root`, `medre`, or `medre.*`, MUST NOT
  repeat a logger, and MUST name valid levels.

These type-level declarations are the plugin's maximum claim, not a complete
operator policy. An operator chooses which type and instance to activate and
which capabilities to grant. Type-declared kinds bound the code's behavior;
route configuration remains authoritative for transport delivery.

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
| `read_routes`      | Reserved; compiled route reads are unimplemented         |
| `modify_routes`    | Reserved — not grantable (see below)                     |
| `read_identity`    | Reserved; reconciled identity reads are unimplemented    |
| `read_storage`     | Reserved; event/evidence reads are unimplemented         |
| `access_telemetry` | Reserved; telemetry reads are unimplemented              |

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

Plugins subscribe to the canonical extension stages between `STORE` and
`ROUTE` (see [architecture.md](architecture.md) §2). Stage execution order
is fixed: `ENRICH` → `TRANSFORM` → `EVENT_POLICY`. Within a stage,
subscribed instances run in the explicit assembly order. Implementations MUST
be independently owned per instance. The host serializes invocations and commits
per instance, while state revisions fence competing writers.

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

- Plugins are invoked only for kinds in `input_kinds` when that tuple is
  nonempty. This filtering also applies to event-policy subscriptions; an
  unmatched event has no verdict from that plugin, not an implicit `Allow`.
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

Derived events are proposals (`DerivedEvent`: a registered event kind, its
own positive payload `schema_version`, and a JSON-safe, deeply frozen payload).
A proposal's schema version defaults to 1 but MUST NOT inherit the triggering
source event's schema version. The host constructs the canonical event
from a proposal and MUST admit it through the same invariants as any other
event; derivation confers no bypass of admission, deduplication, storage,
relation, or provenance handling.

- The host assigns `event_id`, sets `parent_event_id` to the source event,
  appends to `lineage`, and stamps timestamps and the proposal's declared
  payload schema version. A plugin MUST NOT set its own
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

The explicit executor accepts named replay scopes, but `ReplayEngine` does not
integrate the plugin-qualified frontier yet. With a plugin executor configured,
routing, planning, and delivery replay MUST reject the request before selecting
events or invoking transports. Strict inspection and historical re-rendering
remain available. This restriction also applies to dry-run routing: it MUST NOT
report an unqualified original as eligible for delivery.

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

### 9.1 Frozen Execution Plans

Before invoking a plugin, including when the configured plan is empty, the host
MUST bind the source event and execution scope to its plan digest. The digest
includes ordered instances, enabled state, effective normalized configuration,
versions, implementation references, required/optional disposition, and limits.
Recovery compares this binding before any implementation call. A changed plan
requires a separately named replay scope; it MUST NOT silently gate partially
processed work under different semantics.

The executor walks original events before descendants in deterministic order.
Every child resumes the forward position after its producer. It buffers the
qualified frontier until all required transformations and policies finish;
a required failure prevents routing the frontier. Each returned event then
uses normal relation projection, route planning, outbox ownership, and delivery.

Transient invocations have bounded attempts and a retry delay, both in the plan.
Exhaustion retains source work with its existing failure evidence and raises
a processing failure, which consumes the ingress poison-work budget. Required
settled failures, incompatible plans, missing continuations, and invalid stored
evidence also consume that budget; they MUST NOT use attempt-free operational
deferral. Rows reach terminal `failed` after the bounded ingress failure budget.
Only retry-delay windows, pending cancellation, executor shutdown, and stale
uncommitted state snapshots use operational deferral. Storage faults use normal
bounded processing retries. The worker pauses between deferred cycles, and the
claim queue rotates deferred rows behind older untouched work. Optional
status is permitted only for enrichment-only instances. Other stage failures
cannot authorize a pass. A timeout cancels the invocation task without waiting
indefinitely for acknowledgement; that instance cannot run again while the task
is alive, and its invocation state becomes inaccessible immediately. Late return
values are never admitted. `stop()` reports incomplete cleanup rather than
claiming success while a plugin task remains active.
A quarantined instance produces a host `quarantined` disposition without invoking
its implementation: optional enrichment may explicitly pass, while required
stages fail without routing and consume the bounded ingress failure budget. This
is distinct from a fresh contract violation.
The instance quarantine latch lasts for that executor lifetime. A newly assembled
executor performs fresh readiness evaluation; committed source dispositions
remain settled across restarts and cannot be bypassed in the same scope.

`PluginContext.config` is a frozen JSON mapping with typed model defaults applied
at assembly. `PluginContext.state.read()` returns a frozen invocation snapshot;
`write(mapping)` buffers a bounded replacement committed only with successful
decisions. `PluginContext.current_execution()` exposes the immutable logical invocation
key and its stable `idempotency_key` for plugin-owned external protocols. The
key includes replay scope but excludes attempt number, so retries can deduplicate
external effects. The host cannot guarantee an external service honors that key.
State reads/writes and execution-key reads outside an active invocation, including late child
tasks, fail. Initialization and shutdown cannot mutate durable state.

`plugin.custom` output is stored by default. Routes MUST explicitly name
`plugin.custom` in their source-kind selection to deliver it; catch-all bridge
routes do not broadcast observational enrichment. Explicit custom-only routes
and catch-all routes consequently do not overlap on custom events.

### 9.2 Implemented Persistence Interface

`StorageBackend.commit_plugin_execution` is host-owned. It accepts a
`PluginExecutionRecord` and an optional buffered state snapshot. Plugins MUST
NOT receive this interface or call it themselves.

- The logical execution key is `(source_event_id, plugin_id, instance_id,
stage, scope)`. `scope` is `live` or `replay:<run-id>`. The record ID adds
  the attempt number; produced event IDs add the proposal ordinal instead.
  Both identities are host-generated UUIDv5 values. They are idempotency
  identities; SQLite event sequence and attempt number remain ordering
  authorities, not UUID sorting.
- The first attempt is 1. Transient failures and timeouts without an explicit
  enrichment pass permit a subsequent consecutive attempt. Settled decisions
  and contract/capability/quarantined dispositions prohibit another attempt in the same scope.
- Each source event and scope use one execution-plan digest. A changed order,
  instance set, version, grant, or effective configuration MUST produce a
  different plan digest; changing it during recovery requires explicit
  replay. Retry attempts also retain their plugin version and config digest.
- The host constructs bounded children from recorded proposals, stamping IDs,
  timestamps, ancestry, and source context. Native refs are cleared. Derived
  events carry source metadata stripped of the adapter-owned `native` namespace
  and source `routing` metadata; other namespaces remain as source context.
  Same-kind proposals retain source relations; kind changes start without
  relations. Proposals cannot create unrelated native relations or attachment
  content.
- Events, inline relations, and execution evidence commit in one SQLite
  transaction with any buffered plugin-state update. Read-only decisions and
  failed attempts MUST NOT create or advance an operational state snapshot;
  only a successful buffered state write increments its revision. Children do
  not receive transport ingress work: the execution host must resume their
  forward cursor through the parent work item. The executor resumes this continuation before routing.
- Exact repeated commits return the committed record and do not advance state
  again. A different record at the same identity fails closed. A stale state
  revision rejects the entire uncommitted result; the host must discard it,
  reload state, and re-evaluate rather than reuse a decision computed from stale
  state. Cancellation of an awaiting caller does not establish that the SQLite
  worker rolled back; recovery reads evidence before attempting work again.
- Failed invocations cannot persist state writes or derived events. Optional
  enrichment may record an explicit `Pass` together with its failure; a failed
  transform or policy may not invent success or a verdict.
- Per-record storage bounds are 32 proposals and 256 KiB of JSON evidence;
  state snapshots are at most 64 KiB. Derived ancestry is capped at depth 16.
  The execution host must additionally enforce the source-wide expansion
  budget and per-instance serialization before admitting work.

Execution records carry `schema_version: 2`, independent of a proposal's
`schema_version` and of the plugin implementation version. Unknown record or state-envelope versions MUST fail closed, not be
interpreted as absent work or empty state.

Records use the closed
[plugin execution schema](../schemas/plugin-execution-record.schema.json).
Config and state values are not diagnostic summaries: exports MUST NOT dump
raw state, configuration, or proposal content without the existing privacy
and sanitization policy. Digests are internal evidence, not public credentials.

## 10. Failure Taxonomy and Stage Consequences

Failures are classified; classification decides consequence. A temporary
failure MUST NOT permanently quarantine a plugin. Operator-disabled and
discovery-quarantined states describe readiness, not invented invocations.

| Class                | Examples                                               |
| -------------------- | ------------------------------------------------------ |
| Transient failure    | Timeout, unavailable dependency, exception             |
| Contract violation   | Invalid decision type, unregistered kind               |
| Capability violation | Use of an ungranted service                            |
| Operator-disabled    | `enabled: false` on the instance                       |
| Quarantined          | Discovery failure or any contract/capability violation |

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
- The `plugin_state` table (see [storage.md](storage.md)
  §4.6) holds a bounded instance snapshot and revision. Its key encodes the
  tuple `[instance_id, scope]` as canonical JSON; `plugin_id` remains a
  separate key column. No unscoped KV access is exposed. Execution records
  live in the separate append-only `plugin_execution_records` table.
- Plugins MUST NOT receive raw SQLite connections or unrestricted
  `StorageBackend` access. `read_storage` is a reserved capability; a future
  bounded read-only query service may expose authorized event and delivery
  evidence views, but no such plugin service exists in this release slice.

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
requires only `read_events` and `emit_events`. Its declared input kinds are
`message.created` and `message.text`; its only declared output kind is
`plugin.custom`. It declares no configuration and no external dependency. MEDRE registers no providers itself, so the
reference plugin is deliberately not exposed through an entry point.

## 15. Boundary of the Current Host and Future Services

The canonical stage host is intentionally an event processor, **not** a generic
adapter command dispatcher, arbitrary message sender, web server, or workflow
scheduler. Plugins MUST NOT use synthetic ingress, forged native references, or
raw adapter SDK calls to bypass the authority of transport adapters.

The following examples describe separate potential host services; these are
**not implemented** by the current `PluginContext` or `PluginExecutor`:

- **Cross-platform responder:** the current `Derive` operation creates a
  canonical descendant routed by existing configured routes, retaining the
  triggering event's source context. It does not select an arbitrary reply
  destination or establish a new bot sender identity. A future outbound-intent
  interface needs an operator-granted destination scope, explicit actor,
  relation and target semantics, durable idempotency, and normal outbox,
  rendering, and adapter handoff. Derivation is not equivalent to sending.
- **Curated Meshtastic/mtjk operations:** canonical event enrichment is not a
  device command. The operation vocabulary may include bounded reads and
  permitted commands beyond remote administration. A future interface needs a
  narrowly typed operation vocabulary, device/resource grants, sender
  authentication independent of displayed names, command-specific audit and
  confirmation, timeout and retry policy, and execution by the owning
  Meshtastic adapter. No plugin should directly receive a live mtjk client.
- **Statistics dashboard:** deriving `plugin.custom` summaries and buffering
  bounded plugin state are possible today, but they are not a query, cursor,
  streaming, or HTTP API. A dashboard should use a separate read-only,
  authorization-aware host query/stream service, with bounded pagination,
  redaction, retention and backpressure. Hosting a website must not block
  the event-processing stages.

If a plugin requires periodic tasks, external I/O, or long-running services,
their lifecycle and durable effect queues need an explicitly specified host
contract. The synchronous decision result is not a license to perform
unrecorded network side effects. Future services should reuse configured
instance identity, grants, evidence, replay limits, and operator-owned package
management without overloading `emit_events` into universal authority.

Development examples and interface selection guidance live in
[plugin-design.md](../dev/plugin-design.md). Kind subscriptions are event
selection, not per-channel, actor, device, or destination authorization. Plugins
MUST NOT treat a displayed sender name or retained source context as proof of
operation authority.
