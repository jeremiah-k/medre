# Contract 50 — Runtime Topology Contract

**Status:** Active
**Scope:** Authoritative specification for MEDRE runtime topology — how adapters, routes, sessions, codecs, renderers, and the event pipeline compose at runtime, and the boundaries each layer must respect.
**Audience:** Runtime builders, adapter authors, test harnesses, architecture reviewers.
**References:** Contract 47 (Runtime Assembly), Contract 48 (Runtime Observability), Contract 49 (Routing and Bridge), Contract 31 (Session Boundary).

Every agent or document that references MEDRE runtime topology, layer boundaries, or the composition of subsystems must defer to this contract.

## 1. Runtime Layers

The MEDRE runtime is composed of the following layers, from top to bottom:

```
┌─────────────────────────────────────────┐
│  CLI (medre.cli, medre run)               │
├─────────────────────────────────────────┤
│  Runtime (medre.runtime.*)              │
│  ┌─ MedreApp ────────────────────────┐  │
│  │  builder → app → start/stop       │  │
│  └───────────────────────────────────┘  │
│  ┌─ Route Engine ───────────────────┐   │
│  │  routes.py, route_engine.py      │   │
│  └──────────────────────────────────┘   │
├─────────────────────────────────────────┤
│  Core (medre.core.*)                    │
│  ┌─ Events ─────────────────────────┐   │
│  │  CanonicalEvent, EventBus        │   │
│  └──────────────────────────────────┘   │
│  ┌─ Pipeline ───────────────────────┐   │
│  │  PipelineRunner                  │   │
│  └──────────────────────────────────┘   │
│  ┌─ Routing ────────────────────────┐   │
│  │  Router, Route, RouteSource      │   │
│  └──────────────────────────────────┘   │
│  ┌─ Rendering ──────────────────────┐   │
│  │  RenderingPipeline, TextRenderer │   │
│  └──────────────────────────────────┘   │
│  ┌─ Storage ────────────────────────┐   │
│  │  SQLiteStorage, Replay           │   │
│  └──────────────────────────────────┘   │
├─────────────────────────────────────────┤
│  Adapters (medre.adapters.*)            │
│  ┌─ Contracts ──────────────────────┐   │
│  │  AdapterContract, AdapterContext │   │
│  └──────────────────────────────────┘   │
│  ┌─ Per-Transport ──────────────────┐   │
│  │  matrix / meshtastic / ...       │   │
│  │  ├─ adapter.py (orchestration)   │   │
│  │  ├─ session.py (SDK lifecycle)   │   │
│  │  ├─ codec.py (format convert)    │   │
│  │  └─ renderer.py (text output)    │   │
│  └──────────────────────────────────┘   │
└─────────────────────────────────────────┘
```

Each layer has strict import boundaries documented below.

## 2. Import Boundary Rules

### 2.1 Runtime Must Not Import SDKs

`medre.runtime.*` (including `routes.py`, `route_engine.py`, `app.py`, `builder.py`) must not import any transport SDK (`nio`, `meshtastic`, `meshcore`, `RNS`, `lxmf`). Runtime is transport-agnostic.

### 2.2 Runtime Must Not Import Concrete Adapter Packages

`medre.runtime.*` must not import `medre.adapters.{matrix,meshcore,meshtastic,lxmf}`. It may import `medre.core.contracts.adapter` (protocol/base types).

### 2.3 Core Routing Must Not Import Runtime

`medre.core.routing.*` must not import `medre.runtime.*`. The core routing models and router are lower-level than the runtime route engine. The runtime consumes core; core does not know about runtime.

### 2.4 Sessions Must Not Know Routes

Session modules (`medre.adapters.*/session.py`) must not import `medre.runtime.routes`, `medre.runtime.route_engine`, or `medre.core.routing.*`. Sessions own SDK lifecycle; routing decisions are made above them in the pipeline.

### 2.5 Adapters Must Not Orchestrate Routes

Adapter modules must not import `medre.runtime.route_engine` or call `Router` directly for routing decisions. Adapters receive delivery requests via the pipeline; they do not route.

### 2.6 Codecs Must Remain Pure

Codec modules (`medre.adapters.*/codec.py`) must not:

- Import or manage lifecycle (start/stop/reconnect).
- Instantiate SDK clients or routers.
- Import `medre.runtime.*`.
- Import routing modules.

Codecs are pure format converters: canonical event ↔ transport format.

### 2.7 Renderers Must Not Route

Renderer modules (`medre.adapters.*/renderer.py`, `medre.core.rendering.*`) must not:

- Call adapter/session `deliver`, `send`, `start`, or `stop`.
- Import routing modules.
- Manage adapter lifecycle.

Renderers produce display-ready text from canonical events. They are side-effect-free.

### 2.8 Adapters Must Not Import Sibling Adapter Packages

Each adapter package (`medre.adapters.matrix`, etc.) must not import any other adapter package. Cross-adapter communication happens exclusively through the event pipeline and routing.

## 3. Topology Composition at Startup

### 3.1 Build Order

`RuntimeBuilder.build()` constructs subsystems in this order:

1. `EventBus` — central async pub/sub
2. `RenderingPipeline` — with default `TextRenderer`
3. `Router` — empty route table
4. `FallbackResolver` — capability degradation
5. `SQLiteStorage` — using resolved database path
6. `Diagnostician` — metrics and diagnostics
7. `RelationResolver` — cross-adapter event linking
8. `PipelineRunner` — orchestration
9. Adapters — constructed from enabled adapter configs
10. Routes — validated against adapter IDs, then registered on `Router`

Routes are registered **after** adapters are built, so `validate_route_adapter_refs` can verify all references against the actual adapter ID set.

### 3.2 Startup Lifecycle

```
MedreApp.start():
  1. Start storage (if enabled)
  2. Start event bus
  3. Start pipeline runner
  4. Start adapters in sorted order (transport, adapter_id)
  5. Adapters connect to their transports
  6. System is ready
```

### 3.3 Shutdown Lifecycle

```
MedreApp.stop():
  1. Signal shutdown event
  2. Stop adapters in reverse startup order
  3. Stop pipeline runner
  4. Stop event bus
  5. Close storage
```

## 4. Event Flow Topology

### 4.1 Inbound (Transport → Pipeline)

```
Transport SDK
  → Session (callback, normalizes to raw data)
    → Adapter (converts to CanonicalEvent via codec)
      → EventBus (publishes CanonicalEvent)
        → PipelineRunner (receives, routes, renders, delivers)
```

### 4.2 Outbound (Pipeline → Transport)

```
PipelineRunner
  → Router (matches event to routes, resolves targets)
    → RenderingPipeline (renders event to adapter payload)
      → Adapter.deliver() (converts via codec, calls session.send())
        → Session (SDK send call)
```

### 4.3 Cross-Transport Bridge

```
Matrix Session → Matrix Adapter → EventBus → PipelineRunner
  → Router (matches route "matrix_to_radio")
    → RenderingPipeline (renders text)
      → Meshtastic Adapter.deliver()
        → Meshtastic Session.send()
```

The bridge is driven entirely by the pipeline and router. Neither adapter is aware of the other.

## 5. Transport-Agnostic Runtime Guarantee

The runtime (`medre.runtime.*`) is transport-agnostic. It operates on adapter IDs, event kinds, and channel IDs — never on transport-specific concepts like Matrix room IDs or Meshtastic node numbers.

Transport-specific details (room IDs, node numbers, LXMF destinations) appear only in:

- `BridgePolicy` allowlists (string matching, no SDK types)
- Adapter codecs (format conversion)
- Session modules (SDK interaction)

The runtime does not interpret transport-specific identifiers; it passes them through as strings.

## 6. Multi-Adapter Topology Examples

### 6.1 Single-Transport, Multi-Adapter

```
[adapters.matrix.bot1]
[adapters.matrix.bot2]
```

Two Matrix bots in the same runtime. Each has its own session, codec, and adapter. They do not communicate directly; events flow through the event bus.

### 6.2 Multi-Transport Hub

```
[adapters.matrix.hub_bot]
[adapters.meshtastic.radio_a]
[adapters.meshtastic.radio_b]
[adapters.lxmf.node_1]

[routes.hub_to_all]
source_adapters = ["hub_bot"]
dest_adapters = ["radio_a", "radio_b", "node_1"]
directionality = "source_to_dest"
```

Matrix hub fans out to three different transports. Each destination adapter handles delivery independently.

### 6.3 Full Mesh (Bidirectional)

```
[adapters.matrix.bot]
[adapters.meshtastic.radio]

[routes.matrix_radio]
source_adapters = ["bot"]
dest_adapters = ["radio"]
directionality = "bidirectional"
```

Events flow both ways. The router creates two internal routes. Loop prevention at config-time prevents `bot → radio → bot` circularity if both routes match the same event kind.

## 7. Boundary Violation Indicators

The following patterns indicate a boundary violation:

| Pattern                                                       | Violation                    |
| ------------------------------------------------------------- | ---------------------------- |
| `medre.runtime.*` importing `nio` or `meshtastic`             | Runtime imports SDK          |
| `medre.core.routing.*` importing `medre.runtime.*`            | Core depends on runtime      |
| Session module importing `medre.runtime.routes`               | Session knows about routes   |
| Codec importing `Router` or `route_engine`                    | Codec has routing knowledge  |
| Renderer calling `adapter.send()` or `session.deliver()`      | Renderer performs I/O        |
| `medre.adapters.matrix` importing `medre.adapters.meshtastic` | Cross-adapter coupling       |
| Adapter calling `Router.route()` directly                     | Adapter orchestrates routing |
