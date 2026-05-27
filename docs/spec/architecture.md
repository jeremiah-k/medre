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
[Adapters] --> ingress policy --> store source event --> enrichment
                                    |
                              semantic transforms
                                    |
                              event policy
                                    |
                                  routing
                                    |
                              route policy
                                    |
                            delivery planning
                                    |
                      delivery policy / rendering
                                    |
                          adapter execution
                                    |
                      receipts / correlation
```

## 2. Stage Descriptions

| Stage                           | Responsibility                                                                                                                                                          |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Ingress Policy**              | Validate and filter raw inbound data before it enters the pipeline. Reject malformed, unauthorized, or rate-limited ingress at the boundary.                             |
| **Store Source Event**          | Persist the source event to storage with a unique ID, timestamp, and schema version. This is the immutable record.                                                       |
| **Enrichment**                  | Attach supplementary data: identity resolution, geo lookups, radio metadata normalization. Produces a derived event.                                                    |
| **Semantic Transforms**         | Convert derived events into target event kinds (e.g., telemetry to message, telemetry to metrics). Each transform declares input/output kinds.                           |
| **Event Policy**                | Rate limiting, content filtering, permission checks, and user-configurable rules on transformed events. Events may be dropped, flagged, or rate-limited.                |
| **Routing**                     | Determine which adapters should receive the event. Evaluate structured source/target criteria, channel mapping, and bridge group resolution.                              |
| **Route Policy**                | Per-route rules after routing but before delivery planning. Per-route rate limits, quiet hours, and permission checks on the route+adapter pair.                        |
| **Delivery Planning**           | Construct delivery plans: primary method, fallback chain, retry strategy, ordering constraints, deduplication scope, and cross-adapter threading resolution.             |
| **Delivery Policy / Rendering** | Adapter-specific content filtering, size limits, capability downgrade. Produce the final rendered payload for each adapter.                                             |
| **Adapter Execution**           | Dequeue and execute delivery plans respecting adapter rate limits, connection state, and priority.                                                                       |
| **Receipts / Correlation**      | Record delivery results and correlate back to the originating event. Failed deliveries trigger fallback plans or dead-letter processing.                                 |

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

| Layer            | May Import From                                            | Must Not Import From              |
| ---------------- | ---------------------------------------------------------- | --------------------------------- |
| `core/`          | `core/` only                                               | `adapters/`, `config/`, `runtime/`|
| `config/`        | `config/` (including `config.adapters`, `config.routes`)   | `adapters/`, `runtime/`           |
| `adapters/`      | `core.contracts.adapter`, `config.adapters.*`, `core.*`    | Other adapter packages, `runtime/`|
| `runtime/`       | `core.*`, `config.*`, `adapters.*`                         | —                                 |

### 4.3 Key Invariants

- **CLI commands never import adapter implementations directly.** The `run`
  command calls `RuntimeBuilder` which handles adapter construction.
- **`RuntimeBuilder` is the single assembly point.** It is the only module
  that imports both config model types and adapter base classes.
- **`core/` is transport-agnostic.** No module under `core/` imports from
  `adapters/` or `runtime/`.
- **Config package follows the same no-adapters, no-SDK rule as core.**

## 5. Adapter Roles

| Role             | Description                                                                    | Examples                                    |
| ---------------- | ------------------------------------------------------------------------------ | ------------------------------------------- |
| **TRANSPORT**    | Moves data to/from a physical or logical transport layer.                     | Meshtastic, MeshCore, LXMF, MQTT            |
| **PRESENTATION** | Presents events to human users. Handles formatting, threading, reactions.     | Matrix, Discord, Telegram, Web UI           |
| **HYBRID**       | Both transports and presents simultaneously.                                   | IRC, XMPP                                   |

## 6. Cross-Transport Comparison

| Dimension         | Matrix                    | Meshtastic           | MeshCore             | LXMF                 |
| ----------------- | ------------------------- | -------------------- | -------------------- | -------------------- |
| Role              | Presentation              | Transport            | Transport            | Transport            |
| Identity          | MXID                      | NodeNum / fromId     | Ed25519 pubkey       | Destination hash     |
| Payload limit     | ~100 KB                   | ~227 bytes           | 184 bytes            | Variable             |
| Reply mechanism   | `m.in_reply_to`           | `replyId`            | None native          | None native          |
| Encryption        | TLS / Megolm              | Optional per-packet  | Always-on E2EE       | Reticulum link-layer |
| ACK model         | Sync `/sync` confirm      | Async LoRa ACK       | Async ACK + CRC      | Link-level ACK       |
| Send returns      | Event ID string           | MeshPacket protobuf  | Event + ACK info     | Delivery status      |
