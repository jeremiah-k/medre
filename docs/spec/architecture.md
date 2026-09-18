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

| Stage                 | Responsibility                                                                                                                                                                                                                                                                                                                                                        | Ends With                                                            |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| **Ingress**           | Validate required fields (`event_id`, `event_kind`, `source_adapter`) on inbound canonical events. Reject malformed events at the boundary.                                                                                                                                                                                                                           | Validated event in memory.                                           |
| **Dedup**             | Check the inbound native-message ref (`source_native_ref`) against persisted refs. Suppress duplicate native refs before storage to prevent echo loops.                                                                                                                                                                                                               | Duplicate suppressed (returns `[]`); or unique event.                |
| **Resolve Relations** | Resolve event-level relations by looking up `target_native_ref` → `target_event_id` mappings via `RelationResolver`. Preserve unresolved refs unchanged. Assign the **ingress-time** `root_event_id` / `conversation_id` snapshot via `ConversationGraphAuthority` using the first currently resolvable relation target.                                              | Event with the admission-time relation/identity snapshot populated.  |
| **Store**             | Persist the canonical event and inbound `NativeMessageRef` as immutable facts. Reconcile `conversation_membership` from the now-current event/relation/native-ref graph, and overlay current membership on the in-memory event before downstream work. Unresolved native relations may be re-resolved on that in-memory copy, never by updating stored relation rows. | Immutable facts durably stored; current derived membership repaired. |
| **Route**             | Match the stored event against registered routes via `Router.match`. Create a `DeliveryPlan` per target using `FallbackResolver`. Attach route-level retry policies.                                                                                                                                                                                                  | Ordered list of `(Route, DeliveryPlan)` pairs.                       |
| **Deliver**           | `PipelineRunner` fans out ordered target work to `DeliveryCoordinator`. For each target the coordinator sequences replay/loop/policy/capability preflight, capacity ownership, `OutboxManager`, runner-owned relation enrichment, `TargetDeliveryService`, and lifecycle finalization.                                                                                | `DeliveryOutcome` per target; receipt in storage.                    |

### Stage Invariants

1. **Ingress**: Events missing `event_id`, `event_kind`, or `source_adapter` raise `ValueError`.
2. **Dedup**: Suppressed duplicates produce no `DeliveryReceipt`. Evidence is recorded via `RuntimeAccounting` counters only.
3. **Resolve Relations**: Relation resolution and ingress identity assignment produce immutable in-memory copies. They never mutate an already stored event/relation row.
4. **Store**: Canonical events, relations, and native refs are immutable facts. `conversation_membership` is derived current state and MAY be updated idempotently. A late relation target affects routing/rendering only through an in-memory relation refresh plus the current conversation projection; historical rows are not rewritten.
5. **Route**: An event that matches zero routes produces no deliveries and no receipts. The pipeline returns an empty outcome list.
6. **Deliver**: Each target is independent — one target's failure does not prevent sibling deliveries. `DeliveryCoordinator` is orchestration only: it does not re-decide routing, rendering, retry policy, receipt lifecycle, or outbox transitions. Once a runtime delivery-capacity slot is acquired, that slot is released on every exit path, including cancellation during outbox creation and failure while finalizing the outbox. Every attempted adapter delivery produces append-only receipt evidence through the existing lifecycle authorities. Receipt and outbox state machines are defined in [state-machines.md](state-machines.md).

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
checks persisted projection revision/cleanliness **after storage initialization and
before pipeline workers or adapters start**. Dirty, interrupted, or older-revision state
triggers a deterministic paged rebuild; a clean current marker skips the full scan. This
makes interrupted incremental repair self-healing without mutation of evidence.

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
    engine/       pipeline runner, per-target delivery coordinator, replay
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

## 7. Runtime Orchestration

`MedreApp` (`src/medre/runtime/app.py`) owns process-level startup, shutdown,
and supervision order. `RuntimeBuilder` (`src/medre/runtime/builder.py`) is
the single assembly point that constructs the runtime and wires the capacity
controller. The sequences below are normative ownership invariants; state
vocabulary for individual transitions is owned by
[state-machines.md](state-machines.md) and shutdown handoff semantics by
[durable-ingress.md](durable-ingress.md).

### 7.1 Startup Order

`MedreApp.start()` MUST bring up subsystems in this order:

1. Storage initialization (a prerelease schema mismatch fails startup).
2. Conversation-projection rebuild/check — after storage facts exist and
   before any worker or adapter can consume stale pre-crash projection state.
3. Pipeline runner (which fans target work out to `DeliveryCoordinator`).
4. Durable-ingress worker construction, with processing deferred until
   adapter startup completes so cursor-owned adapters can admit work while
   delivery targets are still coming up.
5. Retry worker — only when retry is enabled and storage is present.
6. Adapters, in sorted `adapter_id` order. Adapter start failures are
   logged and attributed; they do not abort sibling adapters.

Startup outcomes: zero adapters started (including build failures) raises
`RuntimeStartupError` after core cleanup; partial adapter startup enters
`RUNNING` with degraded health; full startup enters `RUNNING` healthy.

### 7.2 Shutdown Order

`MedreApp.stop()` MUST tear down in reverse dependency order:

1. Replay engine cancelled.
2. Durable-ingress worker told to stop claiming rows; the active ingress row
   and the subsequent delivery/replay drain share one
   `limits.shutdown_drain_timeout_seconds` deadline (see
   [durable-ingress.md](durable-ingress.md)).
3. Capacity controller `stop_accepting()` — no new delivery work is admitted.
4. Retry worker stop. A cancellation-resistant retry worker may be abandoned;
   abandonment is logged for visibility and is not itself a shutdown failure.
5. In-flight delivery/replay work drained within the deadline remainder.
   Deliveries not completed by the deadline are abandoned with persisted
   `shutdown_rejection` evidence (see [state-machines.md](state-machines.md)
   §2.5).
6. Pipeline runner stop.
7. Adapters stopped in reverse start order, each under a two-stage deadline:
   a cooperative completion stage, then forced cancellation with bounded
   grace, then abandonment. Abandoned adapter-stop tasks are retained rather
   than discarded.
8. Storage closed.

External cancellation arriving during shutdown is deferred, not dropped:
pending cancellations are drained, core cleanup runs, and the cancellation is
re-raised afterwards so pipeline and storage cleanup always execute.

### 7.3 Authority Boundaries

- The retry worker owns polling, claiming, capacity admission, and retry
  events only. Durable outbox/retry state transitions are owned by the
  delivery lifecycle authority (see [delivery-lifecycle.md](delivery-lifecycle.md)).
  Durable double-process protection is the outbox claim (`worker_id` +
  `lease_until`), not process-local state.
- The pipeline runner delegates per-target orchestration to
  `DeliveryCoordinator`; the coordinator sequences preflight, capacity,
  outbox, enrichment, target delivery, and finalization, and does not
  re-decide routing, rendering, retry policy, or lifecycle transitions.
- Terminal runtime states: success ends `STOPPED`; a separate error,
  cancellation, or unfinished ingress (e.g. durable ingress worker still
  active after cancellation) ends `FAILED`. A successful stop that abandons
  capacity drain after the deadline still ends `STOPPED` — the abandoned
  work is persisted as `shutdown_rejection` receipts with
  `error="shutdown_drain_timeout"` and the projection-clean marker is
  skipped, but no error is raised. `shutdown_status="drain_timeout"` is a
  diagnostic classification emitted by `core/evidence/shutdown.py` and is
  orthogonal to the runtime terminal state.
