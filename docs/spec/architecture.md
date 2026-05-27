# Architecture

System architecture, pipeline stages, module boundaries, and data flow
constraints.

See also: [principles.md](principles.md), [event-model.md](event-model.md),
[adapter-runtime.md](adapter-runtime.md),
[routing-delivery.md](routing-delivery.md).

---

## 1. Pipeline Overview

Events flow through a fixed sequence of stages. Each stage has a defined
responsibility and produces traceable output.

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

| Stage                 | Responsibility                                                                                                                                                                                             | Ends With                                             |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **Ingress**           | Validate required fields (`event_id`, `event_kind`, `source_adapter`) on inbound canonical events. Reject malformed events at the boundary.                                                                | Validated event in memory.                            |
| **Dedup**             | Check the inbound native-message ref (`source_native_ref`) against persisted refs. Suppress duplicate native refs before storage to prevent echo loops.                                                    | Duplicate suppressed (returns `[]`); or unique event. |
| **Resolve Relations** | Resolve event-level relations by looking up `target_native_ref` → `target_event_id` mappings via `RelationResolver`. Preserve unresolved refs unchanged.                                                   | Event with resolved relation IDs (may be same event). |
| **Store**             | Persist the canonical event to the storage backend via `StorageBackend.append`. Also persist the inbound `NativeMessageRef`. This is the immutable record.                                                 | Event durably stored; native ref recorded.            |
| **Route**             | Match the stored event against registered routes via `Router.match`. Create a `DeliveryPlan` per target using `FallbackResolver`. Attach route-level retry policies.                                       | Ordered list of `(Route, DeliveryPlan)` pairs.        |
| **Deliver**           | For each target: evaluate route policy, acquire capacity, create outbox item, enrich target-specific relations, render, call adapter `deliver()`, persist a `DeliveryReceipt`, and update the outbox item. | `DeliveryOutcome` per target; receipt in storage.     |

### Stage Invariants

1. **Ingress**: Events missing `event_id`, `event_kind`, or `source_adapter` raise `ValueError`.
2. **Dedup**: Suppressed duplicates produce no `DeliveryReceipt`. Evidence is recorded via `RuntimeAccounting` counters only.
3. **Resolve Relations**: The stored event is never mutated. If relations change, a new immutable event is created via `msgspec.structs.replace`.
4. **Store**: Events are appended immutably. No `UPDATE` or `DELETE` is issued on event rows.
5. **Route**: An event that matches zero routes produces no deliveries and no receipts. The pipeline returns an empty outcome list.
6. **Deliver**: Each target is independent — one target's failure does not prevent sibling deliveries. Every delivery attempt produces an append-only `DeliveryReceipt`. Receipt and outbox state machines are defined in [state-machines.md](state-machines.md).

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
4. The canonical event log is the only persistent record of event history.
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
    storage/      backend, SQLite implementation, replay
    engine/       pipeline runner
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
