# Architecture

System architecture, pipeline stages, module boundaries, and data flow
constraints.

See also: [principles.md](principles.md), [event-model.md](event-model.md),
[adapter-runtime.md](adapter-runtime.md),
[routing-delivery.md](routing-delivery.md).

---

## 1. Pipeline Overview

Events flow through a fixed sequence of stages. Each stage has a defined
responsibility and produces traceable output. The runtime implements
**six** top-level stages, defined by the `PipelinePhase` enum in
`src/medre/core/engine/phases.py`: `INGRESS`, `DEDUP`,
`RESOLVE_RELATIONS`, `STORE`, `ROUTE`, `DELIVER`. Ingress-time conversation
identity assignment happens inside `RESOLVE_RELATIONS` execution (see §2), while
current conversation ancestry is a separate rebuildable projection repaired after
storage facts change and overlaid before routing/rendering. Neither operation is a
separate `PipelinePhase`.

```text
[Adapters] --> ingress --> dedup --> resolve_relations --> store
                                                  |
                                            route
                                                  |
                                            deliver
                                                  |
                                     receipt (append-only)
```

## 2. Stage Descriptions

| Stage                 | Responsibility                                                                                                                                                                                                                           | Ends With                                                   |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| **Ingress**           | Validate required fields (`event_id`, `event_kind`, `source_adapter`) on inbound canonical events. Reject malformed events at the boundary.                                                                                              | Validated event in memory.                                  |
| **Dedup**             | Check the inbound native-message ref (`source_native_ref`) against persisted refs. Suppress duplicate native refs before storage to prevent echo loops.                                                                                  | Duplicate suppressed (returns `[]`); or unique event.       |
| **Resolve Relations** | Resolve event-level relations by looking up `target_native_ref` → `target_event_id` mappings via `RelationResolver`. Preserve unresolved refs unchanged. Assign the **ingress-time** `root_event_id` / `conversation_id` snapshot via `ConversationGraphAuthority` using the first currently resolvable relation target. | Event with the admission-time relation/identity snapshot populated. |
| **Store**             | Persist the canonical event and inbound `NativeMessageRef` as immutable facts. Reconcile `conversation_membership` from the now-current event/relation/native-ref graph, and overlay current membership on the in-memory event before downstream work. Unresolved native relations may be re-resolved on that in-memory copy, never by updating stored relation rows. | Immutable facts durably stored; current derived membership repaired. |
| **Route**             | Match the stored event against registered routes via `Router.match`. Create a `DeliveryPlan` per target using `FallbackResolver`. Attach route-level retry policies.                                                                     | Ordered list of `(Route, DeliveryPlan)` pairs.              |
| **Deliver**           | For each target: evaluate route policy, acquire capacity, create outbox item, enrich target-specific relations, render, call adapter `deliver()`, persist a `DeliveryReceipt`, and update the outbox item.                               | `DeliveryOutcome` per target; receipt in storage.           |

### Stage Invariants

1. **Ingress**: Events missing `event_id`, `event_kind`, or `source_adapter` raise `ValueError`.
2. **Dedup**: Suppressed duplicates produce no `DeliveryReceipt`. Evidence is recorded via `RuntimeAccounting` counters only.
3. **Resolve Relations**: Relation resolution and ingress identity assignment produce immutable in-memory copies. They never mutate an already stored event/relation row.
4. **Store**: Canonical events, relations, and native refs are immutable facts. `conversation_membership` is derived current state and MAY be updated idempotently. A late relation target affects routing/rendering only through an in-memory relation refresh plus the current conversation projection; historical rows are not rewritten.
5. **Route**: An event that matches zero routes produces no deliveries and no receipts. The pipeline returns an empty outcome list.
6. **Deliver**: Each target is independent — one target's failure does not prevent sibling deliveries. Every delivery attempt produces an append-only `DeliveryReceipt`. Receipt and outbox state machines are defined in [state-machines.md](state-machines.md).

### Conversation Projection Convergence

`ConversationProjectionService` owns current relation ancestry independently of the
canonical ingress snapshot. The selected parent is the first stored relation, in relation
order, whose target currently resolves to an existing canonical event. Explicit
`target_event_id` values are authoritative; a missing explicit canonical target does not
fall back to the same relation's native reference.

When a canonical event or native-message mapping becomes available, the projection uses
reverse relation traversal to recompute dependent children and descendants. Incremental
repair/rebuild operations are serialized by the projection authority so an older
calculation cannot overwrite the result of a newer completed repair. Runtime startup
performs a deterministic full rebuild **after storage initialization and before pipeline
workers or adapters start**. This makes interrupted incremental repair self-healing
without a migration log or mutation of evidence.

The projection is required to be idempotent and arrival-order convergent. The same final
set of immutable events, relation rows, and native refs must yield the same semantic
membership whether parents arrive before or after children.

### Future Extension Points

The following stages are reserved for future implementation and have no current
code path: **enrich**, **transform**, **event policy**.

These stages are described below for planning purposes only. They MUST NOT be
referenced as implemented behavior.

| Reserved Stage   | Intended Responsibility                                                                      | Insertion Point                    |
| ---------------- | -------------------------------------------------------------------------------------------- | ---------------------------------- |
| **Enrich**       | Attach supplementary data (identity resolution, geo lookups, radio metadata normalization).  | After **store**, before **route**  |
| **Transform**    | Convert enriched events into target event kinds. Each transform declares input/output kinds. | After **enrich**, before **route** |
| **Event Policy** | Rate limiting, content filtering, permission checks on transformed events.                   | After **transform**, before route  |

When implemented, each extension stage MUST produce derived events with
`parent_event_id` and lineage, and MUST NOT mutate the original stored event.

## 3. Data Flow Constraints

1. Events flow in one direction through the pipeline. No cycles.
2. Adapters never call other adapters directly.
3. All inter-adapter communication goes through the pipeline.
4. The canonical event log and relation/native-ref facts are the persistent historical record. `conversation_membership` is a rebuildable current-state projection, not additional event history.
5. Adapter state (connection status, queue depth) is tracked separately from events.

## 4. Module Boundaries

### 4.1 Package Layout

```text
src/medre/
  cli/            argument parsing, command dispatch, I/O formatting
  runtime/        builder, app, route engine, operational tooling
  core/           event model, storage, pipeline, routing, rendering
    contracts/    adapter protocol and contract types
    events/       bus, canonical event, schema, kinds
    storage/      backend, SQLite implementation
    engine/       pipeline runner, replay
    routing/      models, router, stats
    planning/     delivery plan, fallback resolution, relation resolution
    rendering/    renderer pipeline, text renderer
    policies/     transport-neutral policy helpers
    identity/     actor model, resolver
    lifecycle/    states, manager
    observability/ logging, metrics, sanitization
    supervision/  capacity controller, health, diagnostics
    diagnostics/  replay metrics, snapshot
  adapters/       per-transport packages (matrix/, meshtastic/, meshcore/, lxmf/)
    fakes/        fake adapters for testing
  config/         loader, model, env overrides, paths, sample generation
    adapters/     per-transport config dataclasses and credential helpers
    routes/       route configuration models
  plugins/        scaffolding: Plugin protocol, PluginCapability enum
```

### 4.2 Import Rules

| Layer       | May Import From                                          | Must Not Import From               |
| ----------- | -------------------------------------------------------- | ---------------------------------- |
| `core/`     | `core/` only                                             | `adapters/`, `config/`, `runtime/` |
| `config/`   | `config/` (including `config.adapters`, `config.routes`) | `adapters/`, `runtime/`            |
| `adapters/` | `core.contracts.adapter`, `config.adapters.*`, `core.*`  | Other adapter packages, `runtime/` |
| `runtime/`  | `core.*`, `config.*`, `adapters.*`                       | —                                  |

### 4.3 Key Invariants

- **CLI commands never import adapter implementations directly.** The `run`
  command calls `RuntimeBuilder` which handles adapter construction.
- **`RuntimeBuilder` is the single assembly point.** It is the only module
  that imports both config model types and adapter base classes.
- **`core/` is transport-agnostic.** No module under `core/` imports from
  `adapters/` or `runtime/`.
- **Config package follows the same no-adapters, no-SDK rule as core.**

## 5. Adapter Roles

| Role             | Description                                                               | Examples                          |
| ---------------- | ------------------------------------------------------------------------- | --------------------------------- |
| **TRANSPORT**    | Moves data to/from a physical or logical transport layer.                 | Meshtastic, MeshCore, LXMF, MQTT  |
| **PRESENTATION** | Presents events to human users. Handles formatting, threading, reactions. | Matrix, Discord, Telegram, Web UI |
| **HYBRID**       | Both transports and presents simultaneously.                              | IRC, XMPP                         |

## 6. Cross-Transport Comparison

| Dimension       | Matrix               | Meshtastic          | MeshCore         | LXMF                 |
| --------------- | -------------------- | ------------------- | ---------------- | -------------------- |
| Role            | Presentation         | Transport           | Transport        | Transport            |
| Identity        | MXID                 | NodeNum / fromId    | Ed25519 pubkey   | Destination hash     |
| Payload limit   | ~100 KB              | ~227 bytes          | 184 bytes        | Variable             |
| Reply mechanism | `m.in_reply_to`      | `replyId`           | None native      | None native          |
| Encryption      | TLS / Megolm         | Optional per-packet | Always-on E2EE   | Reticulum link-layer |
| ACK model       | Sync `/sync` confirm | Async LoRa ACK      | Async ACK + CRC  | Link-level ACK       |
| Send returns    | Event ID string      | MeshPacket protobuf | Event + ACK info | Delivery status      |
