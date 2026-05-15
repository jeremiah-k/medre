# Contract 49 — Routing and Bridge Contract

**Status:** Active
**Scope:** Authoritative specification for MEDRE route definitions, bridge directionality, static bridge policies, replay route attribution, loop-prevention ownership, and route diagnostics expectations.
**Audience:** Runtime builders, adapter authors, replay/replay engine authors, test harnesses.
**References:** Contract 47 (Runtime Assembly), Contract 48 (Runtime Observability), Contract 31 (Session Boundary), Contract 29 (Diagnostics).

Every agent or document that references MEDRE routing, bridging between adapters, route directionality, replay route attribution, or loop prevention must defer to this contract.


## 1. Route Definition Model

### 1.1 Route Identity

Every route has a unique `route_id` (alphanumeric, underscores, hyphens). Route IDs come from the TOML section key under `[routes.<id>]`. Two routes with the same ID cannot coexist in a single `RouteConfigSet`.

### 1.2 Source and Destination

Each route declares:

- `source_adapters` — tuple of adapter IDs that produce events.
- `dest_adapters` — tuple of adapter IDs that receive matched events.

Adapter IDs are plain strings. Routes are **transport-agnostic**: they reference adapter IDs, not transport types or SDK objects.

### 1.3 Directionality

```
RouteDirectionality enum:
  SOURCE_TO_DEST   — events flow from source → dest only
  DEST_TO_SOURCE   — events flow from dest → source only
  BIDIRECTIONAL    — events flow in both directions
```

Default: `SOURCE_TO_DEST`.

For bidirectional routes, the runtime engine registers **two** internal `Route` objects (one per direction), both sharing the same `route_id` prefix.


## 2. Bridge Policy

### 2.1 Static Allowlists

A `BridgePolicy` is an optional frozen dataclass attached to a route. All fields default to empty tuples (meaning "no restriction"). Fields:

| Field | Meaning |
|-------|---------|
| `allowed_event_types` | Event kinds that may traverse this bridge |
| `allowed_source_adapters` | Source adapters that may originate bridged events |
| `allowed_dest_adapters` | Destination adapters that may receive bridged events |
| `room_allowlist` | Matrix room IDs allowed to bridge |
| `channel_allowlist` | Channel/conversation IDs allowed to bridge |
| `sender_allowlist` | Sender identifiers allowed to bridge |

An empty tuple = "allow all" for that dimension.

### 2.2 Immutability

`BridgePolicy` is frozen after construction. It may only be set at configuration load time and never mutated at runtime.


## 3. Bridge Directionality — Concrete Examples

### 3.1 Matrix ↔ Meshtastic Unidirectional Bridge

```
[routes.matrix_to_radio]
source_adapters = ["bot1"]
dest_adapters = ["longfast"]
directionality = "source_to_dest"

[routes.matrix_to_radio.policy]
allowed_event_types = ["message"]
channel_allowlist = ["!bridge-room:example.com"]
```

Flow: messages from Matrix room `!bridge-room` → Meshtastic `longfast` radio.
Meshtastic responses do **not** flow back via this route.

### 3.2 Matrix ↔ Meshtastic Bidirectional Bridge

```
[routes.matrix_radio_bidir]
source_adapters = ["bot1"]
dest_adapters = ["longfast"]
directionality = "bidirectional"
```

Flow: Matrix `bot1` → Meshtastic `longfast` AND Meshtastic `longfast` → Matrix `bot1`.
The engine internally creates two routes.

### 3.3 Dual-Radio Bridge (Meshtastic → Meshtastic)

```
[routes.longfast_to_shortturbo]
source_adapters = ["longfast"]
dest_adapters = ["shortturbo"]
directionality = "source_to_dest"
```

Flow: events arriving on `longfast` are forwarded to `shortturbo`. This allows cross-band relay between two Meshtastic radios.

### 3.4 Matrix Hub Topology (Fan-Out)

```
[routes.hub_to_radio_a]
source_adapters = ["bot1"]
dest_adapters = ["radio_alpha"]
directionality = "source_to_dest"

[routes.hub_to_radio_b]
source_adapters = ["bot1"]
dest_adapters = ["radio_beta"]
directionality = "source_to_dest"
```

One Matrix bot fans out to multiple radios. Each route is independent; a failure on `radio_beta` does not block delivery to `radio_alpha`.

### 3.5 Isolated Route Groups

```
[routes.team_a_bridge]
source_adapters = ["bot_a"]
dest_adapters = ["radio_a"]
directionality = "bidirectional"

[routes.team_b_bridge]
source_adapters = ["bot_b"]
dest_adapters = ["radio_b"]
directionality = "bidirectional"
```

`team_a` and `team_b` are fully isolated: no event crosses between groups. The router has no implicit "route everything" default.


## 4. Route Engine Ownership

### 4.1 Who Owns What

| Component | Owns |
|-----------|------|
| `medre.config.loader` | Parsing `[routes.*]` TOML sections into `RouteConfig` / `RouteConfigSet` |
| `medre.runtime.routes` | Immutable route model dataclasses (transport-agnostic) |
| `medre.runtime.route_engine` | Converting `RouteConfigSet` → core `Route` objects, validating adapter refs, registering on `Router` |
| `medre.core.routing.models` | Core `Route` / `RouteSource` / `RouteTarget` data structures |
| `medre.core.routing.router` | In-memory matching engine (pure, no I/O) |
| `medre.core.storage.replay` | Replay route attribution, loop detection for replay |

### 4.2 What the Route Engine Must NOT Do

- Must not import SDKs (`nio`, `meshtastic`, `meshcore`, `RNS`, `lxmf`).
- Must not import concrete adapter packages (`medre.adapters.{matrix,meshtastic,...}`).
- Must not perform I/O (network calls, file reads during routing).
- Must not own adapter lifecycle (start/stop/connect/disconnect).

### 4.3 What Adapters Must NOT Do

- Adapters must not orchestrate routes. They receive delivery requests; they do not decide routing.
- Adapters must not query the `Router` directly for routing decisions.


## 5. Replay Route Attribution and Semantics

### 5.1 Attribution Placement

Route attribution during replay is stored on `ReplayResult.route_attribution` (a `ReplayRouteAttribution` instance), **not** on `CanonicalEvent`. The canonical event schema remains immutable.

### 5.2 ReplayRouteAttribution Fields

| Field | Description |
|-------|-------------|
| `route_ids` | Routes that matched the replayed event |
| `source_adapter` | The original source adapter |
| `target_adapters` | All resolved target adapters |
| `replay_mode` | The `ReplayMode` used |
| `is_replay` | Always `True` — distinguishes replay from live routing |
| `loop_warnings` | Loop-prevention warnings (empty if no loops detected) |

### 5.3 Determinism

For the same stored event and route configuration, replay attribution is identical across runs. Replay is deterministic.

### 5.4 Replay Modes and Routing

| Mode | Routes? | Renders? | Delivers? |
|------|---------|----------|-----------|
| STRICT | No | No | No |
| RE_RENDER | No | Yes (capture) | No |
| RE_ROUTE | Yes | No | No |
| BEST_EFFORT | Yes | Yes | Yes |
| DRY_RUN | Yes | Yes (capture) | Skip (no-op) |


## 6. Loop-Prevention Ownership

### 6.1 Live Routing

The `Router` does **not** enforce loop prevention. Live routing trusts that route configuration is non-circular. Route configuration validation may warn about obvious loops at config-load time, but runtime enforcement operates per-delivery and per-ingress, not per-route-topology.

### 6.2 Runtime Pipeline Guards

In addition to the startup-time `check_route_loops` warning, three runtime pipeline guards prevent loop propagation during live delivery:

1. **Native-ref duplicate suppression** — `PipelineRunner.handle_ingress` Stage 1.5 checks inbound events against stored native message references. If the event's `source_native_ref` matches a previously seen ref, the event is dropped. This prevents echo from re-delivered or duplicate packets at the pipeline boundary.

2. **Self-loop guard** — `PipelineRunner._execute_single_delivery` checks whether `target_adapter == event.source_adapter` for each delivery target. If true, the delivery is skipped with `status="skipped"` and `error="loop_prevented"`.

3. **Route-trace guard** — `PipelineRunner._execute_single_delivery` checks the `route_trace` counter on the event's `RoutingMetadata`. If a route ID appears more than once in the trace (indicating a cycle), the delivery is skipped.

### 6.3 Replay

Loop prevention is explicitly owned by `_filter_replay_loops` in `medre.core.storage.replay`. It detects:

1. **Self-loop**: a route would deliver an event back to its own `source_adapter`.
2. **Previously routed**: the event's `RoutingMetadata.matched_routes` overlaps with a matched route ID.

Looping routes are **skipped** (not erroring). A `loop_warnings` tuple is attached to the `ReplayRouteAttribution`.

### 6.4 Ownership Summary

| Context | Loop prevention owner |
|---------|-----------------------|
| Live routing | Config validation (load time) |
| Replay | `_filter_replay_loops` in `medre.core.storage.replay` |
| Runtime startup | `RouteConfigSet` validation (config load) |

### 6.5 Loop-Prevention Taxonomy

| Mechanism | Owner | Layer | When | Effect |
|---|---|---|---|---|
| `check_route_loops` | `route_engine` | Config | Startup | Log warning |
| Native-ref dedup | `PipelineRunner.handle_ingress` | Pipeline | Per-ingress | Drop event |
| Self-loop guard | `PipelineRunner._execute_single_delivery` | Pipeline | Per-delivery | Skip target |
| Route-trace guard | `PipelineRunner._execute_single_delivery` | Pipeline | Per-delivery | Skip target |
| `_filter_replay_loops` | `medre.core.storage.replay` | Replay | Per-replay event | Skip + warn |

### 6.6 Supplier of Native IDs

Native-ref duplicate suppression (Stage 1.5) depends on adapters providing a stable, unique `native_message_id` via `source_native_ref`. Adapters that return `None` or an empty string for `native_message_id` bypass dedup entirely — every inbound event from that adapter is treated as novel.

| Adapter | Native ID field | Stability | Notes |
|---------|----------------|-----------|-------|
| Matrix | `event_id` | Stable | Synapse-assigned, globally unique per event. |
| Meshtastic | `packet_id` | Stable per node | Small integer; may collide across sessions or nodes. Not globally unique. |
| MeshCore | `sender_timestamp` | Stable per sender | Distinguishes messages from the same sender but not globally unique across senders. |
| LXMF | `message_id` (hex of `message_id` bytes from packet) | Stable | Derived from source_hash + nonce at the protocol level. Unique per LXMF message. Codec normalises bytes to hex string. |

**Consequences for adapters without stable native IDs:** If a transport or adapter configuration produces `native_message_id = None` or `""`, the native-ref dedup stage cannot suppress duplicates from that source. Events from such adapters always pass through to routing and delivery. Operators relying on dedup for a specific transport must verify that the adapter's codec populates `native_message_id` for the relevant packet types.

### 6.7 Duplicate Suppression vs. Replay

Duplicate suppression is **NOT** replay dedupe. Replay generates fresh canonical events with independent receipts and new `event_id` values. The native-ref dedup stage prevents echo from transport-layer re-delivery of the same physical packet — it does not suppress replay-originated events. Multiple `BEST_EFFORT` replays of the same original event produce additional deliveries, each with its own receipt lineage. See Contract 49 §5 (Replay Route Attribution) and the Bridge Operation Runbook §7.


## 7. Route Diagnostics Expectations

### 7.1 Router Diagnostics

The `Router` exposes a read-only snapshot of registered routes for diagnostics (route count, route IDs, source/target specs). It does not expose internal mutable state.

### 7.2 Runtime Route Diagnostics

The runtime builder registers routes after adapter assembly. Route registration failures (unknown adapter refs) are raised as `RouteValidationError` during startup. A partially registered route set is not left in an inconsistent state — all routes register or startup fails.

### 7.3 Replay Route Diagnostics

Replay summaries include `route_attribution` per event when replay modes include routing (`RE_ROUTE`, `BEST_EFFORT`, `DRY_RUN`). This provides an audit trail of which routes matched which historical events.


## 8. Startup Expectations

### 8.1 Route Registration Order

Routes register in the same order they appear in `RouteConfigSet` (which preserves TOML declaration order). Registration is deterministic.

### 8.2 Validation Before Registration

`validate_route_adapter_refs` runs **before** any route is registered on the `Router`. If any enabled route references an adapter ID not present in the assembled runtime, startup fails with `RouteValidationError`.

### 8.3 Disabled Routes

Routes with `enabled = false` are parsed into `RouteConfigSet` but skipped during registration. They are not validated against adapter IDs.
